#!/usr/bin/env python3
"""Diagnose the payload editor on a real display: which of the four defects bite.

Nothing here guesses.  The window is built, the editor is focused, and the key
events a user really presses are *generated* (``<Command-v>`` / ``<Control-v>``,
``<Command-a>`` / ``<Control-a>``) with the Tk clipboard really holding the text —
no direct ``insert()`` call stands in for a paste, and no handler is called in place
of a keystroke.  Every step then reports what the widget, the view model, the
counter and the status line actually say.

    python -u tools/smoke_editor_capacity.py --diagnose   # the matrix, exit 0
    python -u tools/smoke_editor_capacity.py             # the assertions, exit 1 on a defect

The functional mode is what CI gates on: it asserts that a small paste, a large
paste, select-all, the clear button and the capacity panel all do what the user was
promised.  ``--diagnose`` is the same run with the assertions turned into a report,
which is what a bug report needs.
"""

from __future__ import annotations

import argparse
import sys
import time
import tkinter as tk
import traceback

SIZES = {"small": 120, "medium": 6_000, "large": 260_000}

MOD = "Command" if sys.platform == "darwin" else "Control"


def _text(size: int) -> str:
    """A payload of a known size, deterministic and compressible-ish."""
    unit = "Phantom payload 0123456789 — строка текста. "
    body = unit * (size // len(unit) + 1)
    return body[:size]


class Report:
    """What was tried and what happened."""

    def __init__(self, diagnose: bool) -> None:
        self.diagnose = diagnose
        self.lines: list[str] = []
        self.failures: list[str] = []

    def say(self, what: str) -> None:
        print(what, flush=True)
        self.lines.append(what)

    def check(self, name: str, condition: bool, detail: str) -> None:
        mark = "ok  " if condition else "FAIL"
        self.say(f"{mark} {name}: {detail}")
        if not condition:
            self.failures.append(f"{name}: {detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnose", action="store_true", help="report instead of assert")
    parser.add_argument("--watchdog", type=float, default=60.0, help="seconds before giving up")
    args = parser.parse_args(argv)

    import customtkinter as ctk

    from phantom.ui.app import PhantomApp

    started = time.monotonic()
    report = Report(args.diagnose)
    state: dict[str, object] = {"done": False}

    def say(what: str) -> None:
        report.say(f"{time.monotonic() - started:6.2f}s  {what}")

    root = ctk.CTk()
    app = PhantomApp(root)
    inner = app.embed_text._textbox if hasattr(app.embed_text, "_textbox") else app.embed_text

    say(f"built: tk {tk.TkVersion}, tcl {tk.TclVersion}, editor={type(inner).__name__}")
    say(f"debug: bindings on the editor = {sorted(inner.bind())}")
    say(f"debug: bindtags = {inner.bindtags()}")
    say(f"debug: platform modifier = {MOD}")

    def clipboard_put(text: str) -> None:
        root.clipboard_clear()
        root.clipboard_append(text)

    def editor_text() -> str:
        try:
            return inner.get("1.0", "end-1c")
        except tk.TclError:
            return ""

    def selection() -> str:
        try:
            return inner.get("sel.first", "sel.last")
        except tk.TclError:
            return ""

    def key(sequence: str) -> None:
        """A real keystroke, delivered through Tk's own dispatch."""
        inner.focus_force()
        inner.event_generate(sequence)

    def step_index(size: int) -> str:
        return _text(size)

    # -- the phases, each one its own callback --------------------------
    def phase_paste(name: str, size: int) -> None:
        payload = step_index(size)
        clipboard_put(payload)
        before = len(editor_text())
        key(f"<{MOD}-v>")

        def after() -> None:
            got = editor_text()
            expect = before + len(payload)
            report.check(
                f"paste {name} ({size} chars)",
                len(got) == expect,
                f"editor holds {len(got)} chars, expected {expect}; "
                f"view model {len(app.embed_vm.text)}; "
                f"status {app.status_label.cget('text')!r}",
            )
            next_phase()

        root.after(400, after)

    def phase_select_all() -> None:
        key(f"<{MOD}-a>")

        def after_event() -> None:
            selected = selection()
            whole = editor_text()
            report.check(
                "select all by key",
                selected == whole and bool(whole),
                f"selected {len(selected)} of {len(whole)} chars",
            )
            # The control: the same code path, called directly.  If the gesture is
            # what fails, this passes; if the code is what fails, both fail.
            app._text_shortcuts[0].actions.select_all()
            report.check(
                "select all by code",
                selection() == whole and bool(whole),
                f"selected {len(selection())} of {len(whole)} chars",
            )
            next_phase()

        root.after(300, after_event)

    def phase_type_replaces_selection() -> None:
        root.clipboard_clear()
        inner.delete("1.0", "end")
        inner.insert("1.0", "abcdef")
        inner.tag_add("sel", "1.0", "1.3")
        clipboard_put("XYZ")
        key(f"<{MOD}-v>")
        root.after(300, after_paste_over_selection)

    def after_paste_over_selection() -> None:
        got = editor_text()
        report.check(
            "paste over a selection",
            got == "XYZdef",
            f"editor holds {got!r}",
        )
        next_phase()

    def phase_clear_button() -> None:
        root.clipboard_clear()
        inner.delete("1.0", "end")
        inner.insert("1.0", "text that must survive nothing")
        app.embed_vm.set_text(editor_text())
        app.embed_vm.set_password("keep-me-please")
        before_carrier = app.embed_vm.carrier_path
        button = getattr(app, "clear_field_button", None)
        if button is None:
            report.check("clear field button", False, "the window has no clear-field button")
            next_phase()
            return
        button.invoke()

        def after() -> None:
            report.check(
                "clear field",
                editor_text() == "" and app.embed_vm.text == "",
                f"editor {len(editor_text())} chars, model {len(app.embed_vm.text)} chars",
            )
            report.check(
                "clear field keeps the password",
                app.embed_vm.password == "keep-me-please",
                f"password now {app.embed_vm.password!r}",
            )
            report.check(
                "clear field keeps the carrier",
                app.embed_vm.carrier_path == before_carrier,
                f"carrier now {app.embed_vm.carrier_path!r}",
            )
            next_phase()

        root.after(300, after)

    def phase_capacity() -> None:
        panel = getattr(app, "capacity_summary", None)
        if panel is None:
            report.check("capacity panel", False, "the window has no capacity summary")
            next_phase()
            return
        report.check(
            "capacity panel speaks",
            bool(panel.cget("text")),
            f"summary = {panel.cget('text')!r}",
        )
        next_phase()

    phases = [
        lambda: phase_paste("small", SIZES["small"]),
        lambda: phase_paste("medium", SIZES["medium"]),
        lambda: phase_paste("large", SIZES["large"]),
        phase_select_all,
        phase_type_replaces_selection,
        phase_clear_button,
        phase_capacity,
    ]
    queue = list(phases)

    def next_phase() -> None:
        if not queue:
            finish()
            return
        action = queue.pop(0)
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - a step must not hide the next
            report.check("phase", False, f"{action} raised {exc!r}")
            next_phase()

    def finish() -> None:
        state["done"] = True
        say(f"heartbeat: event loop alive, {len(report.lines)} checks")
        say(f"SUMMARY checks={len(report.lines)} failures={len(report.failures)}")
        for failure in report.failures:
            say(f"PROBLEM {failure}")
        app._on_close()
        root.after(150, root.destroy)

    def watchdog() -> None:
        if state["done"]:
            return
        say("WATCHDOG: the editor run did not finish")
        root.quit()

    root.after(int(args.watchdog * 1000), watchdog)
    root.after(600, next_phase)

    try:
        root.mainloop()
    except tk.TclError:  # pragma: no cover - only on a broken build
        traceback.print_exc()
        return 1
    say("main loop finished cleanly")
    if args.diagnose:
        return 0
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
