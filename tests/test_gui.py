"""GUI smoke tests. Need a real Tk display; gated by the ``gui`` marker.

On Linux CI run them under ``xvfb-run -a python -m pytest -m gui``. Without a
display every test in this module is skipped with an explicit reason — a silent
pass would hide a broken Tk layer.
"""

from __future__ import annotations

import threading
import time
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

    deadline = time.monotonic() + seconds
    while (app.worker.busy or app._busy) and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.02)
    app.root.update()
    assert not (app.worker.busy or app._busy), (
        f"still busy after {seconds:.0f}s: worker={app.worker.busy}, "
        f"app={app._busy}, operation={app._operation!r}"
    )


@pytest.fixture
def app(tmp_path: Path, isolated_settings: Path, monkeypatch: pytest.MonkeyPatch):
    import customtkinter as ctk

    from phantom.ui.app import PhantomApp
    from phantom.ui.dnd import DndProvider
    from phantom.ui.settings import SettingsStore

    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    root.withdraw()
    instance = PhantomApp(
        root,
        settings_store=SettingsStore(path=isolated_settings / "settings.json"),
        dnd=DndProvider(),
    )
    yield instance
    try:
        instance.worker.shutdown(timeout=2)
    finally:
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
    assert app.current_tab in ("embed", "extract", "settings")
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
    for ident in ("extract", "settings", "embed"):
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

    assert len(app._scroll_frames) == 3
    assert app._scroll.area_count == 3
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


def _make_scrollable(canvas, *, top: float = 0.25) -> float:
    """Give the canvas a document longer than the window and set the view.

    Routing is what is under test, not the height of the layout: a tab whose
    content happens to fit would make ``yview`` immovable and the test would
    report a routing failure for a layout reason.
    """
    canvas.update_idletasks()
    width = max(1, canvas.winfo_width())
    canvas.configure(scrollregion=(0, 0, width, 6000))
    canvas.yview_moveto(top)
    canvas.update_idletasks()
    return float(canvas.yview()[0])


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
    before = _make_scrollable(area.canvas)
    boxes = [widget for widget in _descendants(area.frame) if is_text_area(widget)]
    if not boxes:
        pytest.skip("no text area in this tab on this platform")
    box = boxes[0]
    x, y = _screen_centre(box)
    if app._scroll.resolve(x, y) is not None:
        pytest.skip("the text area is not the widget under its own centre")

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


