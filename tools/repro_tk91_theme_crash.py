#!/usr/bin/env python3
"""Reproduce the macOS Tk 9.1 crash on a theme switch, without PHANTOM.

No cryptography, no metadata, no PHANTOM import: one window with enough
CustomTkinter widgets to register a few hundred appearance-mode callbacks, and a
button whose command calls ``customtkinter.set_appearance_mode``.  That is the
whole of ``test_theme_switch_persists`` as far as the native crash is concerned.

    python tools/repro_tk91_theme_crash.py                # the bug
    python tools/repro_tk91_theme_crash.py --quiet        # the fix
    python tools/repro_tk91_theme_crash.py --hidden       # a withdrawn window
    python tools/repro_tk91_theme_crash.py --direct       # not from a callback
    python tools/repro_tk91_theme_crash.py --toggles 10
    python tools/repro_tk91_theme_crash.py --update-between   # pump between clicks

On an affected build (macOS ARM64, Tcl/Tk 9.1, Aqua) the default mode dies with
``Fatal Python error: Segmentation fault`` inside ``update_idletasks``, and the
shell reports exit code 139.  ``--quiet`` refuses those display passes for the
duration of the mode change — the fix in ``phantom.ui.widgets.quiet_redraws`` —
and the same window switches mode as many times as asked.

The tool prints a line per toggle and exits 0; a crash prints nothing after the
last toggle and the exit code says 139.  Compare the four modes on the machine
that fails: that is what tells a callback-only crash from a window-state crash.
"""

from __future__ import annotations

import argparse
import platform
import sys
import tkinter as tk
from typing import Any

import customtkinter as ctk

#: One window this size registers a few hundred appearance-mode callbacks, which is
#: the scale of the real window (it is a few hundred widgets in four tabs).
ROWS = 24


def build_window(rows: int) -> tuple[ctk.CTk, list[object]]:
    """A window with enough CTk widgets for the tracker to have work to do."""
    root = ctk.CTk()
    root.title("repro: theme switch")

    frame = ctk.CTkScrollableFrame(root, width=520, height=420)
    frame.pack(fill="both", expand=True, padx=12, pady=12)

    widgets: list[object] = []
    for index in range(rows):
        row = ctk.CTkFrame(frame)
        row.pack(fill="x", padx=4, pady=2)
        label = ctk.CTkLabel(row, text=f"row {index}: a caption in the card")
        label.pack(side="left", padx=6)
        widget = ctk.CTkButton(row, text="button", width=90)
        widget.pack(side="right", padx=6)
        entry = ctk.CTkEntry(row, width=140)
        entry.pack(side="right", padx=6)
        widgets.extend((row, label, widget, entry))

    for text in ("embed", "extract", "metadata", "settings"):
        tab = ctk.CTkButton(root, text=text, width=110)
        tab.pack(side="left", padx=4)
        widgets.append(tab)

    return root, widgets


def no_flush(*_args: object, **_kwargs: object) -> None:
    """The refusal: a display pass that is asked for but not run."""


def callback_count() -> int:
    """How many widgets CustomTkinter will notify for one mode change."""
    from customtkinter.windows.widgets.appearance_mode import AppearanceModeTracker

    return len(AppearanceModeTracker.callback_list)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="refuse the display passes")
    parser.add_argument("--hidden", action="store_true", help="withdraw the window first")
    parser.add_argument("--direct", action="store_true", help="call from the top level")
    parser.add_argument("--toggles", type=int, default=4, help="how many mode changes")
    parser.add_argument(
        "--update-between",
        action="store_true",
        help="pump the event loop after each change, the way a test does",
    )
    parser.add_argument("--rows", type=int, default=ROWS, help="how many card rows")
    parser.add_argument("--delay", type=int, default=400, help="ms before the first click")
    args = parser.parse_args(argv)

    print(
        f"python {platform.python_version()} on {platform.machine()} "
        f"{platform.mac_ver()[0]} · Tk {tk.TkVersion} "
        f"(patchlevel {tk.TclVersion}) · customtkinter {ctk.__version__}"
    )
    print(f"mode: {'quiet' if args.quiet else 'default'}"
          f"{' · hidden' if args.hidden else ''}"
          f"{' · direct' if args.direct else ''}")

    root, widgets = build_window(args.rows)
    print(f"widgets: {len(widgets)} · appearance-mode callbacks: {callback_count()}")

    state = {"dark": False, "done": 0}

    def toggle() -> None:
        state["dark"] = not state["dark"]
        mode = "Dark" if state["dark"] else "Light"
        if args.quiet:
            misc: Any = tk.Misc
            canvas: Any = tk.Canvas
            saved = (misc.update_idletasks, canvas.update_idletasks)
            misc.update_idletasks = no_flush
            canvas.update_idletasks = no_flush
            try:
                ctk.set_appearance_mode(mode)
            finally:
                misc.update_idletasks = saved[0]
                canvas.update_idletasks = saved[1]
        else:
            ctk.set_appearance_mode(mode)
        state["done"] += 1
        print(f"  toggle {state['done']}/{args.toggles}: {mode} applied")
        if args.update_between:
            root.update()
        if state["done"] >= args.toggles:
            root.after(50, root.destroy)
            return
        root.after(args.delay, toggle)

    if args.hidden:
        root.withdraw()
    button = ctk.CTkButton(root, text="switch theme", command=toggle)
    button.pack(side="bottom", pady=8)
    root.after(args.delay, toggle if args.direct else button.invoke)

    try:
        root.mainloop()
    except tk.TclError as exc:  # a destroyed interpreter is a normal exit path
        print(f"mainloop ended: {exc}")
    print(f"survived {state['done']} of {args.toggles} mode changes")
    return 0 if state["done"] >= args.toggles else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
