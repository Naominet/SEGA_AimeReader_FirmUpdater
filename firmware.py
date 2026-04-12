"""
Firmware - Firmware file loading and processing for SEGA AIME readers.

Supports two firmware file formats:
    - .bin: Raw ARM Cortex-M0 binary (LPC1112, typically 12,288 bytes)
    - .hex: Intel HEX format (ATmega series)

Provides chunking for transmission (default 43 bytes per chunk)
and checksum calculation.
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class FirmwareError(Exception):
    """Firmware loading or processing error."""
    pass


class Firmware:
    """
    Firmware file loader and processor.

    Usage:
        fw = Firmware()
        fw.load("firmware.bin")
        print(f"Size: {fw.get_size()} bytes")
        for chunk in fw.get_chunks():
            send(chunk)
    """

    def __init__(self):
        self._data: bytes = b""
        self._filepath: str = ""
        self._format: str = ""

    @property
    def filepath(self) -> str:
        return self._filepath

    @property
    def format(self) -> str:
        return self._format

    def load(self, filepath: str) -> None:
        """
        Auto-detect format and load firmware file.

        Args:
            filepath: Path to firmware file (.bin or .hex).

        Raises:
            FirmwareError: If file cannot be loaded or format is unknown.
        """
        filepath = str(filepath)
        if not os.path.isfile(filepath):
            raise FirmwareError(f"Firmware file not found: {filepath}")

        ext = Path(filepath).suffix.lower()
        if ext == ".bin":
            self.load_bin(filepath)
        elif ext == ".hex":
            self.load_hex(filepath)
        else:
            raise FirmwareError(
                f"Unknown firmware format '{ext}'. Supported: .bin, .hex"
            )

    def load_bin(self, filepath: str) -> None:
        """
        Load a raw binary firmware file.

        Typical size: 12,288 bytes for LPC1112 ARM Cortex-M0.

        Args:
            filepath: Path to .bin file.
        """
        filepath = str(filepath)
        if not os.path.isfile(filepath):
            raise FirmwareError(f"File not found: {filepath}")

        with open(filepath, "rb") as f:
            self._data = f.read()

        self._filepath = filepath
        self._format = "bin"

        if len(self._data) == 0:
            raise FirmwareError("Firmware file is empty")

        logger.info("Loaded BIN firmware: %s (%d bytes)", filepath, len(self._data))

    def load_hex(self, filepath: str) -> None:
        """
        Load an Intel HEX format firmware file.

        Parses standard Intel HEX records:
            :LLAAAATT[DD...]CC
            LL = byte count
            AAAA = address
            TT = record type (00=data, 01=EOF, 02=ext_seg, 04=ext_linear)
            DD = data bytes
            CC = checksum (two's complement of sum of all bytes)

        Args:
            filepath: Path to .hex file.
        """
        filepath = str(filepath)
        if not os.path.isfile(filepath):
            raise FirmwareError(f"File not found: {filepath}")

        segments: dict[int, int] = {}  # address -> byte value
        base_address = 0

        with open(filepath, "r") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                if not line.startswith(":"):
                    raise FirmwareError(
                        f"Invalid Intel HEX line {line_no}: missing ':' prefix"
                    )

                try:
                    hex_data = bytes.fromhex(line[1:])
                except ValueError:
                    raise FirmwareError(
                        f"Invalid hex data at line {line_no}: {line}"
                    )

                if len(hex_data) < 5:
                    raise FirmwareError(
                        f"HEX record too short at line {line_no}"
                    )

                # Verify checksum
                if sum(hex_data) & 0xFF != 0:
                    raise FirmwareError(
                        f"Checksum error at line {line_no}"
                    )

                byte_count = hex_data[0]
                address = (hex_data[1] << 8) | hex_data[2]
                record_type = hex_data[3]
                data = hex_data[4:-1]

                if len(data) != byte_count:
                    raise FirmwareError(
                        f"Byte count mismatch at line {line_no}: "
                        f"expected {byte_count}, got {len(data)}"
                    )

                if record_type == 0x00:
                    # Data record
                    for i, byte_val in enumerate(data):
                        segments[base_address + address + i] = byte_val

                elif record_type == 0x01:
                    # End Of File
                    break

                elif record_type == 0x02:
                    # Extended Segment Address
                    if len(data) >= 2:
                        base_address = ((data[0] << 8) | data[1]) << 4

                elif record_type == 0x04:
                    # Extended Linear Address
                    if len(data) >= 2:
                        base_address = ((data[0] << 8) | data[1]) << 16

                # Other record types (03, 05) are ignored

        if not segments:
            raise FirmwareError("No data records found in HEX file")

        # Convert sparse segments to contiguous binary
        min_addr = min(segments.keys())
        max_addr = max(segments.keys())
        size = max_addr - min_addr + 1

        binary = bytearray(b"\xFF" * size)  # Fill with 0xFF (erased flash)
        for addr, val in segments.items():
            binary[addr - min_addr] = val

        self._data = bytes(binary)
        self._filepath = filepath
        self._format = "hex"

        logger.info(
            "Loaded HEX firmware: %s (%d bytes, addr 0x%04X-0x%04X)",
            filepath, len(self._data), min_addr, max_addr,
        )

    def get_data(self) -> bytes:
        """Get the raw firmware binary data."""
        return self._data

    def get_size(self) -> int:
        """Get firmware size in bytes."""
        return len(self._data)

    def get_chunks(self, chunk_size: int = 43) -> list[bytes]:
        """
        Split firmware into transmission chunks.

        Args:
            chunk_size: Size of each chunk in bytes (default 43).

        Returns:
            List of byte chunks.
        """
        chunks = []
        for i in range(0, len(self._data), chunk_size):
            chunks.append(self._data[i:i + chunk_size])
        return chunks

    def get_checksum(self) -> int:
        """Calculate simple checksum (sum of all bytes & 0xFFFF)."""
        return sum(self._data) & 0xFFFF

    def __repr__(self) -> str:
        return (
            f"Firmware(path='{self._filepath}', format='{self._format}', "
            f"size={len(self._data)})"
        )
