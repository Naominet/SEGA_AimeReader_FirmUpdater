"""
SEGA AIME Reader Firmware Update Tool

CLI entry point for managing SEGA arcade NFC card readers:
    - Query reader info (firmware/hardware version)
    - Update firmware (.bin / .hex)
    - Control LED colors and effects
    - Scan and read cards (MIFARE / FeliCa)

Supported readers: 837-15084, 837-15286, 837-15396

Usage:
    python main.py --port COM3 info
    python main.py --port COM3 update -f firmware.bin --verify
    python main.py --port COM3 led --color red
    python main.py --port COM3 led --rgb 255 0 128
    python main.py --port COM3 scan --continuous
    python main.py --port COM3 read-card
"""

import argparse
import configparser
import logging
import os
import sys

from sg_serial import SGSerial, SGSerialError
from nfc_reader import NFCReader, NFCReaderError
from led_controller import LEDController, COLOR_PRESETS
from firmware import Firmware, FirmwareError
from updater import FirmwareUpdater, UpdateError
from card_test import CardTester


# ---------------------------------------------------------------------------
# Config file support
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.ini")


def load_config(config_path: str = DEFAULT_CONFIG_PATH) -> dict:
    """Load default settings from config.ini if it exists."""
    defaults = {
        "port": "",
        "baudrate": "115200",
        "verbose": "false",
    }
    if os.path.isfile(config_path):
        config = configparser.ConfigParser()
        config.read(config_path, encoding="utf-8")
        if "DEFAULT" in config:
            for key in defaults:
                if key in config["DEFAULT"]:
                    defaults[key] = config["DEFAULT"][key]
    return defaults


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_diag(sg_serial: SGSerial, args: argparse.Namespace) -> int:
    """Low-level serial diagnostic — send raw reset frame and dump all received bytes."""
    import time
    from sg_protocol import sg_frame_encode
    from serial.tools.list_ports import comports

    baud = sg_serial._serial.baudrate
    port = sg_serial._serial.port
    print(f"=== Serial Diagnostics ===")
    print(f"  Port: {port}  Baud: {baud}")

    # 0. List all available serial ports
    print(f"\n[0] Available serial ports:")
    ports = comports()
    if ports:
        for p in sorted(ports, key=lambda x: x.device):
            print(f"    {p.device}: {p.description} [{p.hwid}]")
    else:
        print(f"    (no ports found)")

    # 1. Check if there's any stale data on the line
    print(f"\n[1] Reading stale data (1s)...")
    sg_serial._serial.timeout = 1.0
    stale = sg_serial._serial.read(256)
    if stale:
        print(f"  Got {len(stale)} bytes: {stale.hex(' ')}")
    else:
        print(f"  (empty — no stale data)")

    # 2. Send RESET command (0x62, payload=[0x00] = normal reset)
    print(f"\n[2] Sending RESET command (cmd=0x62 mode=0x00)...")
    frame = sg_frame_encode(0x00, 0x00, 0x62, bytes([0x00]))
    print(f"  TX frame: {frame.hex(' ')}")
    sg_serial._serial.write(frame)
    sg_serial._serial.flush()

    print(f"  Waiting for response (3s)...")
    sg_serial._serial.timeout = 3.0
    resp = sg_serial._serial.read(256)
    if resp:
        print(f"  RX ({len(resp)} bytes): {resp.hex(' ')}")
    else:
        print(f"  RX: (no data — reader did not respond)")

    time.sleep(0.5)

    # 3. Send GET_FW_VERSION (0x30) and dump raw response
    print(f"\n[3] Sending GET_FW_VERSION (cmd=0x30)...")
    frame2 = sg_frame_encode(0x00, 0x01, 0x30, b"")
    print(f"  TX frame: {frame2.hex(' ')}")
    sg_serial._serial.write(frame2)
    sg_serial._serial.flush()

    print(f"  Waiting for response (2s)...")
    sg_serial._serial.timeout = 2.0
    resp2 = sg_serial._serial.read(256)
    if resp2:
        print(f"  RX ({len(resp2)} bytes): {resp2.hex(' ')}")
        # Try to find 0xE0 sync
        sync_positions = [i for i, b in enumerate(resp2) if b == 0xE0]
        if sync_positions:
            print(f"  SYNC (0xE0) found at positions: {sync_positions}")
    else:
        print(f"  RX: (no data)")

    # 4. Send GET_HW_VERSION (0x32) and dump raw response
    print(f"\n[4] Sending GET_HW_VERSION (cmd=0x32)...")
    frame3 = sg_frame_encode(0x00, 0x02, 0x32, b"")
    print(f"  TX frame: {frame3.hex(' ')}")
    sg_serial._serial.write(frame3)
    sg_serial._serial.flush()

    print(f"  Waiting for response (2s)...")
    sg_serial._serial.timeout = 2.0
    resp3 = sg_serial._serial.read(256)
    if resp3:
        print(f"  RX ({len(resp3)} bytes): {resp3.hex(' ')}")
    else:
        print(f"  RX: (no data)")

    # 5. Try reading any remaining data
    print(f"\n[5] Draining remaining data (1s)...")
    sg_serial._serial.timeout = 1.0
    extra = sg_serial._serial.read(1024)
    if extra:
        print(f"  Got {len(extra)} more bytes: {extra.hex(' ')}")
    else:
        print(f"  (nothing)")

    any_response = bool(stale or resp or resp2 or resp3 or extra)
    print(f"\n--- Summary ---")
    if any_response:
        print(f"  Reader IS responding on {port} at {baud} baud.")
        print(f"  If data looks garbled, try a different baud rate.")
    else:
        print(f"  No data received on {port} at {baud} baud.")
        print(f"  Possible causes:")
        print(f"    - Wrong COM port (check Device Manager)")
        print(f"    - Reader not powered (needs 5V/12V)")
        print(f"    - TX/RX wires swapped")
        print(f"    - Defective cable or reader")

    return 0 if any_response else 1


def cmd_info(reader: NFCReader, args: argparse.Namespace) -> int:
    """Query and display reader information."""
    print("=== AIME Reader Info ===")

    # Send a normal reset (mode=0x00) to put the reader into a known state.
    # NOTE: mode=0x03 enters firmware-update/bootloader mode and must NOT
    # be used here — it causes the reader to return status=0x03 for all
    # subsequent commands.
    print("\nInitializing reader...")
    reader.reset(post_delay=0.3, mode=0x00)

    # After a normal reset the reader should be immediately ready.
    # Poll briefly as a safety net (e.g. if the port-open toggled DTR).
    if not reader.wait_ready(timeout=3.0):
        print("  Reader not responding with status=0x00.")
        print("  Trying to query version anyway...")

    print("\nQuerying version info...")
    info = reader.get_reader_info()

    if not info.fw_version and not info.hw_version:
        print("\n  WARNING: No response from reader.")
        print("  Check connection, port, and baud rate.")
        return 1

    print(f"\n  Hardware Version: {info.hw_version}")
    print(f"  Firmware Version: {info.fw_version}")
    print(f"  Model:            {info.model}")
    print(f"  Generation:       {info.generation or 'Unknown'}")

    return 0


def cmd_update(reader: NFCReader, args: argparse.Namespace) -> int:
    """Perform firmware update."""
    print("=== Firmware Update ===")

    firmware_path = args.firmware
    if not os.path.isfile(firmware_path):
        print(f"ERROR: Firmware file not found: {firmware_path}")
        return 1

    print(f"\nFirmware file: {firmware_path}")

    # Initialize reader before update (normal reset to query version first)
    print("\nInitializing reader...")
    reader.reset(post_delay=0.3, mode=0x00)
    reader.wait_ready(timeout=3.0)

    # Confirm unless --force
    if not args.force:
        print("\n  WARNING: Firmware update may brick the reader if interrupted!")
        print("  Make sure the reader is connected and powered properly.")
        try:
            answer = input("\n  Proceed? [y/N]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return 1
        if answer not in ("y", "yes"):
            print("  Cancelled.")
            return 1

    updater = FirmwareUpdater(reader)
    try:
        updater.update(
            firmware_path,
            verify=args.verify,
            force=args.force,
        )
    except UpdateError as e:
        print(f"\nERROR: {e}")
        return 1

    return 0


def cmd_led(serial: SGSerial, args: argparse.Namespace) -> int:
    """Control reader LED."""
    led = LEDController(serial)

    if args.led_off:
        print("LED off")
        led.off()
    elif args.color:
        color_name = args.color.lower()
        print(f"Setting LED color: {color_name}")
        try:
            led.set_named_color(color_name)
        except ValueError as e:
            print(f"ERROR: {e}")
            return 1
    elif args.rgb:
        r, g, b = args.rgb
        print(f"Setting LED color: RGB({r}, {g}, {b})")
        led.set_color(r, g, b)
    elif args.flash:
        color = args.flash
        times = args.times or 3
        print(f"Flashing LED: {color} x{times}")
        led.flash(color, times=times)
    elif args.rainbow:
        duration = args.duration or 5.0
        print(f"Rainbow effect ({duration}s)")
        led.rainbow(duration=duration)
    else:
        print("No LED action specified. Use --color, --rgb, --flash, --rainbow, or --off")
        return 1

    return 0


def cmd_scan(reader: NFCReader, args: argparse.Namespace) -> int:
    """Scan for cards."""
    print("=== Card Scanner ===\n")

    print("Initializing reader...")
    reader.reset(post_delay=0.3, mode=0x00)
    reader.wait_ready(timeout=3.0)

    print("Enabling radio...")
    reader.radio_on()

    tester = CardTester(reader)

    if args.continuous:
        duration = args.duration or 0  # 0 = indefinite
        tester.continuous_scan(duration=duration)
    else:
        print("\nSingle scan:")
        tester.scan_once()

    return 0


def cmd_read_card(reader: NFCReader, args: argparse.Namespace) -> int:
    """Read a card's access code."""
    print("=== Card Reader ===\n")

    print("Initializing reader...")
    reader.reset(post_delay=0.3, mode=0x00)
    reader.wait_ready(timeout=3.0)

    print("Enabling radio...")
    reader.radio_on()

    tester = CardTester(reader)
    access_code = tester.read_aime_card()

    if access_code:
        hex_str = "".join(f"{b:02X}" for b in access_code)
        print(f"\n  Result: {hex_str}")
        return 0
    else:
        print("\n  Failed to read card")
        return 1


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser(defaults: dict) -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="aime-reader-tool",
        description="SEGA AIME Reader Firmware Update & Test Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --port COM3 info\n"
            "  %(prog)s --port COM3 update -f firmware.bin --verify\n"
            "  %(prog)s --port COM3 led --color red\n"
            "  %(prog)s --port COM3 led --rgb 255 0 128\n"
            "  %(prog)s --port COM3 scan --continuous\n"
            "  %(prog)s --port COM3 read-card\n"
        ),
    )

    # Global options
    parser.add_argument(
        "--port", "-p",
        default=defaults.get("port", ""),
        help="Serial port (e.g. COM3, /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--baud", "-b",
        type=int,
        default=int(defaults.get("baudrate", "115200")),
        help="Baud rate (default: 115200)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=defaults.get("verbose", "false").lower() == "true",
        help="Enable verbose debug output",
    )
    parser.add_argument(
        "--gen",
        type=int,
        choices=[1, 2, 3],
        help="Reader generation (1/2/3, default: auto-detect)",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # info
    subparsers.add_parser("info", help="Query reader information")

    # diag
    subparsers.add_parser("diag", help="Low-level serial diagnostic (troubleshoot connection)")

    # update
    update_parser = subparsers.add_parser("update", help="Update reader firmware")
    update_parser.add_argument(
        "-f", "--firmware",
        required=True,
        help="Path to firmware file (.bin or .hex)",
    )
    update_parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify firmware version after update",
    )
    update_parser.add_argument(
        "--force",
        action="store_true",
        help="Force update (skip compatibility checks and confirmation)",
    )

    # led
    led_parser = subparsers.add_parser("led", help="Control reader LED")
    led_group = led_parser.add_mutually_exclusive_group()
    led_group.add_argument(
        "--color", "-c",
        choices=list(COLOR_PRESETS.keys()),
        help="Set LED to a named color",
    )
    led_group.add_argument(
        "--rgb",
        nargs=3,
        type=int,
        metavar=("R", "G", "B"),
        help="Set LED to RGB values (0-255)",
    )
    led_group.add_argument(
        "--flash",
        metavar="COLOR",
        help="Flash LED with named color",
    )
    led_group.add_argument(
        "--rainbow",
        action="store_true",
        help="Rainbow color cycle effect",
    )
    led_group.add_argument(
        "--off",
        dest="led_off",
        action="store_true",
        help="Turn off LED",
    )
    led_parser.add_argument(
        "--times",
        type=int,
        default=3,
        help="Number of flashes (with --flash, default: 3)",
    )
    led_parser.add_argument(
        "--duration",
        type=float,
        default=5.0,
        help="Duration in seconds (with --rainbow, default: 5.0)",
    )

    # scan
    scan_parser = subparsers.add_parser("scan", help="Scan for NFC cards")
    scan_parser.add_argument(
        "--continuous",
        action="store_true",
        help="Continuously scan (until Ctrl+C or timeout)",
    )
    scan_parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="Duration for continuous scan in seconds (0 = indefinite)",
    )

    # read-card
    subparsers.add_parser("read-card", help="Read AIME card access code")

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool) -> None:
    """Configure logging."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    if verbose:
        fmt = "%(asctime)s [%(name)s:%(lineno)d] %(levelname)s: %(message)s"
    logging.basicConfig(level=level, format=fmt)


def main() -> int:
    defaults = load_config()
    parser = build_parser(defaults)
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    if not args.port:
        print("ERROR: No serial port specified. Use --port COMx")
        return 1

    setup_logging(args.verbose)

    # Open serial connection
    sg_serial = SGSerial()
    sg_serial.set_verbose(args.verbose)

    try:
        sg_serial.open(args.port, args.baud)
    except Exception as e:
        print(f"ERROR: Could not open {args.port}: {e}")
        return 1

    reader = NFCReader(sg_serial)

    try:
        if args.command == "diag":
            return cmd_diag(sg_serial, args)
        elif args.command == "info":
            rc = cmd_info(reader, args)
            # If failed with default baud, try the other one
            if rc == 0:
                return rc
            alt_baud = 38400 if args.baud == 115200 else 115200
            print(f"\nNo response at {args.baud} baud, trying {alt_baud}...")
            try:
                sg_serial.close()
                sg_serial.open(args.port, alt_baud)
                reader = NFCReader(sg_serial)
                return cmd_info(reader, args)
            except Exception as e:
                print(f"Also failed at {alt_baud}: {e}")
                return 1
        elif args.command == "update":
            return cmd_update(reader, args)
        elif args.command == "led":
            return cmd_led(sg_serial, args)
        elif args.command == "scan":
            return cmd_scan(reader, args)
        elif args.command == "read-card":
            return cmd_read_card(reader, args)
        else:
            parser.print_help()
            return 0
    except SGSerialError as e:
        print(f"\nSerial error: {e}")
        return 1
    except NFCReaderError as e:
        print(f"\nReader error: {e}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    finally:
        try:
            sg_serial.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
