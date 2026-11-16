"""GUI smoke tests. Need a real Tk display; gated by the ``gui`` marker.

On Linux CI run them under ``xvfb-run -a python -m pytest -m gui``. Without a
display every test in this module is skipped with an explicit reason — a silent
pass would hide a broken Tk layer.
"""

from __future__ import annotations

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
    both have to settle.
    """
    import time

    deadline = time.monotonic() + seconds
    while (app.worker.busy or app._busy) and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.02)
    app.root.update()


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

    text = app.format_label.cget("text")
    assert written_layout_text(app.i18n) == app.i18n.t(
        "layout_trellis", version=C.STC_CONTAINER_VERSION
    )
    assert text == f"v{C.STC_CONTAINER_VERSION} · синдромный решётчатый код", text


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
def test_every_tab_registered_its_scrollable_area(app) -> None:
    from phantom.ui.widgets import CTkAdapter

    assert len(app._scroll_frames) == 3
    assert app._wheel.bound_count > 0
    for frame in app._scroll_frames:
        assert CTkAdapter.scroll_canvas(frame) is not None


def test_every_widget_in_every_tab_answers_the_wheel(app) -> None:
    """Every plain Tk widget under a scrollable frame carries the handler."""
    from phantom.ui.scroll import is_scrollbar, is_text_area

    checked = 0
    missing: list[str] = []
    for frame in app._scroll_frames:
        stack: list[object] = [frame]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if is_text_area(widget) or is_scrollbar(widget):
                continue
            try:
                script = widget.bind("<MouseWheel>")
            except (NotImplementedError, ValueError):
                continue
            if script:
                checked += 1
                continue
            # A CustomTkinter wrapper forwards bind() to the Tk widgets it is
            # made of and answers nothing; those children are walked above.
            if type(widget).__module__.startswith("customtkinter"):
                continue
            missing.append(
                f"{widget} cls={type(widget).__name__} mod={type(widget).__module__} "
                f"master={widget.master} master_cls={type(widget.master).__name__} "
                f"children={len(widget.winfo_children())}"
            )
    assert not missing, "unbound: " + " | ".join(missing[:6])
    assert checked >= 10, f"only {checked} widgets answered the wheel"


def test_the_broken_global_handler_is_gone(app) -> None:
    """``bind_all("<MouseWheel>")`` was the macOS dead end; the area carries it."""
    assert not app.root.bind_all("<MouseWheel>")
    assert app._scroll_frames[0].bind("<MouseWheel>")


def test_wheel_steps_reach_the_canvas_of_an_area(app) -> None:
    """The maths the bound handler runs moves the real canvas of the tab."""
    import types

    from phantom.ui.scroll import scroll_canvas, wheel_steps
    from phantom.ui.widgets import CTkAdapter

    canvas = CTkAdapter.scroll_canvas(app._scroll_frames[0])
    calls: list[tuple[int, str]] = []
    original = canvas.yview_scroll
    canvas.yview_scroll = lambda steps, what: (calls.append((steps, what)), original(steps, what))[1]
    try:
        event = types.SimpleNamespace(delta=120, num=None, x_root=5, y_root=5)
        assert scroll_canvas(canvas, wheel_steps(event, platform="linux")) is True
    finally:
        canvas.yview_scroll = original
    assert calls == [(-1, "units")]


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
