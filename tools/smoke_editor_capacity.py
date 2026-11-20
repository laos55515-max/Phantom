#!/usr/bin/env python3
"""Drive the payload editor and the capacity panel through real use, then exit.

The GUI test suite pumps Tk with ``root.update()`` from script level, and on macOS
Aqua under Tcl/Tk 9.1 that call segfaults inside Tk's own drawing.  A harness cannot
be the evidence for an editor fix in that situation, because the harness itself is
the second victim.  This tool therefore does what the application does — it reacts to
``after`` callbacks and lets Tk paint when Tk paints, with no ``update()`` anywhere.

What it really does, with a real keyboard and the real clipboard:

* pastes a small, a medium and a 260 000-character block through ``<Command-v>`` /
  ``<Control-v>`` and checks that every byte arrived in the widget *and* the model;
* selects everything with ``<Command-a>`` / ``<Control-a>`` and checks the selection
  the widget reports — not a call to the handler;
* pastes over a selection;
* clears the field with the button and checks that only the field was cleared;
* loads a real PNG carrier, watches the capacity panel fill in, types text far over
  the capacity, and checks that the embed button is blocked and the text survives;
* swaps the carrier and checks the text is still there;
* closes the window the way the close button does, and lets the loop end itself.

    python -u tools/smoke_editor_capacity.py              # assertions: exit 1 on a defect
    python -u tools/smoke_editor_capacity.py --diagnose   # the same run, exit 0

A segfault prints nothing after the last step and the shell reports 139, the same
signature as the crash the earlier tools were written for.
"""

from __future__ import annotations

import argparse
import os
import random
import string
import sys
import tempfile
import time
import tkinter as tk
import traceback
from collections.abc import Callable

SMALL = 120
MEDIUM = 6_000
LARGE = 260_000
#: Tk lays out a Text line for display; a 260 000-character *single line* under word
#: wrap is the pathological case for that, and it is measured here rather than
#: asserted, because the cost is Tk's own and grows faster than the payload.
ONE_LINE = 120_000

MOD = "Command" if sys.platform == "darwin" else "Control"


def _text(size: int, *, alphabet: str | None = None, seed: int = 7) -> str:
    """A payload of an exact size; incompressible words when ``alphabet`` is given."""
    if alphabet is None:
        unit = "Phantom payload 0123456789 — строка текста. "
        return (unit * (size // len(unit) + 1))[:size]
    rng = random.Random(seed)
    return "".join(rng.choices(alphabet, k=size))


def _wrapped(size: int) -> str:
    """The same size, broken into lines the way a document really is."""
    line = "Phantom payload 0123456789 — строка текста, обычная строка документа. "
    body = (line * 4 + "\n") * (size // (len(line) * 4 + 1) + 1)
    return body[:size]


def _clipboard_diff(payload: str, back: str) -> str:
    """Where the clipboard handed something different back — and what it was."""
    if payload == back:
        return "identical"
    shared = min(len(payload), len(back))
    where = next((index for index in range(shared) if payload[index] != back[index]), shared)
    return (
        f"first difference at {where}: payload {payload[where : where + 14]!r} vs "
        f"clipboard {back[where : where + 14]!r}; clipboard tail {back[-14:]!r}; "
        f"{len(back) - len(payload)} characters more"
    )


def _carrier(directory: str, name: str, size: tuple[int, int]) -> str:
    from PIL import Image

    pixels = Image.effect_noise((size[0], size[1]), 32).convert("RGB")
    path = os.path.join(directory, name)
    pixels.save(path, format="PNG", compress_level=1)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnose", action="store_true", help="report, do not assert")
    parser.add_argument("--watchdog", type=float, default=120.0, help="seconds before giving up")
    parser.add_argument("--trace", action="store_true", help="print every step")
    args = parser.parse_args(argv)

    import customtkinter as ctk

    from phantom.core import constants as C
    from phantom.ui.app import PhantomApp

    started = time.monotonic()
    failures: list[str] = []
    checks = [0]
    state: dict[str, object] = {"done": False, "phase": "build"}
    temporary = tempfile.mkdtemp(prefix="phantom-editor-")

    def say(what: str) -> None:
        print(f"{time.monotonic() - started:6.2f}s  {what}", flush=True)

    def check(name: str, condition: bool, detail: str) -> bool:
        checks[0] += 1
        say(f"{'ok  ' if condition else 'FAIL'} {name}: {detail}")
        if not condition:
            failures.append(f"{name}: {detail}")
        return condition

    root = ctk.CTk()
    app = PhantomApp(root)
    inner = app.embed_text._textbox if hasattr(app.embed_text, "_textbox") else app.embed_text
    say(f"built: tk {tk.TkVersion}, tcl {tk.TclVersion}, editor={type(inner).__name__}")
    say(f"bindings: {len(inner.bind())} on the editor, modifier {MOD}")

    # -- helpers ---------------------------------------------------------
    def editor_text() -> str:
        try:
            return inner.get("1.0", "end-1c")
        except tk.TclError:  # pragma: no cover - only after destroy
            return ""

    def selection() -> str:
        try:
            return inner.get("sel.first", "sel.last")
        except tk.TclError:
            return ""

    def clip_put(text: str) -> None:
        root.clipboard_clear()
        root.clipboard_append(text)

    def key(sequence: str) -> None:
        inner.focus_force()
        inner.event_generate(sequence)

    def settle(condition: Callable[[], bool], then: Callable[[], None], tries: int = 0) -> None:
        """Wait on Tk's own loop — never with ``update()``."""
        if condition() or tries > 400:
            if tries > 400:
                check("settle", False, f"{state['phase']}: the loop never settled")
            then()
            return
        root.after(25, settle, condition, then, tries + 1)

    def idle() -> Callable[[], bool]:
        return lambda: not app.worker.busy and not app._busy

    def after_settle(then: Callable[[], None]) -> None:
        settle(idle(), then)

    # -- the script ------------------------------------------------------
    def phase_paste(name: str, payload: str, *, budget: float | None = None) -> None:
        state["phase"] = f"paste {name}"
        clip_put(payload)
        handed_back = root.clipboard_get()
        check(
            f"clipboard round-trip {name}",
            handed_back == payload,
            f"put {len(payload)} chars, got {len(handed_back)} back — "
            + _clipboard_diff(payload, handed_back),
        )
        before = len(editor_text())
        started_at = time.monotonic()
        key(f"<{MOD}-v>")
        spent = time.monotonic() - started_at
        say(f"     insert of {len(payload)} chars took {spent * 1000:.0f} ms")
        if budget is not None:
            check(
                f"paste {name} finishes in time",
                spent < budget,
                f"{spent:.1f} s for {len(payload)} chars (budget {budget:.0f} s)",
            )

        def verify() -> None:
            got = editor_text()
            # What the app owes the user is that the *whole clipboard* arrives: the
            # pasteboard itself may hand back a converted string, which is Tk's
            # business and is reported above, not silently folded into this check.
            check(
                f"paste {name} ({len(payload)} chars)",
                len(got) == before + len(handed_back) and got.endswith(handed_back),
                f"editor {len(got)} chars (expected {before + len(handed_back)}), "
                f"{spent * 1000:.0f} ms",
            )
            if len(payload) > C.EDITOR_WRAP_LINE_LIMIT:
                inner_widget = (
                    app.embed_text._textbox
                    if hasattr(app.embed_text, "_textbox")
                    else app.embed_text
                )
                wrapping = str(inner_widget.cget("wrap"))
                one_line = max(len(line) for line in payload.split("\n"))
                check(
                    f"wrap after {name}",
                    (wrapping == "none") == (one_line > C.EDITOR_WRAP_LINE_LIMIT),
                    f"wrap is {wrapping!r}, longest line {one_line} chars",
                )
            next_phase()

        root.after(120, verify)

    def phase_clipboard_types() -> None:
        state["phase"] = "clipboard types"
        samples = (
            ("ascii", "abcdefghij" * 400),
            ("cyrillic", "їїїґ українська" * 400),
            ("emoji", "👩‍👩‍👧‍👦🇺🇦" * 200),
            ("dashes", "a — b – c" * 500),
            ("code", ("def f(x):\n    return x + 1  # comment\n" * 200)),
        )
        for name, payload in samples:
            clip_put(payload)
            back = root.clipboard_get()
            check(
                f"clipboard keeps {name} text",
                back == payload,
                f"put {len(payload)} chars, got {len(back)} — "
                + _clipboard_diff(payload, back),
            )
        next_phase()

    def phase_paste_over_selection() -> None:
        state["phase"] = "paste over selection"
        inner.tag_remove("sel", "1.0", "end")
        inner.tag_add("sel", "1.0", "end")
        clip_put("XYZ")
        key(f"<{MOD}-v>")

        def verify() -> None:
            got = editor_text()
            check("paste over a selection", got == "XYZ", f"editor holds {got[:20]!r}")
            next_phase()

        root.after(120, verify)

    def phase_select_all() -> None:
        state["phase"] = "select all"
        whole = editor_text()
        key(f"<{MOD}-a>")

        def verify() -> None:
            selected = selection()
            check(
                "select all by key",
                selected == whole and bool(whole),
                f"selected {len(selected)} of {len(whole)} chars",
            )
            next_phase()

        root.after(120, verify)

    def phase_clear_field() -> None:
        state["phase"] = "clear field"
        app.embed_vm.set_password("keep-me-please")
        before_carrier = app.embed_vm.carrier_path
        button = getattr(app, "clear_field_button", None)
        if button is None:
            check("clear field button", False, "the window has no clear-field button")
            next_phase()
            return
        button.invoke()

        def verify() -> None:
            check(
                "clear field empties the field",
                editor_text() == "" and app.embed_vm.text == "",
                f"editor {len(editor_text())} chars, model {len(app.embed_vm.text)}",
            )
            check(
                "clear field keeps the password",
                app.embed_vm.password == "keep-me-please",
                "password untouched",
            )
            check(
                "clear field keeps the carrier",
                app.embed_vm.carrier_path == before_carrier,
                f"carrier {app.embed_vm.carrier_path!r}",
            )
            next_phase()

        root.after(120, verify)

    def phase_choose_carrier() -> None:
        state["phase"] = "choose carrier"
        path = _carrier(temporary, "carrier.png", (640, 360))
        state["carrier"] = path
        state["carrier_size"] = os.path.getsize(path)
        app._set_carrier(path)
        clip_put(_text(MEDIUM))

        def loaded() -> None:
            app.clear_field_button.invoke()
            app.embed_text.insert("1.0", _text(MEDIUM))
            app._on_text_changed()

            def estimated() -> bool:
                estimate = app.embed_vm.estimate
                return estimate is not None and app._applied_revision == app._estimate_revision

            settle(estimated, verify)

        def verify() -> None:
            report = app.capacity_report
            panel = app.capacity_panel.cget("text")
            say(f"panel: {panel!r}")
            check("capacity: carrier probed", app.embed_vm.carrier is not None, "probe landed")
            check(
                "capacity: panel names the container",
                "carrier.png" in panel and "640×360" in panel,
                panel.splitlines()[0] if panel else "empty panel",
            )
            check(
                "capacity: panel numbers agree",
                report is not None
                and report.prepared_bytes is not None
                and report.capacity_bytes == app.embed_vm.capacity,
                f"prepared {None if report is None else report.prepared_bytes} "
                f"of {None if report is None else report.capacity_bytes}",
            )
            check(
                "capacity: a small payload fits",
                report is not None and report.can_embed,
                f"state {None if report is None else report.state}",
            )
            check(
                "capacity: the note explains the units",
                app.capacity_note.cget("text") == app.i18n.t("cap_note"),
                "note present",
            )
            next_phase()

        settle(lambda: app.embed_vm.carrier is not None, loaded)

    def phase_over_capacity() -> None:
        state["phase"] = "over capacity"
        capacity = app.embed_vm.capacity
        # Random letters do not compress away: this really cannot fit.
        payload = _text(capacity * 4, alphabet=string.ascii_letters + " ")
        app.clear_field_button.invoke()
        app.embed_text.insert("1.0", payload)
        app._on_text_changed()

        def fitted() -> bool:
            estimate = app.embed_vm.estimate
            return (
                estimate is not None
                and not app.worker.busy
                and not app._busy
                and app._applied_revision == app._estimate_revision
            )

        def verify() -> None:
            report = app.capacity_report
            panel = app.capacity_panel.cget("text")
            say(f"panel: {panel!r}")
            check(
                "over capacity: the text is kept",
                len(editor_text()) == len(payload) and app.embed_vm.text == payload,
                f"editor {len(editor_text())} chars of {len(payload)}",
            )
            check(
                "over capacity: the panel reports the excess",
                report is not None and report.excess_bytes > 0,
                f"excess {None if report is None else report.excess_bytes} bytes",
            )
            check(
                "over capacity: the fill bar stays capped",
                report is not None and 0.0 <= report.fill <= 1.0,
                f"fill {None if report is None else report.fill}",
            )
            check(
                "over capacity: embedding is blocked",
                str(app.embed_button.cget("state")) == "disabled",
                f"button {app.embed_button.cget('state')}",
            )
            next_phase()

        settle(fitted, verify)

    def phase_swap_carrier() -> None:
        state["phase"] = "swap carrier"
        before = app.embed_vm.text
        second = _carrier(temporary, "carrier-small.png", (320, 240))
        app._set_carrier(second)

        def verify() -> None:
            check(
                "swap carrier: the text survives",
                app.embed_vm.text == before and editor_text() == before,
                f"editor {len(editor_text())} chars",
            )
            report = app.capacity_report
            check(
                "swap carrier: the estimate is re-measured",
                report is not None and report.capacity_bytes == app.embed_vm.capacity,
                f"capacity now {None if report is None else report.capacity_bytes}",
            )
            next_phase()

        settle(lambda: app.embed_vm.carrier is not None and not app.worker.busy, verify)

    def phase_empty_state() -> None:
        state["phase"] = "empty field"
        app.clear_field_button.invoke()

        def verify() -> None:
            report = app.capacity_report
            check(
                "empty field: the panel has no numbers to show",
                report is not None and report.prepared_bytes is None,
                f"state {None if report is None else report.state}",
            )
            check(
                "empty field: the counters are zero",
                "0" in app.char_label.cget("text"),
                app.char_label.cget("text"),
            )
            finish()

        root.after(150, verify)

    phases: list[Callable[[], None]] = [
        lambda: phase_paste("small", _text(SMALL)),
        lambda: phase_paste("medium", _text(MEDIUM)),
        lambda: phase_paste("large with line breaks", _wrapped(LARGE), budget=20.0),
        lambda: phase_paste("one 120 000-char line", _text(ONE_LINE), budget=60.0),
        phase_clipboard_types,
        phase_select_all,
        phase_paste_over_selection,
        phase_clear_field,
        phase_choose_carrier,
        phase_over_capacity,
        phase_swap_carrier,
        phase_empty_state,
    ]
    queue = list(phases)

    def next_phase() -> None:
        if not queue:
            finish()
            return
        action = queue.pop(0)
        if args.trace:
            say(f"phase: {getattr(action, '__name__', action)}")
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - a step must not hide the next one
            check(getattr(action, "__name__", "phase"), False, f"raised {exc!r}")
            traceback.print_exc()
            next_phase()

    def finish() -> None:
        state["done"] = True
        say(f"SUMMARY checks={checks[0]} failures={len(failures)}")
        for failure in failures:
            say(f"PROBLEM {failure}")
        say("main loop: closing the window the way the close button does")
        try:
            app._on_close()
        except Exception as exc:  # noqa: BLE001 - closing is best effort in a tool
            say(f"close raised {exc!r}")
        root.after(250, root.destroy)

    def watchdog() -> None:
        if state["done"]:
            return
        say(f"WATCHDOG: stuck in phase {state['phase']!r}")
        root.quit()

    root.after(int(args.watchdog * 1000), watchdog)
    root.after(500, next_phase)

    try:
        root.mainloop()
    except tk.TclError:  # pragma: no cover - only on a broken build
        traceback.print_exc()
        return 1
    say("main loop finished cleanly")
    if args.diagnose:
        return 0
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
