"""
Servo driver for SG90 hobby servos on a Raspberry Pi, using pigpio (DMA-timed
PWM straight from GPIO). If pigpio or its daemon is not available (for example
during desktop testing with no hardware), it drops into a logging mock so the
rest of the stack can run unchanged.

Wiring reminder:
  - Servo signal wires go to GPIO pins (BCM numbering).
  - Servo power comes from a SEPARATE 5V supply, never the Pi 5V pin.
  - All grounds are tied together (Pi, servo supply).

pigpio needs its daemon running on the host:  sudo systemctl enable --now pigpiod
"""

import logging

log = logging.getLogger("servo-driver")

# SG90: roughly 500 us = 0 deg, 2500 us = 180 deg. Tune per servo if needed.
US_MIN, US_MAX = 500, 2500


class ServoDriver:
    def __init__(self, channels, angle_min=0.0, angle_max=180.0):
        # "channels" are BCM GPIO pin numbers, for example [17, 27, 22, 23]
        # (Pi 4 physical header pins 11, 13, 15, 16).
        self.channels = channels
        self.angle_min = angle_min
        self.angle_max = angle_max
        self.pi = None
        try:
            import pigpio
            self.pi = pigpio.pi()  # connects to the local pigpiod daemon
            if not self.pi.connected:
                raise RuntimeError("pigpiod not running")
            log.info("pigpio connected, driving GPIO pins %s", channels)
        except Exception as exc:  # noqa: BLE001
            log.warning("No pigpio (%s). MOCK mode: no servos will move.", exc)
            self.pi = None

    def _angle_to_us(self, angle):
        span = US_MAX - US_MIN
        return int(US_MIN + (angle / 180.0) * span)

    def set_angle(self, channel, angle):
        angle = max(self.angle_min, min(self.angle_max, angle))
        if self.pi is not None:
            self.pi.set_servo_pulsewidth(channel, self._angle_to_us(angle))
        else:
            log.info("[mock] pin %s -> %.1f deg", channel, angle)
        return angle

    def release(self, channel):
        """Pulse width 0 stops the signal so the servo goes limp (safe-stop)."""
        if self.pi is not None:
            self.pi.set_servo_pulsewidth(channel, 0)
        else:
            log.info("[mock] pin %s -> released", channel)

    def release_all(self):
        for ch in self.channels:
            self.release(ch)
