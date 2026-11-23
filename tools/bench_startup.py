"""Measure what it costs to put the PHANTOM window on screen.

The number that matters is not "how long does a function take" but "how long
until the window is there and usable", so this builds the whole window three
times, from an empty Tk root, and reports the median.

It counts two things that used to be paid once per widget:

* ``families`` — calls to ``tkinter.font.families``, i.e. how often the system
  font list is read;
* ``fonts`` — ``CTkFont`` objects created, each of which asks Tk for metric
  information.

Run it on the machine that feels slow:

    python tools/bench_startup.py
    PHANTOM_STARTUP_DEBUG=1 python tools/bench_startup.py --stages

It needs a display: directly on macOS or Windows, and under ``xvfb-run`` on a
Linux runner.  Every line starts with ``STARTUP:`` so a CI step can find it.
"""

from __future__ import annotations

import os
import statistics
import sys
import time
import tkinter.font as tkfont
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
try:
    # An explicit PYTHONPATH wins: that is how the same script measures another
    # checkout (this branch against its base) without being edited.
    import phantom  # noqa: F401
except ImportError:
    sys.path.insert(0, str(ROOT / "src"))

RUNS = 3


class Counters:
    """How often the font list was read and how many font objects were built."""

    def __init__(self) -> None:
        self.families = 0
        self.fonts = 0

    def reset(self) -> None:
        self.families = 0
        self.fonts = 0

    def install(self) -> Any:
        """Patch Tk's font list and ``CTkFont`` once per process."""
        if getattr(Counters, "_installed", False):
            return None
        counters = self

        real_families = tkfont.families

        def families(*args: Any, **kwargs: Any) -> Any:
            counters.families += 1
            return real_families(*args, **kwargs)

        tkfont.families = families  # type: ignore[assignment]

        import customtkinter as ctk

        real_init = ctk.CTkFont.__init__

        def init(instance: Any, *args: Any, **kwargs: Any) -> None:
            counters.fonts += 1
            real_init(instance, *args, **kwargs)

        ctk.CTkFont.__init__ = init  # type: ignore[method-assign]
        Counters._installed = True
        return ctk

    _installed: bool = False


def measure(ctk: Any, counters: Counters, *, stages: bool) -> dict[str, float]:
    """Build one full window and report what it cost."""
    from phantom.ui.app import PhantomApp

    counters.reset()
    root = ctk.CTk()
    root.deiconify()  # the window must be viewable before any display pass
    try:
        started = time.perf_counter()
        app = PhantomApp(root)
        # The window's own _flush_layout knows about Tk 9.1 on Aqua: a direct
        # ``update_idletasks()`` here segfaults on that platform (the owner's
        # MacBook and the macOS CI runner both reproduce it).  Use the safe pass
        # instead.
        app._flush_layout()
        build_ms = (time.perf_counter() - started) * 1000.0

        painted = time.perf_counter()
        root.update()
        frame_ms = (time.perf_counter() - painted) * 1000.0

        trace = getattr(app, "trace", None)
        if stages and trace is not None:
            for name, at, delta in trace.marks:
                print(f"STARTUP: stage={name} at={at:.1f}ms (+{delta:.1f}ms)", flush=True)
            print(f"STARTUP: summary={trace.summary()}", flush=True)
    finally:
        root.destroy()

    return {
        "build_ms": round(build_ms, 1),
        "first_frame_ms": round(frame_ms, 1),
        "families": float(counters.families),
        "fonts": float(counters.fonts),
    }


def profile(ctk: Any, counters: Counters, *, limit: int = 8) -> None:
    """Where the build time actually goes, as ``PROFILE:`` lines."""
    import cProfile

    profiler = cProfile.Profile()
    profiler.enable()
    try:
        measure(ctk, counters, stages=False)
    finally:
        profiler.disable()

    entries = []
    for entry in profiler.getstats():
        # ``entry.code`` is a code object (a (file, line, name) tuple in some
        # builds), so ask for the parts instead of unpacking it.
        code = entry.code
        filename = getattr(code, "co_filename", None) or code[0]
        lineno = getattr(code, "co_firstlineno", None) or code[1]
        name = getattr(code, "co_name", None) or code[2]
        entries.append((entry.totaltime, entry.inlinetime, entry.callcount, filename, lineno, name))
    entries.sort(key=lambda item: item[0], reverse=True)
    for cumulative, inline, calls, filename, lineno, name in entries[:limit]:
        where = f"{Path(filename).name}:{lineno}"
        print(
            f"PROFILE: cum={cumulative * 1000:8.1f}ms self={inline * 1000:8.1f}ms "
            f"calls={calls:<6} {name} @ {where}",
            flush=True,
        )


def main(argv: list[str]) -> int:
    stages = "--stages" in argv or os.environ.get("PHANTOM_STARTUP_DEBUG", "") not in ("", "0")
    wants_profile = "--profile" in argv
    counters = Counters()
    ctk = counters.install()
    import customtkinter as plain_ctk

    ctk = ctk or plain_ctk

    results: list[dict[str, float]] = []
    for index in range(RUNS):
        try:
            result = measure(ctk, counters, stages=stages and index == 0)
        except Exception as exc:  # noqa: BLE001 - a benchmark reports, never raises
            print(f"STARTUP: error={type(exc).__name__}: {exc}", flush=True)
            return 1
        results.append(result)
        print(
            "STARTUP: run={run} build_ms={build_ms} first_frame_ms={first_frame_ms} "
            "families={families:.0f} fonts={fonts:.0f}".format(run=index + 1, **result),
            flush=True,
        )

    median = {
        key: round(statistics.median(result[key] for result in results), 1)
        for key in results[0]
    }
    print(
        "STARTUP: median build_ms={build_ms} first_frame_ms={first_frame_ms} "
        "families={families:.0f} fonts={fonts:.0f}".format(**median),
        flush=True,
    )
    if wants_profile:
        # Last, so a bug here can never cost the numbers above.
        print("PROFILE: begin", flush=True)
        try:
            profile(ctk, counters)
        except Exception as exc:  # noqa: BLE001 - a benchmark reports, never raises
            print(f"PROFILE: error={type(exc).__name__}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
