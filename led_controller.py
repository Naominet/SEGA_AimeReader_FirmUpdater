"""
LED Controller - RGB LED control for SEGA AIME card readers.

Controls the RGB LED ring on AIME readers via the SG protocol.
Supports solid color, flash effects, rainbow animation, and off.

LED command format:
    Address: 0x08 (or 0x00 for newer firmware)
    Command: 0x81
    Payload: [board_id (0x00), R, G, B]
    Note: LED commands don't require waiting for a response.

Reference:
    amdaemon/src/arcade/AimeReader.cpp (cmdSetLed)
"""

import logging
import time
import colorsys

from sg_serial import SGSerial
from nfc_reader import AimeCmd

logger = logging.getLogger(__name__)

# Named color presets
COLOR_PRESETS = {
    "red":     (255, 0, 0),
    "green":   (0, 255, 0),
    "blue":    (0, 0, 255),
    "white":   (255, 255, 255),
    "yellow":  (255, 255, 0),
    "cyan":    (0, 255, 255),
    "magenta": (255, 0, 255),
    "orange":  (255, 128, 0),
    "purple":  (128, 0, 255),
    "pink":    (255, 105, 180),
    "off":     (0, 0, 0),
}


class LEDController:
    """
    Controls the RGB LED on SEGA AIME card readers.

    Usage:
        serial = SGSerial()
        serial.open("COM3", 115200)
        led = LEDController(serial)
        led.set_color(0, 255, 0)   # Green
        led.flash("red", times=3)
        led.rainbow(duration=5.0)
        led.off()
    """

    def __init__(self, serial: SGSerial, addr: int = 0x08):
        self._serial = serial
        self._addr = addr

    def set_color(self, r: int, g: int, b: int, board_id: int = 0) -> None:
        """
        Set LED to a solid RGB color.

        Args:
            r: Red component (0-255).
            g: Green component (0-255).
            b: Blue component (0-255).
            board_id: Board ID (default 0).
        """
        r = max(0, min(255, r))
        g = max(0, min(255, g))
        b = max(0, min(255, b))
        payload = bytes([board_id & 0xFF, r, g, b])
        self._serial.send_led_command(AimeCmd.LED_SET_COLOR, payload, addr=self._addr)
        logger.debug("LED set: R=%d G=%d B=%d", r, g, b)

    def set_named_color(self, name: str) -> None:
        """
        Set LED to a named color preset.

        Args:
            name: Color name (red, green, blue, white, yellow, cyan,
                  magenta, orange, purple, pink, off).

        Raises:
            ValueError: If color name is not recognized.
        """
        name_lower = name.lower()
        if name_lower not in COLOR_PRESETS:
            raise ValueError(
                f"Unknown color '{name}'. Available: {', '.join(sorted(COLOR_PRESETS))}"
            )
        r, g, b = COLOR_PRESETS[name_lower]
        self.set_color(r, g, b)

    def flash(self, color: tuple | str = "white", times: int = 3, interval: float = 0.3) -> None:
        """
        Flash the LED on and off.

        Args:
            color: RGB tuple (r, g, b) or color name string.
            times: Number of flashes.
            interval: Time between on/off transitions in seconds.
        """
        if isinstance(color, str):
            name_lower = color.lower()
            if name_lower in COLOR_PRESETS:
                r, g, b = COLOR_PRESETS[name_lower]
            else:
                r, g, b = 255, 255, 255
        else:
            r, g, b = color

        for i in range(times):
            self.set_color(r, g, b)
            time.sleep(interval)
            self.set_color(0, 0, 0)
            if i < times - 1:
                time.sleep(interval)

    def rainbow(self, duration: float = 5.0, steps: int = 60) -> None:
        """
        Display a rainbow color cycle animation.

        Args:
            duration: Total animation duration in seconds.
            steps: Number of color steps in the cycle.
        """
        step_duration = duration / steps
        for i in range(steps):
            hue = i / steps
            r_f, g_f, b_f = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
            r = int(r_f * 255)
            g = int(g_f * 255)
            b = int(b_f * 255)
            self.set_color(r, g, b)
            time.sleep(step_duration)

    def off(self) -> None:
        """Turn off the LED."""
        self.set_color(0, 0, 0)
        logger.info("LED off")
