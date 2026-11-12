"""Small animations on tk's after(): values that glide from one number to another. A frame timer runs only while
something moves and stops itself, so an idle window does no extra work."""
import ctypes
import time
from ctypes import wintypes

FRAME_MS = 16  # about 60 frames a second
HOVER_MS, GLIDE_MS, FADE_MS, SPIN_MS = 140, 300, 150, 900


def ease_out(t):
    """Cubic ease-out: fast start, gentle stop. 0 -> 0, 1 -> 1."""
    return 1 - (1 - t) ** 3


def linear(t):
    return t


def motion_on():
    """False when Windows has 'Animation effects' off (SPI_GETCLIENTAREAANIMATION): show end states at once."""
    on = wintypes.BOOL(True)
    try:
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0):
            return bool(on.value)
    except (AttributeError, OSError):
        pass
    return True


class Animator:
    """Keyed tweens sharing one frame timer. on_frame() is called once per frame while any tween runs, and once
    more after the last one ends, so the caller draws the exact end values."""

    def __init__(self, root, on_frame, motion=None, clock=time.perf_counter):
        self.root, self.on_frame, self.clock = root, on_frame, clock
        self.motion = motion or (lambda: motion_on())  # looked up each time, so tests can turn it off
        self.tweens = {}  # key -> [start value, end value, start time, ms, ease, mode]
        self.values = {}  # key -> value now (end value once done)
        self.job = None

    def value(self, key, default=0.0):
        return self.values.get(key, default)

    def to(self, key, end, ms, ease=ease_out, start=None):
        """Glide key from its value now (or start) to end in ms. Same target again: keeps going, no restart."""
        t = self.tweens.get(key)
        if t and t[1] == end and t[5] is None:
            return
        now_v = self.values.get(key, end) if start is None else start
        if not self.motion() or ms <= 0 or now_v == end:
            self.set(key, end)
            return
        self.values[key] = now_v
        self.tweens[key] = [now_v, end, self.clock(), ms, ease, None]
        self._wake()

    def set(self, key, value):
        """Jump to value with no animation."""
        self.tweens.pop(key, None)
        self.values[key] = value
        if not self.tweens and self.job is not None:  # nothing left to move: no idle frame
            self.root.after_cancel(self.job)
            self.job = None

    def spin(self, key, ms=SPIN_MS):
        """Turn key (degrees, 0-360) one full turn every ms, until settle(key)."""
        t = self.tweens.get(key)
        if t and t[5] == 'spin':
            return
        if not self.motion() or ms <= 0:
            self.set(key, 0.0)
            return
        self.tweens[key] = [self.values.get(key, 0.0) % 360, None, self.clock(), ms, linear, 'spin']
        self._wake()

    def settle(self, key, ms=SPIN_MS):
        """End a spin: finish the turn it is on, slowing down, and rest at 0."""
        t = self.tweens.get(key)
        if not t or t[5] != 'spin':
            return
        if not self.motion() or ms <= 0:
            self.set(key, 0.0)
            return
        v = self.values.get(key, 0.0)
        end = 360.0 if v <= 270 else 720.0  # at least a quarter turn left, so the slow-down shows
        # ease_out starts 3x faster than linear: 3x the time keeps the spin's speed at the hand-over (up to 1.2 s)
        self.tweens[key] = [v, end, self.clock(), min(1200, ms * (end - v) / 360 * 3), ease_out, 'settle']

    @property
    def running(self):
        return bool(self.tweens)

    def _wake(self):
        if self.job is None:
            self.job = self.root.after(FRAME_MS, self._frame)

    def _frame(self):
        self.job = None
        if not self.motion():  # turned off mid-animation: end states now
            self.cancel()
            self.on_frame()
            return
        now = self.clock()
        for key, (v0, v1, t0, ms, ease, mode) in list(self.tweens.items()):
            f = (now - t0) * 1000 / ms
            if mode == 'spin':
                self.values[key] = (v0 + 360 * f) % 360
            elif f >= 1:
                del self.tweens[key]
                self.values[key] = v1 % 360 if mode == 'settle' else v1
            else:
                self.values[key] = v0 + (v1 - v0) * ease(f)
        if self.tweens:
            self._wake()
        self.on_frame()

    def cancel(self):
        """Jump every tween to its end (window closed)."""
        for key, t in self.tweens.items():
            self.values[key] = 0.0 if t[5] else t[1]
        self.tweens.clear()
        if self.job is not None:
            self.root.after_cancel(self.job)
            self.job = None
