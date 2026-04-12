"""
SG Serial - Serial port communication wrapper for SEGA AIME readers.

Wraps pyserial for SG frame send/receive with automatic sequence numbering,
byte-level unescaping on receive, and configurable timeouts.

Reading uses a byte-by-byte approach (matching SEGA835Lib ReadLenByOffset)
which is more reliable across USB-serial adapters than bulk reads.

Reference:
    SEGA835Lib/Serial/SProtSerial.cs  (ReadLenByOffset / Write)
    amdaemon/src/arcade/AimeReader.cpp  (sendPacket / recvPacket)
"""

import logging
import time

import serial

from sg_protocol import (
    ESCAPE_BYTE,
    SYNC_BYTE,
    calc_checksum,
    escape_data,
    sg_frame_encode,
)

logger = logging.getLogger(__name__)


class SGSerialError(Exception):
    """Serial communication error."""
    pass


class SGSerial:
    """
    Serial communication layer for SG-frame protocol.

    Handles opening/closing the port, framing, escaping, checksum,
    and automatic sequence number management.
    """

    DEFAULT_BAUDRATE = 115200
    DEFAULT_TIMEOUT = 1.0  # seconds

    def __init__(self):
        self._serial: serial.Serial | None = None
        self._seq_no: int = 0
        self._verbose: bool = False

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self, port: str, baudrate: int = DEFAULT_BAUDRATE) -> None:
        """Open serial port.

        Configures 8N1 with no flow control, matching the DCB settings
        used by the reference implementations (amdaemon, SEGA835Lib).
        RTS and DTR are explicitly disabled since some USB-TTL adapters
        use these lines to reset or hold the reader in a special state.

        After opening, a brief settle time is applied to allow USB-serial
        adapters (e.g. CP210x) to stabilize — opening the port may briefly
        toggle DTR/RTS, which some readers interpret as a reset signal.
        """
        if self._serial and self._serial.is_open:
            self._serial.close()

        self._serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.DEFAULT_TIMEOUT,
            write_timeout=self.DEFAULT_TIMEOUT,
            # Disable all flow control — match reference DCB
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        # Explicitly set DTR and RTS low to avoid interfering with
        # readers that use these lines for reset or mode selection
        self._serial.dtr = False
        self._serial.rts = False

        # Brief settle time for USB-serial adapters.
        # Opening the port may briefly toggle control lines, some readers
        # need a moment to stabilize before accepting commands.
        time.sleep(0.1)
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

        self._seq_no = 0
        logger.info("Opened %s at %d baud", port, baudrate)

    def close(self) -> None:
        """Close serial port."""
        if self._serial and self._serial.is_open:
            self._serial.close()
            logger.info("Serial port closed")
        self._serial = None

    def set_verbose(self, verbose: bool) -> None:
        """Enable/disable verbose hex dumping."""
        self._verbose = verbose

    def _next_seq(self) -> int:
        """Get and increment the sequence number (wraps at 256)."""
        seq = self._seq_no
        self._seq_no = (self._seq_no + 1) & 0xFF
        return seq

    # ------------------------------------------------------------------
    # Low-level byte reading helpers
    # These do NOT modify the serial timeout — the caller sets it once.
    # ------------------------------------------------------------------

    def _read_one_byte(self) -> int | None:
        """Read a single raw byte. Does NOT modify serial timeout."""
        data = self._serial.read(1)
        if len(data) == 0:
            return None
        return data[0]

    def _read_one_unescaped(self) -> int | None:
        """Read a single de-escaped byte. Does NOT modify serial timeout.

        Handles SG escape sequences: 0xD0 xx -> (xx + 1) & 0xFF
        """
        b = self._read_one_byte()
        if b is None:
            return None
        if b == ESCAPE_BYTE:
            b2 = self._read_one_byte()
            if b2 is None:
                return None
            return (b2 + 1) & 0xFF
        return b

    def _hex_dump(self, tag: str, data: bytes) -> None:
        """Log hex dump of data."""
        if self._verbose:
            hex_str = " ".join(f"{b:02X}" for b in data[:80])
            suffix = " ..." if len(data) > 80 else ""
            logger.debug("%s (%d): %s%s", tag, len(data), hex_str, suffix)

    def send_raw(self, data: bytes) -> None:
        """Send raw bytes over serial."""
        if not self.is_open:
            raise SGSerialError("Serial port not open")
        self._hex_dump("TX", data)
        self._serial.write(data)
        self._serial.flush()

    def recv_frame(self, timeout: float = 1.0) -> tuple | None:
        """
        Receive and decode a single SG frame response.

        Uses byte-by-byte reading with de-escaping, matching the
        SEGA835Lib ReadLenByOffset approach:
          1. Wait for SYNC byte (0xE0) with the specified timeout
          2. Read frame_len byte (de-escaped)
          3. Read frame_len more body bytes (de-escaped)
          4. Verify checksum
          5. Parse fields

        This approach is more reliable than bulk reads on various
        USB-serial adapters (CP210x, CH340, FTDI).

        Protocol response format (after de-escaping):
            [frame_len] [addr] [seq] [cmd] [status] [payload...] [checksum]

        Args:
            timeout: Maximum time to wait for SYNC byte in seconds.

        Returns:
            Tuple of (addr, seq_no, cmd, status, payload) or None on timeout.
        """
        if not self.is_open:
            raise SGSerialError("Serial port not open")

        old_timeout = self._serial.timeout
        self._serial.timeout = timeout

        try:
            # Phase 1: Wait for SYNC byte (0xE0)
            while True:
                data = self._serial.read(1)
                if len(data) == 0:
                    logger.debug("RX: timeout waiting for SYNC (%.1fs)", timeout)
                    return None
                if data[0] == SYNC_BYTE:
                    break
                # Skip non-SYNC bytes (stale data from port open, etc.)
                logger.debug("RX: skipping non-SYNC byte 0x%02X", data[0])

            # After SYNC, remaining bytes should arrive quickly.
            # Use a shorter timeout for individual bytes to avoid
            # hanging if the frame is truncated.
            self._serial.timeout = 0.5

            # Phase 2: Read frame_len byte (with de-escaping)
            frame_len = self._read_one_unescaped()
            if frame_len is None:
                logger.debug("RX: timeout reading frame_len")
                return None

            if frame_len < 6:
                logger.warning("RX: frame too short (frame_len=%d)", frame_len)
                return None

            # Phase 3: Read 'frame_len' body bytes (with de-escaping)
            # body = [addr, seq, cmd, status, payload_len, payload..., checksum]
            body = bytearray()
            for i in range(frame_len):
                b = self._read_one_unescaped()
                if b is None:
                    logger.debug("RX: timeout at body byte %d/%d", i, frame_len)
                    return None
                body.append(b)

            # Phase 4: Verify checksum
            # checksum = (frame_len + addr + seq + cmd + status + payload) & 0xFF
            rx_checksum = body[-1]
            computed = frame_len
            for b in body[:-1]:
                computed = (computed + b) & 0xFF

            if rx_checksum != computed:
                logger.warning(
                    "RX: checksum mismatch (got 0x%02X, calc 0x%02X)",
                    rx_checksum, computed,
                )

            # Phase 5: Parse fields
            # Response format: [addr] [seq] [cmd] [status] [payload_len] [payload...] [checksum]
            addr = body[0]
            seq_no = body[1]
            cmd = body[2]
            status = body[3]

            # Extract payload using the payload_len byte
            if len(body) >= 6:
                payload_len = body[4]
                payload = bytes(body[5:5 + payload_len])
            else:
                payload = b""

            self._hex_dump("RX payload", payload)
            logger.debug(
                "RX: addr=%02X seq=%02X cmd=%02X status=%02X payload_len=%d",
                addr, seq_no, cmd, status, len(payload),
            )

            return (addr, seq_no, cmd, status, payload)

        finally:
            self._serial.timeout = old_timeout

    def send_command(
        self,
        addr: int,
        cmd: int,
        payload: bytes = b"",
        timeout: float = 1.0,
    ) -> tuple | None:
        """
        Send a command and wait for a response.

        Automatically assigns a sequence number.

        Args:
            addr: Device address (usually 0x00).
            cmd: Command byte.
            payload: Command payload.
            timeout: Response timeout in seconds.

        Returns:
            Tuple of (addr, seq_no, cmd, status, payload) or None on timeout.
        """
        seq = self._next_seq()
        frame = sg_frame_encode(addr, seq, cmd, payload)
        self.send_raw(frame)
        return self.recv_frame(timeout=timeout)

    def send_led_command(
        self,
        cmd: int,
        payload: bytes,
        addr: int = 0x08,
    ) -> None:
        """
        Send an LED command (fire-and-forget, minimal response wait).

        LED commands typically don't get meaningful responses.

        Args:
            cmd: LED command byte (0x81).
            payload: LED payload (e.g., [board_id, R, G, B]).
            addr: Device address for LED (default 0x08).
        """
        # Purge any stale RX data before sending
        if self.is_open:
            self._serial.reset_input_buffer()

        seq = self._next_seq()
        frame = sg_frame_encode(addr, seq, cmd, payload)
        self.send_raw(frame)

        # Try a very short read — LED commands may or may not respond
        self.recv_frame(timeout=0.05)

    def purge(self) -> None:
        """Clear serial input/output buffers."""
        if self.is_open:
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
