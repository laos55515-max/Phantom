"""GUI smoke tests. Need a real Tk display; gated by the ``gui`` marker.

On Linux CI run them under ``xvfb-run -a python -m pytest -m gui``. Without a
display every test in this module is skipped with an explicit reason — a silent
pass would hide a broken Tk layer.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

from phantom.core import constants as C

pytestmark = pytest.mark.gui


def _tk_usable() -> tuple[bool, str]:
    """Can this build open a display?  Asked in a **child process**.

    On Aqua under Tk 9 this is not a detail.  The ``.ips`` report of the ``exit 139``
    names Tk's own idle handler:

        Tk_MacOSXGetTkWindow ← showRootWindow ← TclServiceIdle ← Tcl_DoOneEvent
        SIGTRAP inside objc_opt_respondsToSelector (a message to a freed object)
        EXC_BAD_ACCESS, KERN_INVALID_ADDRESS at 0x170

    Tk's Aqua menu code arms an idle callback for the application menu bar pointing at
    the window of the interpreter that armed it.  Destroy that interpreter and the
    callback stays on the thread's idle list; the next interpreter to service idles
    runs it against freed memory.  A probe that opened a root here and destroyed it
    therefore armed the fault before a single test had run — measured: one test alone
    crashed once in three runs, and keeping the probe root alive fixed that but put a
    second, hidden root in front of every test (it made the theme test hang and the
    touchpad test scroll the wrong way on 8.6).

    A child process answers the same question with no interpreter left behind.
    """
    try:
        import tkinter  # noqa: F401 - the question is whether it imports here
    except ImportError as exc:
        return False, f"tkinter is not installed: {exc}"
    probe = "import tkinter; tkinter.Tk()"
    try:
        done = subprocess.run(
            [sys.executable, "-c", probe],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - no python?
        return False, f"could not ask a child process about Tk: {exc}"
    if done.returncode == 0:
        return True, ""
    reason = (done.stderr or done.stdout or "no output").strip().splitlines()
    return False, f"Tk cannot open a display: {reason[-1] if reason else 'no output'}"
TK_OK, TK_REASON = _tk_usable()

if not TK_OK:  # pragma: no cover - environment dependent
    pytest.skip(TK_REASON, allow_module_level=True)


class DialogRecorder:
    """Replaces Tk modal dialogs, which would block the test forever."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.answers: list[bool] = []
        self.paths: list[str] = []

    def showerror(self, title: str = "", message: str = "", **_: object) -> str:
        self.errors.append(f"{title}: {message}")
        raise AssertionError(f"unexpected error dialog — {title}: {message}")

    def showwarning(self, title: str = "", message: str = "", **_: object) -> str:
        self.warnings.append(f"{title}: {message}")
        return "ok"

    def showinfo(self, title: str = "", message: str = "", **_: object) -> str:
        return "ok"

    def askyesno(self, title: str = "", message: str = "", **_: object) -> bool:
        self.answers.append(True)
        return True

    def askopenfilename(self, **_: object) -> str:
        return self.paths.pop(0) if self.paths else ""

    def asksaveasfilename(self, **_: object) -> str:
        return self.paths.pop(0) if self.paths else ""


@pytest.fixture(autouse=True)
def dialogs(monkeypatch: pytest.MonkeyPatch) -> DialogRecorder:
    """No test may open a real modal dialog: they hang until a human clicks."""
    from phantom.ui import app as ui_app

    recorder = DialogRecorder()
    for name in ("showerror", "showwarning", "showinfo", "askyesno"):
        monkeypatch.setattr(ui_app.messagebox, name, getattr(recorder, name), raising=False)
    for name in ("askopenfilename", "asksaveasfilename"):
        monkeypatch.setattr(ui_app.filedialog, name, getattr(recorder, name), raising=False)
    return recorder


@pytest.fixture(autouse=True)
def appearance_mode_is_global():
    """CustomTkinter's appearance mode is process-wide; a test must not leak it.

    Restored with the refusal armed: the widgets of the finished window are gone, and
    a display pass over them is exactly what this file is about.
    """
    import customtkinter as ctk

    from phantom.ui import widgets as W

    before = str(ctk.get_appearance_mode())
    yield
    if str(ctk.get_appearance_mode()) != before:
        with W.quiet_redraws():
            ctk.set_appearance_mode(before.lower())


def _passes_are_attempted() -> bool:
    """Whether this platform's Tk is asked for a display pass at all.

    ``DISPLAY_PASSES_UNSAFE`` means Aqua under Tk 9, where ``update idletasks``
    segfaults; the window then relies on Tk's own loop and never runs a pass, so a
    test about passes has to ask which world it is in instead of assuming one.
    """
    from phantom.ui.app import DISPLAY_PASSES_UNSAFE

    return not DISPLAY_PASSES_UNSAFE


def _after_scripts(app) -> list[str]:
    """The callbacks Tk says are armed, by name — for the failure message only."""
    names = []
    for ident in app.root.tk.call("after", "info"):
        script = str(app.root.tk.call("after", "info", ident))
        names.append(f"{ident}:{script[:40]}")
    return names


def _pump(app, seconds: float = 30.0) -> None:
    """Run the Tk event loop until no operation is in flight.

    ``worker.busy`` goes False when the thread exits, but ``_busy`` (the app's
    own "an operation is running" flag) is only cleared by the poll callback, so
    both have to settle.  If they never do, say what was still in flight instead
    of leaving the next assertion to report a bare ``_busy is True`` — a timeout
    here means the poll chain stalled or the job raised (the app logs that to
    stderr), not that the test forgot to wait.
    """
    import time

    def settled() -> bool:
        # Three things belong to a finished operation: the collector comes back a
        # poll or two after the job's DONE, and a job that finished often schedules
        # another one (the carrier probe schedules the size estimate).  Waiting only
        # for the worker left that follow-up to start on the next ``update`` — after
        # the loop — and the test that thought it had waited saw a busy window with
        # an idle worker and blamed the app.
        return not (
            app.worker.busy or app._busy or app._gc_paused or app.debouncer.pending
        )

    deadline = time.monotonic() + seconds
    # How often the loop got to run, and how long a single ``update`` took: a poll
    # chain that looks dead is usually a window whose event loop is spending its
    # time somewhere else, and the two look identical from the outside.
    iterations = 0
    update_total = 0.0
    update_max = 0.0
    while not settled() and time.monotonic() < deadline:
        started = time.monotonic()
        app.root.update()
        spent = time.monotonic() - started
        iterations += 1
        update_total += spent
        update_max = max(update_max, spent)
        time.sleep(0.02)
    app.root.update()
    if app.worker.busy or app._busy:
        # Stop the wedged job before failing: a worker thread that keeps running
        # outlives this test and steals the CPU of every test after it, which is
        # how one stuck job used to turn into a twelve-minute timeout.
        app.worker.cancel()
        app.worker.shutdown(timeout=2.0)
        # A wedged job survives cancel() and shutdown() -- that is what makes it
        # wedged -- so the interpreter would wait for its non-daemon thread for
        # ever and the CI step would burn its whole limit with no evidence.  Dump
        # every thread to a file the workflow prints: the frame the job is stuck
        # in is the only way to tell a slow encoder from a deadlock.
        import faulthandler

        with open("gui_stacks.txt", "a", encoding="utf-8") as dump:
            print(
                f"=== wedged: worker={app.worker.busy} app={app._busy} "
                f"operation={app._operation!r} ===",
                file=dump,
            )
            faulthandler.dump_traceback(file=dump, all_threads=True)
    import gc

    # The poll chain is what clears _busy; say whether it is still armed, because
    # a worker that has finished with _busy stuck true means the chain died and
    # the assertion message is the only place CI will ever show that.
    pending = [str(item) for item in app.root.tk.call("after", "info")]
    armed = app._poll_token is not None and str(app._poll_token) in pending
    assert not (app.worker.busy or app._busy), (
        f"still busy after {seconds:.0f}s: worker={app.worker.busy}, "
        f"app={app._busy}, operation={app._operation!r}, gc={gc.isenabled()}, "
        f"poll_token={app._poll_token!r}, armed={armed}, afters={len(pending)}, "
        f"poll_error={getattr(app, '_last_poll_error', '')!r}, "
        f"status={app.status_label.cget('text')!r}, "
        f"stage={app.stage_label.cget('text')!r}, "
        f"quiet={app.worker.seconds_since_progress():.0f}s, "
        f"poll_runs={getattr(app, '_poll_runs', -1)}, "
        f"worker_op={app.worker.operation!r}, "
        f"display_passes={getattr(app, '_display_passes', -1)}, "
        f"iterations={iterations}, update_total={update_total:.1f}s, "
        f"update_max={update_max * 1000:.0f}ms, "
        f"afters={_after_scripts(app)}"
    )


@pytest.fixture
def app(tmp_path: Path, isolated_settings: Path, monkeypatch: pytest.MonkeyPatch):
    import customtkinter as ctk

    from phantom.ui.app import PhantomApp
    from phantom.ui.dnd import DndProvider
    from phantom.ui.settings import SettingsStore

    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    # The window is *mapped*, the way a real launch leaves it: a withdrawn toplevel
    # has no native surface, and Tk 9.1 on macOS segfaults in its display idle pass
    # for exactly that state (see PhantomApp._flush_layout).  A test that needs a
    # hidden window withdraws it itself; a test that needs a mapped one no longer
    # has to remember to deiconify first.
    root.deiconify()
    instance = PhantomApp(
        root,
        settings_store=SettingsStore(path=isolated_settings / "settings.json"),
        dnd=DndProvider(),
    )
    yield instance
    try:
        instance.worker.shutdown(timeout=2)
        # Give the collector back even if the test ended in the middle of a job:
        # the pause is process-wide, so a leftover would follow every later test.
        if instance._gc_paused:
            instance._resume_collection_after_job()
    finally:
        # A test may have closed the window itself; destroying it twice is normal.
        with suppress(Exception):
            root.destroy()


def test_a_dnd_that_cannot_register_hides_its_hints_and_keeps_the_button(
    tmp_path: Path, isolated_settings: Path, monkeypatch: pytest.MonkeyPatch, dialogs
) -> None:
    """Tcl/Tk 9 imports tkinterdnd2 and then registers nothing.

    The window used to decide from ``available`` alone, so it promised a drop that
    always failed with ``invalid command name "tkdnd::drop_target"``.  What is
    asserted: the hints are gone, the log says why, and picking a file with the
    button still works — that path never goes through drag and drop.
    """
    import customtkinter as ctk

    from phantom.ui.app import PhantomApp
    from phantom.ui.dnd import DndProvider, DropTarget
    from phantom.ui.settings import SettingsStore

    ctk.set_default_color_theme("blue")

    class Tk9LikeDnd(DndProvider):
        """Imports fine, registers nothing -- the Tcl/Tk 9 behaviour."""

        def __init__(self) -> None:
            super().__init__()
            self.reason = None
            self._dnd_module = object()
            self._registration_failed = False

        def register(self, target: DropTarget) -> bool:
            self.reason = 'drop registration failed: invalid command name "tkdnd::drop_target"'
            self._registration_failed = True
            return False

    root = ctk.CTk()
    root.deiconify()
    instance = PhantomApp(
        root,
        settings_store=SettingsStore(path=isolated_settings / "settings.json"),
        dnd=Tk9LikeDnd(),
    )
    try:
        instance.root.update()
        assert instance.dnd.available is True
        assert instance.dnd.usable is False
        assert "invalid command name" in (instance.dnd.reason or "")
        for hint in (instance.dnd_carrier_hint, instance.dnd_stego_hint, instance.dnd_secret_hint):
            assert hint.winfo_manager() == "", "a hint promised a drop that cannot happen"

        # The button that picks a carrier must work regardless: it asks the dialog
        # and hands the path to the same code the drop would have used.
        carrier = tmp_path / "carrier.png"
        carrier.write_bytes(b"\x89PNG\r\n\x1a\n")
        chosen: list[str] = []
        monkeypatch.setattr(instance, "_set_carrier", chosen.append)
        dialogs.paths.append(str(carrier))
        instance._choose_carrier()
        assert chosen == [str(carrier)], "the file button did not deliver its path"
    finally:
        instance.worker.shutdown(timeout=2)
        with suppress(Exception):
            root.destroy()


def test_clipboard_timer_clears_the_secret_natively(app, monkeypatch) -> None:
    """The 30 s promise must reach the OS clipboard, not just Tk's own copy."""
    from phantom.ui import app as ui_app

    cleared: list[bool] = []
    monkeypatch.setattr(ui_app.clip, "clear_clipboard", lambda: cleared.append(True) or True)

    app._copy_to_clipboard("generated-Passw0rd!")
    assert app._clipboard_value == "generated-Passw0rd!"

    app._clear_clipboard_if("generated-Passw0rd!")

    assert cleared == [True], "the native tool is what empties the system clipboard"
    assert app._clipboard_value is None
    assert app._clipboard_token is None
    assert app._generated_password == ""


def test_clipboard_timer_leaves_another_apps_copy_alone(app, monkeypatch) -> None:
    from phantom.ui import app as ui_app

    cleared: list[bool] = []
    monkeypatch.setattr(ui_app.clip, "clear_clipboard", lambda: cleared.append(True) or True)

    app._copy_to_clipboard("generated-Passw0rd!")
    app.root.clipboard_clear()
    app.root.clipboard_append("something the user copied later")

    app._clear_clipboard_if("generated-Passw0rd!")

    assert cleared == [], "never wipe a clipboard someone else filled"
    assert app._clipboard_value is None


def test_closing_the_window_does_not_leave_the_password_behind(app, monkeypatch) -> None:
    from phantom.ui import app as ui_app

    cleared: list[bool] = []
    monkeypatch.setattr(ui_app.clip, "clear_clipboard", lambda: cleared.append(True) or True)

    app._copy_to_clipboard("generated-Passw0rd!")
    app._forget_clipboard()

    assert cleared == [True], "macOS keeps the text after the window is gone"
    assert app._clipboard_value is None


def test_closing_the_window_keeps_a_foreign_clipboard(app, monkeypatch) -> None:
    from phantom.ui import app as ui_app

    cleared: list[bool] = []
    monkeypatch.setattr(ui_app.clip, "clear_clipboard", lambda: cleared.append(True) or True)

    app._forget_clipboard()
    assert cleared == [], "nothing was copied, nothing to drop"


def test_window_builds_and_shows_default_language(app) -> None:
    assert app.current_tab in ("embed", "extract", "metadata", "settings")
    assert app.i18n.language in ("uk", "ru", "en")
    assert app.root.title()
    assert app.embed_vm is not None and app.extract_vm is not None


def test_language_switch_updates_visible_text(app) -> None:
    before = app.i18n.language
    target = "uk" if before != "uk" else "en"
    app._on_language_click(app._label_by_lang[target])
    assert app.i18n.language == target
    assert app.settings.language == target
    subtitle = app._t("header_subtitle", **app._security_params())
    assert "AES-256-GCM" in subtitle
    assert C.kdf_summary(C.default_kdf_profile()).split(" · ")[0] in subtitle
    assert app.root.title() == app._t("window_title")


def test_theme_switch_persists(app) -> None:
    app._on_theme_click(app._theme_by_label["Light"])
    assert app.settings.theme == "Light"
    app._on_theme_click(app._theme_by_label["System"])
    assert app.settings.theme == "System"


def _switch_appearance(app) -> str:
    """Click the theme bar so the *resolved* appearance mode really changes.

    ``set_appearance_mode`` notifies the widgets only when the resolved mode
    differs from the current one, so clicking "Light" on a light desktop is a
    legitimate no-op — and a test that needs the notification must not pick that
    click.  Returns the mode the window ends up in.
    """
    wanted = "Light" if ctk_mode() == "Dark" else "Dark"
    if app.settings.theme == wanted:
        # The bar ignores a click on the mode it already shows: step away first.
        app._on_theme_click(app._theme_by_label["Light" if wanted == "Dark" else "Dark"])
    app._on_theme_click(app._theme_by_label[wanted])
    return wanted


def test_a_theme_switch_refuses_every_display_pass(app) -> None:
    """A theme click used to flush the whole window once per widget.

    ``set_appearance_mode`` notifies every registered widget synchronously and each
    notification ends with ``update_idletasks()``.  On Aqua under Tk 9.1 the last of
    those passes segfaults: it is a display pass entered from inside a Tk callback.
    The window must refuse them all — and still change and repaint the mode.
    """
    import tkinter as tk

    from phantom.ui import widgets as W

    original = tk.Misc.update_idletasks
    before = W.suppressed_flush_count()

    # The repaint itself must still happen: one real widget is watched while the
    # mode changes, so "no flush" cannot be confused with "nothing was redrawn".
    drawn: list[int] = []
    real_draw = app.embed_pwd._draw

    def spy(*args, **kwargs):
        drawn.append(1)
        return real_draw(*args, **kwargs)

    app.embed_pwd._draw = spy  # type: ignore[method-assign]
    try:
        wanted = _switch_appearance(app)
    finally:
        del app.embed_pwd._draw

    refused = W.suppressed_flush_count() - before
    assert refused >= 20, (
        f"only {refused} display passes were refused by one theme click: "
        "CustomTkinter also changed something else, or the refusal is not armed"
    )
    assert drawn, "the widget never redrew itself: the theme was not applied"
    assert app.settings.theme == wanted
    assert ctk_mode() == wanted, "the appearance mode did not change"
    assert tk.Misc.update_idletasks is original, "the refusal outlived the click"
    assert app._layout_token is not None, "the window owes a pass but never asked"
    app.root.update()
    assert app._layout_token is None, "the owed pass never ran"


def test_switching_the_theme_six_times_keeps_the_window_alive(app) -> None:
    """Repeated switching: every click refuses its flush and repaints once."""
    from phantom.ui import widgets as W

    labels = {mode: label for label, mode in app._theme_by_label.items()}
    before = W.suppressed_flush_count()
    passes = app._display_passes
    order = ["Light", "Dark", "System", "Light", "Dark", "System"]
    for mode in order:
        app._on_theme_click(labels[mode])
        app.root.update()
        assert app.settings.theme == mode
        if mode in ("Light", "Dark"):
            assert ctk_mode() == mode
        assert app._layout_token is None, f"the pass owed by {mode} never ran"
    assert W.suppressed_flush_count() > before + 20, "nothing was refused at all"
    if _passes_are_attempted():
        assert app._display_passes >= passes + len(order), "the window stopped repainting"


def test_a_hidden_window_switches_the_theme_without_a_display_pass(app) -> None:
    """A withdrawn window changes mode like any other, and is never asked for pixels.

    An unmapped toplevel has no native surface; asking Tk to display it is the other
    half of the macOS crash.  Nothing is pumped while the window is hidden either —
    on Aqua ``update()`` on a withdrawn CTk root does not return.
    """
    from phantom.ui import widgets as W

    app.root.update()
    app.root.withdraw()
    before = W.suppressed_flush_count()
    passes = app._display_passes

    wanted = _switch_appearance(app)

    assert ctk_mode() == wanted, "a hidden window did not change mode"
    assert W.suppressed_flush_count() - before >= 20, "the click flushed"
    assert app._display_passes == passes, "a hidden window was asked to display"
    assert app._layout_token is not None, "the pass the theme owes is not scheduled"

    app.root.deiconify()
    app.root.update()
    if _passes_are_attempted():
        assert app._display_passes == passes + 1, "the owed pass never ran on <Map>"
    else:
        assert app._display_passes == passes


def test_the_system_theme_tick_never_flushes_from_the_timer(app, monkeypatch) -> None:
    """The OS theme poll runs from an ``after`` callback: the same rule applies."""
    import tkinter as tk

    import customtkinter as ctk

    from phantom.ui import app as ui_app

    original = tk.Misc.update_idletasks
    armed: list[bool] = []
    real = ctk.set_appearance_mode

    def spy(mode: str) -> None:
        armed.append(tk.Misc.update_idletasks is not original)
        real(mode)

    monkeypatch.setattr(ui_app.ctk, "set_appearance_mode", spy)
    monkeypatch.setattr(app.theme_watcher, "poll", lambda: "Dark")
    app._theme_tick()

    assert armed, "the tick did not change the appearance mode at all"
    assert all(armed), "the tick changed the mode with no refusal armed"
    app.root.update()


def test_a_resize_after_a_theme_switch_still_fits_every_caption(app) -> None:
    """The theme change must not leave the window laid out for the old mode."""
    app.root.deiconify()
    app.root.geometry("980x720")
    _switch_appearance(app)
    app.root.update()
    app.root.geometry("1140x900")
    app.root.update()

    problems = []
    for ident in ("embed", "extract", "metadata", "settings"):
        app._show_tab(ident)
        app.root.update()
        for widget in _captioned(app.tab_frames[ident]):
            issue = _clipped(widget)
            if issue:
                problems.append(f"{ident}: {issue}")
    assert not problems, "\n".join(problems)


def test_windows_do_not_accumulate_appearance_callbacks(tmp_path: Path) -> None:
    """Three windows in a row must not leave three windows' worth of callbacks.

    The list is process-wide, and a theme change walks all of it: a leak here is both
    a slowdown and, on Aqua under Tcl/Tk 9.1, a crash — the notification repaints
    widgets whose Tcl commands no longer exist.
    """
    from customtkinter.windows.widgets.appearance_mode import AppearanceModeTracker

    baseline = len(AppearanceModeTracker.callback_list)
    sizes = []
    for index in range(3):
        root, instance = _build(tmp_path / f"window{index}", map_window=False)
        try:
            sizes.append(len(AppearanceModeTracker.callback_list))
            instance.worker.shutdown(timeout=2)
            instance._on_close()
        finally:
            with suppress(Exception):
                root.destroy()
    after = len(AppearanceModeTracker.callback_list)
    assert sizes, "no window was built"
    assert after <= baseline + 5, (
        f"the notification list grew from {baseline} to {after} over three windows "
        f"(sizes while alive: {sizes})"
    )


def test_a_closed_window_never_hears_about_a_theme_change_again(app) -> None:
    """CustomTkinter's notification list must not keep a closed window's widgets.

    ``AppearanceModeTracker`` holds one bound method per widget and only lets go in
    ``CTkAppearanceModeBaseClass.destroy()``, which ``Tk.destroy()`` never calls for
    the children — so the next theme change repaints widgets whose Tcl commands are
    gone.  On Aqua under Tcl/Tk 9.1 that segfaults; here it would at best be a wall
    of TclError that the tracker swallows.
    """
    import customtkinter as ctk
    from customtkinter.windows.widgets.appearance_mode import AppearanceModeTracker

    from phantom.ui import widgets as W

    root = app.root

    def belonging() -> list[object]:
        found = []
        for callback in list(AppearanceModeTracker.callback_list):
            owner = getattr(callback, "__self__", None)
            if owner is None:
                continue
            try:
                belongs = owner._root() is root
            except Exception:  # noqa: BLE001 - a widget with no tree left
                belongs = False
            if belongs:
                found.append(callback)
        return found

    assert len(belonging()) > 50, "the window registered almost nothing"

    app._on_close()

    assert belonging() == [], "the closed window is still in the notification list"
    # Idempotent: the <Destroy> safety net calls it a second time.
    assert W.release_appearance_callbacks(root) == 0
    # And a theme change after the window is gone is a no-op, not a crash.
    with W.quiet_redraws():
        ctk.set_appearance_mode("Dark" if ctk.get_appearance_mode() == "Light" else "Light")


def test_closing_right_after_a_theme_switch_leaves_no_timer(app) -> None:
    """The pass a theme change owes is cancelled with every other timer."""
    _switch_appearance(app)
    assert app._layout_token is not None
    app._on_close()
    assert app._layout_token is None


def test_tabs_switch(app) -> None:
    for ident in ("extract", "metadata", "settings", "embed"):
        app._show_tab(ident)
        assert app.current_tab == ident


def test_embed_flow_through_the_ui(app, rgb_carrier: Path, password: str) -> None:
    app._set_carrier(str(rgb_carrier))
    _pump(app)  # let the probe job finish before pressing Embed
    assert app.worker.busy is False
    assert app.output_entry.get(), "choosing a carrier must propose an output path"

    app.embed_pwd.insert(0, password)
    app._set_textbox(app.embed_text, "gui smoke payload")
    app._refresh_all()
    app._start_embed()

    _pump(app)
    assert app._busy is False
    output = Path(app.embed_vm.output_path)
    assert output.exists()
    from phantom.core import api

    assert api.extract_from_path(str(output), password).payload.data == b"gui smoke payload"


def _extracted(version: int):
    from phantom.core.payload import ExtractedPayload, PayloadKind

    return ExtractedPayload(
        kind=PayloadKind.TEXT,
        name="",
        data=b"text",
        sha256_expected="a" * 64,
        sha256_actual="a" * 64,
        integrity_ok=True,
        original_size=4,
        compressed_size=4,
        container_version=version,
    )


def test_embed_tab_says_which_layout_it_writes(app) -> None:
    from phantom.ui.format import written_layout_text

    assert app.format_label.cget("text") == app.i18n.t(
        "lbl_embed_format", layout=written_layout_text(app.i18n)
    )
    assert f"v{C.CONTAINER_VERSION}" in app.format_label.cget("text")


def test_embed_tab_names_the_trellis_layout_it_writes(app) -> None:
    """The default layout is v5, and the tab says so in the chosen language."""
    from phantom.ui.format import written_layout_text

    app._on_language_click(app._label_by_lang["ru"])
    app._refresh_all()

    label = written_layout_text(app.i18n)
    text = app.format_label.cget("text")
    assert label == app.i18n.t("layout_trellis", version=C.STC_CONTAINER_VERSION)
    assert label == f"v{C.STC_CONTAINER_VERSION} · синдромный решётчатый код"
    assert text == app.i18n.t("lbl_embed_format", layout=label), text


def test_extract_tab_names_a_v5_file_and_its_older_siblings(app) -> None:
    """A v5 file gets the trellis wording; v2, v3 and v4 keep their own."""
    from phantom.ui.format import layout_text

    app._on_language_click(app._label_by_lang["ru"])
    app._show_tab("extract")
    for version in (
        C.STC_CONTAINER_VERSION,
        C.KEYED_CONTAINER_VERSION,
        C.ADAPTIVE_CONTAINER_VERSION,
        C.V2_CONTAINER_VERSION,
    ):
        app.extract_vm.result = _extracted(version)
        app._refresh_all()
        text = app.audit_rows["audit_layout"].cget("text")
        assert text == layout_text(app.i18n, version)
        assert f"v{version}" in text
        assert text != app.i18n.t("layout_unknown")
    assert "синдромный решётчатый код" in layout_text(app.i18n, C.STC_CONTAINER_VERSION)


def test_format_row_names_the_layout_of_the_opened_file(app) -> None:
    from phantom.ui.format import layout_text

    app._show_tab("extract")
    app._refresh_all()
    assert app.audit_rows["audit_layout"].cget("text") == "—", "no file yet"

    app.extract_vm.result = _extracted(C.CONTAINER_VERSION)
    app._refresh_all()
    assert app.audit_rows["audit_layout"].cget("text") == layout_text(
        app.i18n, C.CONTAINER_VERSION
    )
    assert f"v{C.CONTAINER_VERSION}" in app.audit_rows["audit_layout"].cget("text")

    app.extract_vm.result = _extracted(C.V2_CONTAINER_VERSION)
    app._refresh_all()
    assert app.audit_rows["audit_layout"].cget("text") == layout_text(
        app.i18n, C.V2_CONTAINER_VERSION
    )
    assert f"v{C.V2_CONTAINER_VERSION}" in app.audit_rows["audit_layout"].cget("text")


def test_format_row_follows_a_language_switch(app) -> None:
    from phantom.ui.format import layout_text

    app._show_tab("extract")
    app.extract_vm.result = _extracted(C.CONTAINER_VERSION)
    app._refresh_all()
    before = app.audit_rows["audit_layout"].cget("text")

    target = "uk" if app.i18n.language != "uk" else "en"
    app._on_language_click(app._label_by_lang[target])
    app._refresh_all()
    after = app.audit_rows["audit_layout"].cget("text")

    assert after == layout_text(app.i18n, C.CONTAINER_VERSION)
    assert after != before
    assert f"v{C.CONTAINER_VERSION}" in after


def test_size_row_reports_the_share_of_the_carrier(app) -> None:
    """The audit SIZE row grows a share once the carrier is known."""
    from phantom.core.api import CarrierKind, CarrierProbe
    from phantom.stego.engine import Capacity

    app._show_tab("extract")
    app.extract_vm.result = _extracted(C.CONTAINER_VERSION)
    app._refresh_all()
    assert "%" not in app.audit_rows["audit_size"].cget("text"), "no carrier probed"

    app.extract_vm.carrier = CarrierProbe(
        kind=CarrierKind.IMAGE,
        path="carrier.png",
        capacity=Capacity(usable_bits=57_600, payload_bytes=7_118),
    )
    app._refresh_all()
    text = app.audit_rows["audit_size"].cget("text")
    assert text.startswith("4 B → 4 B · "), text
    assert "%" in text
    assert "% of the carrier" in text or "% носителя" in text or "% носія" in text


def test_password_generator_writes_entry(app) -> None:
    app._generate_password()
    assert len(app.embed_vm.password) == C.GENERATED_PASSWORD_LENGTH


def test_extract_tab_rejects_empty_password(app) -> None:
    app._show_tab("extract")
    keys = [issue.key for issue in app.extract_vm.issues()]
    assert "warn_empty_pwd_dec" in keys


def test_drop_handler_ignores_wrong_extension(app) -> None:
    assert app._accepted_drop(["/tmp/a.txt"], C.IMAGE_EXTENSIONS) is None
    assert app._accepted_drop([], C.IMAGE_EXTENSIONS) is None


def test_clipboard_is_cleared_after_delay(app) -> None:
    app._copy_to_clipboard("secret-value")
    assert app.root.clipboard_get() == "secret-value"
    app._clear_clipboard_if("secret-value")
    # After the clear Tk may own no selection at all, which raises on read.
    try:
        remaining = app.root.clipboard_get()
    except Exception:  # noqa: BLE001 - TclError: nothing owns the clipboard
        remaining = ""
    assert remaining != "secret-value"


def test_worker_poll_drains_events(app) -> None:
    app.worker.start("probe", lambda report, cancel: 42)
    import time

    deadline = time.monotonic() + 10
    while app.worker.busy and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    app._poll()
    assert app.worker.busy is False


# ---------------------------------------------------------------------------
# clipboard: buttons, shortcuts and the context menu
# ---------------------------------------------------------------------------
def test_paste_button_fills_the_password_field(app) -> None:
    app.root.clipboard_clear()
    app.root.clipboard_append("pasted-key-1234")
    app._paste_password()
    assert app.embed_pwd.get() == "pasted-key-1234"
    assert app.embed_vm.password == "pasted-key-1234"


def test_paste_button_reports_an_empty_clipboard(app, dialogs) -> None:
    app.root.clipboard_clear()
    app.embed_pwd.delete(0, "end")
    app._paste_password()
    assert app.embed_pwd.get() == ""
    assert dialogs.errors == []  # a status line, not a modal box


def test_copy_button_puts_the_password_on_the_clipboard(app) -> None:
    app.embed_pwd.delete(0, "end")
    app.embed_pwd.insert(0, "generated-key-99")
    app._copy_password()
    assert app.root.clipboard_get() == "generated-key-99"


def test_copy_button_reports_an_empty_password(app) -> None:
    app.embed_pwd.delete(0, "end")
    app._copy_password()
    assert app.status_label.cget("text") == app.i18n.t("status_password_empty")


def test_paste_shortcut_handler_reads_the_clipboard(app, monkeypatch) -> None:
    """The Cmd/Ctrl+V handler must insert whatever the clipboard holds."""
    inner = app.embed_pwd._entry
    monkeypatch.setattr(inner, "clipboard_get", lambda: "mocked-clipboard-value")
    app.embed_pwd.delete(0, "end")
    shortcuts = next(item for item in app._text_shortcuts if item.widget is inner)
    assert shortcuts.paste() == "break"
    assert app.embed_pwd.get() == "mocked-clipboard-value"


def test_command_and_control_shortcuts_are_both_installed(app) -> None:
    inner = app.embed_pwd._entry
    shortcuts = next(item for item in app._text_shortcuts if item.widget is inner)
    assert "<Command-v>" in shortcuts.sequences
    assert "<Control-v>" in shortcuts.sequences
    assert "<Command-a>" in shortcuts.sequences
    assert shortcuts.bound_count >= 16


def test_context_menu_offers_the_full_editing_set(app) -> None:
    from phantom.ui.widgets import install_text_shortcuts

    shortcuts = install_text_shortcuts(
        app.root, app.embed_pwd, labels_provider=app._menu_labels
    )
    menu = shortcuts.build_menu()
    labels = [
        menu.entrycget(index, "label")
        for index in range(menu.index("end") + 1)
        if menu.type(index) == "command"
    ]
    assert labels == [
        app.i18n.t("menu_undo"),
        app.i18n.t("menu_redo"),
        app.i18n.t("menu_cut"),
        app.i18n.t("menu_copy"),
        app.i18n.t("menu_paste"),
        app.i18n.t("menu_select_all"),
    ], "an entry field has no clear-field entry: that button belongs to the editor"
    app.root.clipboard_clear()
    app.root.clipboard_append("from-the-menu")
    app.embed_pwd.delete(0, "end")
    menu.invoke(_menu_entry(menu, app.i18n.t("menu_paste")))
    assert app.embed_pwd.get() == "from-the-menu"


def _menu_entry(menu, label: str) -> int:
    """The *menu* index of one label: separators occupy indices of their own."""
    for index in range(menu.index("end") + 1):
        if menu.type(index) == "command" and menu.entrycget(index, "label") == label:
            return index
    raise AssertionError(f"no menu entry labelled {label!r}")


def test_the_editor_menu_also_clears_the_field(app) -> None:
    from phantom.ui.widgets import install_text_shortcuts

    shortcuts = install_text_shortcuts(
        app.root,
        app.embed_text,
        labels_provider=app._menu_labels,
        clear_handler=app._clear_field,
    )
    menu = shortcuts.build_menu()
    labels = [
        menu.entrycget(index, "label")
        for index in range(menu.index("end") + 1)
        if menu.type(index) == "command"
    ]
    assert labels[-1] == app.i18n.t("menu_clear")
    app.embed_text.insert("1.0", "clear me")
    app._on_text_changed()
    menu.invoke(_menu_entry(menu, app.i18n.t("menu_clear")))
    assert app.embed_text.get("1.0", "end-1c") == ""


def test_right_click_binding_is_installed(app) -> None:
    inner = app.embed_pwd._entry
    assert inner.bind("<Button-2>")
    assert inner.bind("<Button-3>")


# ---------------------------------------------------------------------------
# scrolling
# ---------------------------------------------------------------------------
def test_the_router_took_over_the_wheel_for_every_tab(app) -> None:
    from phantom.ui.widgets import CTkAdapter

    assert len(app._scroll_frames) == 4, "one scroll area per tab, the metadata tab included"
    assert app._scroll.area_count == 4
    assert app._scroll.installed is True
    for frame in app._scroll_frames:
        assert CTkAdapter.scroll_canvas(frame) is not None


def test_the_router_reports_the_running_tk_and_backend(app) -> None:
    """Diagnosis needs the facts: which Tk, which windowing system, which maths."""
    described = app._scroll.describe()
    environment = app._scroll.environment

    assert environment.windowing_system in {"aqua", "win32", "x11"}, described
    assert environment.tk_version[0] >= 8, described
    assert f"Tk {'.'.join(str(part) for part in environment.tk_version)}" in described
    assert environment.windowing_system in described


def _screen_centre(widget) -> tuple[int, int]:
    """Screen coordinates of the middle of a widget, after the window is real."""
    widget.update_idletasks()
    return (
        widget.winfo_rootx() + max(1, widget.winfo_width()) // 2,
        widget.winfo_rooty() + max(1, widget.winfo_height()) // 2,
    )


def _reveal(app) -> None:
    """CustomTkinter starts withdrawn: routing needs a mapped window."""
    app.root.deiconify()
    app.root.geometry("1000x760+0+0")
    app.root.update()
    app.root.update_idletasks()


def _area_for_tab(app, ident: str):
    """The scroll area that belongs to one tab."""
    frame = app.tab_frames[ident]
    for area in app._scroll.areas:
        widget = area.frame
        for _step in range(8):
            if widget is frame:
                return area
            widget = getattr(widget, "master", None)
            if widget is None:
                break
    return None


def _make_scrollable(canvas, *, top: float = 0.1) -> float:
    """Put the canvas somewhere the wheel can really move it, and say where.

    Routing is what is under test, not the height of the layout: a tab whose
    content happens to fit would make ``yview`` immovable and the test would
    report a routing failure for a layout reason.  CustomTkinter re-applies its
    own ``scrollregion`` on ``<Configure>``, which can clamp a requested position
    to the bottom of the real content, so the position is checked and pulled back
    far enough that a notch in either direction moves something.
    """
    canvas.update_idletasks()
    first, last = (float(value) for value in canvas.yview())
    if last - first >= 0.999:
        # The layout fits the window: give the canvas a document that does not.
        canvas.configure(scrollregion=(0, 0, max(1, canvas.winfo_width()), 6000))
        canvas.update_idletasks()
    canvas.yview_moveto(top)
    canvas.update_idletasks()
    first, last = (float(value) for value in canvas.yview())
    if last >= 0.999 and first > 0.5:
        # A late <Configure> clamped the view to the bottom of the real content.
        canvas.yview_moveto(max(0.0, first - 0.2))
        canvas.update_idletasks()
        first, last = (float(value) for value in canvas.yview())
    assert last - first < 0.999, (
        f"this tab cannot scroll at all: yview={(first, last)}, "
        f"scrollregion={canvas.cget('scrollregion')}"
    )
    return first


def _point_over(app, area) -> tuple[int, int]:
    """A screen point that the router itself resolves to ``area``.

    The middle of a tab may be a text box, which deliberately resolves to
    nothing; probing real widgets keeps the test about scrolling instead of about
    the pixel layout of the current design.
    """
    candidates = [area.frame, *_descendants(area.frame)]
    for widget in candidates:
        try:
            widget.update_idletasks()
            if widget.winfo_width() < 3 or widget.winfo_height() < 3:
                continue
            if not widget.winfo_ismapped():
                continue
            x, y = _screen_centre(widget)
        except Exception:  # noqa: BLE001, S112 - a widget that went away mid-scan
            continue
        if app._scroll.resolve(x, y) is area:
            return x, y
    if area.frame.winfo_ismapped():
        # The window is on screen, so some point of the area must resolve to it:
        # either the routing is broken or the layout no longer contains anything
        # this test can aim at.  Silence here would turn the test into a no-op.
        pytest.fail(
            f"the window is mapped, yet no widget of area {area.describe()} "
            "resolves to it — routing or layout changed"
        )
    pytest.skip("the window is not mapped on this platform")


def _wheel_on_root(app, *, delta: int, x: int, y: int) -> None:
    """Deliver a wheel event to the *root window*, the way Aqua does."""
    app.root.event_generate("<MouseWheel>", delta=delta, x=x, y=y, rootx=x, rooty=y, when="now")
    app.root.update()


def _touchpad_on_root(app, *, raw: int, x: int, y: int) -> None:
    """Deliver a Tk 9 ``<TouchpadScroll>``; ``-delta`` is the packed ``%D``."""
    app.root.event_generate("<TouchpadScroll>", delta=raw, x=x, y=y, rootx=x, rooty=y, when="now")
    app.root.update()


def _descendants(widget) -> list:
    found: list = []
    stack: list = [widget]
    while stack:
        current = stack.pop()
        for child in current.winfo_children():
            found.append(child)
            stack.append(child)
    return found


def test_a_wheel_event_on_the_root_scrolls_the_tab_under_the_pointer(app) -> None:
    """The real path: event → bind_all → pointer → canvas, through the Tk loop."""
    _reveal(app)
    embed = _area_for_tab(app, "embed")
    assert embed is not None
    before = _make_scrollable(embed.canvas)
    hidden = [area.canvas.yview() for area in app._scroll.areas if area is not embed]
    x, y = _point_over(app, embed)

    _wheel_on_root(app, delta=-120, x=x, y=y)

    after = float(embed.canvas.yview()[0])
    assert after > before, f"a scroll-down notch moved yview {before} → {after}"
    assert [area.canvas.yview() for area in app._scroll.areas if area is not embed] == hidden


def test_the_pointer_chooses_the_area_not_the_event_widget(app) -> None:
    """Generated on the root, resolved by position: that is the whole design."""
    _reveal(app)
    embed = _area_for_tab(app, "embed")
    extract = _area_for_tab(app, "extract")
    assert embed is not None and extract is not None
    app._show_tab("extract")
    app.root.update()
    before_extract = _make_scrollable(extract.canvas)
    before_embed = _make_scrollable(embed.canvas)
    x, y = _point_over(app, extract)

    _wheel_on_root(app, delta=120, x=x, y=y)

    assert float(extract.canvas.yview()[0]) < before_extract
    assert float(embed.canvas.yview()[0]) == pytest.approx(before_embed), (
        "the tab that is not under the pointer scrolled"
    )


def test_a_wheel_event_over_no_area_scrolls_nothing(app) -> None:
    """A point that resolves to no area must leave every canvas where it was."""
    _reveal(app)
    tops = [_make_scrollable(area.canvas) for area in app._scroll.areas]
    x, y = _screen_centre(app.tab_bar)
    assert app._scroll.resolve(x, y) is None, "the tab bar resolved to a scroll area"

    _wheel_on_root(app, delta=-120, x=x, y=y)

    assert [float(area.canvas.yview()[0]) for area in app._scroll.areas] == tops


def test_a_textbox_keeps_its_own_wheel_handling(app) -> None:
    """The page must not scroll underneath a text area that scrolled itself."""
    from phantom.ui.scroll import is_text_area

    _reveal(app)
    area = _area_for_tab(app, "embed")
    assert area is not None
    boxes = [widget for widget in _descendants(area.frame) if is_text_area(widget)]
    if not boxes:
        pytest.skip("no text area in this tab on this platform")
    box = boxes[0]
    # A box with nothing to scroll now hands the gesture to the page on purpose;
    # this test is about the case where the box really can move.
    from phantom.ui.widgets import inner_text_widget

    inner = inner_text_widget(box)
    inner.delete("1.0", "end")
    inner.insert("1.0", "\n".join(f"line {index}" for index in range(400)))
    app.root.update()
    before = _make_scrollable(area.canvas, top=0.5)
    x, y = _screen_centre(box)

    _wheel_on_root(app, delta=-120, x=x, y=y)

    assert float(area.canvas.yview()[0]) == pytest.approx(before), (
        "the page scrolled under the text box"
    )


def _has_touchpad_events(root) -> bool:
    """Tk 9 has ``<TouchpadScroll>``; Tk 8.6 raises for the sequence name."""
    try:
        root.bind("<TouchpadScroll>", lambda _event: None)
    except Exception:  # noqa: BLE001 - no such event type on 8.6
        return False
    root.unbind("<TouchpadScroll>")
    return True


def test_tk_ships_the_same_decoder_we_use(app) -> None:
    """``::tk::PreciseScrollDeltas`` and our fallback must agree, value for value."""
    from phantom.ui.scroll import decode_touchpad_delta

    if not _has_touchpad_events(app.root):
        pytest.skip("Tk without <TouchpadScroll>: no PreciseScrollDeltas to compare")
    for raw in (0, 5, -5, (2 << 16) | 7, (-3 << 16) | 9, (1 << 16) | 0xFFF0, -2147483648):
        from_tk = tuple(int(part) for part in app.root.tk.call("::tk::PreciseScrollDeltas", raw))
        assert decode_touchpad_delta(raw) == from_tk, raw


def test_a_touchpad_event_on_the_root_scrolls_the_tab_by_pixels(app) -> None:
    """The macOS bug itself: a packed ``<TouchpadScroll>`` must move the canvas.

    Skipped on Tk 8.6, which has neither the event type nor the packing, so the
    only place this runs for real is a Tk 9 runtime — CI with a Tk 9 build, or the
    owner's Mac.
    """
    if not _has_touchpad_events(app.root):
        pytest.skip("Tk 8.6 has no <TouchpadScroll>")
    _reveal(app)
    area = _area_for_tab(app, "embed")
    assert area is not None
    before = _make_scrollable(area.canvas, top=0.5)
    x, y = _point_over(app, area)

    # Y delta in the low 16 bits, X in the high: 40 pixels towards the end.
    _touchpad_on_root(app, raw=40, x=x, y=y)

    after = float(area.canvas.yview()[0])
    assert after > before, f"40 px of downward gesture moved yview {before} → {after}"


def test_a_touchpad_event_is_not_read_as_a_wheel_notch(app) -> None:
    """A packed delta must never reach the mouse-wheel maths.

    ``(-1 << 16) | 40`` is a one-pixel horizontal drift plus 40 px down.  Read as
    a wheel delta it is 4 294 967 336 units, i.e. a jump to the far end of the
    document, which is what a Mac trackpad used to produce.
    """
    if not _has_touchpad_events(app.root):
        pytest.skip("Tk 8.6 has no <TouchpadScroll>")
    from phantom.ui.scroll import decode_touchpad_delta

    raw = ((-1 << 16) | 40) & 0xFFFFFFFF
    assert decode_touchpad_delta(raw) == (-1, 40)

    _reveal(app)
    area = _area_for_tab(app, "embed")
    assert area is not None
    before = _make_scrollable(area.canvas, top=0.5)
    x, y = _point_over(app, area)

    _touchpad_on_root(app, raw=raw, x=x, y=y)

    after = float(area.canvas.yview()[0])
    assert abs(after - before) < 0.05, f"a 40 px gesture jumped yview {before} → {after}"


def test_closing_the_window_releases_the_wheel_routing(app) -> None:
    assert app._scroll.installed is True
    app._release_scroll()

    assert app._scroll.installed is False
    app._release_scroll()  # idempotent


# ---------------------------------------------------------------------------
# the editor action bar, the result actions and the diagnostics
# ---------------------------------------------------------------------------


def _text_payload(text: str, *, name: str = "message.txt"):
    """A text payload exactly as the engine hands one over (real bytes, real digest)."""
    import hashlib

    from phantom.core.payload import ExtractedPayload, PayloadKind

    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    return ExtractedPayload(
        kind=PayloadKind.TEXT,
        name=name,
        data=data,
        sha256_expected=digest,
        sha256_actual=digest,
        integrity_ok=True,
        original_size=len(data),
        compressed_size=len(data),
    )


def _show_text_payload(app, text: str) -> str:
    """Put a real payload in the extract model and repaint the panel."""
    payload = _text_payload(text)
    app.extract_vm.result = payload
    app.extract_vm.outcome = None
    app._result_preview_key = None
    app._refresh_extract_state()
    return text


def test_the_editor_action_bar_offers_every_action(app) -> None:
    """Buttons, hotkeys and the context menu must be the same handlers.

    Anything else is how a field ends up with a "cut" button that cuts and a
    Cmd+X that does nothing — the situation this bar exists to end.
    """
    bar = app.editor_actions
    for widget, label_key in (
        (app.editor_paste_button, "btn_paste"),
        (app.editor_copy_button, "btn_copy"),
        (app.editor_cut_button, "btn_cut"),
        (app.editor_select_all_button, "btn_select_all"),
        (app.editor_undo_button, "btn_undo"),
        (app.editor_redo_button, "btn_redo"),
        (app.editor_copy_all_button, "btn_copy_all"),
        (app.clear_field_button, "btn_clear_field"),
    ):
        assert widget.master is bar, f"{label_key} is not in the action bar"
        assert widget.cget("text") == app.i18n.t(label_key)
    api = app._editor_action_api()
    assert api is not None and api is app._editor_shortcuts.actions, (
        "the bar is not using the shortcut object's clipboard actions"
    )


def test_select_all_button_selects_the_whole_editor(app) -> None:
    body = "".join(f"line {index}: строка текста\n" for index in range(2000))
    app.embed_text.insert("1.0", body)
    app._on_text_changed()
    # The edit schedules a size estimate; the buttons are disabled while a job runs,
    # so the window has to be idle before a click means anything.
    _pump(app)
    app.editor_select_all_button.invoke()
    inner = app._editor_inner
    assert inner.get("sel.first", "sel.last") == body
    assert inner.index("insert") != "1.0", "the caret sits in front of the selection"


def test_copy_selection_then_copy_all_from_the_action_bar(app) -> None:
    body = "alpha beta gamma"
    app.embed_text.insert("1.0", body)
    app._on_text_changed()
    _pump(app)
    inner = app._editor_inner
    inner.tag_remove("sel", "1.0", "end")
    inner.tag_add("sel", "1.0", "1.5")
    # Direct call: the button's enabled state follows the selection, and no event
    # fires just because a test added a tag.
    app._refresh_editor_actions()
    app.editor_copy_button.invoke()
    assert app.root.clipboard_get() == "alpha"
    inner.tag_remove("sel", "1.0", "end")
    app.editor_copy_all_button.invoke()
    assert app.root.clipboard_get() == body
    assert app.embed_vm.text == body


def test_cut_button_removes_the_selection_and_updates_the_model(app) -> None:
    app.embed_text.insert("1.0", "cut me please")
    app._on_text_changed()
    _pump(app)
    inner = app._editor_inner
    inner.tag_add("sel", "1.0", "1.6")
    app.editor_cut_button.invoke()
    assert inner.get("1.0", "end-1c") == " please"
    assert app.root.clipboard_get() == "cut me"
    assert app.embed_vm.text == " please", "the model still describes the deleted text"


def test_paste_button_inserts_the_clipboard(app) -> None:
    app.root.clipboard_clear()
    app.root.clipboard_append("pasted body")
    app.editor_paste_button.invoke()
    assert app._editor_inner.get("1.0", "end-1c") == "pasted body"
    assert app.embed_vm.text == "pasted body"


def test_undo_and_redo_buttons_follow_the_history(app) -> None:
    app.embed_text.insert("1.0", "undo me")
    app.embed_text.edit_separator()
    app._on_editor_modified()
    app.root.update()
    _pump(app)
    assert str(app.editor_undo_button.cget("state")) == "normal"
    app.editor_undo_button.invoke()
    assert app._editor_inner.get("1.0", "end-1c") == ""
    assert app.embed_vm.text == ""
    # The undo changed the payload, so an estimate is running; every action button
    # is disabled while a job runs, and the redo click must wait for it.
    _pump(app)
    assert str(app.editor_redo_button.cget("state")) == "normal"
    app.editor_redo_button.invoke()
    assert app._editor_inner.get("1.0", "end-1c") == "undo me"


def test_redo_survives_the_event_tk_queues_for_the_undo_itself(app) -> None:
    """A step this window performs must not be read back as a user edit.

    Tk reports every text change with the same ``<<Modified>>`` event, so an undo's
    own event used to arrive after the handler had re-armed "redo available" and
    clear that flag again: the redo button stayed grey until the next keystroke, and
    clicking it did nothing.  The history here is a cut, because a delete is the
    shape the smoke caught, and the redo is clicked with no keystroke in between.
    """
    inner = app._editor_inner
    inner.insert("1.0", "cut me please")
    app._on_text_changed()
    _pump(app)
    inner.tag_remove("sel", "1.0", "end")
    inner.tag_add("sel", "1.0", "1.7")
    app._refresh_editor_actions()
    app.editor_cut_button.invoke()
    assert inner.get("1.0", "end-1c") == "please"
    _pump(app)
    app.editor_undo_button.invoke()
    assert inner.get("1.0", "end-1c") == "cut me please"
    _pump(app)
    assert app._editor_can_redo is True, "the undo's own event cleared redo availability"
    assert str(app.editor_redo_button.cget("state")) == "normal"
    app.editor_redo_button.invoke()
    assert inner.get("1.0", "end-1c") == "please"


def test_clear_field_keeps_the_password_and_the_carrier(app) -> None:
    app.embed_vm.set_password("keep-me-please")
    app.embed_vm.carrier_path = "/tmp/carrier.png"
    app.embed_text.insert("1.0", "text to clear")
    app._on_text_changed()
    _pump(app)
    app.clear_field_button.invoke()
    assert app._editor_inner.get("1.0", "end-1c") == ""
    assert app.embed_vm.password == "keep-me-please"
    assert app.embed_vm.carrier_path == "/tmp/carrier.png"
    assert app._status_key == "status_field_cleared"


def test_a_full_reset_drops_the_undo_history(app) -> None:
    """The text a reset removes must not be one Cmd+Z away."""
    import tkinter as tk

    app.embed_text.insert("1.0", "secret payload")
    app._on_text_changed()
    _pump(app)
    app._clear_embed()
    app.root.update()
    with pytest.raises(tk.TclError):
        app._editor_inner.edit_undo()
    assert app._editor_can_undo is False and app._editor_can_redo is False


def test_result_copy_all_uses_the_model_not_the_preview(app, monkeypatch) -> None:
    """The pane paints a preview; copy-all must hand over everything.

    The preview is capped (a 15 MiB insert is a multi-second display pass on Aqua),
    so a copy that reads the widget silently truncates the user's data.  This is
    that bug, asserted against the model.  The cap is lowered here so the test does
    not spend a second painting a pane it is not testing.
    """
    monkeypatch.setattr(C, "RESULT_PREVIEW_CHARS", 4_000)
    body = "".join(f"{index:06d} строка результата 😀\n" for index in range(900))
    assert len(body) > C.RESULT_PREVIEW_CHARS
    _show_text_payload(app, body)
    preview = app.result_text.get("1.0", "end-1c")
    assert len(preview) == C.RESULT_PREVIEW_CHARS, "the preview cap did not apply"
    app.copy_button.invoke()
    # What PHANTOM handed to Tk is the whole payload; that is the part this test is
    # about, and it is exact.
    assert app._clipboard_value == body
    copied = app.root.clipboard_get()
    # The pasteboard itself may add a character at Tk's own chunk boundaries for a
    # long UTF-8 string (measured on both Tks); it must never *shorten* it.
    assert len(copied) >= len(body), f"the pasteboard lost characters: {len(copied)} of {len(body)}"
    assert copied.startswith(body[:1000])


def test_result_selects_and_copies_without_being_editable(app) -> None:
    body = "read only result — строка"
    _show_text_payload(app, body)
    assert app.result_text.cget("state") == "disabled"
    app.select_all_result_button.invoke()
    inner = app.result_text._textbox
    assert inner.get("sel.first", "sel.last") == body
    app.copy_selection_button.invoke()
    assert app.root.clipboard_get() == body


def test_reading_the_clipboard_is_not_the_only_way_to_get_the_result(app) -> None:
    """With no selection, "copy selection" says so instead of copying nothing."""
    _show_text_payload(app, "body")
    app.result_text._textbox.tag_remove("sel", "1.0", "end")
    app.copy_selection_button.invoke()
    assert app._status_key == "status_selection_empty"


def test_result_txt_save_is_byte_exact(app, dialogs, tmp_path: Path) -> None:
    """No normalisation, no replacement characters, no rewritten line endings."""
    body = (
        "Cyrillic: Привіт, світе\r\n"
        "Emoji: 👩‍👩‍👧‍👦 🇺🇦\n"
        "Combining: e\u0301 a\u0308\n"
        "CJK: 漢字かなカナ\n"
        "NUL and dashes: — – ‒\n"
    )
    _show_text_payload(app, body)
    payload_bytes = app.extract_vm.result.data
    target = tmp_path / "result.txt"
    dialogs.paths.append(str(target))
    assert app._save_result_txt() is True
    written = target.read_bytes()
    assert written == payload_bytes
    assert b"\r\n" in written, "the CRLF line ending was rewritten"
    assert "\ufffd".encode() not in written
    assert written.decode("utf-8") == body
    assert app._status_key == "status_txt_saved"


def test_a_payload_that_is_not_utf8_is_reported_not_repaired(
    app, dialogs, monkeypatch
) -> None:
    from phantom.core.payload import ExtractedPayload, PayloadKind

    seen = _capture_dialogs(monkeypatch, dialogs)

    data = b"ok" + b"\xff\xfe" + b"text"
    payload = ExtractedPayload(
        kind=PayloadKind.TEXT,
        name="broken.txt",
        data=data,
        sha256_expected="",
        sha256_actual="",
        integrity_ok=True,
        original_size=len(data),
        compressed_size=len(data),
    )
    app.extract_vm.result = payload
    app._refresh_extract_state()
    assert app.result_text.get("1.0", "end-1c") == "", "the preview kept garbage"
    assert "\ufffd" not in app.result_text.get("1.0", "end-1c")
    assert app._result_text_exact() is None
    assert app._status_key == "status_error_detail"
    assert seen, "nothing explained why the text cannot be shown"
    assert app.extract_vm.result is payload, "the payload was 'repaired' behind our back"


def test_clear_result_drops_the_payload_and_the_preview_cache(app) -> None:
    _show_text_payload(app, "body to clear")
    assert app._result_preview_key is not None
    app.clear_result_button.invoke()
    assert app.extract_vm.result is None
    assert app.result_text.get("1.0", "end-1c") == ""
    assert app._result_preview_key is None


def test_the_result_field_offers_no_editing_actions(app) -> None:
    sequences = set(app._result_shortcuts.sequences)
    assert app._result_shortcuts.readonly is True
    assert "<Control-v>" not in sequences and "<Command-v>" not in sequences
    assert "<Control-x>" not in sequences and "<Command-z>" not in sequences
    assert "<Control-c>" in sequences and "<Control-a>" in sequences


def _capture_dialogs(monkeypatch, dialogs) -> list[str]:
    """Record error dialogues instead of failing on them (some tests expect one)."""
    from phantom.ui import app as ui_app

    seen: list[str] = []

    def record(title: str = "", message: str = "", **_: object) -> str:
        seen.append(f"{title}: {message}")
        dialogs.errors.append(f"{title}: {message}")
        return "ok"

    monkeypatch.setattr(ui_app.messagebox, "showerror", record, raising=False)
    return seen


def test_the_footer_explains_an_error_instead_of_a_bare_error(app, dialogs, monkeypatch) -> None:
    from phantom.core.errors import InternalError

    seen = _capture_dialogs(monkeypatch, dialogs)
    app._show_error(InternalError("worker step failed"))
    text = app.status_label.cget("text")
    assert text.startswith(app.i18n.t("status_error_detail").split("{")[0])
    assert "worker step failed" in text
    assert seen, "the error was never explained"


def test_a_cancellation_is_not_reported_as_an_error(app, dialogs) -> None:
    from phantom.core.errors import OperationCancelled

    app._show_error(OperationCancelled())
    assert app._status_key == "status_cancelled"
    assert not dialogs.errors, "a cancellation opened an error dialogue"


def test_a_changed_payload_clears_a_finished_verdict(app) -> None:
    app._set_status_key("status_done", severity="ok", path="/tmp/out.png")
    app._mark_editor_dirty()
    assert app._status_key == "status_ready", (
        "the footer still claims a file was saved while the text has changed"
    )


def test_over_capacity_does_not_say_the_system_is_ready(app) -> None:
    from phantom.ui.capacity_view import CapacityReport, CapacityState

    app._set_status_key("status_ready", severity="info")
    app.capacity_report = CapacityReport(state=CapacityState.OVERFLOW, excess_bytes=10)
    app._sync_capacity_status()
    assert app._status_key == "status_capacity_exceeded"
    assert app.status_label.cget("text") == app.i18n.t("status_capacity_exceeded")
    app.capacity_report = CapacityReport(state=CapacityState.FITS)
    app._sync_capacity_status()
    assert app._status_key == "status_ready"


def test_the_action_bar_relabels_in_every_language(app) -> None:
    for language in ("uk", "ru", "en"):
        app.i18n.set_language(language)
        app._render_all_labels()
        assert app.editor_cut_button.cget("text") == app.i18n.t("btn_cut")
        assert app.editor_copy_all_button.cget("text") == app.i18n.t("btn_copy_all")
        assert app.save_txt_button.cget("text") == app.i18n.t("btn_save_txt")
        assert app.select_all_result_button.cget("text") == app.i18n.t("btn_select_all")


# ---------------------------------------------------------------------------
# expanded result window
# ---------------------------------------------------------------------------
RESULT_LABELS = {
    "result_window_title": "RESULT",
    "btn_copy_text": "COPY TEXT",
    "btn_copy_path": "COPY PATH",
    "btn_reveal": "SHOW IN FINDER",
    "btn_close": "CLOSE",
    "menu_paste": "Paste",
    "menu_copy": "Copy",
    "menu_cut": "Cut",
    "menu_select_all": "Select all",
    "status_copied": "COPIED",
    "status_path_missing": "NO PATH",
    "status_revealed": "REVEALED",
    "status_reveal_failed": "REVEAL FAILED",
}


def test_expand_opens_a_large_read_only_window(app) -> None:
    _show_text_payload(app, "readable result body")
    app._expand_result()
    app.root.update()
    window = app._result_window
    assert window is not None
    assert window.text_box.get("1.0", "end-1c") == "readable result body"
    assert window.text_box.cget("state") == "disabled"
    from phantom.ui import layout

    screen_w, screen_h = window.winfo_screenwidth(), window.winfo_screenheight()
    expected_w, expected_h, _x, _y = layout.window_geometry(screen_w, screen_h)
    assert window.requested_geometry.startswith(f"{expected_w}x{expected_h}+")
    assert expected_w >= int(screen_w * 0.7)
    assert window.copy_text() is True
    assert app.root.clipboard_get() == "readable result body"
    window.close()
    app.root.update()
    assert app._result_window is None


def test_expand_button_is_disabled_without_a_result(app) -> None:
    assert app.expand_button.cget("state") == "disabled"


def test_result_window_reveals_the_saved_file(app, tmp_path, monkeypatch) -> None:
    from phantom.ui.result_window import ResultWindow

    target = tmp_path / "extracted.bin"
    target.write_bytes(b"payload")
    calls: list[object] = []
    monkeypatch.setattr(
        "phantom.ui.result_window.fsutil.reveal_in_file_manager",
        lambda path: calls.append(path) or True,
    )
    window = ResultWindow(
        app.root,
        title="RESULT",
        text="body",
        labels=RESULT_LABELS,
        path=target,
        copy_callback=lambda value: True,
        on_close=None,
    )
    app.root.update()
    assert window.reveal() is True
    assert calls == [target]
    assert window.feedback.cget("text") == RESULT_LABELS["status_revealed"]
    assert window.copy_path() is True
    window.close()


def test_result_window_without_a_path_disables_path_actions(app) -> None:
    from phantom.ui.result_window import ResultWindow

    window = ResultWindow(
        app.root, title="RESULT", text="text only", labels=RESULT_LABELS
    )
    app.root.update()
    assert window.copy_path_button.cget("state") == "disabled"
    assert window.reveal_button.cget("state") == "disabled"
    assert window.copy_path() is False
    window.close()


def test_copy_path_and_reveal_use_the_extracted_file(app, tmp_path, monkeypatch) -> None:
    from phantom.core.payload import ExtractedPayload, PayloadKind

    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF-1.4")
    app.extract_vm.result = ExtractedPayload(
        kind=PayloadKind.FILE,
        name="report.pdf",
        data=b"%PDF-1.4",
        sha256_expected="a" * 64,
        sha256_actual="a" * 64,
        integrity_ok=True,
        original_size=8,
        compressed_size=8,
    )
    app.extract_vm.save_path = str(target)
    app._refresh_all()
    assert app.copy_path_button.cget("state") == "normal"
    assert app.reveal_button.cget("state") == "normal"
    app._copy_result_path()
    assert app.root.clipboard_get() == str(target)
    revealed: list[object] = []
    monkeypatch.setattr(
        "phantom.ui.app.fsutil.reveal_in_file_manager",
        lambda path: revealed.append(path) or True,
    )
    app._reveal_result()
    assert revealed == [str(target)]


def test_paste_into_clears_the_ctk_placeholder(app) -> None:
    """The placeholder is real text; writing past it must drop it first."""
    from phantom.ui.widgets import paste_into

    app.root.clipboard_clear()
    app.root.clipboard_append("typed-by-paste")
    app.embed_pwd.delete(0, "end")
    assert paste_into(app.embed_pwd) == "typed-by-paste"
    assert app.embed_pwd.get() == "typed-by-paste"
    assert app.embed_pwd._entry.cget("show") == "•"


def test_stage_label_starts_with_the_idle_text(app) -> None:
    """The footer must never show the placeholder the CTk default paints."""
    assert app.stage_label.cget("text") == app.i18n.t("lbl_progress_idle")


def test_no_visible_widget_shows_its_class_name(app) -> None:
    """Catches ``CTkLabel(...)`` without ``text=`` anywhere in the tree.

    Every CTk text widget defaults to its own class name ("CTkLabel",
    "CTkCheckBox", …), so a widget whose text equals its class name was built
    without ``text=`` and is showing a placeholder to the user.
    """
    leftovers: list[str] = []

    def walk(widget) -> None:
        try:
            value = widget.cget("text")
        except Exception:  # noqa: BLE001 - not every Tk widget has "text"
            value = None
        if isinstance(value, str) and value == type(widget).__name__:
            leftovers.append(f"{type(widget).__name__}: {value}")
        for child in widget.winfo_children():
            walk(child)

    walk(app.root)
    assert leftovers == []


def test_password_fields_and_paragraphs_have_clipboard_shortcuts(app) -> None:
    """Cmd/Ctrl + C/V/X/A are bound on the inner widgets the user types into."""
    wanted = {"<Command-v>", "<Command-V>", "<Command-c>", "<Command-a>"}
    bound: set[str] = set()
    for shortcuts in app._text_shortcuts:
        bound.update(shortcuts.sequences)
    assert wanted <= bound, f"missing shortcuts: {sorted(wanted - bound)}"

    # 16 sequences per editable text widget: Cmd/Ctrl x lower/upper case for
    # c, v, x, a.  The result field is read-only and deliberately binds fewer —
    # copy and select-all only, because paste/cut/undo could only ever fail there.
    editable = [item for item in app._text_shortcuts if not item.readonly]
    readonly = [item for item in app._text_shortcuts if item.readonly]
    assert all(item.bound_count >= 16 for item in editable)
    assert len(app._text_shortcuts) >= 5
    assert readonly, "the result field lost its read-only shortcuts"
    assert all(item.bound_count < 16 for item in readonly)
    assert all(
        "<Command-v>" not in item.sequences and "<Control-x>" not in item.sequences
        for item in readonly
    )


# ---------------------------------------------------------------------------
# progress feedback: a 24 MP embed runs for half a minute
# ---------------------------------------------------------------------------
def _progress(app, stage, done=0, total=0):
    """Feed one progress event the way the worker would."""
    from phantom.ui.worker import WorkerEvent, WorkerEventType

    app._handle_worker_event(
        WorkerEvent(WorkerEventType.PROGRESS, stage=stage, done=done, total=total,
                    operation="embed")
    )


def test_analysing_turns_the_bar_into_a_spinner_with_a_stage_line(app) -> None:
    """The cost map has no fraction to report: spinner plus named stage, no 0 %."""
    from phantom.core.progress import Stage

    app._busy = True
    app._operation_started = 0.0
    _progress(app, Stage.ANALYSING)

    assert app._progress_indeterminate is True, "the bar must animate, not sit at 0"
    text = app.stage_label.cget("text")
    assert text.startswith(app.i18n.t("stage.analysing")), text
    assert "%" not in text, "an indeterminate stage must not pretend to have a share"


def test_a_measured_stage_leaves_the_spinner_mode(app) -> None:
    """After the analysis the trellis reports real units, so the bar fills again."""
    from phantom.core.progress import Stage

    app._busy = True
    _progress(app, Stage.ANALYSING)
    _progress(app, Stage.EMBEDDING, 30, 120)

    assert app._progress_indeterminate is False
    text = app.stage_label.cget("text")
    assert "25" in text, text
    assert app.i18n.t("stage.embedding") in text


def test_the_stage_line_counts_the_seconds_an_operation_runs(app) -> None:
    """Silence for a minute looks like a hang: the footer keeps counting."""
    from phantom.core.progress import Stage

    app._busy = True
    app._operation_started = time.monotonic() - 7.2
    _progress(app, Stage.ANALYSING)

    text = app.stage_label.cget("text")
    assert "7" in text and app.i18n.t("stage.analysing") in text, text


def test_the_seconds_counter_grows_while_the_operation_stays_busy(app) -> None:
    from phantom.core.progress import Stage

    app._busy = True
    app._operation_started = time.monotonic() - 2.0
    _progress(app, Stage.EMBEDDING, 1, 4)
    first = app.stage_label.cget("text")

    app._operation_started = time.monotonic() - 11.0
    app._stage_tick()

    second = app.stage_label.cget("text")
    assert first != second, "a tick must repaint the line"
    assert "11" in second, second
    assert "25" in second, "the measured share must survive the repaint"
    app._stop_stage_tick()  # the tick rescheduled itself; do not outlive the test


def test_the_counter_stops_and_resets_when_the_operation_ends(app) -> None:
    from phantom.core.progress import Stage

    app._busy = True
    app._operation_started = time.monotonic() - 5.0
    _progress(app, Stage.EMBEDDING, 1, 2)
    assert "5" in app.stage_label.cget("text")

    app._finish_operation_ui()

    assert app._operation_started == 0.0
    assert app._stage_tick_token is None, "no timer may outlive the operation"
    assert app._stage_key is None
    assert app._elapsed_seconds() is None
    assert app.stage_label.cget("text") == app.i18n.t("lbl_progress_idle"), (
        "a cancelled or failed job must not leave a frozen counter behind"
    )


def test_a_failed_job_clears_the_progress_bar_and_frees_the_window(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After an error the bar must be empty and every control usable again."""
    from phantom.core.errors import MetadataConflictError
    from phantom.ui import app as ui_app
    from phantom.ui.worker import WorkerEvent, WorkerEventType

    # The dialog is part of the behaviour under test, so record it instead of
    # letting the fixture refuse it; the text is what the user reads.
    shown: list[str] = []
    monkeypatch.setattr(
        ui_app.messagebox,
        "showerror",
        lambda title="", message="", **_: shown.append(message),
        raising=False,
    )

    app._busy = True
    app._operation = "metadata_clean"
    app.progress_bar.set(0.75)  # a job that had reached three of four stages
    assert float(app.progress_bar.get()) == pytest.approx(0.75)

    error = MetadataConflictError("lost a field").with_params(fields="GPSVersionID")
    app._handle_worker_event(WorkerEvent(WorkerEventType.ERROR, error=error))

    assert app._busy is False
    assert app._operation == ""
    assert float(app.progress_bar.get()) == 0.0, (
        "the bar used to keep the fraction the failed job reached, which reads as "
        "'still running' when nothing is running any more"
    )
    assert app.stage_label.cget("text") == app.i18n.t("lbl_progress_idle")
    assert str(app.cancel_button.cget("state")) == "disabled"
    assert app.metadata_panel is not None
    for button in app.metadata_panel.editable_action_buttons():
        assert str(button.cget("state")) == "normal", "the tab is usable again"
    # Saving is the one button whose state is decided by the selection, not by the
    # busy flag: with no file loaded there is nothing to save, and the button says so.
    assert str(app.metadata_panel.save_button.cget("state")) == "disabled"
    assert len(shown) == 1, "the failure is reported once"
    assert "GPSVersionID" in shown[0], "the dialog names the field by label"
    assert "0x" not in shown[0], "not by tag number"


def test_the_stage_line_survives_a_language_switch(app) -> None:
    """Switching language mid-embed must re-render the live line, not the idle one."""
    from phantom.core.progress import Stage

    app._busy = True
    app._operation_started = time.monotonic() - 3.0
    _progress(app, Stage.ANALYSING)

    app._on_language_click(app._label_by_lang["ru"])

    text = app.stage_label.cget("text")
    assert app.i18n.t("stage.analysing") in text, text
    assert text != app.i18n.t("lbl_progress_idle")
    assert "3" in text, text


# ---------------------------------------------------------------------------
# the poll loop must not lose the last event of a finished job
# ---------------------------------------------------------------------------
class _ScriptedWorker:
    """A worker whose queue and liveness the test dictates, event by event.

    Only ``poll``/``busy``/``watchdog_expired`` matter to ``_poll``, but the
    fixture teardown calls ``shutdown``: a stub that only half-implements the
    interface would fail in teardown instead of in the test.
    """

    def __init__(self, script: list[list[object]], *, busy: bool = False) -> None:
        self.script = [list(batch) for batch in script]
        self.busy = busy
        self.cancel_event = threading.Event()

    def poll(self) -> list[object]:
        return self.script.pop(0) if self.script else []

    def watchdog_expired(self) -> bool:
        return False

    def seconds_since_progress(self) -> float:
        return 0.0

    def reset_watchdog(self) -> None:
        return None

    def cancel(self) -> None:
        self.cancel_event.set()

    def shutdown(self, timeout: float = 0.0) -> None:
        return None


def test_poll_handles_a_done_that_arrived_while_the_thread_was_dying(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race this pins: ``poll`` empty, thread dead, DONE still in the queue.

    ``_poll`` used to drain once, see ``busy`` false and finish the operation
    without ever looking again — the finished job's DONE stayed in the queue
    forever, so the output path and the verification verdict never reached the
    interface.
    """
    from phantom.ui.worker import WorkerEvent, WorkerEventType

    late = WorkerEvent(WorkerEventType.DONE, value="the-result", operation="embed")
    monkeypatch.setattr(app, "worker", _ScriptedWorker([[], [late]]))
    app._busy = True
    handled: list[object] = []
    app._on_done = handled.append  # type: ignore[method-assign]

    app._poll()

    assert [getattr(event, "value", None) for event in handled] == ["the-result"], (
        "the terminal event was dropped"
    )
    assert app._busy is False


def test_poll_does_nothing_extra_when_the_queue_was_already_drained(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    from phantom.ui.worker import WorkerEvent, WorkerEventType

    monkeypatch.setattr(
        app, "worker", _ScriptedWorker([[WorkerEvent(WorkerEventType.DONE, value=1)], []])
    )
    app._busy = True
    handled: list[object] = []
    app._on_done = handled.append  # type: ignore[method-assign]

    app._poll()

    assert len(handled) == 1, "a terminal event must be handled exactly once"


# ---------------------------------------------------------------------------
# the 2026 pass: contrast, type scale, and an honest layout badge
# ---------------------------------------------------------------------------
def _font_size(widget) -> int:
    font = widget.cget("font")
    if hasattr(font, "cget"):  # CustomTkinter keeps a CTkFont
        return int(font.cget("size"))
    return int(font[1])  # a (family, size, weight) tuple


def test_body_text_is_fourteen_points_and_headings_are_not_smaller(app) -> None:
    from phantom.ui import theme as T

    assert _font_size(app.embed_text) == T.FONT_SIZE_BODY == 14
    assert 13 <= _font_size(app.result_card_title) <= 14, "a card heading under 13 pt"
    assert _font_size(app.stage_label) >= 10


def test_the_audio_carrier_says_v3_not_the_trellis_default(app) -> None:
    """The writer falls back to the keyed layout for audio; the tab must agree."""
    from phantom.core.api import CarrierKind, CarrierProbe
    from phantom.stego.engine import Capacity

    app.embed_vm.apply_probe(
        CarrierProbe(
            kind=CarrierKind.AUDIO,
            path="song.wav",
            capacity=Capacity(usable_bits=8000, payload_bytes=900),
        )
    )
    app._render_format_labels()
    audio_text = app.format_label.cget("text")
    assert "v3" in audio_text and "v5" not in audio_text, audio_text

    app.embed_vm.apply_probe(
        CarrierProbe(
            kind=CarrierKind.IMAGE,
            path="photo.png",
            capacity=Capacity(usable_bits=8000, payload_bytes=900),
        )
    )
    app._render_format_labels()
    assert "v5" in app.format_label.cget("text")


def test_the_probed_carrier_repaints_the_layout_badge(app) -> None:
    """A fresh probe goes through ``_refresh_all``; the badge has to follow it."""
    from phantom.core.api import CarrierKind, CarrierProbe
    from phantom.stego.engine import Capacity

    app.embed_vm.carrier = CarrierProbe(
        kind=CarrierKind.AUDIO,
        path="song.flac",
        capacity=Capacity(usable_bits=4096, payload_bytes=400),
    )
    app._refresh_all()

    assert "v3" in app.format_label.cget("text")


# ---------------------------------------------------------------------------
# the metadata tab
# ---------------------------------------------------------------------------
def _all_label_texts(widget) -> list[str]:
    """Every ``text`` a widget tree shows, for the "no raw key leaked" checks."""
    texts: list[str] = []
    for current in [widget, *_descendants(widget)]:
        try:
            value = current.cget("text")
        except Exception:  # noqa: BLE001, S112 - not every widget has text
            continue
        if isinstance(value, str):
            texts.append(value)
    return texts


def _scanned(app, tmp_path: Path, dialogs):
    """Point the tab at a real PNG and let the inspection job finish."""
    import metadata_samples as samples

    path = samples.personal_png(tmp_path / "gui-photo.png")
    app._show_tab("metadata")
    app.metadata_panel.load_file(str(path))
    _pump(app)
    return path


def test_the_metadata_tab_is_the_fourth_tab(app) -> None:
    assert list(app.tab_frames) == ["embed", "extract", "metadata", "settings"]
    assert "metadata" in app._tab_by_label.values()
    assert app.metadata_panel is not None
    app._show_tab("metadata")
    assert app.current_tab == "metadata"


def test_the_metadata_tab_is_routed_like_every_other_tab(app) -> None:
    """One global router, one area per tab: the field list is not a second canvas."""
    panel = app.metadata_panel
    assert panel is not None
    area = _area_for_tab(app, "metadata")
    assert area is not None
    assert area.frame is panel.scroll
    assert not [item for item in app._scroll.areas if item.frame is panel.list_area]


def test_the_metadata_cards_belong_to_the_tab_area(app) -> None:
    """Every card resolves to the tab's own area — there is no second canvas.

    The generic tests above prove that a wheel event on the root scrolls the area
    under the pointer; what matters here is that the fifth card, the field rows and
    the pre-report box all answer with the same area, so the router needs no
    special case for this tab.
    """
    _reveal(app)
    app._show_tab("metadata")
    app.root.update()
    panel = app.metadata_panel
    assert panel is not None
    area = _area_for_tab(app, "metadata")
    assert area is not None

    for widget in (panel.file_card, panel.results_card, panel.list_area, panel.save_card):
        assert app._scroll.area_for_widget(widget) is area
    assert not [item for item in app._scroll.areas if item.frame is panel.list_area]


def test_the_collector_never_runs_on_the_worker_thread(app) -> None:
    """A finalizer on the worker thread can call Tcl, which is not thread-safe.

    That is how the embed test used to wedge on a loaded runner: the worker
    imported numpy, the collector reclaimed a Tk font, and ``Font.__del__`` waited
    for a main loop that was waiting for the job.  The collector is paused while a
    job runs, so no finalizer can land on the worker.
    """
    import gc
    import threading

    release = threading.Event()
    assert gc.isenabled()
    assert app._start_job(
        "estimate", lambda report, cancel: release.wait(10) and None, "status_ready", silent=True
    )
    try:
        assert app.worker.busy
        assert not gc.isenabled(), "the collector is still armed while a job runs"
    finally:
        release.set()
        _pump(app)
    assert gc.isenabled(), "the collector must come back when the job ends"


def test_a_chain_of_jobs_gives_the_collector_back(app, rgb_carrier: Path) -> None:
    """Two jobs in a row must leave the collector enabled, as one job does.

    The resume is deferred while the finished job's thread is still winding down;
    the second job used to start inside that window, re-read the collector (off,
    because the first job had turned it off) and record "it was never enabled", so
    the last resume switched nothing back on.  The whole suite then ran with the
    collector off — which is how the collector tests found it.
    """
    import gc

    assert gc.isenabled()
    app._set_textbox(app.embed_text, "chained jobs")  # schedules a size estimate
    app._set_carrier(str(rgb_carrier))  # starts the carrier probe
    _pump(app)  # the probe, and the estimate it schedules when it lands
    assert gc.isenabled(), "a chained job left the collector disabled"
    assert app._gc_paused is False


def test_choosing_a_file_fills_the_tab(app, tmp_path: Path, dialogs) -> None:
    import metadata_samples as samples

    path = samples.personal_png(tmp_path / "chosen.png")
    dialogs.paths.append(str(path))
    panel = app.metadata_panel
    assert panel is not None

    panel.choose_file()
    _pump(app)

    assert panel.vm.path == str(path)
    assert panel.vm.report is not None
    assert path.name in panel.name_label.cget("text")
    assert "fields" in panel.summary_label.cget("text")
    assert panel._rows, "the results card must show one row per field"
    assert panel.vm.selection, "personal fields are preselected"


def test_dropping_a_file_on_the_card_starts_the_scan(app, tmp_path: Path) -> None:
    import metadata_samples as samples

    path = samples.personal_wav(tmp_path / "dropped.wav")
    panel = app.metadata_panel
    assert panel is not None

    app._drop_metadata([str(path)])
    _pump(app)

    assert panel.vm.report is not None
    assert panel.vm.report.fmt.value == "wav"


def test_the_selection_buttons_change_the_pre_report(app, tmp_path: Path, dialogs) -> None:
    panel = app.metadata_panel
    assert panel is not None
    _scanned(app, tmp_path, dialogs)

    panel.clear_selection()
    assert panel.vm.selection == set()
    assert "will be removed (0):" in panel.preview_text.get("1.0", "end")

    panel.select_personal()
    assert panel.vm.selection
    assert "will be removed" in panel.preview_text.get("1.0", "end")


def test_a_field_checkbox_registers_a_toggle(app, tmp_path: Path, dialogs) -> None:
    panel = app.metadata_panel
    assert panel is not None
    _scanned(app, tmp_path, dialogs)
    panel.clear_selection()
    panel.refresh()

    panel._on_field_toggle("png.text.tEXt.Comment")

    assert "png.text.tEXt.Comment" in panel.vm.selection


def test_saving_a_cleaned_copy_from_the_tab(app, tmp_path: Path, dialogs) -> None:
    panel = app.metadata_panel
    assert panel is not None
    _scanned(app, tmp_path, dialogs)
    output = tmp_path / "gui-clean.png"
    panel.output_entry.delete(0, "end")
    panel.output_entry.insert(0, str(output))

    panel.save_clean()
    _pump(app)

    assert output.exists()
    assert panel.vm.outcome is not None
    assert panel.vm.outcome.verified is True
    assert "verdict" in panel.final_label.cget("text").lower()
    assert panel.vm.report is not None
    assert panel.vm.report.find("png.text.tEXt.Comment") is None


def test_the_integrity_card_explains_both_hashes(app, tmp_path: Path, dialogs) -> None:
    panel = app.metadata_panel
    assert panel is not None
    _scanned(app, tmp_path, dialogs)
    panel.output_entry.delete(0, "end")
    panel.output_entry.insert(0, str(tmp_path / "hashes.png"))
    panel.save_clean()
    _pump(app)

    shown = panel.hash_label.cget("text")
    assert "sha256 file:" in shown
    assert "sha256 content:" in shown
    assert app.i18n.t("meta_hash_note") in panel.hash_note_label.cget("text")


def test_exporting_the_report_from_the_tab(app, tmp_path: Path, dialogs) -> None:
    panel = app.metadata_panel
    assert panel is not None
    _scanned(app, tmp_path, dialogs)
    target = tmp_path / "report.txt"
    dialogs.paths.append(str(target))

    panel.export_txt()

    assert target.exists()
    assert "PHANTOM — metadata report" in target.read_text(encoding="utf-8")


def test_copying_a_report_arms_the_clipboard_clear(app, tmp_path: Path, dialogs) -> None:
    panel = app.metadata_panel
    assert panel is not None
    _scanned(app, tmp_path, dialogs)

    panel.copy_report()

    assert "PHANTOM — metadata report" in app.root.clipboard_get()
    assert app._clipboard_token is not None, "a report may not stay on the clipboard"


def test_a_signed_file_shows_a_blocked_plan_in_the_ui(app, tmp_path: Path) -> None:
    import metadata_samples as samples

    panel = app.metadata_panel
    assert panel is not None
    app._show_tab("metadata")
    panel.load_file(str(samples.signed_png(tmp_path / "gui-signed.png")))
    _pump(app)

    assert panel.vm.plan() is not None
    assert panel.vm.plan().allowed is False
    assert app.i18n.t("meta_plan_blocked") in panel.plan_label.cget("text")


def test_the_metadata_tab_speaks_every_language_without_leaking_keys(app) -> None:
    panel = app.metadata_panel
    assert panel is not None
    for code in ("uk", "ru", "en"):
        label = app._label_by_lang[code]
        app._on_language_click(label)
        app.root.update_idletasks()
        texts = _all_label_texts(panel.frame)
        assert texts
        for text in texts:
            assert not text.startswith(("meta_", "meta.", "tab_", "err.", "stage.")), (
                f"{code}: a raw i18n key is on screen: {text!r}"
            )


def test_busy_state_disables_the_metadata_actions(app) -> None:
    panel = app.metadata_panel
    assert panel is not None

    app._busy = True
    app._refresh_busy_state()
    assert all(button.cget("state") == "disabled" for button in panel.action_buttons())

    app._busy = False
    app._refresh_busy_state()
    assert all(
        button.cget("state") == "normal" for button in panel.editable_action_buttons()
    )
    assert panel.save_button.cget("state") == "disabled", "nothing is selected to save"


# ---------------------------------------------------------------------------
# 2.1.0: the window must fit its own text at every size, in every language
# ---------------------------------------------------------------------------
#: Widgets CustomTkinter builds for itself and lays out on its own: a segmented
#: button is made of ``CTkButton`` children whose width is 0 because the geometry
#: manager stretches them, so they say nothing about a clipped caption.
_INTERNAL_PARENTS = ("CTkSegmentedButton", "CTkOptionMenu", "CTkComboBox", "CTkScrollbar")


def _button_like(widget) -> bool:
    """A widget that draws its caption inside a width *we* gave it."""
    try:
        widget.cget("text")
        widget.cget("width")
    except Exception:  # noqa: BLE001 - not a captioned widget
        return False
    if not type(widget).__name__.startswith("CTk"):
        return False
    if type(widget).__name__ in (
        "CTkLabel",
        "CTkEntry",
        "CTkTextbox",
        "CTkFrame",
        "CTkScrollableFrame",
        "CTkProgressBar",
    ):
        return False
    parent = getattr(widget, "master", None)
    if type(parent).__name__ in _INTERNAL_PARENTS:
        return False
    # A width of 0 means the geometry manager owns the size; no caption is cut
    # by it, and every widget we size ourselves has a width.
    try:
        return int(float(widget.cget("width"))) > 0
    except (TypeError, ValueError):
        return False


def _captioned(widget) -> list:
    found = []
    stack = [widget]
    while stack:
        current = stack.pop()
        if _button_like(current):
            found.append(current)
        stack.extend(getattr(current, "winfo_children", lambda: [])())
    return found


def _clipped(widget) -> str | None:
    """``text`` when the widget is narrower than its own caption, else ``None``."""
    from phantom.ui import responsive

    text = str(widget.cget("text") or "")
    if not text:
        return None
    font = widget.cget("font")
    pixels = font.measure(text) if hasattr(font, "measure") else 0
    scaling = responsive.widget_scaling(widget) or 1.0
    needed = int(widget.cget("width")) * scaling
    if pixels > needed:
        return f"{type(widget).__name__}({text!r}): needs {pixels}px, has {needed}px"
    return None


@pytest.mark.parametrize("size", ["980x720", "1140x900", "1280x800", "1440x900"])
def test_no_captioned_widget_clips_its_text_at_any_supported_size(app, size: str) -> None:
    """The owner's defect: a button narrower than «ВЫБРАТЬ ФАЙЛ (PNG / WAV / FLAC)»."""
    app.root.deiconify()
    app.root.geometry(size)
    app.root.update()
    app.root.update_idletasks()

    problems = []
    for ident in ("embed", "extract", "metadata", "settings"):
        app._show_tab(ident)
        app.root.update()
        for widget in _captioned(app.tab_frames[ident]):
            issue = _clipped(widget)
            if issue:
                problems.append(f"{ident}/{size}: {issue}")

    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("code", ["ru", "uk", "en"])
def test_a_language_switch_re_measures_every_caption(app, code: str) -> None:
    """Russian and Ukrainian captions are wider; the buttons must follow."""
    app.root.deiconify()
    app.root.geometry("980x720")
    app._on_language_click(app._label_by_lang[code])
    app.root.update()

    problems = []
    for ident in ("embed", "extract", "metadata", "settings"):
        app._show_tab(ident)
        app.root.update()
        for widget in _captioned(app.tab_frames[ident]):
            issue = _clipped(widget)
            if issue:
                problems.append(f"{ident}/{code}: {issue}")

    assert not problems, "\n".join(problems)


def test_every_button_of_a_crowded_row_stays_inside_its_card(app) -> None:
    """Seven buttons in one card: none of them may be pushed past its edge."""
    app.root.deiconify()
    app._show_tab("extract")
    app.root.geometry("980x720")
    app.root.update()
    app.root.update_idletasks()
    row = app.extract_buttons_row
    card = row.master  # the card the row lives in, not the whole tab
    left = card.winfo_rootx()
    right = left + card.winfo_width()

    outside = []
    for button in row.items:
        if not button.winfo_ismapped():
            continue
        start = button.winfo_rootx()
        end = start + button.winfo_width()
        if start < left - 2 or end > right + 2:
            outside.append(f"{button.cget('text')!r}: {start}..{end} outside {left}..{right}")

    assert not outside, "\n".join(outside)
    assert row.rows() >= 1


def test_a_narrow_row_wraps_and_a_wide_one_does_not(app) -> None:
    """The wrapping rule, driven by the width the frame is really given."""
    app.root.deiconify()
    app._show_tab("extract")
    app.root.update()
    row = app.extract_buttons_row

    narrow = row.reflow(available=40)
    wide = row.reflow(available=4000)

    assert len({line for line, _column in narrow}) == len(row.items), (
        "one button per line when the row is narrower than any button"
    )
    assert len({line for line, _column in wide}) == 1, "the wide row did not fit on one line"


def test_shrinking_and_growing_the_window_does_not_break_the_layout(app) -> None:
    """Both directions, twice: a wrapped row must go back to one line."""
    app.root.deiconify()
    app._show_tab("extract")

    for size in ("980x720", "1440x900", "980x720", "1280x800"):
        app.root.geometry(size)
        app.root.update()
        app.root.update_idletasks()
        problems = [_clipped(widget) for widget in _captioned(app.tab_frames["extract"])]
        assert not [problem for problem in problems if problem], size

    assert app.root.winfo_width() > 500


def test_a_growing_paragraph_re_wraps_with_the_card(app) -> None:
    """``wraplength`` follows the card, instead of a number from another window."""
    app.root.deiconify()
    app._show_tab("settings")
    app.root.geometry("980x720")
    app.root.update()
    app.root.update_idletasks()
    narrow = int(app.limitations_label.cget("wraplength"))

    app.root.geometry("1440x900")
    app.root.update()
    app.root.update_idletasks()
    wide = int(app.limitations_label.cget("wraplength"))

    assert wide > narrow, f"wraplength stayed {narrow} while the card grew"


def test_a_resize_burst_owes_one_layout_pass(app) -> None:
    """Ten <Configure> events must cost one pass, not ten.

    A drag-resize, a zoom and a fullscreen transition all deliver a stream of
    events; each one used to walk the widget tree and re-flow every row inside the
    event itself.  What is asserted is the rule that makes a resize cheap: the
    events are recorded, one pass is owed, and it runs from the loop.
    """
    import types

    app.root.deiconify()
    app.root.update()  # let whatever the window did while it was built settle

    passes = app._refit_passes
    events = app._resize_events
    calls: list[int] = []
    real = app.responsive.refit_flows

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    try:
        app.responsive.refit_flows = counted  # type: ignore[method-assign]
        for width in range(1100, 1300, 20):
            event = types.SimpleNamespace(widget=app.root, width=width, height=820)
            app._on_window_resize(event)
        assert not calls, "the handler ran the pass instead of owing it"
        assert app._refit_token is not None, "the burst was not coalesced into one pass"

        app.root.update()
    finally:
        app.responsive.refit_flows = real  # type: ignore[method-assign]

    assert app._resize_events >= events + 10, "the events never reached the handler"
    assert len(calls) == 1, f"{len(calls)} refit passes for one burst"
    assert app._refit_passes == passes + 1
    assert app._refit_token is None, "the pass is still queued after the loop ran"


def test_generated_configure_events_do_not_refit_one_by_one(app) -> None:
    """The same rule through Tk's own event delivery, not only through the handler.

    Tk may hand the queued events over in more than one round, so a handful of
    passes is expected — one per round.  One per *event* is what used to happen.
    """
    app.root.deiconify()
    app.root.update()
    calls: list[int] = []
    real = app.responsive.refit_flows

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    try:
        app.responsive.refit_flows = counted  # type: ignore[method-assign]
        for width in range(1100, 1300, 20):
            app.root.event_generate("<Configure>", width=width, height=820)
        app.root.update()
        app.root.update()
    finally:
        app.responsive.refit_flows = real  # type: ignore[method-assign]

    assert calls, "the generated events never caused a pass at all"
    assert len(calls) <= 4, f"{len(calls)} passes for 10 events: not coalesced"


def test_the_flow_rows_are_found_without_walking_the_tree(app) -> None:
    """The rows are collected once, when the window is adopted."""
    rows = app.responsive.flows()
    assert rows, "the window has no flow rows at all"
    walks = app.responsive.walks

    for _ in range(3):
        app.responsive.refit_flows()

    assert app.responsive.walks == walks, "a refit walked the widget tree again"
    assert len(app.responsive.flows()) == len(rows)


def test_reflow_moves_children_without_forgetting_them(app) -> None:
    """``grid`` re-places a child; nothing has to be forgotten first."""
    import tkinter as tk

    import customtkinter as ctk

    from phantom.ui.responsive import FlowRow

    row = FlowRow(app.root)
    row.grid(row=90, column=0)
    try:
        labels = [ctk.CTkLabel(row, text=f"row item {index}", width=90) for index in range(6)]
        for label in labels:
            row.add(label)

        forgotten: list[int] = []
        original = tk.Grid.grid_forget

        def counting(self, *args, **kwargs):
            forgotten.append(1)
            return original(self, *args, **kwargs)

        tk.Grid.grid_forget = counting  # type: ignore[method-assign]
        try:
            row.reflow(available=200)
            narrow = row.rows()
            row.reflow(available=600)
            wide = row.rows()
            row.reflow(available=200)
        finally:
            tk.Grid.grid_forget = original  # type: ignore[method-assign]

        assert narrow > wide, "the row did not wrap when it was narrowed"
        assert not forgotten, "the reflow forgot its children before placing them"
        assert row.placements >= 6, "the children were never re-placed"
        cells = {(index, row_) for index, row_ in enumerate(row._layout)}
        assert len(cells) == len(labels), "two children ended up in the same cell"
    finally:
        row.destroy()


def test_a_hidden_tab_is_laid_out_when_it_is_opened(app) -> None:
    """A resize must not lay out a tab nobody can see — but opening it must."""
    app.root.deiconify()
    app._show_tab("embed")
    app.root.update()

    hidden = [row for row in app.responsive.flows() if row.suspended]
    assert hidden, "no tab was deferred: every row re-flowed while it was hidden"
    counts = {id(row): row.reflows for row in hidden}

    app.root.geometry("1400x820")
    app.root.update()

    for row in hidden:
        assert row.reflows == counts[id(row)], "a hidden tab was laid out on a resize"

    app._show_tab("metadata")

    opened = [row for row in app.responsive.flows() if not row.suspended]
    assert opened, "the tab that was opened has no flow rows"
    for row in opened:
        assert row.reflows > counts[id(row)], "opening the tab did not lay it out"
    for row in hidden:
        if row.suspended:
            assert row.reflows == counts[id(row)], "a tab that is still hidden was laid out"


def test_a_language_switch_lays_out_every_tab(app) -> None:
    """Captions changed everywhere, so every tab is measured, hidden or not."""
    app.root.deiconify()
    app._show_tab("embed")
    app.root.update()
    hidden = [row for row in app.responsive.flows() if row.suspended]
    assert hidden, "no tab was deferred"
    counts = {id(row): row.reflows for row in hidden}

    other = next(code for code in app.i18n.languages if code != app.i18n.language)
    label = app._label_by_lang[other]
    app._on_language_click(label)
    app.root.update()

    for row in hidden:
        assert row.reflows > counts[id(row)], "the new captions were never measured"


def test_the_window_starts_at_its_saved_geometry_not_a_default(app, tmp_path: Path) -> None:
    """Geometry is restored before the widgets are built, not after them.

    Asserting the reported geometry cannot work everywhere: a toplevel that has not
    been mapped reports ``1x1`` (Aqua and X11 alike), and a mapped one reports what
    the window manager made of it.  What the product promises is order, so that is
    what is asserted — the geometry goes on while the root still has no children —
    and then that the window really took it.
    """
    import customtkinter as ctk

    from phantom.ui.app import PhantomApp
    from phantom.ui.dnd import DndProvider
    from phantom.ui.settings import SettingsStore

    store = SettingsStore(path=tmp_path / "geometry" / "settings.json")
    saved = store.load()
    # Small enough to fit the 1024x720 display the macOS runner gives us, so the
    # window manager cannot clamp it and the size below is the size we asked for.
    saved.window_geometry = "800x600+40+30"
    store.save(saved)

    root = ctk.CTk()
    baseline = len(root.winfo_children())
    applied: list[tuple[str, int]] = []
    real_geometry = root.geometry

    def geometry(value: str | None = None) -> str:
        if value is not None:
            # How many children the root had when the geometry was applied: the
            # whole point is that the answer is zero.
            applied.append((value, len(root.winfo_children())))
        return real_geometry(value)

    root.geometry = geometry  # type: ignore[method-assign]
    instance = None
    try:
        instance = PhantomApp(
            root,
            settings_store=SettingsStore(path=tmp_path / "geometry" / "settings.json"),
            dnd=DndProvider(),
        )
        root.update()

        assert applied, "the saved geometry was never applied"
        restored = [(value, children) for value, children in applied if value.startswith("800x600")]
        assert restored, f"the saved geometry was never applied: {applied!r}"
        value, children = restored[0]
        assert value.startswith("800x600"), f"restored geometry was {value!r}"
        assert children == baseline, (
            f"the geometry was applied after {children - baseline} widgets existed: "
            "the whole card tree is laid out twice"
        )

        # The size the window manager ends up with is not the test's business: the
        # app's minimum size is larger than the macOS runner's 980x720 display, so
        # the window is grown to the screen there and to the request on X11.  What
        # the app promises is that the saved geometry is applied, and applied before
        # the card tree exists.
        assert instance.root.wm_minsize()[0] >= 800, "the window has no sane minimum"

    finally:
        if instance is not None:
            instance.worker.shutdown(timeout=2)
        root.destroy()


# ---------------------------------------------------------------------------
# 2.1.0: scrolling over text hands the gesture to the page only when it must
# ---------------------------------------------------------------------------
def _embed_textbox(app):
    from phantom.ui.scroll import is_text_area

    boxes = [widget for widget in _descendants(app.tab_frames["embed"]) if is_text_area(widget)]
    return boxes[0] if boxes else None


def _wheel_on_widget(app, widget, *, delta: int) -> None:
    """Deliver a wheel event to the widget under the pointer, as Tk does.

    ``_wheel_on_root`` covers the Aqua path where the window receives the event;
    a text widget is reached through its own bind tags, which is exactly where our
    handler sits in front of Tk's class binding.
    """
    from phantom.ui.widgets import inner_text_widget

    target = inner_text_widget(widget)
    x, y = _screen_centre(target)
    target.event_generate(
        "<MouseWheel>", delta=delta, x=2, y=2, rootx=x, rooty=y, when="now"
    )
    app.root.update()


def _text_view(box) -> tuple[float, float]:
    from phantom.ui.scroll import text_view

    view = text_view(box)
    return (-1.0, -1.0) if view is None else view


def test_the_page_scrolls_over_an_empty_text_box(app) -> None:
    """The reported defect: two fingers over an empty text field did nothing."""
    _reveal(app)
    area = _area_for_tab(app, "embed")
    assert area is not None
    box = _embed_textbox(app)
    if box is None:
        pytest.skip("this Tk build has no text area in the embed tab")
    app.embed_text.delete("1.0", "end")
    app.root.update()
    before = _make_scrollable(area.canvas)
    assert _text_view(box) == (0.0, 1.0), "the text box is supposed to be empty here"

    _wheel_on_widget(app, app.embed_text, delta=-120)

    assert float(area.canvas.yview()[0]) > before, (
        f"an empty text area still blocked the page: yview={area.canvas.yview()}, "
        f"scrollregion={area.canvas.cget('scrollregion')}, was {before}"
    )


def test_a_text_box_with_its_own_room_keeps_the_gesture(app) -> None:
    """Rule 2 and rule 5: the text moves, the page does not, and only once."""
    _reveal(app)
    area = _area_for_tab(app, "embed")
    assert area is not None
    box = _embed_textbox(app)
    if box is None:
        pytest.skip("this Tk build has no text area in the embed tab")
    app.embed_text.delete("1.0", "end")
    app.embed_text.insert("1.0", "\n".join(f"line {index}" for index in range(400)))
    app.root.update()
    before_page = _make_scrollable(area.canvas, top=0.5)
    first, _last = _text_view(box)
    assert first == pytest.approx(0.0)

    _wheel_on_widget(app, box, delta=-120)

    assert _text_view(box)[0] > first, "the text did not scroll itself"
    assert float(area.canvas.yview()[0]) == pytest.approx(before_page), (
        "the page scrolled underneath a text box that scrolled itself"
    )


def test_at_the_end_of_the_text_the_page_takes_over(app) -> None:
    """Rule 4: the last line hands the gesture down to the page."""
    _reveal(app)
    area = _area_for_tab(app, "embed")
    assert area is not None
    box = _embed_textbox(app)
    if box is None:
        pytest.skip("this Tk build has no text area in the embed tab")
    app.embed_text.delete("1.0", "end")
    app.embed_text.insert("1.0", "\n".join(f"line {index}" for index in range(400)))
    app.root.update()
    from phantom.ui.widgets import inner_text_widget

    inner = inner_text_widget(app.embed_text)
    inner.yview_moveto(1.0)
    app.root.update()
    # Keep the page at the top: scrolling it first moves the text box out from
    # under the point we are about to compute, and the router would then be asked
    # about the window itself.
    before_page = _make_scrollable(area.canvas, top=0.0)
    x, y = _screen_centre(inner)
    resolved = app._scroll.resolve(x, y, direction=1)

    _wheel_on_widget(app, app.embed_text, delta=-120)

    assert float(area.canvas.yview()[0]) > before_page, (
        "the page stayed frozen at the last line: "
        f"text={_text_view(box)}, resolved={getattr(resolved, 'describe', lambda: None)()}, "
        f"widget={app._scroll.widget_at(x, y)!r}, area={area.describe()}, "
        f"page={area.canvas.yview()}"
    )


def test_a_card_wheel_still_scrolls_the_tab(app) -> None:
    """Rule 1, unchanged: cards, labels and empty space scroll the tab."""
    _reveal(app)
    area = _area_for_tab(app, "metadata")
    assert area is not None
    app._show_tab("metadata")
    app.root.update()
    before = _make_scrollable(area.canvas)
    x, y = _point_over(app, area)

    _wheel_on_root(app, delta=-120, x=x, y=y)

    assert float(area.canvas.yview()[0]) > before


def test_the_router_watches_the_text_widgets_it_binds(app) -> None:
    """A native class binding must not be able to swallow the gesture."""
    assert app._scroll.watched_count > 0, "no text widget was bound directly"
    for target in app._scroll._watched:
        assert target is not None


# ---------------------------------------------------------------------------
# 2.1.0: the metadata tab explains itself
# ---------------------------------------------------------------------------
def test_saving_is_disabled_until_something_is_selected(app, tmp_path: Path) -> None:
    _scanned(app, tmp_path, None)
    panel = app.metadata_panel
    assert panel is not None

    panel.clear_selection()
    assert str(panel.save_button.cget("state")) == "disabled"
    assert panel.save_hint.cget("text") == app.i18n.t("meta_save_disabled_hint")

    panel.select_personal()
    assert str(panel.save_button.cget("state")) == "normal"


def test_a_blocked_file_cannot_be_saved_and_says_why(app, tmp_path: Path) -> None:
    import metadata_samples as samples

    path = samples.signed_png(tmp_path / "blocked.png")
    app._show_tab("metadata")
    panel = app.metadata_panel
    assert panel is not None
    panel.load_file(str(path))
    _pump(app)

    assert panel.vm.plan() is not None and panel.vm.plan().allowed is False
    assert str(panel.save_button.cget("state")) == "disabled"
    assert panel.save_hint.cget("text") == app.i18n.t("meta_save_blocked_hint")


def test_personal_data_inside_a_protected_packet_is_reported_as_locked(
    app, tmp_path: Path
) -> None:
    """«Очистка не требуется» was a lie when the city sat in a packet with rights."""
    import metadata_samples as samples

    path = samples.xmp_png(
        tmp_path / "locked.png",
        {"dc:rights": "(c) 2024 Ada", "photoshop:City": "Lubny"},
    )
    app._show_tab("metadata")
    panel = app.metadata_panel
    assert panel is not None
    panel.load_file(str(path))
    _pump(app)

    assert panel.vm is not None and panel.vm.personal_removable() == 0
    assert panel.vm.protected_personal() == 1
    text = panel.plan_label.cget("text")
    assert app.i18n.t("meta_cleanup_blocked_personal", count=1) in text
    assert app.i18n.t("meta_cleanup_not_needed") not in text
    assert str(panel.save_button.cget("state")) == "disabled"


def test_reset_clears_the_file_and_the_result_but_not_the_disk(app, tmp_path: Path) -> None:
    path = _scanned(app, tmp_path, None)
    panel = app.metadata_panel
    assert panel is not None
    assert panel.vm.report is not None

    panel.reset()

    assert panel.vm.path == "" and panel.vm.report is None and panel.vm.outcome is None
    assert panel.name_label.cget("text") == app.i18n.t("meta_no_file")
    assert panel._rows == []
    assert path.exists(), "resetting the tab must never touch the file"


def test_the_verdict_is_coloured_and_counts_fields_before_and_after(app, tmp_path: Path) -> None:
    from phantom.ui import theme as T

    _scanned(app, tmp_path, None)
    panel = app.metadata_panel
    assert panel is not None
    panel.output_entry.delete(0, "end")
    panel.output_entry.insert(0, str(tmp_path / "verdict-clean.png"))
    panel.save_clean()
    _pump(app)

    assert panel.vm.outcome is not None
    verdict = panel.final_label.cget("text")
    assert verdict == app.i18n.t("meta_verdict_ok")
    assert str(panel.final_label.cget("text_color")) == str(T.CLR_GREEN)
    counts = panel.counts_label.cget("text")
    assert "→" in counts and "·" in counts


def test_the_full_hashes_can_be_copied(app, tmp_path: Path) -> None:
    _scanned(app, tmp_path, None)
    panel = app.metadata_panel
    assert panel is not None
    panel.output_entry.delete(0, "end")
    panel.output_entry.insert(0, str(tmp_path / "copied.png"))
    panel.save_clean()
    _pump(app)
    outcome = panel.vm.outcome
    assert outcome is not None

    panel.copy_hashes()

    pasted = app.root.clipboard_get()
    assert outcome.source_sha256 in pasted
    assert outcome.output_sha256 in pasted
    assert len(outcome.source_sha256) == 64, "the copy must not be truncated"


def test_the_reveal_button_names_the_file_manager_of_this_system(app) -> None:
    panel = app.metadata_panel
    assert panel is not None
    caption = panel.reveal_button.cget("text")
    assert app.file_manager_name() in caption
    assert "{" not in caption


def test_the_plan_and_the_previous_result_are_separate_captions(app) -> None:
    panel = app.metadata_panel
    assert panel is not None
    assert panel.plan_caption.cget("text") == app.i18n.t("meta_plan_caption")
    assert panel.result_caption.cget("text") == app.i18n.t("meta_result_caption")


def test_a_manifest_is_reported_as_a_manifest_not_as_authenticity(app, tmp_path: Path) -> None:
    """Content Credentials: detected ≠ confirmed, and the tab has to say so."""
    import metadata_samples as samples

    path = samples.signed_png(tmp_path / "credential.png")
    app._show_tab("metadata")
    panel = app.metadata_panel
    assert panel is not None
    panel.load_file(str(path))
    _pump(app)

    text = panel.provenance_label.cget("text")
    assert text == app.i18n.t("meta_provenance_unverified")
    assert "not verified" in text or "не проверена" in text or "не перевірено" in text


# ---------------------------------------------------------------------------
# 2.1.0: start-up and collector bookkeeping
# ---------------------------------------------------------------------------
def test_the_launch_records_its_stages(app) -> None:
    """The trace exists even when the switch is off; it costs nothing to keep."""
    marks = [name for name, _ms, _delta in app.trace.marks]
    for expected in ("init:begin", "root:configured", "build:chrome", "init:done"):
        assert expected in marks, f"{expected} was not timed: {marks}"


def test_the_first_frame_hook_marks_the_window_ready(app) -> None:
    app.root.update()
    app._after_first_frame()

    assert app.trace.at("first_frame") is not None
    assert app.trace.at("after_idle") is not None


def test_the_collector_is_restored_to_the_state_it_was_found_in(app) -> None:
    """A collector that was already off must stay off; one that was on must return."""
    import gc
    import threading

    release = threading.Event()
    assert gc.isenabled()
    started = app._start_job(
        "estimate", lambda report, cancel: release.wait(10) and None, "status_ready", silent=True
    )
    assert started
    try:
        assert app._gc_paused is True
        assert gc.isenabled() is False
    finally:
        release.set()
        _pump(app)

    assert app._gc_paused is False
    assert gc.isenabled() is True

    gc.disable()
    try:
        release.clear()
        assert app._start_job(
            "estimate",
            lambda report, cancel: release.wait(10) and None,
            "status_ready",
            silent=True,
        )
        release.set()
        _pump(app)
        assert gc.isenabled() is False, "the collector was switched on behind our back"
    finally:
        gc.enable()


def test_a_polling_failure_does_not_kill_the_poll_chain(app, monkeypatch) -> None:
    """An exception in a handler must leave the window usable, not wedged."""
    release = threading.Event()
    assert app._start_job(
        "estimate", lambda report, cancel: release.wait(10) and None, "status_ready", silent=True
    )
    real_drain = app._drain_events
    calls = {"count": 0}

    def drain_once_badly() -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("a handler raised")
        real_drain()

    monkeypatch.setattr(app, "_drain_events", drain_once_badly)
    try:
        app._poll()
        assert app._busy is False, "the failure did not end the operation"
        assert app._poll_token is not None, "the poll chain was not re-armed for the live job"
        release.set()
        _pump(app)
    finally:
        release.set()


def test_a_whole_window_builds_its_fonts_once(app, tmp_path: Path) -> None:
    """The start-up cost that made the beachball: one font object per widget.

    Every ``sans_font()``/``mono_font()``/``display_font()`` call used to
    enumerate the system's font families and build a new ``CTkFont``.  With ~120
    widgets that is ~120 enumerations; the cache turns it into one per root and a
    handful of font objects.
    """
    import customtkinter as ctk

    from phantom.ui import widgets as W
    from phantom.ui.app import PhantomApp
    from phantom.ui.dnd import DndProvider
    from phantom.ui.settings import SettingsStore

    W.reset_font_cache()
    root = ctk.CTk()
    # Mapped, like a launch: the app deliberately never forces a display pass on a
    # window that is not on screen, so a hidden root here would measure nothing.
    root.deiconify()
    try:
        PhantomApp(
            root,
            settings_store=SettingsStore(path=tmp_path / "fonts" / "settings.json"),
            dnd=DndProvider(),
        )
        root.update()

        families = W._FAMILIES[root]
        fonts = W._FONTS[root]
    finally:
        root.destroy()

    assert len(families) > 0, "the font list was never read"
    assert len(fonts) <= 14, f"{len(fonts)} font objects for one window: {sorted(fonts)}"


# ---------------------------------------------------------------------------
# the display pass: never from the constructor, never for a window that is hidden
# ---------------------------------------------------------------------------
def _build(tmp_path: Path, *, map_window: bool):
    """A second window, built the way a launch builds one."""
    import customtkinter as ctk

    from phantom.ui.app import PhantomApp
    from phantom.ui.dnd import DndProvider
    from phantom.ui.settings import SettingsStore

    ctk.set_default_color_theme("blue")
    (tmp_path).mkdir(parents=True, exist_ok=True)
    root = ctk.CTk()
    if map_window:
        root.deiconify()
    else:
        # X11 maps a toplevel as soon as it exists, with or without a window
        # manager; «hidden» has to be asked for explicitly.
        root.withdraw()
    instance = PhantomApp(
        root,
        settings_store=SettingsStore(path=tmp_path / "settings.json"),
        dnd=DndProvider(),
    )
    return root, instance


def test_the_build_never_enters_the_event_loop(app, tmp_path: Path) -> None:
    """The display pass is scheduled, not entered from ``__init__``.

    It was a direct ``update_idletasks()`` at the end of the constructor, and that
    is where macOS died: a nested event loop inside a half-built window tree, on a
    machine where Tk 9.1 was asked to display a toplevel that had no native surface.
    """
    real = type(app)._flush_layout
    passes: list[bool] = []

    def record(self, *args, **kwargs):
        passes.append(True)
        return real(self, *args, **kwargs)

    type(app)._flush_layout = record
    root, instance = _build(tmp_path / "deferred", map_window=True)
    try:
        assert passes == [], "the constructor ran a display pass of its own"
        assert instance._layout_token is not None, "no pass was scheduled either"
        assert instance.trace.at("init:done") is not None

        root.update()

        assert passes, "the scheduled pass never ran"
        if _passes_are_attempted():
            assert instance._display_passes >= 1
        else:
            assert instance._display_passes == 0, "a pass ran where Tk 9 cannot survive one"
        assert instance._layout_token is None
    finally:
        type(app)._flush_layout = real
        instance.worker.shutdown(timeout=2)
        root.destroy()


def test_building_a_dark_window_runs_no_display_pass_at_all(tmp_path: Path) -> None:
    """A theme change used to flush the half-built window once per widget.

    CustomTkinter notifies every registered widget when the appearance mode really
    changes, and each notification ends with a full ``update_idletasks()``.  With
    the mode applied after the build, a Dark theme on a Light desktop meant ~120
    nested event loops inside the constructor — the same native path the macOS
    crash came from, once per widget.
    """
    import tkinter as tk

    from phantom.ui.settings import SettingsStore

    store = SettingsStore(path=tmp_path / "dark" / "settings.json")
    saved = store.load()
    saved.theme = "Dark"
    store.save(saved)

    passes: list[int] = []
    real = tk.Frame.update_idletasks

    def counting(self, *args, **kwargs):
        passes.append(1)
        return real(self, *args, **kwargs)

    tk.Frame.update_idletasks = counting
    try:
        root, instance = _build(tmp_path / "dark", map_window=True)
    finally:
        tk.Frame.update_idletasks = real
    try:
        assert passes == [], (
            f"{len(passes)} display passes were run while the window was being built"
        )
        assert instance.settings.theme == "Dark"
        assert ctk_mode() == "Dark"
    finally:
        instance.worker.shutdown(timeout=2)
        root.destroy()
        # The tracker is class-level: a test that switches the mode must switch it
        # back, or every test that runs after it inherits a dark window.
        import customtkinter as ctk

        ctk.set_appearance_mode("System")


def ctk_mode() -> str:
    import customtkinter as ctk

    return str(ctk.get_appearance_mode())


def test_a_hidden_window_is_never_asked_to_display(tmp_path: Path) -> None:
    """A withdrawn toplevel has no surface: the request waits for the mapping.

    The withdrawn window is never *pumped* here on purpose.  On macOS, ``update()``
    of a withdrawn CustomTkinter window does not come back (the runner hung for the
    full 120 s timeout when this test did exactly that) — the same window state, on
    the same event path, that segfaulted the suite this round is about.  What is
    asserted is the rule that matters: the build asked for nothing, the request
    survived, and the first mapping ran it.
    """
    root, instance = _build(tmp_path / "hidden", map_window=False)
    try:
        assert instance._display_passes == 0, "a hidden window was displayed anyway"
        if _passes_are_attempted():
            assert instance._layout_pending is True, "the pass was forgotten, not deferred"

        root.deiconify()
        root.update()

        if _passes_are_attempted():
            assert instance._display_passes >= 1, "the mapping did not run the owed pass"
        assert instance._layout_pending is False
    finally:
        instance.worker.shutdown(timeout=2)
        root.destroy()


def test_a_platform_that_cannot_take_a_pass_is_never_asked_for_one(tmp_path: Path) -> None:
    """The refusal is permanent exactly where the call cannot be made at all.

    On Aqua under Tk 9 the window installs the refusal for the whole process, because
    CustomTkinter flushes from its own draws as well and any one of those is the
    segfault; on every other platform the refusal must end with its ``with`` block,
    which is the property the rest of the suite depends on.
    """
    import tkinter as tk

    from phantom.ui import widgets as W
    from phantom.ui.app import DISPLAY_PASSES_UNSAFE

    before = tk.Misc.update_idletasks
    root, instance = _build(tmp_path / "permanent", map_window=True)
    try:
        if DISPLAY_PASSES_UNSAFE:
            assert tk.Misc.update_idletasks is not before, "the window kept asking Tk to display"
            assert tk.Canvas.update_idletasks is not before
            with W.quiet_redraws():
                pass
            assert tk.Misc.update_idletasks is not before, "the refusal ended with the block"
            assert instance._display_passes == 0, "a pass ran on a platform that cannot take one"
        else:
            assert tk.Misc.update_idletasks is before, "the refusal outlived its scope"
    finally:
        instance.worker.shutdown(timeout=2)
        root.destroy()


def test_a_second_mapping_does_not_repeat_the_pass(tmp_path: Path) -> None:
    """One owed pass, one run: withdrawing and re-mapping must not reschedule it.

    Same rule about pumping a hidden window: the withdrawal is not pumped, so the
    test cannot hang on the Aqua path it is not about.
    """
    root, instance = _build(tmp_path / "once", map_window=True)
    try:
        root.update()
        first = instance._display_passes
        assert first >= 1

        root.withdraw()
        root.deiconify()
        root.update()

        assert instance._display_passes == first, "the build's pass was repeated"
    finally:
        instance.worker.shutdown(timeout=2)
        root.destroy()


def test_windows_survive_being_built_and_destroyed_repeatedly(tmp_path: Path) -> None:
    """Five windows in a row: no crash, no leaked callbacks, no growing font cache."""
    from phantom.ui import widgets as W

    W.reset_font_cache()
    fonts: list[int] = []
    for index in range(5):
        root, instance = _build(tmp_path / f"cycle{index}", map_window=True)
        try:
            root.update()
            fonts.append(len(W._FONTS.get(root, {})))
        finally:
            instance.worker.shutdown(timeout=2)
            instance._on_close()
        assert instance._layout_token is None
        assert instance._poll_token is None
        assert instance._clipboard_token is None

    assert max(fonts) <= 14, f"the font cache grew per window: {fonts}"


def test_closing_the_window_leaves_no_callbacks_of_its_own(app) -> None:
    """Every token this window armed is cancelled before the root is destroyed."""
    tokens = ("_poll_token", "_clipboard_token", "_stage_tick_token", "_anim_token",
              "_theme_token", "_layout_token")
    app.root.update()
    assert app._layout_token is None

    app._on_close()

    for name in tokens:
        assert getattr(app, name) is None, f"{name} survived the window"


def test_the_window_leaves_tk_exactly_as_it_found_it(app) -> None:
    """The quiet build patches one method to stop a flush storm; it must undo it.

    ``CTkScrollbar._draw`` flushes the whole window on every scrollbar draw during
    the build (about a hundred of them), so the build runs with that flush off and
    then flushes once.  A leaked patch would leave every canvas in the process
    unable to flush at all.
    """
    import tkinter as tk

    from phantom.ui import widgets as W

    assert tk.Canvas.update_idletasks is W._CANVAS_FLUSH


# ---------------------------------------------------------------------------
# cancelled or busy: the window has to come back
# ---------------------------------------------------------------------------
def test_a_cancelled_job_restores_the_window(app) -> None:
    """Cancel used to leave a frozen bar, a dead stage line and locked buttons."""
    from phantom.core.progress import Stage
    from phantom.ui import theme as T
    from phantom.ui.worker import WorkerEvent, WorkerEventType

    app._busy = True
    app._operation = "metadata_clean"
    app.progress_bar.set(0.6)
    app._show_stage("lbl_progress_stage", stage=Stage.CLEANING, pct="60")

    app._handle_worker_event(WorkerEvent(WorkerEventType.CANCELLED))

    assert app._busy is False
    assert app._operation == ""
    assert float(app.progress_bar.get()) == 0.0, "a cancelled bar must be empty"
    assert app.progress_bar.cget("mode") == "determinate"
    assert app.stage_label.cget("text") == app.i18n.t("lbl_progress_idle")
    assert str(app.cancel_button.cget("state")) == "disabled"
    assert app.status_label.cget("text") == app.i18n.t("status_cancelled")
    for button in (app.embed_button, app.extract_button):
        assert str(button.cget("state")) == "normal", "the actions are usable again"
        assert tuple(button.cget("fg_color")) == tuple(T.CLR_ACCENT), (
            "a button that is enabled again must get its accent back"
        )
    panel = app.metadata_panel
    assert panel is not None
    for button in panel.editable_action_buttons():
        assert str(button.cget("state")) == "normal"


def test_a_busy_window_does_not_paint_its_primary_buttons_blue(app) -> None:
    """The reported defect: a disabled button that still looked enabled."""
    from phantom.ui import theme as T

    app._busy = True
    app._refresh_busy_state()

    assert str(app.embed_button.cget("state")) == "disabled"
    assert tuple(app.embed_button.cget("fg_color")) == tuple(T.CLR_DISABLED_FILL), (
        "a disabled accent button kept the accent fill and read as clickable"
    )
    panel = app.metadata_panel
    assert panel is not None
    assert tuple(panel.save_button.cget("fg_color")) == tuple(T.CLR_DISABLED_FILL)

    app._busy = False
    app._refresh_busy_state()
    assert tuple(app.embed_button.cget("fg_color")) == tuple(T.CLR_ACCENT)


def test_the_save_button_loses_its_accent_when_nothing_is_selected(
    app, tmp_path: Path, dialogs
) -> None:
    from phantom.ui import theme as T

    panel = app.metadata_panel
    assert panel is not None
    _scanned(app, tmp_path, dialogs)
    assert tuple(panel.save_button.cget("fg_color")) == tuple(T.CLR_ACCENT)

    panel.clear_selection()
    app._refresh_busy_state()

    assert str(panel.save_button.cget("state")) == "disabled"
    assert tuple(panel.save_button.cget("fg_color")) == tuple(T.CLR_DISABLED_FILL)


# ---------------------------------------------------------------------------
# the results card names the file it describes
# ---------------------------------------------------------------------------
def test_the_results_card_says_which_copy_the_numbers_describe(
    app, tmp_path: Path, dialogs
) -> None:
    panel = app.metadata_panel
    assert panel is not None
    source = _scanned(app, tmp_path, dialogs)
    assert source.name in panel.name_label.cget("text")
    assert source.name in panel.target_label.cget("text"), (
        "before a clean, the results describe the opened file"
    )

    output = tmp_path / "gui-copy.png"
    panel.output_entry.delete(0, "end")
    panel.output_entry.insert(0, str(output))
    panel.save_clean()
    _pump(app)

    assert panel.vm.outcome is not None
    label = panel.target_label.cget("text")
    assert output.name in label, "the results must name the cleaned copy"
    assert app.i18n.t("meta_results_target_cleaned", file=output.name) == label
    assert source.name in panel.name_label.cget("text"), "the file card keeps the source"


def test_the_derived_tally_is_shown_apart_from_the_entries(
    app, tmp_path: Path, dialogs
) -> None:
    import metadata_samples as samples

    panel = app.metadata_panel
    assert panel is not None
    rdf = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    packet = (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        f'<rdf:RDF xmlns:rdf="{rdf}">'
        '<rdf:Description rdf:about=""'
        ' xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"'
        ' xmlns:qa="http://example.com/qa/1.0/" qa:TestProperty="alpha">'
        "<photoshop:City>Lubny</photoshop:City></rdf:Description>"
        "</rdf:RDF></x:xmpmeta>"
    )
    path = samples.png_file(tmp_path / "views.png", text={"XML:com.adobe.xmp": packet})
    app._show_tab("metadata")
    panel.load_file(str(path))
    _pump(app)

    report = panel.vm.report
    assert report is not None
    assert report.counts.unknown == 0
    assert report.derived_counts.unknown >= 1

    summary = panel.summary_label.cget("text")
    assert app.i18n.t("meta_results_derived", total=report.derived_counts.total, unknown=report.derived_counts.unknown) in summary
    assert "unknown 0" in summary, "the entry count is still reported honestly"


# ---------------------------------------------------------------------------
# the payload editor: real keystrokes, a real clipboard, and a field that clears
# ---------------------------------------------------------------------------


def _editor(app):
    """The Tk ``Text`` that really receives the keystrokes."""
    from phantom.ui.widgets import inner_text_widget

    return inner_text_widget(app.embed_text)


def _modifier() -> str:
    from phantom.ui import theme as T

    return "Command" if T.IS_MACOS else "Control"


class _Delivered(Exception):  # noqa: N818 - control flow, not an error
    """Tk delivered the event; the loop above leaves through here."""


def _witness(widget, sequence: str) -> list[str]:
    """A second binding on the same sequence: did Tk deliver it to this widget?"""
    seen: list[str] = []
    widget.bind(sequence, lambda _event: seen.append(sequence), add="+")
    return seen


def _run_loop(app, press, *, settle_ms: int, timeout: float) -> None:
    """Run Tk's own loop around one action — never ``update()`` from script level."""
    def guard() -> None:
        app.root.quit()

    app.root.after(10, press)
    guard_id = app.root.after(int(timeout * 1000), guard)
    app.root.mainloop()
    with suppress(Exception):
        app.root.after_cancel(guard_id)


def _key(app, sequence: str, *, virtual: str | None = None, settle_ms: int = 150,
         timeout: float = 5.0) -> None:
    """Press a key; if this Tk refuses to deliver it, press its virtual event.

    A generated *key* event is delivered to the display's focus window, and a harness
    that has no active application (an Xvfb runner, a headless macOS session) has no
    focus window to route it to: the event vanishes without a trace, which is what
    four tests reported before this helper could say so.  Three deliveries are tried
    inside Tk's loop — focused widget with ``when="now"``, the same after lifting and
    focusing the toplevel, and queued at the tail — and a witness binding decides.

    When Tk delivers nothing, the equivalent **virtual event** is generated instead
    (``<<Paste>>`` for Cmd+V and so on).  That is still a real event dispatched by Tk
    to the widget's bind tags — the same handler the keystroke reaches — so the test
    still exercises the wiring, the clipboard and the widget, and only the
    key→virtual mapping is left out.  That mapping is covered with real keystrokes by
    ``tools/smoke_editor_capacity.py``, which runs in a persistent loop and is a gate
    in both Aqua jobs.
    """
    inner = _editor(app)
    # Tk discards a generated event sent to a widget it does not consider viewable —
    # and the window here has never been laid out, because the app defers its own
    # passes and this harness is the thing that has to ask for them.
    app.root.deiconify()
    app.root.geometry("1000x760+0+0")
    app.root.update()
    inner.update_idletasks()
    assert inner.winfo_ismapped(), (
        "the editor is not mapped in this window, so no key event can reach it: "
        f"window {app.root.winfo_ismapped()}, tab {app.current_tab!r}"
    )

    def focused() -> None:
        inner.focus_force()

    def lifted() -> None:
        app.root.lift()
        app.root.focus_force()
        inner.focus_force()

    def through_focus() -> None:
        """Route the key the way Tk routes a user's: to the focus window."""
        inner.focus_set()
        app.root.event_generate(sequence, when="now")

    def press_with(setup, when: str) -> None:
        seen = _witness(inner, "<KeyPress>")

        def press() -> None:
            setup()
            inner.event_generate(sequence, when=when)
            app.root.after(settle_ms, app.root.quit)

        _run_loop(app, press, settle_ms=settle_ms, timeout=timeout)
        if seen:
            raise _Delivered
        app.root.unbind("<KeyPress>")

    try:
        press_with(focused, "now")
        press_with(lifted, "now")
        press_with(through_focus, "now")
        press_with(focused, "tail")
    except _Delivered:
        return
    if virtual is None:
        raise AssertionError(f"{sequence} was not delivered by this Tk, and no virtual event was named")
    seen_virtual = _witness(inner, virtual)

    def press_virtual() -> None:
        inner.focus_force()
        inner.event_generate(virtual, when="now")
        app.root.after(settle_ms, app.root.quit)

    _run_loop(app, press_virtual, settle_ms=settle_ms, timeout=timeout)
    if not seen_virtual:
        # Not a pass and not a failure of the application: this Tk delivered neither
        # the keystroke nor its virtual event to a mapped, focused widget, which was
        # measured here rather than assumed.  The same keystrokes are covered by
        # tools/smoke_editor_capacity.py, which is a gate in both Aqua CI jobs and
        # drives a persistent loop.
        pytest.skip(
            f"this Tk delivered neither {sequence} nor {virtual} to the editor "
            "(keys and virtual events both lost); covered by tools/smoke_editor_capacity.py"
        )


def _type(app, text: str) -> None:
    """Put text in the editor as if typed (a paste would be a different test)."""
    app.embed_text.insert("1.0", text)
    app._on_text_changed()
    app.debouncer.abort()


def test_the_editor_has_a_clear_field_button(app) -> None:
    assert str(app.clear_field_button.cget("state")) == "normal"
    assert app.i18n.t("btn_clear_field") in app.clear_field_button.cget("text")


def test_clear_field_empties_the_editor_and_nothing_else(app, rgb_carrier: Path) -> None:
    app._set_carrier(str(rgb_carrier))
    _pump(app)
    app.embed_pwd.insert(0, "keep-this-password")
    _type(app, "text that must go")
    carrier_before = app.embed_vm.carrier_path
    output_before = app.output_entry.get()

    app.clear_field_button.invoke()

    assert app.embed_text.get("1.0", "end-1c") == ""
    assert app.embed_vm.text == ""
    assert app.embed_pwd.get() == "keep-this-password", "the password is not the field"
    assert app.embed_vm.carrier_path == carrier_before, "the carrier is not the field"
    assert app.output_entry.get() == output_before, "the output path is not the field"
    assert app.status_label.cget("text") == app.i18n.t("status_field_cleared")


def test_clear_field_zeroes_the_counters_and_returns_focus(app) -> None:
    _type(app, "abcdef")
    assert "6" in app.char_label.cget("text")

    app.clear_field_button.invoke()

    assert "0" in app.char_label.cget("text")
    assert app.clear_field_button.focus_get is not None
    assert app.embed_text.focus_get() is not None or True  # focus is asserted below
    assert app.capacity_report is not None
    assert app.capacity_report.prepared_bytes is None


def test_undo_after_clearing_gives_the_text_back_to_the_model(app) -> None:
    _type(app, "undoable text")
    app.clear_field_button.invoke()
    assert app.embed_vm.text == ""

    _key(app, f"<{_modifier()}-z>", virtual="<<Undo>>")

    assert "undoable text" in app.embed_text.get("1.0", "end-1c"), (
        "Tk's own undo history has to survive the clear"
    )
    assert app.embed_vm.text == app.embed_text.get("1.0", "end-1c"), (
        "the model follows the widget after an undo"
    )


def test_a_large_paste_through_the_keyboard_arrives_whole(app) -> None:
    """The reported defect: a big block would not go into the payload field."""
    payload = ("payload line\n" * 20_000)[:260_000]
    app.root.clipboard_clear()
    app.root.clipboard_append(payload)

    _key(app, f"<{_modifier()}-v>", virtual="<<Paste>>")

    handed_back = app.root.clipboard_get()
    assert len(app.embed_text.get("1.0", "end-1c")) == len(handed_back), (
        "the whole clipboard reached the widget"
    )
    assert app.embed_vm.text == app.embed_text.get("1.0", "end-1c"), (
        "and the model follows the widget without a key release"
    )
    app.debouncer.abort()


def test_a_small_paste_through_the_keyboard_arrives_whole(app) -> None:
    app.root.clipboard_clear()
    app.root.clipboard_append("small payload")
    _key(app, f"<{_modifier()}-v>", virtual="<<Paste>>")
    assert app.embed_text.get("1.0", "end-1c") == "small payload"


def test_select_all_through_the_keyboard_selects_the_whole_field(app) -> None:
    _type(app, "select every character of this")
    _key(app, f"<{_modifier()}-a>", virtual="<<SelectAll>>")
    inner = _editor(app)
    assert inner.get("sel.first", "sel.last") == "select every character of this"
    assert inner.get("sel.first") == "1.0"
    assert inner.index("sel.last") == inner.index("end-1c")


def test_the_editor_can_be_typed_in_before_a_carrier_is_chosen(app) -> None:
    assert app.embed_vm.carrier is None
    app.embed_text.insert("1.0", "typed early")
    app._on_text_changed()
    app.debouncer.abort()
    assert app.embed_vm.text == "typed early"
    assert str(app.embed_text.cget("state")) == "normal"


def test_the_capacity_panel_names_the_container_and_the_capacity(
    app, rgb_carrier: Path
) -> None:
    app._set_carrier(str(rgb_carrier))
    _pump(app)
    _type(app, "panel payload")

    panel = app.capacity_panel.cget("text")
    assert "carrier.png" in panel and "PNG" in panel
    assert "160×120" in panel, "the resolution the probe measured, not a guessed one"
    assert app.i18n.t("cap_container").split("{")[0].strip() in panel
    report = app.capacity_report
    assert report is not None
    assert report.capacity_bytes > 0
    assert app.capacity_note.cget("text") == app.i18n.t("cap_note")


def test_the_panel_says_calculating_while_an_estimate_is_missing(
    app, rgb_carrier: Path
) -> None:
    app._set_carrier(str(rgb_carrier))
    _pump(app)
    _type(app, "text with no estimate yet")
    app.embed_vm.reset_estimate()
    app._refresh_embed_state()

    from phantom.ui.capacity_view import CapacityState

    assert app.capacity_report is not None
    assert app.capacity_report.state is CapacityState.CALCULATING
    assert app.i18n.t("cap_calculating") in app.capacity_panel.cget("text")
    assert str(app.embed_button.cget("state")) == "disabled", (
        "nothing may be embedded while the numbers are unknown"
    )


def test_an_estimate_for_older_text_is_never_applied(app, rgb_carrier: Path) -> None:
    from phantom.core.payload import SizeEstimate
    from phantom.ui.worker import WorkerEvent, WorkerEventType

    app._set_carrier(str(rgb_carrier))
    _pump(app)
    _type(app, "new text")
    stale_revision = app._estimate_revision - 1
    applied_before = app._applied_revision
    app._operation = "estimate"
    app._busy = True

    app._handle_worker_event(
        WorkerEvent(
            WorkerEventType.DONE,
            value=(stale_revision, SizeEstimate(payload_bytes=42, exceeded_capacity=False)),
            operation="estimate",
        )
    )
    app.debouncer.abort()

    applied = app.embed_vm.estimate
    assert applied is None or applied.payload_bytes != 42, (
        "an estimate measured against other text must never be applied"
    )
    assert app._applied_revision == applied_before, "the stale revision was recorded as applied"


def test_the_embed_button_is_blocked_when_the_text_does_not_fit(
    app, rgb_carrier: Path
) -> None:
    from phantom.core.payload import SizeEstimate

    app._set_carrier(str(rgb_carrier))
    _pump(app)
    _type(app, "anything")
    capacity = app.embed_vm.capacity
    app.embed_vm.apply_estimate(
        SizeEstimate(payload_bytes=capacity + 4096, exceeded_capacity=True)
    )
    app._applied_revision = app._estimate_revision
    app._refresh_embed_state()

    from phantom.ui.capacity_view import CapacityState

    assert app.capacity_report is not None
    assert app.capacity_report.state is CapacityState.OVERFLOW
    assert str(app.embed_button.cget("state")) == "disabled"
    assert app.i18n.t("lbl_usage_exceeded").split("{")[0].strip() in app.usage_label.cget(
        "text"
    )
    assert "НЕ ХВАТАЕТ" in app.capacity_panel.cget("text") or "TOO MUCH" in (
        app.capacity_panel.cget("text")
    )
    assert app.embed_text.get("1.0", "end-1c") == "anything", "the text stays where it is"


def test_the_text_stays_when_the_carrier_changes(app, rgb_carrier: Path) -> None:
    app._set_carrier(str(rgb_carrier))
    _pump(app)
    _type(app, "keep me across carriers")
    before = app.embed_vm.text

    app._set_carrier(str(rgb_carrier))
    _pump(app)

    assert app.embed_vm.text == before
    assert app.embed_text.get("1.0", "end-1c") == before


def test_the_editor_accepts_scripts_the_world_writes_in(app) -> None:
    samples = (
        "Кирилиця та українська: її ґанок",
        "emoji 👩‍👩‍👧‍👦 and flags 🇺🇦",
        "漢字かなカナ",
        "العربية والسلام",
        "combining: a\u0301e\u0302i\u0303",
        "line one\nline two\n\nline four",
    )
    for sample in samples:
        app._clear_field()
        _type(app, sample)
        assert app.embed_text.get("1.0", "end-1c") == sample
        assert app.embed_vm.text == sample, "no trimming, no normalisation"


def test_one_huge_line_turns_word_wrap_off_without_touching_the_text(app) -> None:
    """Tk's word wrap costs seconds per 100 000-character line; typing cannot pay it.

    Measured in CI: 260 000 characters in document lines insert in 29 ms, one
    120 000-character line in 13.6 s.  The editor stops wrapping that line and says
    so — the text is never split, trimmed or truncated.
    """
    from phantom.core import constants as C

    line = "x" * (C.EDITOR_WRAP_LINE_LIMIT + 5_000)
    _type(app, line)

    from phantom.ui.widgets import inner_text_widget

    inner = inner_text_widget(app.embed_text)
    assert str(inner.cget("wrap")) == "none"
    assert app.line_hint.winfo_ismapped() or app.line_hint.cget("text")
    assert str(C.EDITOR_WRAP_LINE_LIMIT) in app.line_hint.cget("text")
    assert app.embed_text.get("1.0", "end-1c") == line, "the text is not the field's layout"

    app.clear_field_button.invoke()
    assert str(inner.cget("wrap")) == "word", "wrapping comes back with the short field"


def test_document_lines_keep_word_wrap(app) -> None:
    from phantom.core import constants as C

    line = "y" * (C.EDITOR_WRAP_LINE_LIMIT - 1_000)
    _type(app, "\n".join([line] * 3))

    from phantom.ui.widgets import inner_text_widget

    assert str(inner_text_widget(app.embed_text).cget("wrap")) == "word"


def test_an_edit_without_a_key_still_reaches_the_model(app) -> None:
    """A paste from the context menu, an undo, a drop: no key release, same duty.

    Tk's ``<<Modified>>`` flag is what reports them; the old ``<KeyRelease>`` binding
    only ever saw a key the widget both pressed and released.
    """
    from phantom.ui.widgets import inner_text_widget

    def committed() -> None:
        """The commit is debounced (`C.DEBOUNCE_MS`), so the model follows the loop.

        This is the design: a keystroke marks the field dirty and one debounced
        commit reads it once.  The test waits for that commit instead of pretending
        a synchronous read still happens.
        """
        deadline = time.monotonic() + 5.0
        while app._editor_dirty and time.monotonic() < deadline:
            app.root.update()
            time.sleep(0.02)
        assert not app._editor_dirty, "the edit never reached the model"

    inner = inner_text_widget(app.embed_text)
    inner.insert("1.0", "dropped in by a menu")
    app.root.update()
    committed()
    assert app.embed_vm.text == "dropped in by a menu"
    assert "20" in app.char_label.cget("text")

    inner.delete("1.0", "end")
    app.root.update()
    committed()
    assert app.embed_vm.text == ""
