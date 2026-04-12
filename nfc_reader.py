"""
NFC Reader - High-level command interface for SEGA AIME card readers.

Implements all reader commands:
    - Reset, firmware/hardware version query
    - Radio on/off, card polling
    - MIFARE select, authenticate, read, key management
    - FeliCa encapsulation
    - Firmware data transfer (send_hex_data)

Supports reader models:
    - 837-15084 (TN32MSEC003S) Gen1
    - 837-15286                Gen2
    - 837-15396                Gen3

Reference:
    amdaemon/src/arcade/AimeReader.cpp
"""

import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum

from sg_serial import SGSerial

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class AimeCmd(IntEnum):
    """AIME reader command bytes."""
    GET_FW_VERSION      = 0x30
    GET_HW_VERSION      = 0x32
    RADIO_ON            = 0x40
    RADIO_OFF           = 0x41
    POLL                = 0x42
    MIFARE_SELECT       = 0x43
    MIFARE_SET_KEY_BANA = 0x50
    MIFARE_READ_BLOCK   = 0x52
    MIFARE_SET_KEY_AIME = 0x54
    MIFARE_AUTHENTICATE = 0x55
    TO_UPDATE_MODE      = 0x60  # Enter firmware update mode (bootloader)
    SEND_HEX_DATA       = 0x61  # Gen1: Send Intel HEX firmware data
    RESET               = 0x62
    SEND_BINDATA        = 0x63  # Gen2/3: Init binary firmware transfer
    BINDATA_EXEC        = 0x64  # Gen2/3: Send 256-byte firmware chunk
    FELICA_ENCAP        = 0x71
    LED_SET_COLOR       = 0x81


class CardType(IntEnum):
    """Card type identifiers from poll response."""
    NONE   = 0
    MIFARE = 1
    FELICA = 2


# MIFARE type bytes in poll response
MIFARE_TYPES = {0x04, 0x10}
# FeliCa type bytes in poll response
FELICA_TYPES = {0x11, 0x12, 0x20}

# BANA key: 57 43 43 46 76 32 ("WCCF v2" in ASCII)
BANA_KEY = bytes([0x57, 0x43, 0x43, 0x46, 0x76, 0x32])

# Default device address
DEFAULT_ADDR = 0x00


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CardInfo:
    """Information about a detected card."""
    detected: bool = False
    card_type: CardType = CardType.NONE
    uid: bytes = b""
    uid_len: int = 0
    idm: bytes = b""       # FeliCa IDm (8 bytes)
    pmm: bytes = b""       # FeliCa PMm (8 bytes)
    access_code: bytes = b""  # 10-byte access code

    @property
    def type_name(self) -> str:
        if self.card_type == CardType.MIFARE:
            return "MIFARE"
        elif self.card_type == CardType.FELICA:
            return "FeliCa"
        return "Unknown"

    def uid_hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.uid)

    def access_code_hex(self) -> str:
        return "".join(f"{b:02X}" for b in self.access_code)


@dataclass
class ReaderInfo:
    """Reader identification information."""
    fw_version: str = ""
    hw_version: str = ""
    model: str = ""
    generation: int = 0

    def identify(self) -> None:
        """Identify reader model and generation from version strings."""
        hw = self.hw_version
        if "TN32MSEC003S" in hw:
            self.model = "837-15084 (TN32MSEC003S)"
            self.generation = 1
        elif "837-15286" in hw:
            self.model = "837-15286"
            self.generation = 2
        elif "837-15396" in hw:
            self.model = "837-15396"
            self.generation = 3
        else:
            self.model = f"Unknown ({hw})"
            self.generation = 0


# ---------------------------------------------------------------------------
# NFC Reader
# ---------------------------------------------------------------------------

class NFCReaderError(Exception):
    """NFC reader command error."""
    pass


class NFCReader:
    """
    High-level command interface for SEGA AIME NFC card readers.

    Usage:
        serial = SGSerial()
        serial.open("COM3", 115200)
        reader = NFCReader(serial)
        reader.reset()
        info = reader.get_reader_info()
        print(info.fw_version, info.hw_version)
    """

    def __init__(self, serial: SGSerial, addr: int = DEFAULT_ADDR):
        self._serial = serial
        self._addr = addr

    def _send(self, cmd: int, payload: bytes = b"", timeout: float = 1.0) -> tuple | None:
        """Send command and return response tuple or None."""
        return self._serial.send_command(self._addr, cmd, payload, timeout=timeout)

    def _send_ok(self, cmd: int, payload: bytes = b"", timeout: float = 1.0) -> bytes:
        """Send command and return payload, raising on error."""
        resp = self._send(cmd, payload, timeout=timeout)
        if resp is None:
            raise NFCReaderError(f"No response for cmd 0x{cmd:02X}")
        addr, seq, rcmd, status, rpayload = resp
        if status != 0:
            raise NFCReaderError(
                f"Command 0x{cmd:02X} failed with status 0x{status:02X}"
            )
        return rpayload

    # ------------------------------------------------------------------
    # Basic commands
    # ------------------------------------------------------------------

    def reset(self, post_delay: float = 0.5, mode: int = 0x00) -> bool:
        """
        Reset the reader (cmd 0x62).

        Args:
            post_delay: Seconds to wait after reset for reader reboot.
            mode: Reset mode byte.
                0x00 = normal reset (reader stays operational, responds immediately)
                0x03 = firmware update mode (reader enters bootloader, no response)
        """
        import time

        logger.info("Resetting reader (mode=0x%02X)...", mode)
        self._serial.purge()

        resp = self._send(AimeCmd.RESET, bytes([mode]), timeout=1.0)
        if resp is None:
            self._serial.purge()
            logger.info("Reset sent (no response — normal for some readers)")
        else:
            addr, seq, rcmd, status, payload = resp
            logger.info("Reset response: status=0x%02X", status)

        time.sleep(post_delay)
        self._serial.purge()
        return True

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """
        Poll GET_FW_VERSION until the reader returns status=0x00.

        After opening the serial port (which may toggle DTR/RTS and
        cause a hardware reset), the reader needs time to fully boot.
        During boot, it responds with status=0x03 ("not ready").

        This method polls periodically until status=0x00 or timeout.

        Args:
            timeout: Maximum wait time in seconds.

        Returns:
            True if reader became ready, False on timeout.
        """
        import time

        deadline = time.monotonic() + timeout
        attempt = 0

        while time.monotonic() < deadline:
            attempt += 1
            resp = self._send(AimeCmd.GET_FW_VERSION, b"", timeout=1.0)
            if resp is not None:
                addr, seq, rcmd, status, payload = resp
                if status == 0:
                    logger.info("Reader ready after %d attempts", attempt)
                    return True
                logger.debug("wait_ready: status=0x%02X (attempt %d)", status, attempt)
            else:
                logger.debug("wait_ready: no response (attempt %d)", attempt)

            # Brief pause before next poll
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.5, remaining))

        logger.warning("Reader not ready after %.1fs (%d attempts)", timeout, attempt)
        return False

    def get_fw_version(self, retries: int = 3) -> str:
        """
        Query firmware version string.

        Request payload: [0x00]
        Response payload: [length] [ASCII string...]

        The reader may return status=0x03 ("not ready") if it hasn't
        fully initialized yet.  In that case, we wait longer between
        retries to give it time.

        Args:
            retries: Number of retry attempts on failure.
        """
        import time

        for attempt in range(retries):
            resp = self._send(AimeCmd.GET_FW_VERSION, b"", timeout=1.0)
            if resp is not None:
                addr, seq, rcmd, status, payload = resp
                if status != 0:
                    # Reader responded but isn't ready yet (e.g. status=0x03)
                    logger.info("FW version: status=0x%02X (not ready, attempt %d/%d)",
                                status, attempt + 1, retries)
                    if attempt < retries - 1:
                        time.sleep(1.0)  # Wait longer — reader needs time to init
                    continue
                # Payload now contains just the version data (payload_len is
                # handled by the frame layer).
                # Gen1: printable ASCII like "TN32MSEC003S F/W Ver1.2"
                # Gen2/Gen3: single byte like 0x94
                if len(payload) >= 1:
                    if len(payload) == 1 and payload[0] >= 0x80:
                        return f"0x{payload[0]:02X}"
                    return payload.decode("ascii", errors="replace")
                else:
                    return ""
            else:
                logger.debug("FW version: no response (attempt %d/%d)", attempt + 1, retries)
                if attempt < retries - 1:
                    time.sleep(0.5)

        raise NFCReaderError("Failed to get FW version after retries")

    def get_hw_version(self, retries: int = 3) -> str:
        """
        Query hardware version string.

        Response payload: [length] [ASCII string...]

        Args:
            retries: Number of retry attempts on failure.
        """
        import time

        for attempt in range(retries):
            resp = self._send(AimeCmd.GET_HW_VERSION, b"", timeout=1.0)
            if resp is not None:
                addr, seq, rcmd, status, payload = resp
                if status != 0:
                    logger.info("HW version: status=0x%02X (not ready, attempt %d/%d)",
                                status, attempt + 1, retries)
                    if attempt < retries - 1:
                        time.sleep(1.0)
                    continue
                # Payload now contains just the version string directly
                # (payload_len is handled by the frame layer).
                if len(payload) >= 1:
                    return payload.decode("ascii", errors="replace")
                else:
                    return ""
            else:
                logger.debug("HW version: no response (attempt %d/%d)", attempt + 1, retries)
                if attempt < retries - 1:
                    time.sleep(0.5)

        raise NFCReaderError("Failed to get HW version after retries")

    def get_reader_info(self) -> ReaderInfo:
        """Query firmware and hardware version, identify the reader model."""
        info = ReaderInfo()
        try:
            info.fw_version = self.get_fw_version()
        except NFCReaderError as e:
            logger.warning("Failed to get FW version: %s", e)
        try:
            info.hw_version = self.get_hw_version()
        except NFCReaderError as e:
            logger.warning("Failed to get HW version: %s", e)
        info.identify()
        return info

    # ------------------------------------------------------------------
    # Radio control
    # ------------------------------------------------------------------

    def radio_on(self, radio_type: int = 3) -> None:
        """
        Turn on radio/RF field.

        Args:
            radio_type: Type of radio to enable.
                1 = MIFARE only, 2 = FeliCa only, 3 = both.
        """
        self._send_ok(AimeCmd.RADIO_ON, bytes([radio_type]))
        logger.info("Radio ON (type=%d)", radio_type)

    def radio_off(self) -> None:
        """Turn off radio/RF field."""
        try:
            self._send(AimeCmd.RADIO_OFF, b"", timeout=0.5)
        except Exception:
            pass
        logger.info("Radio OFF")

    # ------------------------------------------------------------------
    # Card polling
    # ------------------------------------------------------------------

    def poll(self) -> CardInfo:
        """
        Poll for a card on the reader.

        Supports two response formats:
            Format A (standard): [count] [type] [uid_len] [uid...]
            Format B (length-prefixed): [data_len] [count] [type] [uid_len] [uid...]

        Returns:
            CardInfo with detection results.
        """
        card = CardInfo()

        # Purge stale data before polling
        self._serial.purge()

        resp = self._send(AimeCmd.POLL, b"", timeout=0.5)
        if resp is None:
            return card

        addr, seq, cmd, status, p = resp
        if status != 0 or len(p) < 1:
            return card

        n = len(p)

        # Try Format B first: [data_len] [count] [type] [uid_len] [uid...]
        if n >= 5 and p[0] == (n - 1) and p[1] >= 1:
            card_type_byte = p[2]
            if card_type_byte in MIFARE_TYPES:
                card.detected = True
                card.card_type = CardType.MIFARE
                uid_len = min(p[3], 8)
                if 4 + uid_len <= n:
                    card.uid = bytes(p[4:4 + uid_len])
                    card.uid_len = uid_len
                logger.info("Poll: MIFARE (fmtB type=0x%02X uid_len=%d)", card_type_byte, card.uid_len)
                return card
            if card_type_byte in FELICA_TYPES:
                card.detected = True
                card.card_type = CardType.FELICA
                if n >= 12:
                    card.idm = bytes(p[4:12])
                    card.uid = card.idm
                    card.uid_len = 8
                    if n >= 20:
                        card.pmm = bytes(p[12:20])
                    # FeliCa access code: 02 FE + IDm
                    card.access_code = bytes([0x02, 0xFE]) + card.idm
                logger.info("Poll: FeliCa (fmtB type=0x%02X)", card_type_byte)
                return card

        # Try Format A: [count] [type] [uid_len] [uid...]
        count = p[0]
        if count == 0 or n < 2:
            return card

        card_type_byte = p[1]
        if card_type_byte in MIFARE_TYPES:
            card.detected = True
            card.card_type = CardType.MIFARE
            if n >= 3:
                uid_len = min(p[2], 8)
                if 3 + uid_len <= n:
                    uid_len = min(uid_len, n - 3)
                card.uid = bytes(p[3:3 + uid_len])
                card.uid_len = uid_len
            logger.info("Poll: MIFARE (fmtA type=0x%02X uid_len=%d)", card_type_byte, card.uid_len)
        elif card_type_byte in FELICA_TYPES:
            card.detected = True
            card.card_type = CardType.FELICA
            if n >= 11:
                card.idm = bytes(p[3:11])
                card.uid = card.idm
                card.uid_len = 8
                card.access_code = bytes([0x02, 0xFE]) + card.idm
            logger.info("Poll: FeliCa (fmtA type=0x%02X)", card_type_byte)
        else:
            logger.warning("Poll: unrecognized type=0x%02X data=%s",
                           card_type_byte,
                           " ".join(f"{b:02X}" for b in p[:16]))

        return card

    # ------------------------------------------------------------------
    # MIFARE commands
    # ------------------------------------------------------------------

    def mifare_select(self, uid: bytes) -> bool:
        """Select a MIFARE tag by UID."""
        try:
            self._send_ok(AimeCmd.MIFARE_SELECT, uid)
            logger.debug("MIFARE select OK (uid=%s)", uid.hex())
            return True
        except NFCReaderError as e:
            logger.warning("MIFARE select failed: %s", e)
            return False

    def mifare_set_key_bana(self) -> bool:
        """
        Set MIFARE authentication key to BANA key (WCCF v2).

        Payload: [0x60] + BANA_KEY
        Uses command 0x54 (MIFARE_SET_KEY_AIME) as per reference implementation.
        """
        payload = bytes([0x60]) + BANA_KEY
        try:
            self._send_ok(AimeCmd.MIFARE_SET_KEY_AIME, payload)
            logger.debug("MIFARE set BANA key OK")
            return True
        except NFCReaderError as e:
            logger.warning("MIFARE set BANA key failed: %s", e)
            return False

    def mifare_set_key_aime(self, key: bytes = BANA_KEY) -> bool:
        """
        Set MIFARE authentication key for AIME.

        Args:
            key: 6-byte key (default: BANA_KEY).
        """
        payload = bytes([0x60]) + key[:6]
        try:
            self._send_ok(AimeCmd.MIFARE_SET_KEY_AIME, payload)
            logger.debug("MIFARE set AIME key OK")
            return True
        except NFCReaderError as e:
            logger.warning("MIFARE set AIME key failed: %s", e)
            return False

    def mifare_authenticate(self, block: int, uid: bytes) -> bool:
        """
        Authenticate to a MIFARE block.

        Payload: [block] + uid
        """
        payload = bytes([block & 0xFF]) + uid
        try:
            self._send_ok(AimeCmd.MIFARE_AUTHENTICATE, payload)
            logger.debug("MIFARE auth OK (block=%d)", block)
            return True
        except NFCReaderError as e:
            logger.warning("MIFARE auth failed (block=%d): %s", block, e)
            return False

    def mifare_read_block(self, uid: bytes, block: int) -> bytes | None:
        """
        Read a 16-byte MIFARE block.

        Payload: [data_len] [uid...] [block]
        Response: [0x10] [16 bytes of block data]

        Returns:
            16 bytes of block data, or None on failure.
        """
        data_len = len(uid) + 1  # uid + block byte
        payload = bytes([data_len]) + uid + bytes([block & 0xFF])
        try:
            resp_payload = self._send_ok(AimeCmd.MIFARE_READ_BLOCK, payload)
        except NFCReaderError as e:
            logger.warning("MIFARE read block %d failed: %s", block, e)
            return None

        # Response payload: [16 bytes of block data]
        # (payload_len byte is handled by the frame layer)
        if len(resp_payload) >= 16:
            return bytes(resp_payload[:16])
        else:
            logger.warning("MIFARE read block %d: unexpected response length %d",
                           block, len(resp_payload))
            return None

    # ------------------------------------------------------------------
    # FeliCa commands
    # ------------------------------------------------------------------

    def felica_encap(self, data: bytes) -> bytes | None:
        """
        Send a FeliCa encapsulated command.

        Args:
            data: FeliCa command data.

        Returns:
            Response data or None.
        """
        try:
            return self._send_ok(AimeCmd.FELICA_ENCAP, data)
        except NFCReaderError as e:
            logger.warning("FeliCa encap failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Firmware update support
    # ------------------------------------------------------------------

    def enter_update_mode(self) -> bool:
        """
        Enter firmware update mode (cmd 0x60).

        Puts the reader into bootloader mode for firmware transfer.
        WARNING: Reader stays in update mode until firmware is
        successfully flashed and RESET (0x62) is issued!

        Returns:
            True if update mode entered successfully.
        """
        resp = self._send(AimeCmd.TO_UPDATE_MODE, b"", timeout=2.0)
        if resp is None:
            raise NFCReaderError("No response for TO_UPDATE_MODE")
        addr, seq, cmd, status, payload = resp
        if status != 0:
            raise NFCReaderError(
                f"TO_UPDATE_MODE failed with status 0x{status:02X}"
            )
        logger.info("Entered firmware update mode")
        return True

    def send_hex_data(self, data: bytes) -> int:
        """
        Send a Gen1 firmware data chunk via Intel HEX (cmd 0x61).

        For Gen1 (TN32MSEC003S / ATmega) readers only.
        Each chunk is typically a 43-byte Intel HEX record.

        Args:
            data: Intel HEX record data (typically 43 bytes ASCII).

        Returns:
            Status byte from response (0x20 = success).

        Raises:
            NFCReaderError: On communication failure.
        """
        resp = self._send(AimeCmd.SEND_HEX_DATA, data, timeout=2.0)
        if resp is None:
            raise NFCReaderError("No response for firmware data chunk")
        addr, seq, cmd, status, payload = resp
        return status

    def send_bindata(self) -> bool:
        """
        Initialize Gen2/Gen3 binary firmware transfer (cmd 0x63).

        Resets the firmware byte counter on the reader. Must be called
        before sending firmware chunks via bindata_exec().

        Returns:
            True if initialization succeeded.

        Raises:
            NFCReaderError: On failure.
        """
        resp = self._send(AimeCmd.SEND_BINDATA, b"", timeout=2.0)
        if resp is None:
            raise NFCReaderError("No response for SEND_BINDATA (0x63)")
        addr, seq, cmd, status, payload = resp
        if status != 0:
            raise NFCReaderError(
                f"SEND_BINDATA failed with status 0x{status:02X}"
            )
        logger.info("Binary firmware transfer initialized (byte counter reset)")
        return True

    def bindata_exec(self, fw_chunk: bytes) -> int:
        """
        Send a 256-byte firmware chunk via Gen2/Gen3 binary protocol (cmd 0x64).

        The 256-byte firmware data is sent as payload in a standard SG frame.
        Due to byte overflow, payload_len wraps to 0x00 and frame_len wraps
        to 0x05. The checksum covers the full header + 256 payload bytes.

        After the reader receives 0x3000 (12288) bytes total, it may stop
        responding (final chunk). Status 0x08 on the last chunk indicates
        the reader is executing the new firmware.

        Args:
            fw_chunk: Exactly 256 bytes of firmware data.

        Returns:
            Status byte (0x00 = success, 0x08 = firmware executing).

        Raises:
            NFCReaderError: On communication failure (except for expected
                           no-response on final chunk).
        """
        if len(fw_chunk) != 256:
            raise NFCReaderError(
                f"bindata_exec requires exactly 256 bytes, got {len(fw_chunk)}"
            )

        resp = self._send(AimeCmd.BINDATA_EXEC, fw_chunk, timeout=3.0)
        if resp is None:
            # After 0x3000 bytes, no response is expected (reader is busy)
            logger.debug("bindata_exec: no response (may be expected for final chunk)")
            return -1
        addr, seq, cmd, status, payload = resp
        return status
