"""
Card Test - Card scanning and reading utilities for SEGA AIME readers.

Provides single and continuous card scanning, plus full AIME card
reading (MIFARE block 2 access code) and FeliCa IDm retrieval.

AIME card read procedure (MIFARE):
    1. Poll → detect card, get UID
    2. Select Tag (uid)
    3. Set Key BANA
    4. Authenticate Block 2 (uid)
    5. Read Block 2 → first 10 bytes = access code

FeliCa card read:
    1. Poll → detect card, get IDm
    2. Access code = 0x02 0xFE + IDm[8]

Reference:
    amdaemon/src/arcade/AimeReader.cpp (poll method)
"""

import logging
import time

from nfc_reader import CardInfo, CardType, NFCReader, NFCReaderError

logger = logging.getLogger(__name__)


class CardTester:
    """
    Card scanning and reading test utility.

    Usage:
        serial = SGSerial()
        serial.open("COM3", 115200)
        reader = NFCReader(serial)
        reader.reset()
        reader.radio_on()

        tester = CardTester(reader)
        card = tester.scan_once()
        if card.detected:
            print(card.uid_hex())
    """

    def __init__(self, reader: NFCReader):
        self._reader = reader

    def scan_once(self) -> CardInfo:
        """
        Perform a single card scan.

        Returns:
            CardInfo with detection results.
        """
        card = self._reader.poll()
        if card.detected:
            print(f"  Card detected: {card.type_name}")
            print(f"  UID: {card.uid_hex()}")
            if card.access_code:
                print(f"  Access Code: {card.access_code_hex()}")
        else:
            print("  No card detected")
        return card

    def continuous_scan(self, duration: float = 30.0, interval: float = 0.5) -> None:
        """
        Continuously scan for cards.

        Args:
            duration: Total scan duration in seconds (0 = indefinite).
            interval: Time between polls in seconds.
        """
        print(f"Scanning for cards (duration={duration}s, interval={interval}s)...")
        print("Press Ctrl+C to stop.\n")

        start_time = time.monotonic()
        scan_count = 0
        last_uid = b""

        try:
            while True:
                if duration > 0:
                    elapsed = time.monotonic() - start_time
                    if elapsed >= duration:
                        break

                card = self._reader.poll()
                scan_count += 1

                if card.detected and card.uid != last_uid:
                    last_uid = card.uid
                    timestamp = time.strftime("%H:%M:%S")
                    print(f"[{timestamp}] {card.type_name}: UID={card.uid_hex()}", end="")
                    if card.access_code:
                        print(f"  AccessCode={card.access_code_hex()}", end="")
                    print()
                elif not card.detected and last_uid:
                    last_uid = b""
                    timestamp = time.strftime("%H:%M:%S")
                    print(f"[{timestamp}] Card removed")

                time.sleep(interval)

        except KeyboardInterrupt:
            print("\nScan interrupted by user.")

        print(f"\nTotal polls: {scan_count}")

    def read_aime_card(self) -> bytes | None:
        """
        Read a full AIME card (MIFARE or FeliCa).

        For MIFARE:
            Poll → Select → Set Key BANA → Auth Block 2 → Read Block 2
            Returns 10-byte access code.

        For FeliCa:
            Poll → IDm
            Access code = 0x02 0xFE + IDm[8]

        Returns:
            10-byte access code, or None if reading fails.
        """
        print("Waiting for card... (place card on reader)")

        # Poll until a card is detected
        card = None
        for attempt in range(20):  # Try for ~10 seconds
            card = self._reader.poll()
            if card.detected:
                break
            time.sleep(0.5)

        if not card or not card.detected:
            print("  No card detected after timeout")
            return None

        print(f"  Card type: {card.type_name}")
        print(f"  UID: {card.uid_hex()}")

        if card.card_type == CardType.FELICA:
            # FeliCa: access code is directly available from poll
            access_code = card.access_code
            if access_code:
                print(f"  Access Code: {card.access_code_hex()}")
                return access_code
            else:
                print("  ERROR: Could not read FeliCa IDm")
                return None

        elif card.card_type == CardType.MIFARE:
            # MIFARE: need to select, set key, authenticate, and read
            uid = card.uid

            # Step 1: Select Tag
            print("  Selecting tag...")
            if not self._reader.mifare_select(uid):
                print("  ERROR: Select tag failed")
                return None

            # Step 2: Set BANA Key
            print("  Setting BANA key...")
            if not self._reader.mifare_set_key_bana():
                print("  ERROR: Set key failed")
                return None

            # Step 3: Authenticate Block 2
            print("  Authenticating block 2...")
            if not self._reader.mifare_authenticate(2, uid):
                print("  ERROR: Authentication failed")
                return None

            # Step 4: Read Block 2
            print("  Reading block 2...")
            block_data = self._reader.mifare_read_block(uid, 2)
            if block_data is None:
                print("  ERROR: Read block failed")
                return None

            # First 10 bytes of block 2 = access code
            access_code = block_data[:10]
            access_code_hex = "".join(f"{b:02X}" for b in access_code)
            print(f"  Access Code: {access_code_hex}")

            # Show full block data for debugging
            block_hex = " ".join(f"{b:02X}" for b in block_data)
            print(f"  Block 2 raw: {block_hex}")

            return access_code

        else:
            print(f"  Unknown card type: {card.card_type}")
            return None

    def read_felica_card(self) -> bytes | None:
        """
        Read a FeliCa card and return its IDm.

        Returns:
            8-byte IDm, or None if not detected.
        """
        print("Waiting for FeliCa card...")

        for attempt in range(20):
            card = self._reader.poll()
            if card.detected and card.card_type == CardType.FELICA:
                idm = card.idm
                idm_hex = " ".join(f"{b:02X}" for b in idm)
                print(f"  FeliCa IDm: {idm_hex}")
                if card.pmm:
                    pmm_hex = " ".join(f"{b:02X}" for b in card.pmm)
                    print(f"  FeliCa PMm: {pmm_hex}")
                return idm
            time.sleep(0.5)

        print("  No FeliCa card detected after timeout")
        return None
