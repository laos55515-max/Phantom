#!/usr/bin/env python3
"""Drive the real window through a theme switch on Tk's own loop, then exit.

The GUI test suite pumps Tk with ``root.update()`` from script level, and on macOS
Aqua under Tcl/Tk 9.1 that call segfaults inside Tk's drawing on its own — with the
theme tests removed, with every PHANTOM suppression off, on the runner and on the
owner's MacBook alike.  A test harness cannot be the evidence for a fix in that
situation, because the harness itself is the second victim.

The application does not do that.  It never pumps Tk from script level: it reacts to
events and lets Tk paint when Tk paints.  This tool exercises exactly that path, with
no ``update()`` anywhere:

* build the real ``PhantomApp`` window;
* from ``after`` callbacks: switch the theme Dark → Light → System, resize the
  window, switch a tab, switch the theme again;
* close the window the way the close button does, and let the loop end itself.

    python tools/smoke_tk91_theme.py            # exit 0 or the process dies
    python tools/smoke_tk91_theme.py --trace    # every step, with timestamps

A segfault prints nothing after the last step and the shell reports 139, which is
the same signature as the bug this window was fixed for.  Nothing here is imported
by the application or by the test suite.
"""

from __future__ import annotations

import argparse
import sys
import time
import tkinter as tk
import traceback

#: Sizes to walk through, one per callback.  All of them fit the smallest display a
#: CI runner offers (~1024x720): a request the window manager clamps away produces no
#: <Configure>, so a series of impossible sizes would measure nothing at all.
SIZES = [(980, 700), (1024, 720), (1000, 710), (980, 720), (1024, 700), (1000, 720)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", action="store_true", help="print every step")
    parser.add_argument(
        "--steps", type=int, default=2, help="how many full theme rounds to run (default 2)"
    )
    parser.add_argument(
        "--resizes",
        type=int,
        default=8,
        help="how many sequential resize steps to run, one per callback (default 8)",
    )
    parser.add_argument(
        "--resize-gap",
        type=int,
        default=130,
        help="milliseconds between two resize steps, so frames happen in between",
    )
    args = parser.parse_args(argv)

    started = time.monotonic()

    def say(what: str) -> None:
        mark = f"{time.monotonic() - started:6.2f}s"
        print(f"{mark}  {what}", flush=True)

    import customtkinter as ctk

    from phantom.ui.app import PhantomApp

    root = ctk.CTk()
    root.title("phantom: Tk 9 theme smoke")
    app = PhantomApp(root)
    say(f"built: tk {tk.TkVersion}, tcl {tk.TclVersion}, {len(root.winfo_children())} children")

    labels = {mode: label for label, mode in app._theme_by_label.items()}
    order = ["Dark", "Light", "System"]

    def click(mode: str) -> None:
        app._on_theme_click(labels[mode])
        say(f"theme -> {mode} (mode now {ctk.get_appearance_mode()})")
        if app.settings.theme != mode:
            raise AssertionError(f"the window did not take the theme {mode}")

    # A heartbeat: the loop should serve it every 10 ms, so the longest gap is the
    # freeze a user would feel, and the tick count between two steps is proof the
    # loop really ran in between instead of serving one long callback.
    beats = [0]
    last_beat = [time.monotonic()]
    worst_gap = [0.0]
    problems: list[str] = []
    #: What the window took, step by step: a clamped request must not be read as a
    #: layout that failed to happen.
    state: dict[str, int | None] = {"width": None, "asked": None}

    def beat() -> None:
        now = time.monotonic()
        worst_gap[0] = max(worst_gap[0], now - last_beat[0])
        last_beat[0] = now
        beats[0] += 1
        root.after(10, beat)

    root.after(10, beat)

    def resize() -> None:
        root.geometry("1000x760")
        say(f"resized to {root.geometry()}")

    def resize_series(index: int = 0) -> None:
        """One size per callback, with the loop free in between: the real thing.

        Asking for several sizes inside one callback measures the callback, not the
        window: the window manager never sees the intermediate geometries and Tk
        never paints them.  Each step here waits for the next callback, records the
        size the window actually took, and counts the heartbeats that were served
        while it settled — that count is the intermediate frames.
        """
        if index >= args.resizes:
            say(f"resize series done: {index} steps, worst loop gap {worst_gap[0] * 1000:.1f} ms")
            close()
            root.after(200, root.destroy)
            return
        # Sizes the window can really take: this display is small, and a request the
        # window manager clamps away produces no <Configure> at all, which would make
        # the whole series a measurement of nothing.
        width = SIZES[index % len(SIZES)][0]
        height = SIZES[index % len(SIZES)][1]
        started = beats[0]
        reflows = sum(row.reflows for row in app.responsive.flows())
        last_got = state["width"]
        state["asked"] = width
        root.geometry(f"{width}x{height}")

        def settle() -> None:
            served = beats[0] - started
            got = (root.winfo_width(), root.winfo_height())
            clamped = "" if (got[0], got[1]) == (width, height) else " (clamped by the WM)"
            say(
                f"resize {index}: asked {width}x{height}, got {got[0]}x{got[1]}{clamped}, "
                f"frames served between = {served}"
            )
            if served < 5:
                problems.append(f"resize {index}: the loop served only {served} beats")
            passes = sum(row.reflows for row in app.responsive.flows()) - reflows
            moved = last_got is None or got[0] != last_got
            state["width"] = got[0]
            if moved and passes < 1:
                problems.append(
                    f"resize {index}: the width changed and the visible tab was not "
                    f"laid out at all ({passes} passes)"
                )
            if passes > 2 * len(app.responsive.flows()):
                problems.append(
                    f"resize {index}: {passes} passes for one size change "
                    f"({len(app.responsive.flows())} rows)"
                )
            resize_series(index + 1)

        root.after(args.resize_gap, settle)

    def tab() -> None:
        labels_ = list(app._tab_by_label)
        if not labels_:
            say("tab switch skipped: no tab table on this build")
            return
        app._on_tab_click(labels_[-1])
        say(f"tab -> {app.current_tab}")

    def close() -> None:
        say("closing")
        app._on_close()

    # Every step is an ``after`` callback: Tk enters this from its own event loop,
    # which is the situation the application runs in.
    delay = 150
    for _ in range(args.steps):
        for mode in order:
            root.after(delay, click, mode)
            delay += 150
        root.after(delay, resize)
        delay += 150
        root.after(delay, tab)
        delay += 150

    def start_resizes() -> None:
        # The heartbeat is only meaningful over the resize series: the build and the
        # theme rounds are not what this measures.
        last_beat[0] = time.monotonic()
        worst_gap[0] = 0.0
        beats[0] = 0
        resize_series(0)

    root.after(delay, start_resizes)

    try:
        root.mainloop()
    except tk.TclError:  # pragma: no cover - only on a broken build
        traceback.print_exc()
        return 1
    say(f"heartbeats served: {beats[0]}, worst loop gap {worst_gap[0] * 1000:.1f} ms")
    if problems:
        for problem in problems:
            say(f"PROBLEM {problem}")
        return 1
    say("main loop finished cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
