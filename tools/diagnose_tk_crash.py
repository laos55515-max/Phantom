#!/usr/bin/env python3
"""Find which program makes Tk die on this machine, one variant per process.

The Tk 9.1 job on macOS has twice reported ``exit 139`` — SIGSEGV — for a run that
printed "main loop finished cleanly".  A crash that happens *after* a clean finish
has no failing test and no traceback, so guessing from the log is how it stayed
unexplained: the only honest way to name a cause is to run one candidate at a time
in its own process and look at the exit code.

Each variant runs in a child process, so a SIGSEGV costs that child and the table
still prints.  A child prints ``child: <name> done`` as its last statement, which
tells the parent whether the process died *while working* or *while exiting* — the
second one is the signature the smokes showed.

Variants (``kind`` decides whether a crash is a failure for ``--gate``):

``product``  code the application really runs — a crash here is PHANTOM's problem.
``harness``  the pump the pytest suite uses (``root.update()`` from script level);
             measured upstream breakage on Aqua + Tk 9, kept for the record.
``probe``    a bare Tk/CustomTkinter program, to see whether Tk itself dies.

Usage::

    PYTHONPATH=src python -u tools/diagnose_tk_crash.py            # the table
    PYTHONPATH=src python -u tools/diagnose_tk_crash.py --gate     # + exit 1 on a product crash
    PYTHONPATH=src python -u tools/diagnose_tk_crash.py --only app_dnd,app_plain

Exit codes: 0 every variant behaved as documented, 1 a product variant crashed
(with ``--gate``), 3 neither tkinter nor a display is usable here.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, Final

TOOLS = pathlib.Path(__file__).resolve().parent
REPO = TOOLS.parent
SRC = REPO / "src"

#: name -> (kind, one line for the table)
VARIANTS: Final[dict[str, tuple[str, str]]] = {
    "tk_plain": ("probe", "tkinter.Tk(): create, withdraw, destroy"),
    "tk_update": ("harness", "tkinter.Tk() + 20 x root.update()"),
    "ctk_plain": ("probe", "customtkinter.CTk() + 20 x root.update()"),
    "ctk_tabs": ("probe", "CTkTabview with four canvas-heavy tabs + 20 x update()"),
    "tkdnd_require": ("probe", "CTk root + TkinterDnD._require (package require tkdnd)"),
    "tkdnd_register": ("probe", "the above + drop_target_register on a Text"),
    "app_plain": ("product", "the real window with PHANTOM_DND=0, update(), close"),
    "app_dnd": ("product", "the real window with drag-and-drop loaded, update(), close"),
    "app_smoke": ("product", "the real window on Tk's own loop, closed by _on_close"),
    "harness_pump": ("harness", "the pytest fixture: CTk root + PhantomApp + 200 x update()"),
}

#: A product crash is a failure; a harness crash is reported, not gated.  The
#: distinction is documented in docs/macos-aqua-crash.md and must not be a place to
#: hide a defect: the harness variant only pumps a window the product variants built.
GATED_KINDS: Final = frozenset({"product"})


# --------------------------------------------------------------------------- children
def _build_tabs(root: Any) -> None:
    """A window with the shape that crashes: a tab view full of canvases."""
    import customtkinter as ctk

    tabs = ctk.CTkTabview(root, width=520, height=360)
    tabs.pack(fill="both", expand=True)
    for name in ("embed", "extract", "metadata", "settings"):
        page = tabs.add(name)
        scroll = ctk.CTkScrollableFrame(page, width=480, height=300)
        scroll.pack(fill="both", expand=True)
        for row in range(12):
            ctk.CTkLabel(scroll, text=f"{name} row {row}").pack(anchor="w")
            ctk.CTkEntry(scroll).pack(fill="x")
        ctk.CTkButton(page, text="action").pack(anchor="e")


def _make_dnd_root() -> Any:
    import customtkinter as ctk
    from tkinterdnd2 import TkinterDnD

    class DndRoot(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self) -> None:
            super().__init__()
            self.TkdndVersion = TkinterDnD._require(self)

    return DndRoot()


def _drop_target_command_present(root: Any) -> bool:
    try:
        return bool(root.tk.call("info", "commands", "tkdnd::drop_target"))
    except Exception:  # noqa: BLE001 - a broken interpreter is an answer too
        return False


def _child(name: str) -> int:
    """One variant, in this process.  The last print() is the "it got here" marker."""
    os.environ.setdefault("PHANTOM_LOG_LEVEL", "WARNING")

    if name == "tk_plain":
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.update_idletasks()
        root.destroy()
    elif name == "tk_update":
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        for _ in range(20):
            root.update()
        root.destroy()
    elif name == "ctk_plain":
        import customtkinter as ctk

        root = ctk.CTk()
        root.deiconify()
        for _ in range(20):
            root.update()
        root.destroy()
    elif name == "ctk_tabs":
        import customtkinter as ctk

        root = ctk.CTk()
        root.geometry("1024x720")
        root.deiconify()
        _build_tabs(root)
        for _ in range(20):
            root.update()
        root.destroy()
    elif name == "tkdnd_require":
        root = _make_dnd_root()
        root.deiconify()
        print(f"child: tkdnd={getattr(root, 'TkdndVersion', None)} "
              f"drop_target_command={_drop_target_command_present(root)}")
        for _ in range(10):
            root.update()
        root.destroy()
    elif name == "tkdnd_register":
        import customtkinter as ctk
        from tkinterdnd2 import DND_FILES

        root = _make_dnd_root()
        root.deiconify()
        text = ctk.CTkTextbox(root, width=400, height=200)
        text.pack()
        try:
            text.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
            print("child: drop_target_register succeeded")
        except Exception as exc:  # noqa: BLE001 - the answer this variant exists for
            print(f"child: drop_target_register raised {type(exc).__name__}: {exc}")
        for _ in range(10):
            root.update()
        root.destroy()
    elif name in {"app_plain", "app_dnd"}:
        if name == "app_plain":
            os.environ["PHANTOM_DND"] = "0"
        import customtkinter as ctk

        from phantom.ui.app import PhantomApp, build_root

        ctk.set_default_color_theme("blue")
        root, provider = build_root()
        print(f"child: dnd available={provider.available} usable={provider.usable} "
              f"reason={provider.reason!r}")
        app = PhantomApp(root, dnd=provider)
        for _ in range(20):
            root.update()
        app._on_close()
    elif name == "app_smoke":
        import customtkinter as ctk

        from phantom.ui.app import PhantomApp, build_root

        ctk.set_default_color_theme("blue")
        root, provider = build_root()
        app = PhantomApp(root, dnd=provider)
        # The smokes close from a callback and let the loop end by itself: no
        # update() anywhere, which is how the application really runs.
        root.after(800, app._on_close)
        root.mainloop()
    elif name == "harness_pump":
        import customtkinter as ctk

        from phantom.ui.app import PhantomApp
        from phantom.ui.dnd import DndProvider
        from phantom.ui.settings import SettingsStore

        ctk.set_default_color_theme("blue")
        root = ctk.CTk()
        root.deiconify()
        app = PhantomApp(root, settings_store=SettingsStore(), dnd=DndProvider())
        # 200 pumps: the suite does this in every test, dozens of times per session.
        for _ in range(200):
            root.update()
        with contextlib.suppress(Exception):  # shutting down is not the subject here
            app.worker.shutdown(timeout=2)
        root.destroy()
    else:  # pragma: no cover - the parent only asks for known names
        print(f"child: unknown variant {name}")
        return 2

    print(f"child: {name} done")
    return 0


# ---------------------------------------------------------------------------- parent
def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    paths = [str(SRC), str(TOOLS)]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env.setdefault("PHANTOM_LOG_LEVEL", "WARNING")
    return env


def _exit_text(code: int) -> str:
    if code < 0:
        return f"signal {-code} (exit {128 - code})"
    return f"exit {code}"


def run_variant(name: str, timeout: float) -> dict[str, Any]:
    """One child run: its exit code, whether it got to the end, and its last lines."""
    started = time.monotonic()
    command = [
        sys.executable,
        "-X",
        "faulthandler",
        "-u",
        str(pathlib.Path(__file__)),
        "--child",
        name,
    ]
    try:
        done = subprocess.run(
            command,
            check=False,
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        code = done.returncode
        output = (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired as exc:
        code = 124
        raw_out = exc.stdout or ""
        raw_err = exc.stderr or ""
        if isinstance(raw_out, bytes):
            raw_out = raw_out.decode(errors="replace")
        if isinstance(raw_err, bytes):
            raw_err = raw_err.decode(errors="replace")
        output = raw_out + raw_err
    finished = f"child: {name} done" in output
    elapsed = time.monotonic() - started
    verdict = "clean"
    if code != 0:
        # "finished" separates the two shapes this exists to tell apart.
        verdict = "crash-at-exit" if finished else "crash-during-work"
        if code == 124:
            verdict = "hang"
    return {
        "variant": name,
        "kind": VARIANTS[name][0],
        "exit": code,
        "exit_text": _exit_text(code),
        "finished": finished,
        "verdict": verdict,
        "seconds": round(elapsed, 2),
        "output": output,
    }


def _newest_reports_since(stamp: float, limit: int = 3) -> list[pathlib.Path]:
    try:
        reports = sorted(
            (path for path in pathlib.Path.home().glob("Library/Logs/DiagnosticReports/*.ips")),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return []
    return [path for path in reports if path.stat().st_mtime >= stamp][-limit:]


def _print_crash_reports(stamp: float) -> list[dict[str, Any]]:
    """Parse any crash report the variants produced: this is where the cause is."""
    sys.path.insert(0, str(TOOLS))
    try:
        from parse_crash_report import summarise
    except Exception as exc:  # noqa: BLE001 - the table still matters without it
        print(f"crash reports: parser unavailable ({exc})")
        return []
    summaries: list[dict[str, Any]] = []
    for report in _newest_reports_since(stamp):
        try:
            from parse_crash_report import _load

            summary = summarise(_load(report))
        except Exception as exc:  # noqa: BLE001 - a corrupt report is not a failure
            print(f"crash report {report.name}: unreadable ({exc})")
            continue
        summary["path"] = str(report)
        summaries.append(summary)
        head = summary["frames"][:3] or ["(no frames)"]
        print(f"crash report {report.name}: {summary['signal']} at {' | '.join(head)}")
    return summaries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", help="internal: run one variant in this process")
    parser.add_argument("--only", default="", help="comma-separated variant names")
    parser.add_argument("--timeout", type=float, default=180.0, help="per child, seconds")
    parser.add_argument("--gate", action="store_true", help="exit 1 when a product variant crashes")
    parser.add_argument("--json", metavar="PATH", help="write the table here")
    args = parser.parse_args(argv)

    if args.child:
        return _child(args.child)

    names = [name for name in args.only.split(",") if name] or list(VARIANTS)
    unknown = [name for name in names if name not in VARIANTS]
    if unknown:
        print(f"DIAG: unknown variants {unknown}")
        return 3
    if not (SRC / "phantom").is_dir():
        print(f"DIAG: no source tree at {SRC}")
        return 3
    try:
        import tkinter  # noqa: F401 - the question this tool answers
    except Exception as exc:  # noqa: BLE001 - no tkinter is an answer, not a crash
        print(f"DIAG: tkinter is not usable here: {exc}")
        return 3

    stamp = time.time()
    results = [run_variant(name, args.timeout) for name in names]
    for result in results:
        note = VARIANTS[result["variant"]][1]
        print(
            f"DIAG: variant={result['variant']} kind={result['kind']} {result['exit_text']} "
            f"finished={result['finished']} verdict={result['verdict']} "
            f"{result['seconds']}s — {note}"
        )
    crashes = [result for result in results if result["verdict"].startswith("crash")]
    reports = _print_crash_reports(stamp) if crashes else []
    gated = [
        result
        for result in crashes
        if result["kind"] in GATED_KINDS
    ]
    summary = {
        "variants": [
            {key: value for key, value in result.items() if key != "output"}
            for result in results
        ],
        "crashes": [result["variant"] for result in crashes],
        "gated_crashes": [result["variant"] for result in gated],
        "crash_reports": reports,
    }
    print(
        f"DIAG: {len(results)} variants, {len(crashes)} crashed"
        f" ({', '.join(result['variant'] for result in crashes) or 'none'}),"
        f" {len(gated)} of them product code"
    )
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.gate and gated:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
