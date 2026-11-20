#!/usr/bin/env python3
"""Which CustomTkinter widget makes an appearance-mode change crash here?

The real window has a few hundred widgets of a dozen kinds; the plain repro does not
crash, so the trigger is narrower than "set_appearance_mode inside a callback".  This
tool builds one kind at a time, switches the mode, and prints before and after each
attempt with ``flush``: on the machine that segfaults, the last line printed names the
widget that was on screen when the process died.

    python -u tools/bisect_tk91_theme.py               # the crash
    python -u tools/bisect_tk91_theme.py --quiet       # with the refusal armed
    python -u tools/bisect_tk91_theme.py --repeat 3    # more entries per kind
    python -u tools/bisect_tk91_theme.py --only textbox,segmented

It has nothing to do with PHANTOM's core: no cryptography, no metadata, no import of
``phantom``.
"""

from __future__ import annotations

import argparse
import platform
import sys
from collections.abc import Callable
from typing import Any

import customtkinter as ctk


def _kinds() -> list[tuple[str, Callable[[Any], Any]]]:
    """Every CustomTkinter widget the window uses, one constructor each."""
    return [
        ("label", lambda master: ctk.CTkLabel(master, text="a caption")),
        ("button", lambda master: ctk.CTkButton(master, text="button", width=90)),
        ("entry", lambda master: ctk.CTkEntry(master, width=120)),
        ("textbox", lambda master: ctk.CTkTextbox(master, width=160, height=60)),
        ("frame", ctk.CTkFrame),
        (
            "segmented",
            lambda master: ctk.CTkSegmentedButton(master, values=["One", "Two"]),
        ),
        (
            "optionmenu",
            lambda master: ctk.CTkOptionMenu(master, values=["One", "Two"]),
        ),
        ("combobox", lambda master: ctk.CTkComboBox(master, values=["One", "Two"])),
        ("switch", lambda master: ctk.CTkSwitch(master, text="switch")),
        ("checkbox", lambda master: ctk.CTkCheckBox(master, text="check")),
        ("radiobutton", lambda master: ctk.CTkRadioButton(master, text="radio")),
        ("slider", lambda master: ctk.CTkSlider(master, from_=0, to=10)),
        ("progressbar", ctk.CTkProgressBar),
        ("scrollbar", lambda master: ctk.CTkScrollbar(master, orientation="vertical")),
        ("scrollableframe", lambda master: ctk.CTkScrollableFrame(master, height=80)),
        ("tabview", lambda master: ctk.CTkTabview(master, height=80)),
    ]


def _no_flush(*_args: object, **_kwargs: object) -> None:
    """The refusal: a display pass asked for and not run."""


def switch(quiet: bool) -> None:
    """Change the appearance mode once, with or without the display-pass refusal."""
    mode = "Dark" if ctk.get_appearance_mode() == "Light" else "Light"
    if not quiet:
        ctk.set_appearance_mode(mode)
        return
    import tkinter as tk

    misc: Any = tk.Misc
    canvas: Any = tk.Canvas
    saved = (misc.update_idletasks, canvas.update_idletasks)
    misc.update_idletasks = _no_flush
    canvas.update_idletasks = _no_flush
    try:
        ctk.set_appearance_mode(mode)
    finally:
        misc.update_idletasks = saved[0]
        canvas.update_idletasks = saved[1]


def run_kind(name: str, factory: Callable[[Any], Any], count: int, quiet: bool) -> None:
    root = ctk.CTk()
    root.title(f"bisect: {name}")
    root.geometry("700x500")
    row = ctk.CTkFrame(root)
    row.pack(fill="both", expand=True, padx=8, pady=8)
    built = []
    for _ in range(count):
        widget = factory(row)
        widget.pack(pady=1)
        built.append(widget)
    root.deiconify()
    root.update()

    print(f"  {name}: switching", flush=True)
    switch(quiet)
    root.update()
    print(f"  {name}: survived", flush=True)

    # The same window must also survive closing with the mode already changed.
    root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="refuse display passes")
    parser.add_argument("--repeat", type=int, default=6, help="widgets per kind")
    parser.add_argument("--only", default="", help="comma-separated kinds")
    args = parser.parse_args(argv)

    import tkinter

    print(
        f"python {platform.python_version()} on {platform.machine()} "
        f"{platform.mac_ver()[0]} · Tk {tkinter.TkVersion} "
        f"(patchlevel {tkinter.TclVersion}) · customtkinter {ctk.__version__}"
    )
    print(f"mode: {'quiet' if args.quiet else 'default'} · {args.repeat} per kind")

    wanted = {item.strip() for item in args.only.split(",") if item.strip()}
    for name, factory in _kinds():
        if wanted and name not in wanted:
            continue
        print(f"testing {name}", flush=True)
        run_kind(name, factory, args.repeat, args.quiet)

    print("every kind survived", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
