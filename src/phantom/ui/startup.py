"""Start-up measurement, so "it feels slow" becomes numbers (``PHANTOM_STARTUP_DEBUG=1``).

The window used to freeze for seconds before the first frame.  Guessing which
line caused it is how regressions come back, so every step of the launch is
timed here: the module import, the Tcl/Tk root, each part of the window, the
first layout pass and the moment the window is actually on screen.

Two properties matter more than the formatting:

* disabled, it costs one :func:`time.perf_counter` per stage and nothing else —
  no strings are built, nothing is logged;
* the clock is injectable, so the arithmetic is tested on a headless machine
  instead of on the maintainer's laptop.

Usage::

    PHANTOM_STARTUP_DEBUG=1 .venv/bin/python app.py

prints one line per stage and a summary when the window is ready::

    startup: stage=build:tabs:metadata ms=96.1 (+96.1)
    ...
    startup: total=1180 ms first_frame=940 ms ready=1175 ms
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Final

LOGGER: Final = logging.getLogger("phantom.startup")

#: Set to ``1``/``true``/``yes``/``on`` to print per-stage timings to stderr.
ENV: Final = "PHANTOM_STARTUP_DEBUG"
TRUTHY: Final = frozenset({"1", "true", "yes", "on"})


def debug_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``PHANTOM_STARTUP_DEBUG`` asks for timings."""
    source = os.environ if environ is None else environ
    return str(source.get(ENV, "")).strip().lower() in TRUTHY


class StartupTrace:
    """Collects named milestones of one launch.

    Durations are measured against the moment the trace was created (the very
    first line of :func:`phantom.ui.app.main`), so ``total`` includes the
    interpreter work the process did before the window appeared.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        clock: Callable[[], float] = time.perf_counter,
        sink: Callable[[str], None] | None = None,
    ) -> None:
        self.enabled = enabled
        self._clock = clock
        self._sink = sink if sink is not None else LOGGER.info
        self._origin = clock()
        self._last = self._origin
        #: ``(name, milliseconds since origin, milliseconds since previous mark)``
        self.marks: list[tuple[str, float, float]] = []

    # -- recording ------------------------------------------------------
    def ms_since_start(self) -> float:
        return (self._clock() - self._origin) * 1000.0

    def mark(self, name: str) -> float:
        """Record ``name``; returns milliseconds since the trace was created."""
        now = self._clock()
        since_start = (now - self._origin) * 1000.0
        delta = (now - self._last) * 1000.0
        self._last = now
        self.marks.append((name, since_start, delta))
        if self.enabled:
            self._sink(f"startup: stage={name} ms={since_start:.1f} (+{delta:.1f})")
        return since_start

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time a block: ``with trace.stage("build:header"): ...``."""
        try:
            yield
        finally:
            self.mark(name)

    # -- reading --------------------------------------------------------
    def at(self, name: str) -> float | None:
        """Milliseconds since the origin when ``name`` was first reached."""
        for mark_name, since_start, _delta in self.marks:
            if mark_name == name:
                return since_start
        return None

    def total(self) -> float:
        return self.ms_since_start()

    def summary(self) -> dict[str, float]:
        """The milestones the report talks about, in milliseconds."""
        return {
            "total": self.total(),
            "first_frame": self.at("first_frame") or 0.0,
            "ready": self.at("ready") or 0.0,
            "stages": float(len(self.marks)),
        }

    def log_summary(self) -> None:
        """One line the owner can paste into a bug report."""
        if not self.enabled:
            return
        values = self.summary()
        self._sink(
            "startup: total={total:.0f} ms first_frame={first_frame:.0f} ms "
            "ready={ready:.0f} ms stages={stages:.0f}".format(**values)
        )

    def slowest(self, limit: int = 5) -> list[tuple[str, float]]:
        """The ``limit`` slowest stages, biggest first (used by the debug dump)."""
        ordered = sorted(self.marks, key=lambda item: item[2], reverse=True)
        return [(name, delta) for name, _since, delta in ordered[:limit]]


def trace_from_env(environ: Mapping[str, str] | None = None) -> StartupTrace:
    """A trace that is only enabled when the environment asks for it."""
    return StartupTrace(enabled=debug_enabled(environ))
