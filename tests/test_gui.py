"""GUI smoke tests. Need a real Tk display; gated by the ``gui`` marker.

On Linux CI run them under ``xvfb-run -a python -m pytest -m gui``. Without a
display every test in this module is skipped with an explicit reason — a silent
pass would hide a broken Tk layer.
"""

from __future__ import annotations

import re
import threading
import time
from contextlib import suppress
from pathlib import Path

import pytest

from phantom.core import constants as C

pytestmark = pytest.mark.gui


def _tk_usable() -> tuple[bool, str]:
    try:
        import tkinter
    except ImportError as exc:
        return False, f"tkinter is not installed: {exc}"
    try:
        root = tkinter.Tk()
    except Exception as exc:  # noqa: BLE001 - no display, or Tk rejects the connection
        return False, f"Tk cannot open a display: {exc}"
    root.destroy()
    return True, ""


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
        # The collector belongs to the operation too: the pause is given back as
        # soon as the worker thread is really gone, which can be a poll or two
        # after the job's DONE.
        return not (app.worker.busy or app._busy or app._gc_paused)

    deadline = time.monotonic() + seconds
    while not settled() and time.monotonic() < deadline:
        app.root.update()
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
        f"events={app.worker.operation!r}"
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
    finally:
        # A test may have closed the window itself; destroying it twice is normal.
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


def test_context_menu_offers_paste_copy_cut_and_select_all(app) -> None:
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
        app.i18n.t("menu_paste"),
        app.i18n.t("menu_copy"),
        app.i18n.t("menu_cut"),
        app.i18n.t("menu_select_all"),
    ]
    app.root.clipboard_clear()
    app.root.clipboard_append("from-the-menu")
    app.embed_pwd.delete(0, "end")
    menu.invoke(0)  # Paste
    assert app.embed_pwd.get() == "from-the-menu"


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
    app._set_textbox(app.result_text, "readable result body")
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

    # 16 sequences per text widget: Cmd/Ctrl x lower/upper case for c, v, x, a.
    assert all(item.bound_count >= 16 for item in app._text_shortcuts)
    assert len(app._text_shortcuts) >= 5


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
    app._set_carrier(str(rgb_carrier))  # probe job
    _pump(app)
    assert app._start_job(
        "estimate", lambda report, cancel: 0, "status_ready", silent=True
    )
    _pump(app)
    assert gc.isenabled(), "the second job left the collector disabled"
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
    saved.window_geometry = "1040x780+40+30"
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
        restored = [(value, children) for value, children in applied if "1040x780" in value]
        assert restored, f"the saved geometry was never applied: {applied!r}"
        value, children = restored[0]
        assert value.startswith("1040x780"), f"restored geometry was {value!r}"
        assert children == baseline, (
            f"the geometry was applied after {children - baseline} widgets existed: "
            "the whole card tree is laid out twice"
        )

        shown = instance.root.geometry()
        match = re.match(r"(\d+)x(\d+)", shown)
        assert match is not None, f"unreadable geometry {shown!r}"
        assert int(match.group(1)) == 1040, f"width was {shown!r}"
        # A window manager may round the frame by a pixel or two; the size it was
        # given is what the test is about.
        assert abs(int(match.group(2)) - 780) <= 2, f"height was {shown!r}"
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
        assert instance._display_passes >= 1
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
        assert instance._layout_pending is True, "the pass was forgotten, not deferred"

        root.deiconify()
        root.update()

        assert instance._display_passes >= 1, "the mapping did not run the owed pass"
        assert instance._layout_pending is False
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
