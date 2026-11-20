#!/usr/bin/env python3
"""Measure what one window resize actually costs, on a real Aqua session.

Nothing here is guessed: every number comes from wrapped calls inside the real
window, on the real platform, while the window is being resized in steps with
frames in between (each step is its own ``after`` callback, so Tk lays out and
paints between them — a burst in one callback measures something else).

    python -u tools/profile_resize.py --label after
    python -u tools/profile_resize.py --label before --steps 16 --gap 120

Counted, per resize series:

* ``flows_walks``     — how many times the widget tree was walked to find flow rows
* ``reflow``          — how many reflow passes ran, and how many changed the layout
* ``grid``/``forget`` — the Tk calls the reflow made to place its children
* ``resize``          — toplevel ``<Configure>`` handlers that did work
* ``handler_ms``      — wall time inside those handlers
* ``pass_ms``         — wall time inside the whole refit pass
* ``worst_gap_ms``    — the longest gap between two 10 ms heartbeats: how long the
  event loop was unable to serve anything else (this is what a microfreeze is)

The last line is a machine-readable ``SUMMARY`` so CI can diff two runs.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
import tkinter as tk
import traceback
from typing import Any

#: Widths the series walks through, all above the window's 980 px minimum and
#: below the runner's screen, both directions, with a fullscreen round at the end.
WIDTHS = [1120, 1060, 1000, 1040, 1100, 1180, 1240, 1180, 1120, 1060, 1000, 1140]
HEIGHTS = [780, 800, 820, 800, 780, 760, 780, 800, 820, 800, 780, 900]


class Numbers:
    """Everything the wrapped calls add up."""

    def __init__(self) -> None:
        self.flows_walks = 0
        self.reflow = 0
        self.layout_changes = 0
        self.grid = 0
        self.grid_forget = 0
        self.resize = 0
        self.apply = 0
        self.seconds_resize = 0.0
        self.seconds_pass = 0.0
        self.worst_gap = 0.0
        self.rows = 0
        self.fullscreen = "skipped"


def _wrap(
    numbers: Numbers,
    owner: type,
    name: str,
    before: str | None = None,
    after: str | None = None,
) -> None:
    """Count one method call, and optionally time it or record a change."""

    original = getattr(owner, name)

    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if before is not None:
            setattr(numbers, before, getattr(numbers, before) + 1)
        started = time.perf_counter()
        try:
            result = original(self, *args, **kwargs)
        finally:
            elapsed = time.perf_counter() - started
            if name == "refit_flows":
                numbers.seconds_pass += elapsed
            elif name in ("_on_window_resize", "reflow"):
                numbers.seconds_resize += elapsed
        if after is not None:
            setattr(numbers, after, getattr(numbers, after) + 1)
        return result

    setattr(owner, name, wrapper)


def instrument(numbers: Numbers) -> None:
    """Wrap the four places that can do work on a resize, plus Tk's own grid."""
    from phantom.ui.app import PhantomApp
    from phantom.ui.responsive import FlowRow, Responsive

    _wrap(numbers, Responsive, "flows", before="flows_walks")
    _wrap(numbers, Responsive, "refit_flows")
    _wrap(numbers, Responsive, "_apply", before="apply")
    _wrap(numbers, PhantomApp, "_on_window_resize", before="resize")

    original_reflow = FlowRow.reflow

    def reflow(self: Any, *args: Any, **kwargs: Any) -> Any:
        numbers.reflow += 1
        started = time.perf_counter()
        try:
            result = original_reflow(self, *args, **kwargs)
        finally:
            numbers.seconds_resize += time.perf_counter() - started
        return result

    FlowRow.reflow = reflow  # type: ignore[method-assign]

    # ``grid`` lives on the ``Grid`` mixin every widget inherits, not on Misc.
    original_grid = tk.Grid.grid
    original_forget = tk.Grid.grid_forget

    def grid(self: Any, *args: Any, **kwargs: Any) -> Any:
        numbers.grid += 1
        return original_grid(self, *args, **kwargs)

    def grid_forget(self: Any, *args: Any, **kwargs: Any) -> Any:
        numbers.grid_forget += 1
        return original_forget(self, *args, **kwargs)

    tk.Grid.grid = grid  # type: ignore[method-assign]
    tk.Grid.grid_forget = grid_forget  # type: ignore[method-assign]


def run(label: str, steps: int, gap: int, watchdog: float = 45.0) -> int:
    import customtkinter as ctk

    from phantom.ui.app import PhantomApp

    numbers = Numbers()
    root = ctk.CTk()
    app = PhantomApp(root)
    instrument(numbers)
    rows = len(app.responsive.flows())
    say = lambda what: print(what, flush=True)  # noqa: E731

    say(f"built: tk {tk.TkVersion}, tcl {tk.TclVersion}, flow rows {rows}")

    # A heartbeat: the loop should serve it every 10 ms, so the longest gap is the
    # freeze a user would feel.  Scheduled before anything is measured.
    last = [time.perf_counter()]
    worst = [0.0]

    def beat() -> None:
        now = time.perf_counter()
        worst[0] = max(worst[0], now - last[0])
        last[0] = now
        root.after(10, beat)

    root.after(10, beat)

    marks: list[tuple[str, dict[str, float]]] = []
    finished = [False]

    # A native window and a generated event stream can always surprise a tool like
    # this; a run that does not end by itself must still end, with its numbers.
    def watchdog_fire() -> None:
        if finished[0]:
            return
        say(f"WATCHDOG: {watchdog:.0f}s elapsed, destroying the window")
        with contextlib.suppress(tk.TclError):
            root.destroy()

    root.after(int(watchdog * 1000), watchdog_fire)

    def snapshot(name: str) -> None:
        marks.append(
            (
                name,
                {
                    "walks": numbers.flows_walks,
                    "reflow": numbers.reflow,
                    "changes": numbers.layout_changes,
                    "grid": numbers.grid,
                    "forget": numbers.grid_forget,
                    "wrap": numbers.apply,
                    "handlers": numbers.resize,
                    "handler_ms": numbers.seconds_resize * 1000,
                    "pass_ms": numbers.seconds_pass * 1000,
                },
            )
        )

    def series(index: int) -> None:
        """One geometry change per callback, so Tk lays out between them."""
        if index >= steps:
            net_snapshot("geometry")
            burst()
            return
        root.geometry(f"{WIDTHS[index % len(WIDTHS)]}x{HEIGHTS[index % len(HEIGHTS)]}")
        root.after(gap, series, index + 1)

    def burst() -> None:
        """The handler path on its own: 24 Configure events, no waiting between.

        The runner's display clamps a real window to its screen, so the WM cannot
        be asked for the wide sizes a big monitor would produce; a generated event
        carries whatever width it is given, which measures the handler honestly.
        """
        for index in range(24):
            width = 980 + (index % 8) * 90
            try:
                root.event_generate("<Configure>", width=width, height=800)
            except tk.TclError as exc:  # pragma: no cover - platform dependent
                say(f"event_generate failed: {exc}")
                break
        root.after(gap, lambda: (net_snapshot("burst"), fullscreen()))

    def net_snapshot(name: str) -> None:
        snapshot(name)
        name_, values = marks[-1]
        say(
            f"phase {name_}: walks={values['walks']:.0f} reflow={values['reflow']:.0f} "
            f"changes={values['changes']:.0f} grid={values['grid']:.0f} "
            f"forget={values['forget']:.0f} wrap={values['wrap']:.0f} "
            f"handlers={values['handlers']:.0f} "
            f"handler_ms={values['handler_ms']:.1f} pass_ms={values['pass_ms']:.1f}",
        )

    def fullscreen() -> None:
        """The transition the owner feels most: in and out of the native mode."""
        try:
            root.attributes("-fullscreen", True)
        except tk.TclError as exc:
            numbers.fullscreen = f"unavailable: {exc}"
            finish()
            return
        numbers.fullscreen = "applied"

        def leave() -> None:
            try:
                root.attributes("-fullscreen", False)
            except tk.TclError as exc:  # pragma: no cover - platform dependent
                numbers.fullscreen = f"unavailable: {exc}"
            root.after(gap, finish)

        root.after(gap * 3, leave)

    def finish() -> None:
        finished[0] = True
        net_snapshot("fullscreen")
        say("")
        say(f"--- {label}")
        say(f"flow rows                {rows}")
        say(f"tree walks (entries)     {numbers.flows_walks}")
        say(f"reflow passes            {numbers.reflow}")
        say(f"layout changes           {numbers.layout_changes}")
        say(f"grid() calls             {numbers.grid}")
        say(f"grid_forget() calls      {numbers.grid_forget}")
        say(f"wrap re-applications     {numbers.apply}")
        say(f"toplevel resize handlers {numbers.resize}")
        say(f"time in handlers         {numbers.seconds_resize * 1000:.1f} ms")
        say(f"time in refit passes     {numbers.seconds_pass * 1000:.1f} ms")
        say(f"worst event-loop gap     {worst[0] * 1000:.1f} ms")
        say(f"fullscreen               {numbers.fullscreen}")
        say(
            f"SUMMARY label={label} steps={steps} rows={rows} "
            f"walks={numbers.flows_walks} reflow={numbers.reflow} "
            f"changes={numbers.layout_changes} grid={numbers.grid} "
            f"forget={numbers.grid_forget} wrap={numbers.apply} "
            f"handlers={numbers.resize} handler_ms={numbers.seconds_resize * 1000:.1f} "
            f"pass_ms={numbers.seconds_pass * 1000:.1f} "
            f"worst_gap_ms={worst[0] * 1000:.1f} "
            f"fullscreen={numbers.fullscreen}",
            flush=True,
        )
        for name, values in marks:
            say(
                f"PHASE label={label} phase={name} walks={values['walks']:.0f} "
                f"reflow={values['reflow']:.0f} changes={values['changes']:.0f} "
                f"grid={values['grid']:.0f} forget={values['forget']:.0f} "
                f"wrap={values['wrap']:.0f} handlers={values['handlers']:.0f} "
                f"handler_ms={values['handler_ms']:.1f} pass_ms={values['pass_ms']:.1f}",
                flush=True,
            )
        app._on_close()
        root.after(120, root.destroy)

    # The build is not the subject: start counting once the window has settled.
    def begin() -> None:
        numbers.__init__()  # type: ignore[misc]
        worst[0] = 0.0
        last[0] = time.perf_counter()
        series(0)

    root.after(500, begin)
    try:
        root.mainloop()
    except tk.TclError:  # pragma: no cover - only on a broken build
        traceback.print_exc()
        return 1
    say("main loop finished cleanly")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="run", help="name for this run in the summary")
    parser.add_argument("--steps", type=int, default=len(WIDTHS), help="resize steps")
    parser.add_argument("--gap", type=int, default=90, help="milliseconds between steps")
    parser.add_argument(
        "--watchdog",
        type=float,
        default=45.0,
        help="seconds after which the window is destroyed and the run gives up",
    )
    args = parser.parse_args(argv)
    return run(args.label, args.steps, args.gap, args.watchdog)


if __name__ == "__main__":
    sys.exit(main())
