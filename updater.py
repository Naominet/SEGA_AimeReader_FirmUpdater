"""
Firmware Updater - State machine for updating SEGA AIME reader firmware.

Supports two firmware update protocols:

Gen1 (TN32MSEC003S, ATmega):
    1. Enter update mode (cmd 0x60)
    2. Send Intel HEX records via cmd 0x61 (43 bytes each)
    3. Reset reader (cmd 0x62)

Gen2/Gen3 (837-15286/15396, LPC1112 ARM Cortex-M0):
    1. Enter update mode (cmd 0x60)
    2. Initialize binary transfer (cmd 0x63)
    3. Send 256-byte chunks via cmd 0x64 (as SG frame payload)
    4. Reset reader (cmd 0x62)

Key protocol discovery:
    For cmd 0x64, the 256 firmware bytes are sent as normal SG frame
    payload. Due to byte overflow, payload_len wraps to 0x00 and
    frame_len wraps to 0x05. All bytes are escaped normally.
    The checksum covers the full header (5 bytes) + payload (256 bytes).

Reference:
    micetools/dll/devices/ser_tn32msec.c (BindataExec)
    Dniel97/segatools/board/sg-nfc.c (send_hex_data)
"""

import logging
import sys
import time

from firmware import Firmware, FirmwareError
from nfc_reader import NFCReader, NFCReaderError, ReaderInfo

logger = logging.getLogger(__name__)

# Gen2/Gen3 binary firmware chunk size
GEN2_CHUNK_SIZE = 256
# Gen1 Intel HEX record size (43 ASCII chars = :10XXXXXX<32 hex>CC)
GEN1_CHUNK_SIZE = 43
# Gen1 expected status for successful chunk
GEN1_STATUS_OK = 0x20


class UpdateError(Exception):
    """Firmware update error."""
    pass


class FirmwareUpdater:
    """
    Firmware update manager for SEGA AIME readers.

    Usage:
        serial = SGSerial()
        serial.open("COM3", 115200)
        reader = NFCReader(serial)
        updater = FirmwareUpdater(reader)
        updater.update("firmware.bin", verify=True)
    """

    def __init__(self, reader: NFCReader):
        self._reader = reader
        self._reader_info: ReaderInfo | None = None

    def check_reader_info(self) -> ReaderInfo:
        """
        Query and display reader information.

        Returns:
            ReaderInfo with model/version details.
        """
        info = self._reader.get_reader_info()
        self._reader_info = info

        print(f"  Hardware: {info.hw_version}")
        print(f"  Firmware: {info.fw_version}")
        print(f"  Model:    {info.model}")
        print(f"  Gen:      {info.generation or 'Unknown'}")

        return info

    def update(
        self,
        firmware_path: str,
        verify: bool = True,
        force: bool = False,
    ) -> bool:
        """
        Execute a firmware update.

        Automatically selects the correct protocol based on reader generation:
        - Gen1 (TN32MSEC003S): Intel HEX via cmd 0x61
        - Gen2/3 (837-15286/15396): Binary 256-byte chunks via cmd 0x63+0x64

        Args:
            firmware_path: Path to firmware file (.bin or .hex).
            verify: If True, re-query version after update to verify.
            force: If True, skip model compatibility check.

        Returns:
            True if update completed successfully.

        Raises:
            UpdateError: On fatal update error.
        """
        # Step 1: Load firmware
        print("\n[1/7] Loading firmware file...")
        fw = Firmware()
        try:
            fw.load(firmware_path)
        except FirmwareError as e:
            raise UpdateError(f"Failed to load firmware: {e}")

        print(f"  File:     {firmware_path}")
        print(f"  Format:   {fw.format}")
        print(f"  Size:     {fw.get_size()} bytes")
        print(f"  Checksum: 0x{fw.get_checksum():04X}")

        # Step 2: Query current version
        print("\n[2/7] Querying reader info...")
        try:
            info = self.check_reader_info()
        except NFCReaderError as e:
            raise UpdateError(f"Failed to query reader: {e}")

        # Step 3: Compatibility check
        print("\n[3/7] Checking compatibility...")
        if not force and info.generation == 0:
            print("  WARNING: Unknown reader model. Use --force to proceed.")
            raise UpdateError("Unknown reader model (use --force to override)")

        if not force:
            # Gen1 (ATmega) uses .hex, Gen2/3 (LPC1112 ARM) uses .bin
            if info.generation == 1 and fw.format == "bin":
                print("  WARNING: Gen1 (TN32MSEC003S) typically uses .hex firmware")
                raise UpdateError("Firmware format mismatch (use --force to override)")
            if info.generation in (2, 3) and fw.format == "hex":
                print("  WARNING: Gen2/3 (837-15286/15396) uses .bin firmware")
                raise UpdateError("Firmware format mismatch (use --force to override)")

        print("  OK")

        # Step 4: Turn off radio
        print("\n[4/7] Disabling radio...")
        try:
            self._reader.radio_off()
        except Exception as e:
            logger.warning("Radio off failed (continuing): %s", e)
        print("  Radio off")
        time.sleep(0.1)

        # Step 5: Enter firmware update mode
        print("\n[5/7] Entering firmware update mode...")
        try:
            self._reader.enter_update_mode()
            print("  Update mode entered")
        except NFCReaderError as e:
            logger.warning("Enter update mode: %s", e)
            print(f"  Warning: {e} (continuing)")
        time.sleep(0.3)

        # Step 6: Send firmware data
        print("\n[6/7] Sending firmware data...")

        if info.generation in (2, 3):
            success = self._update_gen2(fw)
        elif info.generation == 1:
            success = self._update_gen1(fw)
        else:
            if force:
                # Default to Gen2 protocol for .bin, Gen1 for .hex
                if fw.format == "bin":
                    success = self._update_gen2(fw)
                else:
                    success = self._update_gen1(fw)
            else:
                raise UpdateError("Cannot determine update protocol for unknown reader")

        if not success:
            raise UpdateError("Firmware transfer failed")

        # Step 7: Reset and verify
        print("\n[7/7] Resetting reader...")
        time.sleep(0.5)

        try:
            self._reader.reset(mode=0x00)
        except Exception as e:
            logger.warning("Reset after update: %s", e)

        time.sleep(2.0)

        # Poll for recovery
        recovered = False
        for i in range(30):
            try:
                resp = self._reader._serial.send_command(
                    0x00, 0x30, b"", timeout=1.0
                )
                if resp is not None:
                    addr, seq, cmd, status, payload = resp
                    if status == 0x00:
                        recovered = True
                        break
            except Exception:
                pass
            time.sleep(0.5)

        if recovered:
            print("  Reader recovered!")
        else:
            print("  Reader not responding yet (may need power cycle)")

        # Verify
        if verify and recovered:
            print("\n  Verifying update...")
            time.sleep(1.0)
            try:
                new_info = self._reader.get_reader_info()
                print(f"  New FW: {new_info.fw_version}")
                print(f"  New HW: {new_info.hw_version}")

                if new_info.fw_version != info.fw_version:
                    print("  Firmware version changed - update succeeded!")
                else:
                    print("  WARNING: Firmware version unchanged")
            except NFCReaderError as e:
                print(f"  Verification failed: {e}")

        print(f"\nFirmware update {'complete!' if recovered else 'sent (reader needs power cycle)'}")
        return recovered

    def _update_gen2(self, fw: Firmware) -> bool:
        """
        Gen2/Gen3 firmware update via binary protocol (0x63 + 0x64).

        Protocol:
            1. cmd 0x63 (SendBindata) - Initialize, reset byte counter
            2. cmd 0x64 (BindataExec) - Send 256-byte chunks as SG frame payload
               The reader expects exactly 12288 bytes (48 chunks of 256).
               After receiving all data, the last chunk may return status 0x08.

        Args:
            fw: Loaded firmware object.

        Returns:
            True if transfer completed successfully.
        """
        fw_data = fw.get_data()

        # Pad to multiple of 256 bytes if needed
        remainder = len(fw_data) % GEN2_CHUNK_SIZE
        if remainder != 0:
            pad_size = GEN2_CHUNK_SIZE - remainder
            fw_data = fw_data + (b"\xFF" * pad_size)
            print(f"  Padded firmware from {fw.get_size()} to {len(fw_data)} bytes")

        total_chunks = len(fw_data) // GEN2_CHUNK_SIZE
        print(f"  Protocol:  Gen2/3 binary (0x63 + 0x64)")
        print(f"  Chunks:    {total_chunks} x {GEN2_CHUNK_SIZE} bytes")

        # Initialize binary transfer
        try:
            self._reader.send_bindata()
            print("  Transfer initialized (byte counter reset)")
        except NFCReaderError as e:
            print(f"  Init failed: {e}")
            return False

        # Send firmware chunks
        errors = 0
        status_counts = {}

        for i in range(total_chunks):
            offset = i * GEN2_CHUNK_SIZE
            chunk = fw_data[offset:offset + GEN2_CHUNK_SIZE]

            # Progress bar
            progress = (i + 1) / total_chunks
            bar_len = 40
            filled = int(bar_len * progress)
            bar = "#" * filled + "-" * (bar_len - filled)
            pct = progress * 100
            sys.stdout.write(
                f"\r  [{bar}] {pct:5.1f}% ({i+1}/{total_chunks})"
            )
            sys.stdout.flush()

            try:
                status = self._reader.bindata_exec(chunk)
                key = f"0x{status:02X}" if status >= 0 else "no_resp"
                status_counts[key] = status_counts.get(key, 0) + 1

                if status not in (0x00, 0x08, -1):
                    errors += 1
                    logger.warning(
                        "Chunk %d/%d: status 0x%02X",
                        i + 1, total_chunks, status
                    )
                    if errors > 5:
                        print(f"\n  Too many errors ({errors}) - aborting!")
                        return False
            except NFCReaderError as e:
                # No response on final chunk is expected
                if i >= total_chunks - 2:
                    status_counts["no_resp"] = status_counts.get("no_resp", 0) + 1
                else:
                    errors += 1
                    logger.warning("Chunk %d/%d: %s", i + 1, total_chunks, e)

            time.sleep(0.05)

        print()  # newline after progress bar
        print(f"  Status distribution: {status_counts}")

        if errors > 0:
            print(f"  Completed with {errors} error(s)")
        else:
            print("  Transfer complete!")

        return errors <= 2  # Allow a few errors (final chunk may not respond)

    def _update_gen1(self, fw: Firmware) -> bool:
        """
        Gen1 firmware update via Intel HEX (0x61).

        Protocol:
            1. Enter update mode (0x60)
            2. Send Intel HEX ASCII records via cmd 0x61 (43 bytes each)
            3. Status 0x20 = chunk accepted

        Args:
            fw: Loaded firmware object.

        Returns:
            True if transfer completed successfully.
        """
        # Generate Intel HEX records from firmware data
        fw_data = fw.get_data()
        hex_records = self._bin_to_hex_records(fw_data, bytes_per_record=16)
        total_records = len(hex_records)

        print(f"  Protocol:  Gen1 Intel HEX (0x61)")
        print(f"  Records:   {total_records} ({total_records - 1} data + 1 EOF)")

        errors = 0
        for i, record in enumerate(hex_records):
            # Progress bar
            progress = (i + 1) / total_records
            bar_len = 40
            filled = int(bar_len * progress)
            bar = "#" * filled + "-" * (bar_len - filled)
            pct = progress * 100
            sys.stdout.write(
                f"\r  [{bar}] {pct:5.1f}% ({i+1}/{total_records})"
            )
            sys.stdout.flush()

            payload = record.encode("ascii")
            try:
                status = self._reader.send_hex_data(payload)
                if status != GEN1_STATUS_OK:
                    errors += 1
                    logger.warning(
                        "Record %d/%d: status 0x%02X (expected 0x%02X)",
                        i + 1, total_records, status, GEN1_STATUS_OK
                    )
                    if errors > 10:
                        print(f"\n  Too many errors ({errors}) - aborting!")
                        return False
            except NFCReaderError as e:
                errors += 1
                logger.warning("Record %d/%d: %s", i + 1, total_records, e)

            time.sleep(0.02)

        print()  # newline after progress bar

        if errors > 0:
            print(f"  Completed with {errors} error(s)")
        else:
            print("  Transfer complete!")

        return errors == 0

    @staticmethod
    def _bin_to_hex_records(
        fw_data: bytes, bytes_per_record: int = 16
    ) -> list[str]:
        """
        Convert binary firmware data to Intel HEX records.

        Each record format: :LLAAAATT[DD...]CC
            LL = byte count
            AAAA = address
            TT = record type (00=data, 01=EOF)
            DD = data bytes
            CC = two's complement checksum

        Args:
            fw_data: Raw firmware binary data.
            bytes_per_record: Data bytes per record (default 16).

        Returns:
            List of Intel HEX record strings (with ':' prefix).
        """
        records = []
        for offset in range(0, len(fw_data), bytes_per_record):
            chunk = fw_data[offset:offset + bytes_per_record]
            bc = len(chunk)
            addr = offset & 0xFFFF
            record = f":{bc:02X}{addr:04X}00"
            for b in chunk:
                record += f"{b:02X}"
            cksum = bc + (addr >> 8) + (addr & 0xFF) + 0x00
            for b in chunk:
                cksum += b
            cksum = (~cksum + 1) & 0xFF
            record += f"{cksum:02X}"
            records.append(record)

        # EOF record
        records.append(":00000001FF")
        return records
