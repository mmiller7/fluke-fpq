"""Small phase + heartbeat reporter for long offline parses.

`build-fpq` spends almost all of its time in the counter calibration/chain,
not in the byte walk: 65_ is about 62 s of 65 s, 67_ about 10 s of 11 s.
A byte-percentage bar would sit near 5 %, so this reporter exposes named
phases and a throttled "still working" tick instead.

Status is printed one line per update by default, matching the download
ticker: a completed step keeps its last elapsed time visible while the next
step starts.  `inplace=True` is retained for API callers that want a single
TTY line.  An optional `steps` tuple adds `Step n of N: ...` numbering.

The reporter is deliberately optional and silent by default.  Callers that
pass `progress=None` (guards, replay, the library) see no output and no
change in the `.fpq` bytes.  The CLI creates one on stderr.

Disclaimer: provided "as is", without warranty of any kind, express or
implied, including accuracy or fitness for a particular purpose. Use at your
own risk. Not affiliated with or endorsed by Fluke Corporation.
"""
from __future__ import annotations

import sys
import time


# The known build-fpq analysis steps, in output order.  A fallback may skip
# "solving counter gaps"; the total stays fixed so the user can see progress.
BUILD_FPQ_STEPS = (
    "de-framing pages",
    "scanning item stream",
    "trimming ring suffix",
    "solving the counter timeline",
    "calibrating counter minute phase",
    "calibrating counter hour phase",
    "checking counter phase candidates",
    "solving counter chain",
    "solving counter gaps",
    "assembling .fpq records",
)


class Progress:
    """Throttled phase/heartbeat printer.

    `phase(text)` starts a new named phase and prints it immediately.
    `tick()` prints an update only when `interval` seconds have elapsed since
    the last output.  `done()` ends the phase (or prints a final line).

    `time_fn` and `stream` are injectable so the guard can test throttling
    without sleeping or touching the real terminal.
    """

    def __init__(self, prefix: str = "", stream=None, enabled: bool = True,
                 interval: float = 5.0, time_fn=None, steps=None,
                 inplace: bool = False):
        self.prefix = prefix
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = bool(enabled)
        self.interval = max(0.1, float(interval))
        self.time_fn = time_fn or time.monotonic
        self.steps = tuple(steps or ())
        self.inplace = bool(inplace)
        try:
            self.tty = bool(self.stream.isatty())
        except (AttributeError, OSError):
            self.tty = False
        self._phase_text = ""
        self._phase_label = ""
        self._phase_start = 0.0
        self._last = 0.0
        self._line_len = 0
        self._closed = False
        self._step = 0

    # -- public API -----------------------------------------------------
    def phase(self, text: str) -> None:
        """Start a named phase; prints immediately."""
        if not self.enabled:
            return
        now = self.time_fn()
        self._clear_line()
        label = text
        if self.steps and text in self.steps:
            idx = self.steps.index(text) + 1
            if idx > self._step:
                self._step = idx
            label = f"Step {idx} of {len(self.steps)}: {text}"
        self._phase_text = text
        self._phase_label = self._label(label)
        self._phase_start = now
        self._last = now
        self._write(self._phase_label, newline=not self._inplace())

    def tick(self, text: str | None = None) -> None:
        """Heartbeat: at most one line per `interval` seconds."""
        if not self.enabled or self._closed:
            return
        now = self.time_fn()
        if now - self._last < self.interval:
            return
        self._last = now
        elapsed = max(0.0, now - self._phase_start)
        label = self._label(text) if text else self._phase_label
        self._write(f"{label} … {elapsed:.0f} s", newline=not self._inplace())

    def done(self, text: str | None = None) -> None:
        """End the current phase (clear an in-place line; keep the last)."""
        if not self.enabled:
            return
        self._clear_line()
        if text:
            self._write(self._label(text), newline=True)
        self._closed = True

    # -- rendering ------------------------------------------------------
    def _inplace(self) -> bool:
        return self.inplace and self.tty

    def _label(self, text: str) -> str:
        return f"* {self.prefix}: {text}" if self.prefix else f"* {text}"

    def _clear_line(self) -> None:
        if self._inplace() and self._line_len:
            self.stream.write("\r" + " " * self._line_len + "\r")
            self.stream.flush()
        self._line_len = 0

    def _write(self, text: str, newline: bool) -> None:
        if not newline:
            pad = max(0, self._line_len - len(text))
            self.stream.write("\r" + text + " " * pad)
            self.stream.flush()
            self._line_len = len(text)
        else:
            self.stream.write(text + "\n")
            self.stream.flush()
            self._line_len = 0
