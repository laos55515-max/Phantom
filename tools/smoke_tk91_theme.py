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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", action="store_true", help="print every step")
    parser.add_argument(
        "--steps", type=int, default=2, help="how many full theme rounds to run (default 2)"
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

    def resize() -> None:
        root.geometry("1000x760")
        say(f"resized to {root.geometry()}")

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

    root.after(delay, close)
    root.after(delay + 200, root.destroy)

    try:
        root.mainloop()
    except tk.TclError:  # pragma: no cover - only on a broken build
        traceback.print_exc()
        return 1
    say("main loop finished cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
