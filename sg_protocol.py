"""
SG Frame Protocol - SEGA AIME Reader wire protocol encoder/decoder.

Request frame format (before escaping):
    [0xE0] [frame_len] [addr] [seq] [cmd] [payload_len] [payload...] [checksum]

Response frame format (before escaping):
    [0xE0] [frame_len] [addr] [seq] [cmd] [status] [payload_len] [payload...] [checksum]

frame_len = count of bytes after itself
    Request:  addr(1) + seq(1) + cmd(1) + payload_len(1) + payload(N) + checksum(1) = 5 + N
    Response: addr(1) + seq(1) + cmd(1) + status(1) + payload_len(1) + payload(N) + checksum(1) = 6 + N
checksum  = (frame_len + addr + seq + cmd + ... all fields up to last payload byte) & 0xFF

Byte escaping (applied to everything after the SYNC byte):
    0xE0 -> 0xD0, 0xDF   (escape, byte - 1)
    0xD0 -> 0xD0, 0xCF   (escape, byte - 1)

De-escaping:
    0xD0, xx -> (xx + 1)

Reference:
    segatools/board/sg-cmd.h   (sg_req_header / sg_res_header)
    segatools/board/sg-frame.c
    SEGA835Lib SProtSerial.cs
"""

SYNC_BYTE = 0xE0
ESCAPE_BYTE = 0xD0


def escape_byte(b: int) -> bytes:
    """Escape a single byte for SG wire format."""
    if b == SYNC_BYTE or b == ESCAPE_BYTE:
        return bytes([ESCAPE_BYTE, (b - 1) & 0xFF])
    return bytes([b])


def escape_data(data: bytes) -> bytes:
    """Escape all bytes in data for wire transmission."""
    result = bytearray()
    for b in data:
        result.extend(escape_byte(b))
    return bytes(result)


def unescape_data(data: bytes) -> bytes:
    """Remove escape sequences from wire data."""
    result = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == ESCAPE_BYTE:
            if i + 1 >= len(data):
                raise ValueError("Trailing escape byte in data")
            result.append((data[i + 1] + 1) & 0xFF)
            i += 2
        elif b == SYNC_BYTE:
            raise ValueError("Unexpected SYNC byte (0xE0) in escaped data")
        else:
            result.append(b)
            i += 1
    return bytes(result)


def calc_checksum(data: bytes) -> int:
    """Calculate checksum: sum of all bytes & 0xFF."""
    return sum(data) & 0xFF


def sg_frame_encode(addr: int, seq_no: int, cmd: int, payload: bytes = b"") -> bytes:
    """
    Encode a command into an SG frame for wire transmission.

    The frame includes an explicit payload_len byte between the command
    byte and the payload data, matching the sg_req_header format used
    by segatools and SEGA835Lib.

    Wire format (before escaping):
        [frame_len] [addr] [seq] [cmd] [payload_len] [payload...] [checksum]

    Args:
        addr: Device address (usually 0x00)
        seq_no: Sequence number (0-255)
        cmd: Command byte
        payload: Command payload bytes

    Returns:
        Complete wire-format frame with SYNC, escaping, and checksum.
    """
    payload_len = len(payload)

    # frame_len = addr(1) + seq(1) + cmd(1) + payload_len(1) + payload(N) + checksum(1)
    frame_len = 5 + payload_len

    # Build raw frame (before escaping)
    raw = bytearray()
    raw.append(frame_len & 0xFF)
    raw.append(addr & 0xFF)
    raw.append(seq_no & 0xFF)
    raw.append(cmd & 0xFF)
    raw.append(payload_len & 0xFF)
    raw.extend(payload)

    # Calculate checksum over all raw bytes
    checksum = calc_checksum(raw)
    raw.append(checksum)

    # Escape and prepend SYNC
    wire = bytearray([SYNC_BYTE])
    wire.extend(escape_data(bytes(raw)))

    return bytes(wire)


def sg_frame_decode(data: bytes) -> tuple:
    """
    Decode an SG frame response.

    The input data should be the unescaped frame body (everything after SYNC),
    i.e., [frame_len, addr, seq, cmd, status, payload_len, payload..., checksum].

    Args:
        data: Unescaped frame body bytes.

    Returns:
        Tuple of (addr, seq_no, cmd, status, payload).

    Raises:
        ValueError: If frame is malformed or checksum fails.
    """
    if len(data) < 7:
        raise ValueError(f"Frame too short: {len(data)} bytes (minimum 7)")

    frame_len = data[0]

    # Validate frame length: frame_len = bytes after itself, so total = frame_len + 1
    expected_total = frame_len + 1  # frame_len byte + frame_len more bytes
    if len(data) != expected_total:
        raise ValueError(
            f"Frame length mismatch: frame_len={frame_len}, "
            f"expected {expected_total} bytes, got {len(data)}"
        )

    # Verify checksum (sum of all bytes except the last one)
    received_checksum = data[-1]
    computed_checksum = calc_checksum(data[:-1])
    if received_checksum != computed_checksum:
        raise ValueError(
            f"Checksum mismatch: received 0x{received_checksum:02X}, "
            f"computed 0x{computed_checksum:02X}"
        )

    # Parse fields
    # Response: [frame_len] [addr] [seq] [cmd] [status] [payload_len] [payload...] [checksum]
    addr = data[1]
    seq_no = data[2]
    cmd = data[3]
    status = data[4]
    payload_len = data[5]
    payload = bytes(data[6:6 + payload_len])

    return (addr, seq_no, cmd, status, payload)


def sg_frame_decode_wire(wire_data: bytes) -> tuple:
    """
    Decode a complete wire-format frame (starting with SYNC byte).

    Args:
        wire_data: Complete wire frame starting with 0xE0.

    Returns:
        Tuple of (addr, seq_no, cmd, status, payload).

    Raises:
        ValueError: If frame is malformed.
    """
    if not wire_data or wire_data[0] != SYNC_BYTE:
        raise ValueError("Missing SYNC byte at start of frame")

    # Unescape everything after the SYNC byte
    unescaped = unescape_data(wire_data[1:])

    return sg_frame_decode(unescaped)
