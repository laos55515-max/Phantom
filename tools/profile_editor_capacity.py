#!/usr/bin/env python3
"""Measure the editor and the result pane at realistic payload sizes.

The complaint behind PHANTOM 2.1.0 was not "the editor is slow in a benchmark",
it was "the window freezes and then says ERROR" with a multi-megabyte payload.  A
number that answers that has to be taken where the user feels it: on the GUI
thread, in a real Tk window, with the real system clipboard.

This tool builds the real window, drives it through ``Text`` calls the way the
action bar does, and prints one row per payload size and shape with:

* ``insert`` / ``get`` / ``delete`` — the Tk round trips, in milliseconds;
* ``select all`` — the tag work a Cmd+A costs, including the paint-triggering
  ``tag_raise`` and ``see``;
* ``measure`` — :func:`phantom.core.text.measure_text` with and without the line
  scan (the version that no longer runs per keystroke);
* ``clipboard`` — write + read of the *system* clipboard, plus whether the
  pasteboard handed the string back unchanged (on Aqua it sometimes does not);
* ``undo``/``redo`` — the widget history, which is why it is bounded;
* ``gap`` — the longest interval in which the Tk event loop did not run at all
  while the phase above was executing: the freeze the user actually sees;
* ``rss`` — peak resident memory of the process so far, in MiB.

Nothing is truncated and nothing is written to disk.  No user data is logged: the
payloads are generated here.

    python -u tools/profile_editor_capacity.py                 # all sizes, all shapes
    python -u tools/profile_editor_capacity.py --sizes 102400,1048576
    python -u tools/profile_editor_capacity.py --json report.json
    python -u tools/profile_editor_capacity.py --clipboard-read  # include the
                                                               # read-back of 15 MiB

The last option is off by default: reading 15 MiB back from the Aqua pasteboard is
itself a multi-second operation in Tk, and its cost is Tk's, not the editor's.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import resource
import sys
import time
import tkinter as tk
import traceback
from collections.abc import Callable
from typing import Any, Final

KIB: Final = 1024
DEFAULT_SIZES: Final[tuple[int, ...]] = (
    100 * KIB,
    1 << 20,
    5 << 20,
    10 << 20,
    15 << 20,
)

#: Mixed content: Cyrillic, an emoji family sequence, combining marks and CJK, so a
#: size in bytes and a count in characters are genuinely different numbers.
MIXED_UNIT: Final = (
    "Phantom 2.1 — рядок текста. Привіт, світе! 漢字かな 👩‍👩‍👧‍👦 e\u0301 a\u0308 — "
)


def _multiline(size: int) -> str:
    line = MIXED_UNIT * 2 + "\n"
    return (line * (size // len(line.encode()) + 1))[: size // 2]


def _one_line(size: int) -> str:
    body = MIXED_UNIT * (size // len(MIXED_UNIT.encode()) + 1)
    return body[: size // 2]


def _mixed(size: int) -> str:
    unit = MIXED_UNIT + "\n"
    return (unit * (size // max(1, len(unit.encode())) + 1))[: size // 2]


SHAPES: Final[dict[str, Callable[[int], str]]] = {
    "multiline": _multiline,
    "one long line": _one_line,
    "mixed unicode": _mixed,
}


def _rss_mib() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux and the BSDs report KiB.
    if sys.platform == "darwin":
        return peak / (1024 * 1024)
    return peak / 1024


class Ticker:
    """Longest interval in which the Tk loop did not run.

    An ``after(1)`` callback is re-armed from itself; while a phase blocks the GUI
    thread, the callback cannot run, and the gap between two ticks is exactly how
    long the window was frozen.  That number is the one a user feels.
    """

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self.token: str | None = None
        self.last = time.monotonic()
        self.max_gap = 0.0
        self.running = False

    def start(self) -> None:
        self.running = True
        self.max_gap = 0.0
        self.last = time.monotonic()
        self._tick()

    def stop(self) -> float:
        self.running = False
        if self.token is not None:
            with contextlib.suppress(Exception):  # the window may be gone
                self.root.after_cancel(self.token)
            self.token = None
        return self.max_gap

    def _tick(self) -> None:
        if not self.running:
            return
        now = time.monotonic()
        self.max_gap = max(self.max_gap, now - self.last)
        self.last = now
        self.token = self.root.after(1, self._tick)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        default=",".join(str(size) for size in DEFAULT_SIZES),
        help="comma-separated byte sizes",
    )
    parser.add_argument(
        "--shapes", default=",".join(SHAPES), help=f"comma-separated: {', '.join(SHAPES)}"
    )
    parser.add_argument("--json", default="", help="write the numbers to this file")
    parser.add_argument(
        "--clipboard-read",
        action="store_true",
        help="read the clipboard back (slow for very large strings in Tk)",
    )
    parser.add_argument("--watchdog", type=float, default=900.0)
    args = parser.parse_args(argv)

    sizes = [int(part) for part in args.sizes.split(",") if part.strip()]
    shape_names = [part.strip() for part in args.shapes.split(",") if part.strip()]
    unknown = [name for name in shape_names if name not in SHAPES]
    if unknown:
        parser.error(f"unknown shape(s): {', '.join(unknown)}")

    import customtkinter as ctk

    from phantom.core import constants as C
    from phantom.core.text import measure_text, utf8_size
    from phantom.ui import clipboard as clip
    from phantom.ui.app import PhantomApp

    started = time.monotonic()
    rows: list[dict[str, Any]] = []

    def say(what: str) -> None:
        print(f"{time.monotonic() - started:7.2f}s  {what}", flush=True)

    root = ctk.CTk()
    app = PhantomApp(root)
    inner = app._editor_inner
    say(
        f"tk {tk.TkVersion} tcl {tk.TclVersion} on {sys.platform}; "
        f"editor {type(inner).__name__}, wrap limit {C.EDITOR_WRAP_LINE_LIMIT}"
    )

    ticker = Ticker(root)
    state: dict[str, Any] = {"done": False, "phase": "build"}

    def timed(action: Callable[[], Any]) -> float:
        """Run ``action`` on this thread and return its wall time in milliseconds."""
        started_at = time.perf_counter()
        action()
        return (time.perf_counter() - started_at) * 1000.0

    def measure_case(shape: str, size: int) -> None:
        state["phase"] = f"{shape} {size}"
        payload = SHAPES[shape](size)
        row: dict[str, Any] = {
            "shape": shape,
            "bytes_requested": size,
            "chars": len(payload),
            "utf8_bytes": utf8_size(payload),
            "lines": payload.count("\n") + 1,
            "longest_line": max(len(line) for line in payload.split("\n")),
        }
        # -- Tk round trips ------------------------------------------------
        app._suppress_events = True
        try:
            app.embed_text.delete("1.0", "end")
            row["insert_ms"] = timed(lambda: app.embed_text.insert("1.0", payload))
            row["get_ms"] = timed(lambda: inner.get("1.0", "end-1c"))
            row["select_all_ms"] = timed(lambda: clip.select_all(inner))
            row["undo_ms"] = timed(lambda: clip.undo(inner))
            row["redo_ms"] = timed(lambda: clip.redo(inner))
            # -- the counting that used to run per keystroke ----------------
            row["count_old_ms"] = timed(lambda: (len(payload), utf8_size(payload)))
            row["measure_ms"] = timed(lambda: measure_text(payload))
            row["measure_nolines_ms"] = timed(lambda: measure_text(payload, scan_lines=False))
            # -- clipboard --------------------------------------------------
            row["clip_write_ms"] = timed(
                lambda: (root.clipboard_clear(), root.clipboard_append(payload))
            )
            if args.clipboard_read:
                box: dict[str, Any] = {}

                def read_back() -> None:
                    try:
                        box["value"] = root.clipboard_get()
                    except Exception as exc:  # noqa: BLE001 - Tk owned it, or it is empty
                        box["error"] = str(exc)

                row["clip_read_ms"] = timed(read_back)
                value = box.get("value")
                row["clipboard_identical"] = value == payload
                if isinstance(value, str) and value != payload:
                    shared = min(len(value), len(payload))
                    row["clipboard_first_diff"] = next(
                        (index for index in range(shared) if value[index] != payload[index]),
                        shared,
                    )
            row["delete_ms"] = timed(lambda: app.embed_text.delete("1.0", "end"))
        finally:
            app._suppress_events = False
        row["gap_ms"] = ticker_max()
        row["rss_mib"] = round(_rss_mib(), 1)
        # The panel must be able to take the payload too: this is the commit path
        # the debouncer runs, and the state it leaves behind.
        app._suppress_events = True
        try:
            app.embed_text.insert("1.0", payload)
        finally:
            app._suppress_events = False
        row["commit_ms"] = timed(app._commit_editor)
        row["panel_chars"] = app._editor_metrics.chars if app._editor_metrics else 0
        row["wrap"] = str(inner.cget("wrap"))
        row["clear_field_ms"] = timed(app._clear_field)
        row["reset_all_ms"] = timed(app._clear_embed)
        row["rss_after_reset_mib"] = round(_rss_mib(), 1)
        rows.append(row)
        say(
            f"{shape:14s} {size / KIB:8.0f} KiB -> insert {row['insert_ms']:8.0f} ms, "
            f"get {row['get_ms']:8.0f} ms, gap {row['gap_ms']:6.0f} ms, "
            f"commit {row['commit_ms']:7.0f} ms"
        )

    def ticker_max() -> float:
        return round(ticker.stop(), 1)

    queue: list[tuple[str, int]] = [(shape, size) for size in sizes for shape in shape_names]

    def next_case() -> None:
        if not queue:
            finish()
            return
        shape, size = queue.pop(0)
        ticker.start()
        try:
            measure_case(shape, size)
        except Exception as exc:  # noqa: BLE001 - keep the remaining sizes running
            say(f"FAILED {shape} {size}: {exc!r}")
            traceback.print_exc()
            rows.append({"shape": shape, "bytes_requested": size, "error": repr(exc)})
        root.after(1, next_case)

    def finish() -> None:
        state["done"] = True
        print_table(rows)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as handle:
                json.dump(
                    {"tk": tk.TkVersion, "tcl": tk.TclVersion, "rows": rows}, handle, indent=2
                )
            say(f"wrote {args.json}")
        try:
            app._on_close()
        except Exception as exc:  # noqa: BLE001 - closing is best effort in a tool
            say(f"close raised {exc!r}")
        root.after(250, root.destroy)

    def watchdog() -> None:
        if state["done"]:
            return
        say(f"WATCHDOG: stuck in {state['phase']!r}")
        root.quit()

    root.after(int(args.watchdog * 1000), watchdog)
    root.after(400, next_case)
    try:
        root.mainloop()
    except tk.TclError:  # pragma: no cover - broken build
        traceback.print_exc()
        return 1
    say("main loop finished cleanly")
    return 0


def print_table(rows: list[dict[str, Any]]) -> None:
    """One markdown table, so the numbers can go straight into the changelog."""
    columns = [
        ("shape", "shape"),
        ("chars", "chars"),
        ("utf8_bytes", "UTF-8 B"),
        ("insert_ms", "insert"),
        ("get_ms", "get"),
        ("select_all_ms", "sel all"),
        ("clip_write_ms", "clip w"),
        ("undo_ms", "undo"),
        ("redo_ms", "redo"),
        ("count_old_ms", "count old"),
        ("measure_ms", "measure"),
        ("commit_ms", "commit"),
        ("clear_field_ms", "clear"),
        ("gap_ms", "max gap"),
        ("rss_mib", "peak MiB"),
    ]
    print("\n| " + " | ".join(label for _, label in columns) + " |")
    print("|" + "|".join("---" for _ in columns) + "|")
    for row in rows:
        if "error" in row:
            print(f"| {row['shape']} | failed: {row['error']} |")
            continue
        cells = []
        for key, _label in columns:
            value = row.get(key)
            if isinstance(value, float):
                cells.append(f"{value:.0f}")
            elif value is None:
                cells.append("—")
            else:
                cells.append(str(value))
        print("| " + " | ".join(cells) + " |")
    print()


if __name__ == "__main__":
    sys.exit(main())
