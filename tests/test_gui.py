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
    assert app._t("header_subtitle").count(C.APP_VERSION) == 1


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
    import time

    app._set_carrier(str(rgb_carrier))
    app.embed_vm.set_password(password)
    app.embed_vm.set_text("gui smoke payload")
    app._refresh_all()
    app._start_embed()

    deadline = time.monotonic() + 30
    while app.worker.busy and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.02)
    app.root.update()
    assert app._busy is False
    output = Path(app.embed_vm.output_path)
    assert output.exists()
    from phantom.core import api

    assert api.extract_from_path(str(output), password).payload.data == b"gui smoke payload"


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
    assert app.root.clipboard_get() != "secret-value"


def test_worker_poll_drains_events(app) -> None:
    app.worker.start("probe", lambda report, cancel: 42)
    import time

    deadline = time.monotonic() + 10
    while app.worker.busy and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    app._poll()
    assert app.worker.busy is False
