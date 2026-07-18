"""
Servo driver for SG90 hobby servos on a Raspberry Pi, using lgpio (the modern
gpiochip-based GPIO library). lgpio talks to the kernel /dev/gpiochip*
character device directly, so NO daemon is required (unlike pigpio's pigpiod);
it works on current Raspberry Pi OS (Bookworm/Trixie) and on the Pi 5. If lgpio
or a usable gpiochip is not available (for example during desktop testing with
no hardware), it drops into a logging mock so the rest of the stack can run
unchanged.

Wiring reminder:
  - Servo signal wires go to GPIO pins (BCM numbering).
  - Servo power comes from a SEPARATE 5V supply, never the Pi 5V pin.
  - All grounds are tied together (Pi, servo supply).

lgpio needs no host daemon; ensure the running user can access /dev/gpiochip*
(the `gpio` group on Raspberry Pi OS, or a privileged container).
"""

import logging

log = logging.getLogger("servo-driver")

# SG90: roughly 500 us = 0 deg, 2500 us = 180 deg. Tune per servo if needed.
US_MIN, US_MAX = 500, 2500


def _open_header_chip(lgpio):
    """Open the 40-pin header's gpiochip and return (handle, chip_number).

    The header controller differs by model: 'pinctrl-bcm2711' on gpiochip0
    (Pi 4) and 'pinctrl-rp1' on gpiochip4 (Pi 5). Probe the likely chips and
    pick the one whose label looks like the header, falling back to chip 0.
    """
    candidates = []
    for n in (0, 1, 2, 3, 4):
        try:
            handle = lgpio.gpiochip_open(n)
        except Exception:  # noqa: BLE001
            continue
        label = ""
        try:
            info = lgpio.gpio_get_chip_info(handle)  # [status, lines, name, label]
            label = str(info[3]) if len(info) > 3 else ""
        except Exception:  # noqa: BLE001
            pass
        candidates.append((n, handle, label))

    # Prefer the RP1 header (Pi 5), then the BCM header (Pi 4).
    for wanted in ("pinctrl-rp1", "pinctrl-bcm"):
        for n, handle, label in candidates:
            if wanted in label:
                for _, other, _ in candidates:
                    if other is not handle:
                        lgpio.gpiochip_close(other)
                return handle, n

    if candidates:
        keep = next((c for c in candidates if c[0] == 0), candidates[0])
        for _, handle, _ in candidates:
            if handle is not keep[1]:
                lgpio.gpiochip_close(handle)
        return keep[1], keep[0]

    raise RuntimeError("no gpiochip could be opened")


class ServoDriver:
    def __init__(self, channels, angle_min=0.0, angle_max=180.0):
        # "channels" are BCM GPIO pin numbers, for example [17, 27, 22, 23]
        # (Pi 4 physical header pins 11, 13, 15, 16).
        self.channels = channels
        self.angle_min = angle_min
        self.angle_max = angle_max
        self._lgpio = None
        self._handle = None
        try:
            import lgpio
            self._handle, chip = _open_header_chip(lgpio)
            for ch in channels:
                lgpio.gpio_claim_output(self._handle, ch, 0)
            self._lgpio = lgpio
            log.info("lgpio connected (gpiochip%d), driving GPIO pins %s",
                     chip, channels)
        except Exception as exc:  # noqa: BLE001
            log.warning("No lgpio (%s). MOCK mode: no servos will move.", exc)
            self._lgpio = None
            self._handle = None

    def _angle_to_us(self, angle):
        span = US_MAX - US_MIN
        return int(US_MIN + (angle / 180.0) * span)

    def set_angle(self, channel, angle):
        angle = max(self.angle_min, min(self.angle_max, angle))
        if self._lgpio is not None:
            self._lgpio.tx_servo(self._handle, channel, self._angle_to_us(angle))
        else:
            log.info("[mock] pin %s -> %.1f deg", channel, angle)
        return angle

    def release(self, channel):
        """Pulse width 0 stops the servo pulses so it goes limp (safe-stop)."""
        if self._lgpio is not None:
            self._lgpio.tx_servo(self._handle, channel, 0)
        else:
            log.info("[mock] pin %s -> released", channel)

    def release_all(self):
        for ch in self.channels:
            self.release(ch)
