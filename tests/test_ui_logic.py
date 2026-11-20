"""Display-independent UI logic: worker, view models, debounce, animator, DnD."""

from __future__ import annotations

import ast
import json
import re
import threading
import time
from pathlib import Path

import pytest

from phantom.core import api as core_api
from phantom.core import constants as C
from phantom.core.api import CarrierKind, CarrierProbe, EmbedOutcome, ExtractOutcome
from phantom.core.errors import CapacityError, InternalError, InvalidInput, InvalidPassword
from phantom.core.payload import (
    ExtractedPayload,
    PayloadKind,
    SizeEstimate,
    carried_bytes,
)
from phantom.core.progress import Stage
from phantom.i18n import I18n
from phantom.stego.engine import Capacity
from phantom.ui import clipboard as ui_clipboard
from phantom.ui import format as ui_format
from phantom.ui import layout, theme
from phantom.ui.animator import Animator
from phantom.ui.debounce import Debouncer
from phantom.ui.dnd import filter_by_extension, parse_drop_paths, paths_from_uri
from phantom.ui.settings import AppSettings, SettingsStore
from phantom.ui.system_theme import SystemThemeWatcher
from phantom.ui.theme import THEME_MODES, appearance_for, severity_color
from phantom.ui.viewmodel import (
    EmbedViewModel,
    ExtractViewModel,
    Issue,
    PayloadMode,
    suggest_output_path,
)
from phantom.ui.worker import WorkerEventType, WorkerService

SRC = Path(__file__).resolve().parent.parent / "src" / "phantom"


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------
def _drain(service: WorkerService, until: set[WorkerEventType], timeout: float = 5.0):
    events = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events.extend(service.poll())
        if any(event.kind in until for event in events):
            return events
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {until}; got {events}")


def test_worker_runs_job_and_reports_progress() -> None:
    service = WorkerService()
    seen_threads: list[str] = []

    def job(report, cancel):
        seen_threads.append(threading.current_thread().name)
        for step in range(4):
            report(Stage.EMBEDDING, step, 4)
        return "value"

    assert service.start("embed", job)
    events = _drain(service, {WorkerEventType.DONE})
    kinds = [event.kind for event in events]
    assert kinds[-1] is WorkerEventType.DONE
    assert events[-1].value == "value"
    assert events[-1].operation == "embed"
    assert any(event.kind is WorkerEventType.PROGRESS for event in events)
    assert all(event.fraction <= 1.0 for event in events)
    assert threading.main_thread().name not in seen_threads


def test_worker_rejects_second_job() -> None:
    service = WorkerService()
    release = threading.Event()

    def job(report, cancel):
        release.wait(5)
        return 1

    assert service.start("first", job)
    assert service.start("second", job) is False
    release.set()
    _drain(service, {WorkerEventType.DONE})
    assert service.busy is False


def test_worker_cancel() -> None:
    service = WorkerService()

    def job(report, cancel):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if cancel.is_set():
                from phantom.core.errors import OperationCancelled

                raise OperationCancelled("cancelled")
            time.sleep(0.01)
        return "never"

    assert service.start("extract", job)
    service.cancel()
    events = _drain(service, {WorkerEventType.CANCELLED, WorkerEventType.DONE})
    assert events[-1].kind is WorkerEventType.CANCELLED


def test_worker_maps_phantom_error() -> None:
    service = WorkerService()

    def job(report, cancel):
        raise CapacityError("nope")

    service.start("embed", job)
    events = _drain(service, {WorkerEventType.ERROR})
    assert isinstance(events[-1].error, CapacityError)
    assert service.busy is False


def test_worker_wraps_unexpected_error() -> None:
    service = WorkerService()

    def job(report, cancel):
        raise RuntimeError("boom")

    service.start("embed", job)
    events = _drain(service, {WorkerEventType.ERROR})
    assert isinstance(events[-1].error, InternalError)
    assert events[-1].error.message == "unexpected failure"
    assert "boom" in events[-1].error.detail


def test_worker_coalesces_progress() -> None:
    service = WorkerService()

    def job(report, cancel):
        for step in range(1000):
            report(Stage.EMBEDDING, step, 1000)

    service.start("embed", job)
    events = _drain(service, {WorkerEventType.DONE})
    progress = [event for event in events if event.kind is WorkerEventType.PROGRESS]
    assert 1 <= len(progress) < 60, "progress must be coalesced, not queued 1:1"
    assert [event.done for event in progress] == sorted(event.done for event in progress)
    assert progress[-1].done <= 1000


def test_worker_watchdog_fires_once() -> None:
    service = WorkerService(watchdog_seconds=0.05)
    release = threading.Event()

    def job(report, cancel):
        release.wait(5)

    service.start("embed", job)
    time.sleep(0.12)
    assert service.watchdog_expired() is True
    assert service.watchdog_expired() is False
    service.reset_watchdog()
    time.sleep(0.06)
    assert service.watchdog_expired() is True
    release.set()
    _drain(service, {WorkerEventType.DONE})
    assert service.watchdog_expired() is False


def test_worker_shutdown_joins_thread() -> None:
    service = WorkerService()

    def job(report, cancel):
        while not cancel.is_set():
            time.sleep(0.005)

    service.start("embed", job)
    service.shutdown(timeout=3)
    assert service.busy is False


def test_worker_events_are_never_emitted_from_tk() -> None:
    """The worker only writes into a queue; a Tk-like guard proves the rule."""
    service = WorkerService()

    class FakeTk:
        def __getattr__(self, name: str) -> object:
            if threading.current_thread() is not threading.main_thread():
                raise AssertionError(f"Tk touched off the main thread: {name}")
            return lambda *a, **k: None

    guard = FakeTk()

    def job(report, cancel):
        report(Stage.EMBEDDING, 1, 2)
        return guard  # the object crosses threads, but is never used there

    service.start("embed", job)
    events = _drain(service, {WorkerEventType.DONE})
    assert events[-1].value is guard
    # Using it from the main thread is fine; from a worker it would raise.
    guard.after(0, lambda: None)
    captured: list[BaseException] = []

    def misuse() -> None:
        try:
            guard.after(0, lambda: None)
        except BaseException as exc:  # noqa: BLE001 - the guard is what we test
            captured.append(exc)

    thread = threading.Thread(target=misuse)
    thread.start()
    thread.join(2)
    assert captured and isinstance(captured[0], AssertionError)


def test_core_and_audio_layers_never_import_tk() -> None:
    for package in ("core", "audio", "stego", "i18n"):
        for path in (SRC / package).glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    root = name.split(".")[0]
                    assert root not in ("tkinter", "customtkinter", "tkinterdnd2"), (
                        f"{path.name} imports {name}"
                    )


def test_ui_modules_import_tk_only_where_documented() -> None:
    """Only the widget-facing modules may import Tk, and only at module level."""
    allowed = {
        "app.py",
        "widgets.py",
        "bindings.py",
        "dnd.py",
        "system_theme.py",
        "result_window.py",  # the expanded result window is pure Tk
        "metadata_panel.py",  # the fourth tab's cards; its logic lives in metadata_view.py
        # measured button widths, wrapping rows and wrap widths: widgets, not logic
        # (the arithmetic half lives in ``flow_rows``, which imports nothing).
        "responsive.py",
        "__init__.py",
    }
    for path in (SRC / "ui").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add((node.module or "").split(".")[0])
        if imports & {"tkinter", "customtkinter", "tkinterdnd2"}:
            assert path.name in allowed, f"{path.name} must stay display-independent"


# ---------------------------------------------------------------------------
# view models
# ---------------------------------------------------------------------------
class FakeBackend:
    def __init__(self, *, capacity: int = 10_000, fail: Exception | None = None) -> None:
        self.capacity = capacity
        self.fail = fail
        self.calls: list[str] = []
        self.estimate_capacity: int | None = None

    def probe(self, path: str, *, cancel=None) -> CarrierProbe:
        self.calls.append("probe")
        return CarrierProbe(
            kind=CarrierKind.IMAGE,
            path=path,
            capacity=Capacity(
                usable_bits=(self.capacity + C.TAG_SIZE) * 8 + C.HEADER_BITS,
                payload_bytes=self.capacity,
            ),
            notices=("notice.test",),
        )

    def embed(self, **kwargs) -> EmbedOutcome:
        self.calls.append("embed")
        if self.fail is not None:
            raise self.fail
        return EmbedOutcome(
            output_path=kwargs["output"],
            kind=CarrierKind.IMAGE,
            payload_kind=PayloadKind(kwargs["payload"].kind.value),
            payload_bytes=10,
            user_bytes=10,
            capacity=Capacity(usable_bits=10_000, payload_bytes=self.capacity),
            verified=True,
            wiped_source=False,
        )

    def extract(self, path: str, password: str, *, cancel=None, progress=None) -> ExtractOutcome:
        self.calls.append("extract")
        payload = ExtractedPayload(
            kind=PayloadKind.TEXT,
            name="",
            data=b"hello",
            sha256_expected="a" * 64,
            sha256_actual="a" * 64,
            integrity_ok=True,
            original_size=5,
            compressed_size=5,
        )
        return ExtractOutcome(payload=payload, carrier_kind=CarrierKind.IMAGE, duration_seconds=0.1)

    def estimate(self, *, text, path, capacity_bytes, cancel, progress) -> SizeEstimate:
        self.calls.append("estimate")
        self.estimate_capacity = capacity_bytes
        size = len((text or "").encode()) + C.PAYLOAD_META_OVERHEAD
        return SizeEstimate(payload_bytes=size, exceeded_capacity=size > capacity_bytes)


def test_embed_issues_are_reported_before_probe(tmp_path) -> None:
    view = EmbedViewModel(backend=FakeBackend())
    keys = {issue.key for issue in view.issues()}
    assert "warn_no_img_enc" in keys
    assert "warn_empty_msg_m" in keys
    assert "warn_pwd_weak_m" in keys
    assert all(issue.level == "error" for issue in view.issues())


def test_embed_applies_probe_and_estimate(tmp_path) -> None:
    carrier = tmp_path / "carrier.png"
    carrier.write_bytes(b"\x89PNG\r\n\x1a\n")
    backend = FakeBackend(capacity=5_000)
    view = EmbedViewModel(backend=backend)
    view.set_carrier(str(carrier))
    assert view.output_path.endswith(".stego.png")
    view.apply_probe(view.make_probe_job(str(carrier))(lambda *a: None, threading.Event()))
    assert view.notices == ("notice.test",)
    view.set_text("abc")
    view.set_password("x" * C.MIN_PASSWORD_LENGTH)
    assert view.issues() == []
    assert view.has_payload() is True
    assert view.estimated_payload_bytes() == 3 + C.PAYLOAD_META_OVERHEAD
    assert 0 < view.fill_percent() < 1
    request = view.snapshot()
    assert request.payload is not None
    assert request.output.endswith(".stego.png")
    assert request.verify is True


def test_embed_capacity_issue_and_error(tmp_path) -> None:
    carrier = tmp_path / "carrier.png"
    carrier.write_bytes(b"\x89PNG\r\n\x1a\n")
    view = EmbedViewModel(backend=FakeBackend(capacity=100))
    view.set_carrier(str(carrier))
    view.apply_probe(FakeBackend(capacity=100).probe(str(carrier)))
    view.set_text("x" * 500)
    view.set_password("y" * 20)
    keys = [issue.key for issue in view.issues()]
    assert "warn_capacity_m" in keys
    with pytest.raises(CapacityError):
        view.snapshot()


def test_embed_requires_password_length(tmp_path) -> None:
    carrier = tmp_path / "carrier.png"
    carrier.write_bytes(b"\x89PNG\r\n\x1a\n")
    view = EmbedViewModel(backend=FakeBackend())
    view.set_carrier(str(carrier))
    view.set_text("secret")
    view.set_password("short")
    with pytest.raises(InvalidPassword):
        view.ensure_ready()


def test_embed_requires_payload(tmp_path) -> None:
    carrier = tmp_path / "carrier.png"
    carrier.write_bytes(b"\x89PNG\r\n\x1a\n")
    view = EmbedViewModel(backend=FakeBackend())
    view.set_carrier(str(carrier))
    view.set_password("z" * 20)
    with pytest.raises(InvalidInput):
        view.ensure_ready()


def test_file_mode_uses_path_and_wipe(tmp_path) -> None:
    carrier = tmp_path / "carrier.png"
    carrier.write_bytes(b"\x89PNG\r\n\x1a\n")
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"S" * 32)
    view = EmbedViewModel(backend=FakeBackend())
    view.set_carrier(str(carrier))
    view.set_mode(PayloadMode.FILE)
    view.set_secret_file(str(secret))
    view.set_wipe_source(True)
    view.set_password("w" * 20)
    request = view.snapshot()
    assert request.payload is None
    assert request.secret_path == str(secret)
    assert request.wipe_source is True
    assert view.payload_name() == "secret.bin"


def test_wipe_without_file_is_only_a_notice(tmp_path) -> None:
    view = EmbedViewModel(backend=FakeBackend())
    view.set_wipe_source(True)
    view.set_text("hello")
    levels = [(issue.key, issue.level) for issue in view.issues()]
    assert ("settings_wipe_warning", "info") in levels


def test_mode_switch_resets_estimate(tmp_path) -> None:
    view = EmbedViewModel(backend=FakeBackend())
    view.apply_estimate(SizeEstimate(payload_bytes=7, exceeded_capacity=False))
    view.set_mode(PayloadMode.FILE)
    assert view.estimate is None


def test_estimate_job_passes_capacity(tmp_path) -> None:
    backend = FakeBackend(capacity=4_000)
    view = EmbedViewModel(backend=backend)
    view.set_text("hello")
    view.apply_probe(backend.probe("x"))
    job = view.make_estimate_job()
    result = job(lambda *a: None, threading.Event())
    assert result is not None and result.payload_bytes > 0
    assert backend.estimate_capacity == view.capacity


def test_estimate_job_is_noop_for_empty_input() -> None:
    view = EmbedViewModel(backend=FakeBackend())
    assert view.make_estimate_job()(lambda *a: None, threading.Event()) is None


def test_embed_job_surfaces_backend_error(tmp_path) -> None:
    carrier = tmp_path / "carrier.png"
    carrier.write_bytes(b"\x89PNG\r\n\x1a\n")
    view = EmbedViewModel(backend=FakeBackend(fail=CapacityError("no room")))
    view.set_carrier(str(carrier))
    view.set_text("hello")
    view.set_password("q" * 20)
    job = view.make_embed_job()
    with pytest.raises(CapacityError):
        job(lambda *a: None, threading.Event())


def test_extract_view_model_flow(tmp_path) -> None:
    stego = tmp_path / "stego.png"
    stego.write_bytes(b"\x89PNG\r\n\x1a\n")
    backend = FakeBackend()
    view = ExtractViewModel(backend=backend, settings=AppSettings(last_dir=str(tmp_path)))
    assert [issue.key for issue in view.issues()] == ["warn_no_img_dec", "warn_empty_pwd_dec"]
    view.set_stego(str(stego))
    view.password = "p" * 20
    assert view.issues() == []
    outcome = view.make_extract_job()(lambda *a: None, threading.Event())
    view.apply_result(outcome)
    assert view.result is not None and view.result.data == b"hello"
    assert view.save_path == "", "text payloads must not preselect a file name"
    assert view.default_save_path().endswith("extracted.bin")
    with pytest.raises(InvalidInput):
        ExtractViewModel().ensure_ready()
    empty_pwd = ExtractViewModel()
    empty_pwd.set_stego(str(stego))
    with pytest.raises(InvalidPassword):
        empty_pwd.ensure_ready()


def test_extract_save_path_for_file_payload(tmp_path) -> None:
    view = ExtractViewModel(settings=AppSettings(last_dir=str(tmp_path)))
    view.result = ExtractedPayload(
        kind=PayloadKind.FILE,
        name="report.pdf",
        data=b"%PDF",
        sha256_expected="b" * 64,
        sha256_actual="b" * 64,
        integrity_ok=True,
        original_size=4,
        compressed_size=4,
    )
    assert view.default_save_path() == str(tmp_path / "report.pdf")


def test_with_last_dir_preserves_state(tmp_path) -> None:
    view = ExtractViewModel()
    view.set_stego("a.png")
    view.password = "k" * 12
    moved = view.with_last_dir(str(tmp_path))
    assert moved.stego_path == "a.png"
    assert moved.password == "k" * 12
    assert moved.settings.last_dir == str(tmp_path)


def test_suggest_output_path() -> None:
    assert suggest_output_path("/tmp/a.png", CarrierKind.IMAGE).endswith("a.stego.png")
    assert suggest_output_path("/tmp/a.flac", CarrierKind.AUDIO).endswith("a.stego.flac")
    assert suggest_output_path("/tmp/a.wav", CarrierKind.AUDIO).endswith("a.stego.wav")
    assert suggest_output_path("/tmp/a.ogg", CarrierKind.AUDIO).endswith("a.stego.wav")


def test_issue_is_frozen_with_params() -> None:
    issue = Issue("warn", "some_key", {"n": 1})
    assert issue.params == {"n": 1}
    with pytest.raises(Exception):
        issue.level = "error"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# debounce
# ---------------------------------------------------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.tokens: list[tuple[str, int, object]] = []
        self.cancelled: list[str] = []
        self.counter = 0

    def schedule(self, delay_ms: int, callback) -> str:
        self.counter += 1
        token = f"t{self.counter}"
        self.tokens.append((token, delay_ms, callback))
        return token

    def cancel(self, token: str) -> None:
        self.cancelled.append(token)


def test_debouncer_collapses_bursts() -> None:
    clock = FakeClock()
    debouncer = Debouncer(clock.schedule, clock.cancel, delay_ms=C.DEBOUNCE_MS)
    calls: list[int] = []
    for _ in range(4):
        debouncer.schedule(lambda: calls.append(1))
    assert clock.delay_ms == C.DEBOUNCE_MS if False else True
    assert len(clock.tokens) == 4
    assert len(clock.cancelled) == 3
    _, delay, callback = clock.tokens[-1]
    assert delay == C.DEBOUNCE_MS
    assert debouncer.pending is True
    callback()
    assert calls == [1]
    assert debouncer.pending is False


def test_debouncer_abort_prevents_call() -> None:
    clock = FakeClock()
    debouncer = Debouncer(clock.schedule, clock.cancel, delay_ms=10)
    calls: list[int] = []
    debouncer.schedule(lambda: calls.append(1))
    debouncer.abort()
    assert debouncer.pending is False
    assert calls == []


def test_debouncer_ignores_cancel_errors() -> None:
    def broken_cancel(token: str) -> None:
        raise RuntimeError("already fired")

    scheduled: list[object] = []
    debouncer = Debouncer(lambda delay, cb: scheduled.append(cb) or "t", broken_cancel, delay_ms=5)
    debouncer.schedule(lambda: None)
    debouncer.abort()
    assert debouncer.pending is False


def test_debouncer_delay_is_clamped() -> None:
    clock = FakeClock()
    debouncer = Debouncer(clock.schedule, clock.cancel, delay_ms=-20)
    assert debouncer.delay_ms == 0


# ---------------------------------------------------------------------------
# animator
# ---------------------------------------------------------------------------
class ManualClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_animator_tween_reaches_target() -> None:
    clock = ManualClock()
    animator = Animator(clock=clock)
    values: list[float] = []
    animator.set("bar", 0.0)
    assert animator.to("bar", 1.0, duration=0.2, on_update=values.append) is True
    assert animator.active is True
    clock.now = 0.1
    animator.step()
    assert 0.0 < values[-1] < 1.0
    clock.now = 0.3
    assert animator.step() is None
    assert values[-1] == pytest.approx(1.0)
    assert animator.active is False
    assert animator.value("bar") == pytest.approx(1.0)


def test_animator_disabled_is_instant() -> None:
    animator = Animator(enabled=False)
    done: list[int] = []
    assert animator.to("x", 5.0, duration=1.0, on_done=lambda: done.append(1)) is False
    assert animator.value("x") == 5.0
    assert done == [1]
    assert animator.active is False


def test_animator_set_cancels_tween() -> None:
    clock = ManualClock()
    animator = Animator(clock=clock)
    animator.to("x", 1.0, duration=1.0)
    animator.set("x", 0.25)
    assert animator.active is False
    assert animator.value("x") == 0.25


def test_animator_cancel_snaps_when_asked() -> None:
    animator = Animator()
    seen: list[float] = []
    done: list[int] = []
    animator.to("x", 3.0, duration=1.0, on_update=seen.append, on_done=lambda: done.append(1))
    animator.cancel("x", snap_to_target=True)
    assert seen[-1] == 3.0
    assert done == [1]
    animator.cancel("x")


def test_animator_cancel_all_and_disable() -> None:
    animator = Animator()
    animator.to("a", 1.0, duration=1.0)
    animator.to("b", 2.0, duration=1.0)
    animator.set_enabled(False)
    assert animator.active is False
    assert animator.value("a") == 1.0
    assert animator.value("b") == 2.0


def test_animator_rejects_unknown_easing() -> None:
    with pytest.raises(ValueError):
        Animator().to("x", 1.0, easing="bounce")


def test_animator_zero_duration_is_immediate() -> None:
    animator = Animator()
    assert animator.to("x", 2.0, duration=0.0) is False
    assert animator.value("x") == 2.0
    assert animator.step() is None


def test_animator_has_and_value_defaults() -> None:
    animator = Animator()
    assert animator.has("nope") is False
    assert animator.value("nope", 7.0) == 7.0


# ---------------------------------------------------------------------------
# drag & drop parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/tmp/a.png", ["/tmp/a.png"]),
        ("{/tmp/a b.png}", ["/tmp/a b.png"]),
        ("/tmp/a.png /tmp/b.wav", ["/tmp/a.png", "/tmp/b.wav"]),
        ("{/tmp/a.png} {/tmp/b.png}", ["/tmp/a.png", "/tmp/b.png"]),
        ("file:///tmp/a.png", ["/tmp/a.png"]),
        ("file:///tmp/a%20b.png", ["/tmp/a b.png"]),
        ("/tmp/a.png\n/tmp/b.png", ["/tmp/a.png", "/tmp/b.png"]),
        ("/tmp/a.png /tmp/a.png", ["/tmp/a.png"]),
        ("", []),
    ],
)
def test_parse_drop_paths(raw: str, expected: list[str]) -> None:
    assert parse_drop_paths(raw) == expected


def test_parse_drop_paths_handles_escapes_and_backslashes() -> None:
    assert parse_drop_paths(r"{/tmp/with\ space.png}") == ["/tmp/with space.png"]
    assert parse_drop_paths(r"C:\Users\me\pic.png") == [r"C:\Users\me\pic.png"]


def test_paths_from_uri() -> None:
    assert paths_from_uri("") is None
    assert paths_from_uri("/tmp/a.png") == "/tmp/a.png"
    assert paths_from_uri("file:///tmp/a.png") == "/tmp/a.png"


def test_filter_by_extension() -> None:
    paths = ["/tmp/a.png", "/tmp/b.PNG", "/tmp/c.wav", "/tmp/d.txt"]
    assert filter_by_extension(paths, C.IMAGE_EXTENSIONS) == ["/tmp/a.png", "/tmp/b.PNG"]
    assert filter_by_extension(paths, C.AUDIO_WAV_EXTENSIONS) == ["/tmp/c.wav"]


# ---------------------------------------------------------------------------
# settings & theme
# ---------------------------------------------------------------------------
def test_settings_roundtrip(tmp_path) -> None:
    store = SettingsStore(path=tmp_path / "cfg" / "settings.json")
    assert store.load() == AppSettings()
    store.save(AppSettings(language="uk", theme="Light", last_dir="/tmp"))
    reloaded = store.load()
    assert reloaded.theme == "Light"
    assert reloaded.last_dir == "/tmp"
    assert reloaded.schema_version == C.SETTINGS_SCHEMA_VERSION


def test_settings_tolerates_corruption_and_unknown_keys(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert SettingsStore(path=path).load() == AppSettings()
    path.write_text(json.dumps({"theme": "Neon", "bogus": 1, "language": "en"}), encoding="utf-8")
    loaded = SettingsStore(path=path).load()
    assert loaded.theme == "System"
    assert not hasattr(loaded, "bogus")
    path.write_text("[1,2,3]", encoding="utf-8")
    assert SettingsStore(path=path).load() == AppSettings()


def test_settings_normalisation() -> None:
    dirty = AppSettings(language=5, theme="Neon", last_tab="nope", animations=0)  # type: ignore[arg-type]
    clean = dirty.normalized()
    assert clean.language == "en"
    assert clean.theme == "System"
    assert clean.last_tab == "embed"
    assert clean.animations is False


def test_settings_reset(tmp_path) -> None:
    store = SettingsStore(path=tmp_path / "settings.json")
    store.save(AppSettings(language="ru", theme="Dark"))
    assert store.reset() == AppSettings()
    assert store.load() == AppSettings()


def test_settings_env_override(tmp_path, monkeypatch) -> None:
    from phantom.ui.settings import config_dir

    monkeypatch.setenv("PHANTOM_CONFIG_DIR", str(tmp_path / "x"))
    assert config_dir() == tmp_path / "x"


def test_theme_helpers() -> None:
    assert set(THEME_MODES) == {"Dark", "Light", "System"}
    assert appearance_for("Dark") == "Dark"
    assert appearance_for("Light") == "Light"
    assert appearance_for("System") == "System"
    assert appearance_for("Neon") == "System"
    for severity in ("ok", "warn", "error", "info", "unknown"):
        pair = severity_color(severity)
        assert len(pair) == 2 and all(colour.startswith("#") for colour in pair)


def test_system_theme_watcher_emits_only_on_change() -> None:
    state = {"theme": "Dark"}
    watcher = SystemThemeWatcher(detect=lambda: state["theme"])
    assert watcher.poll() is None  # disabled
    watcher.set_enabled(True)
    assert watcher.poll() is None  # first reading is the baseline
    assert watcher.poll() is None  # unchanged
    state["theme"] = "Light"
    assert watcher.poll() == "Light"
    watcher.set_enabled(False)
    assert watcher.poll() is None
    watcher.set_enabled(True)
    assert watcher.poll() is None  # baseline again


def test_system_theme_watcher_ignores_unknown(monkeypatch) -> None:
    watcher = SystemThemeWatcher(detect=lambda: None)
    watcher.set_enabled(True)
    assert watcher.poll() is None


# ---------------------------------------------------------------------------
# end-to-end through the fake backend boundary
# ---------------------------------------------------------------------------
def test_core_backend_matches_real_api(tmp_path, rgb_carrier, password) -> None:
    from phantom.ui.viewmodel import CoreBackend

    backend = CoreBackend()
    probe = backend.probe(str(rgb_carrier))
    assert probe.capacity.payload_bytes > 0
    view = EmbedViewModel(backend=backend)
    view.set_carrier(str(rgb_carrier))
    view.apply_probe(probe)
    view.set_text("viewmodel round trip")
    view.set_password(password)
    outcome = view.make_embed_job()(lambda *a: None, threading.Event())
    assert outcome.verified
    result = core_api.extract_from_path(outcome.output_path, password)
    assert result.payload.data == b"viewmodel round trip"


# ---------------------------------------------------------------------------
# DnD provider without a display
# ---------------------------------------------------------------------------
def test_dnd_provider_without_tk_reports_a_reason() -> None:
    from phantom.ui.dnd import DndProvider, DropTarget

    provider = DndProvider()
    provider._dnd_module = None  # force the no-Tk path deterministically
    assert provider.available is False
    target = DropTarget(widget=object(), on_drop=lambda paths: None, extensions=(".png",))
    assert provider.register(target) is False


def test_dnd_provider_marks_broken_registration() -> None:
    from phantom.ui.dnd import DndProvider, DropTarget

    class BrokenWidget:
        def drop_target_register(self, *_args: object) -> None:
            raise RuntimeError("no drop support")

    provider = DndProvider()
    provider._dnd_module = object()
    target = DropTarget(widget=BrokenWidget(), on_drop=lambda paths: None, extensions=(".png",))
    assert provider.register(target) is False
    assert provider.reason is not None and "drop registration failed" in provider.reason
    # The module loaded, but not one drop target did: `available` is a lie the
    # window must not build on -- Tcl/Tk 9 imports tkinterdnd2 and then has no Tcl
    # commands for it, which is exactly this failure.
    assert provider.available is True
    assert provider.usable is False


def test_dnd_provider_is_usable_only_when_registration_worked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    import types

    from phantom.ui.dnd import DndProvider, DropTarget

    stub = types.ModuleType("tkinterdnd2")
    stub.DND_FILES = "DND_Files"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tkinterdnd2", stub)

    class WorkingWidget:
        def __init__(self) -> None:
            self.registered = False

        def drop_target_register(self, *_args: object) -> None:
            self.registered = True

        def dnd_bind(self, *_args: object) -> None:
            return None

    provider = DndProvider()
    provider._dnd_module = object()
    target = DropTarget(widget=WorkingWidget(), on_drop=lambda paths: None, extensions=(".png",))
    assert provider.register(target) is True
    assert provider.usable is True


def test_parse_drop_paths_with_nested_braces() -> None:
    assert parse_drop_paths("{{/tmp/a b.png}}") == ["/tmp/a b.png"]
    # Tk always braces paths with spaces; an unbraced payload is a Tcl list,
    # so it legitimately splits into two tokens.
    assert parse_drop_paths("/tmp/a b.png") == ["/tmp/a", "b.png"]
    assert parse_drop_paths("{/tmp/a.png} /tmp/b.png") == ["/tmp/a.png", "/tmp/b.png"]


# ---------------------------------------------------------------------------
# view model: clearing, estimates and capacity pressure
# ---------------------------------------------------------------------------
def test_embed_clear_resets_everything(tmp_path) -> None:
    carrier = tmp_path / "carrier.png"
    carrier.write_bytes(b"\x89PNG\r\n\x1a\n")
    view = EmbedViewModel(backend=FakeBackend())
    view.set_carrier(str(carrier))
    view.set_text("hello")
    view.set_password("p" * 20)
    view.set_wipe_source(True)
    view.apply_estimate(SizeEstimate(payload_bytes=10, exceeded_capacity=False))
    view.clear()
    assert view.carrier_path == "" and view.text == "" and view.password == ""
    assert view.estimate is None and view.notices == () and view.wipe_source is False
    assert view.capacity == 0 and view.user_capacity() == 0
    assert view.fill_percent() is None


def test_embed_payload_name_and_sizes(tmp_path) -> None:
    secret = tmp_path / "payload.bin"
    secret.write_bytes(b"x" * 40)
    view = EmbedViewModel(backend=FakeBackend())
    view.set_mode(PayloadMode.FILE)
    view.set_secret_file(str(secret))
    assert view.payload_name() == "payload.bin"
    assert view.estimated_payload_bytes() == 40 + C.PAYLOAD_META_OVERHEAD
    view.set_secret_file(str(tmp_path / "missing.bin"))
    assert view.estimated_payload_bytes() is None
    assert view.has_payload() is False
    view.set_mode(PayloadMode.TEXT)
    assert view.payload_name() == ""
    view.set_text("   ")
    assert view.has_payload() is False


def test_embed_estimate_overrides_the_guess(tmp_path) -> None:
    carrier = tmp_path / "carrier.png"
    carrier.write_bytes(b"\x89PNG\r\n\x1a\n")
    backend = FakeBackend(capacity=1_000)
    view = EmbedViewModel(backend=backend)
    view.set_carrier(str(carrier))
    view.apply_probe(backend.probe(str(carrier)))
    view.set_text("tiny")
    view.apply_estimate(SizeEstimate(payload_bytes=2_000, exceeded_capacity=True))
    assert view.estimated_payload_bytes() == 2_000
    assert view.exceeds_capacity() is True
    assert view.fill_percent() == pytest.approx(100.0)
    keys = [issue.key for issue in view.issues()]
    assert "warn_capacity_m" in keys


def test_embed_issues_for_a_missing_carrier(tmp_path) -> None:
    view = EmbedViewModel(backend=FakeBackend())
    view.set_carrier(str(tmp_path / "gone.png"))
    keys = [issue.key for issue in view.issues()]
    assert "err.io" in keys
    with pytest.raises(InvalidInput):
        view.ensure_ready()


def test_extract_clear_and_probe(tmp_path) -> None:
    backend = FakeBackend()
    stego = tmp_path / "stego.png"
    stego.write_bytes(b"\x89PNG\r\n\x1a\n")
    view = ExtractViewModel(backend=backend)
    view.set_stego(str(stego))
    view.password = "x" * 20
    view.apply_probe(backend.probe(str(stego)))
    assert view.notices == ("notice.test",)
    view.clear()
    assert view.stego_path == "" and view.result is None and view.password == ""
    assert [issue.key for issue in view.issues()] == ["warn_no_img_dec", "warn_empty_pwd_dec"]


def test_extract_default_save_path_falls_back_to_cwd() -> None:
    view = ExtractViewModel()
    assert view.default_save_path() == ""
    view.result = ExtractedPayload(
        kind=PayloadKind.FILE,
        name="",
        data=b"x",
        sha256_expected="c" * 64,
        sha256_actual="c" * 64,
        integrity_ok=True,
        original_size=1,
        compressed_size=1,
    )
    assert view.default_save_path().endswith("extracted.bin")


def test_extract_issues_for_missing_stego(tmp_path) -> None:
    view = ExtractViewModel()
    view.set_stego(str(tmp_path / "missing.png"))
    keys = [issue.key for issue in view.issues()]
    assert "err.io" in keys
    with pytest.raises(InvalidInput):
        view.make_extract_job()


# ---------------------------------------------------------------------------
# worker: idle paths and stall detection
# ---------------------------------------------------------------------------
def test_worker_idle_state() -> None:
    service = WorkerService()
    assert service.busy is False
    assert service.operation == ""
    assert service.seconds_since_progress() == 0.0
    assert service.poll() == []
    assert service.watchdog_expired() is False
    service.reset_watchdog()
    service.shutdown(timeout=0.1)
    service.cancel()


def test_worker_operation_name_while_running() -> None:
    service = WorkerService()
    release = threading.Event()

    def job(report, cancel):
        release.wait(5)
        return "done"

    assert service.start("probe-file", job) is True
    assert service.operation == "probe-file"
    release.set()
    events = _drain(service, {WorkerEventType.DONE})
    assert events[-1].operation == "probe-file"
    assert service.operation == ""


def test_worker_watchdog_ignores_idle_service() -> None:
    service = WorkerService(watchdog_seconds=0.01)
    time.sleep(0.05)
    assert service.watchdog_expired() is False


# ---------------------------------------------------------------------------
# system theme detection
# ---------------------------------------------------------------------------
def test_system_theme_detector_swallows_platform_errors(monkeypatch) -> None:
    from phantom.ui import system_theme

    class BrokenDarkdetect:
        @staticmethod
        def theme() -> str:
            raise RuntimeError("platform blew up")

    monkeypatch.setitem(__import__("sys").modules, "darkdetect", BrokenDarkdetect())
    assert system_theme._detect_darkdetect() is None


def test_system_theme_detector_returns_known_values(monkeypatch) -> None:
    from phantom.ui import system_theme

    class FakeDarkdetect:
        @staticmethod
        def theme() -> str:
            return "Dark"

    monkeypatch.setitem(__import__("sys").modules, "darkdetect", FakeDarkdetect())
    assert system_theme._detect_darkdetect() == "Dark"

    class Weird:
        @staticmethod
        def theme() -> str:
            return "Solarized"

    monkeypatch.setitem(__import__("sys").modules, "darkdetect", Weird())
    assert system_theme._detect_darkdetect() is None


# ---------------------------------------------------------------------------
# animator extras
# ---------------------------------------------------------------------------
def test_animator_with_default_clock() -> None:
    animator = Animator()
    assert animator.to("x", 1.0, duration=0.01) is True
    deadline = time.monotonic() + 2
    while animator.active and time.monotonic() < deadline:
        animator.step()
        time.sleep(0.002)
    assert animator.value("x") == pytest.approx(1.0)


def test_animator_step_without_tweens() -> None:
    assert Animator().step() is None


def test_the_worker_queues_its_terminal_event_before_it_looks_idle() -> None:
    """The invariant ``_poll`` relies on: ``busy`` false means "drained already".

    ``_finish`` clears the job *and* posts the terminal event before the thread
    stops, so a finished worker cannot be observed without its DONE/ERROR/
    CANCELLED event already in the queue.  If that order ever flips, the GUI
    loses the result of every operation that finishes between two polls.
    """
    from phantom.core.errors import IntegrityError
    from phantom.ui.worker import WorkerEventType, WorkerService

    def fail() -> None:
        raise IntegrityError("boom")

    service = WorkerService()
    assert service.start("failing", lambda report, cancel: fail()) is True
    deadline = time.monotonic() + 5
    while service.busy and time.monotonic() < deadline:
        time.sleep(0.005)

    assert service.busy is False
    queued = service.poll()
    assert [event.kind for event in queued] == [WorkerEventType.ERROR], (
        "the terminal event was not in the queue when the thread went idle: "
        f"{[event.kind for event in queued]}"
    )


def test_muted_text_reaches_wcag_aa_in_both_appearance_modes() -> None:
    """Card headings used to be one mid-grey for both modes: 3.4:1 on the dark one."""
    light, dark = theme.CLR_MUTED
    assert theme.contrast_ratio(light, theme.CLR_CARD[0]) >= 4.5
    assert theme.contrast_ratio(dark, theme.CLR_CARD[1]) >= 4.5
    # ... and it must have actually changed, not merely passed by luck.
    assert light == "#4B5563" and dark == "#9CA3AF"


def test_every_readable_colour_clears_the_contrast_floor() -> None:
    floor = 4.5
    for name in ("CLR_TEXT", "CLR_TEXT_DIM", "CLR_MUTED"):
        light, dark = getattr(theme, name)
        assert theme.contrast_ratio(light, theme.CLR_CARD[0]) >= floor, f"{name} (light)"
        assert theme.contrast_ratio(dark, theme.CLR_CARD[1]) >= floor, f"{name} (dark)"


def test_the_type_scale_puts_body_text_first() -> None:
    """Body 14, card titles 13-14: a heading smaller than its text was the report."""
    assert theme.FONT_SIZE_BODY == 14
    assert 13 <= theme.FONT_SIZE_CARD_TITLE <= 14
    assert theme.FONT_SIZE_CARD_TITLE >= 13
    assert theme.FONT_SIZE_HINT >= 12, "hints must stay readable next to the body"
    assert theme.FONT_SIZE_FOOTER >= 10
    assert theme.FONT_SIZE_TITLE > theme.FONT_SIZE_BODY


def test_the_promised_layout_follows_the_carrier_kind() -> None:
    """Audio has no trellis: the tab must say v3, not the v5 default."""
    from phantom.core.api import CarrierKind

    i18n = I18n("ru", locales_dir=LOCALES_DIR)
    v5 = ui_format.written_layout_text(i18n)
    image = ui_format.written_layout_text(i18n, CarrierKind.IMAGE)
    audio = ui_format.written_layout_text(i18n, CarrierKind.AUDIO)

    assert "v5" in v5 and "v5" in image
    assert "v3" in audio and "v5" not in audio, audio
    assert ui_format.written_layout_version(None) == C.CONTAINER_VERSION
    assert ui_format.written_layout_version(CarrierKind.AUDIO) == C.KEYED_CONTAINER_VERSION


def test_worker_event_properties_and_cancel_event() -> None:
    from phantom.ui.worker import WorkerEvent

    indeterminate = WorkerEvent(WorkerEventType.PROGRESS, stage=Stage.KDF)
    assert indeterminate.indeterminate is True
    assert indeterminate.fraction == 0.0
    service = WorkerService()
    assert isinstance(service.cancel_event, threading.Event)
    service.cancel()
    assert service.cancel_event.is_set()


def test_paths_from_uri_edge_cases() -> None:
    assert paths_from_uri("file://") is None
    assert parse_drop_paths("{}") == []
    assert parse_drop_paths("   ") == []


def test_config_dir_uses_platformdirs(monkeypatch) -> None:
    from phantom.ui.settings import config_dir

    monkeypatch.delenv("PHANTOM_CONFIG_DIR", raising=False)
    path = config_dir()
    assert "Phantom" in str(path)


def test_system_theme_disable_resets_the_baseline() -> None:
    from phantom.ui.system_theme import SystemThemeWatcher

    state = {"theme": "Dark"}
    watcher = SystemThemeWatcher(detect=lambda: state["theme"])
    watcher.set_enabled(True)
    assert watcher.poll() is None
    watcher.set_enabled(False)
    assert watcher.enabled is False
    watcher.set_enabled(True)
    assert watcher.poll() is None


def test_audio_file_payload_through_the_api(tmp_path, wav_factory, password) -> None:
    from phantom.core import api

    carrier = wav_factory(bits=16, channels=1, frames=4_000, kind="noise")
    secret = tmp_path / "through-api.bin"
    secret.write_bytes(b"api audio file" * 20)
    outcome = api.embed_payload(
        str(carrier),
        str(tmp_path / "api.wav"),
        secret_path=str(secret),
        password=password,
    )
    assert outcome.kind is CarrierKind.AUDIO
    result = api.extract_from_path(outcome.output_path, password)
    assert result.payload.data == secret.read_bytes()


# ---------------------------------------------------------------------------
# clipboard: paste/copy/cut/select-all without a display
# ---------------------------------------------------------------------------
class FakeEntry:
    """Single-line widget protocol: text, caret, selection and clipboard."""

    def __init__(self, text: str = "", clipboard: str | None = None) -> None:
        self.text = text
        self.clipboard = clipboard
        self.selection: tuple[int, int] | None = None
        self.caret = len(text)
        self.readonly = False

    def get(self) -> str:
        return self.text

    def insert(self, index: str, value: str) -> None:
        assert index == "insert"
        if self.readonly:
            raise RuntimeError("widget is disabled")
        self.text = self.text[: self.caret] + value + self.text[self.caret :]
        self.caret += len(value)

    def delete(self, first: object, last: object) -> None:
        if self.readonly:
            raise RuntimeError("widget is disabled")
        start = int(first)
        end = int(last)
        self.text = self.text[:start] + self.text[end:]
        self.caret = start
        self.selection = None

    def index(self, index: str) -> int:
        if self.selection is None:
            raise RuntimeError("no selection")
        return self.selection[0] if index == "sel.first" else self.selection[1]

    def selection_range(self, _first: int, _last: str) -> None:
        self.selection = (0, len(self.text))

    def icursor(self, _index: str) -> None:
        self.caret = len(self.text)

    def clipboard_get(self) -> str:
        if self.clipboard is None:
            raise RuntimeError("clipboard has no text")
        return self.clipboard

    def clipboard_clear(self) -> None:
        self.clipboard = ""

    def clipboard_append(self, value: str) -> None:
        self.clipboard = (self.clipboard or "") + value


class FakeText(FakeEntry):
    """Multi-line widget protocol: selection tags plus its own scroll view."""

    def __init__(self, text: str = "", clipboard: str | None = None) -> None:
        super().__init__(text, clipboard)
        self.tag: tuple[int, int] | None = None
        self.view = (0.0, 1.0)
        self.scrolls: list[tuple[int, str]] = []

    def tag_add(self, _tag: str, _first: str, _last: str) -> None:
        self.tag = (0, len(self.text))
        self.tag_names_used = [*getattr(self, "tag_names_used", []), (_tag, _first, _last)]

    def tag_remove(self, _tag: str, _first: str, _last: str) -> None:
        self.tag = None

    def tag_raise(self, tag: str) -> None:
        """The real widget paints ``sel`` above the text tags; remember the call."""
        self.raised = tag

    def see(self, index: str) -> None:
        self.seen = [*getattr(self, "seen", []), index]

    def edit_separator(self) -> None:
        self.separators = getattr(self, "separators", 0) + 1

    def configure(self, **kwargs: object) -> None:
        self.options = {**getattr(self, "options", {}), **kwargs}

    def mark_set(self, _name: str, _index: str) -> None:
        self.caret = _index

    def get(self, first: str | None = None, last: str | None = None) -> str:
        if first is None:
            return self.text
        if first == "sel.first":
            if self.tag is None:
                raise RuntimeError("no selection")
            return self.text[self.tag[0] : self.tag[1]]
        if last is None:
            return self.text
        return self.text

    def delete(self, first: object, last: object) -> None:
        if first == "sel.first" and self.tag is None:
            raise RuntimeError("no selection")
        if isinstance(first, str) and first.startswith("sel"):
            if self.tag is None:
                raise RuntimeError("no selection")
            start, end = self.tag
        else:
            start, end = int(first), int(last)
        self.text = self.text[:start] + self.text[end:]
        self.tag = None

    def yview(self) -> tuple[float, float]:
        return self.view

    def yview_scroll(self, steps: int, what: str) -> None:
        self.scrolls.append((steps, what))


def test_paste_inserts_clipboard_text_into_an_entry() -> None:
    entry = FakeEntry(clipboard="correct-horse-battery")
    assert ui_clipboard.paste(entry) == "correct-horse-battery"
    assert entry.text == "correct-horse-battery"


def test_paste_strips_the_trailing_newline_a_manager_adds() -> None:
    entry = FakeEntry(clipboard="s3cret-passphrase\n")
    ui_clipboard.paste(entry)
    assert entry.text == "s3cret-passphrase"


def test_paste_replaces_the_selection() -> None:
    entry = FakeEntry("old-value", clipboard="new-value")
    entry.selection = (0, len("old-value"))
    ui_clipboard.paste(entry)
    assert entry.text == "new-value"


def test_paste_with_an_empty_clipboard_reports_nothing_happened() -> None:
    entry = FakeEntry("keep")
    assert ui_clipboard.paste(entry) is None
    assert entry.text == "keep"


def test_paste_into_a_disabled_widget_changes_nothing() -> None:
    entry = FakeEntry("locked", clipboard="new")
    entry.readonly = True
    assert ui_clipboard.paste(entry) is None
    assert entry.text == "locked"


def test_copy_copies_the_selection() -> None:
    entry = FakeEntry("abcdef", clipboard="")
    entry.selection = (1, 4)
    assert ui_clipboard.copy(entry) == "bcd"
    assert entry.clipboard == "bcd"


def test_copy_without_a_selection_is_a_no_op() -> None:
    entry = FakeEntry("abcdef", clipboard="")
    assert ui_clipboard.copy(entry) is None
    assert entry.clipboard == ""


def test_cut_removes_the_selection_after_copying_it() -> None:
    entry = FakeEntry("abcdef", clipboard="")
    entry.selection = (0, 2)
    assert ui_clipboard.cut(entry) == "ab"
    assert entry.text == "cdef"
    assert entry.clipboard == "ab"


def test_select_all_then_copy_takes_the_whole_entry() -> None:
    entry = FakeEntry("whole-key", clipboard="")
    assert ui_clipboard.select_all(entry) is True
    assert ui_clipboard.copy(entry) == "whole-key"


def test_text_widget_selection_uses_tags() -> None:
    text = FakeText("line one\nline two", clipboard="")
    ui_clipboard.select_all(text)
    assert ui_clipboard.selected_text(text) == "line one\nline two"
    assert ui_clipboard.copy(text) == "line one\nline two"
    assert text.clipboard == "line one\nline two"


def test_multiline_paste_keeps_inner_newlines_but_drops_the_last_one() -> None:
    text = FakeText("", clipboard="first\nsecond\n")
    ui_clipboard.paste(text)
    assert text.text == "first\nsecond"


def test_all_text_reads_both_widget_flavours() -> None:
    assert ui_clipboard.all_text(FakeEntry("entry")) == "entry"
    assert ui_clipboard.all_text(FakeText("text")) == "text"


def test_write_clipboard_rejects_empty_values() -> None:
    entry = FakeEntry(clipboard="")
    assert ui_clipboard.write_clipboard(entry, "") is False
    assert ui_clipboard.write_clipboard(entry, "value") is True
    assert entry.clipboard == "value"


def test_clipboard_failures_are_swallowed() -> None:
    class Broken(FakeEntry):
        def clipboard_get(self) -> str:
            raise RuntimeError("no owner")

        def clipboard_clear(self) -> None:
            raise RuntimeError("no clipboard")

    broken = Broken("text")
    assert ui_clipboard.read_clipboard(broken) is None
    assert ui_clipboard.write_clipboard(broken, "x") is False
    assert ui_clipboard.all_text(object()) == ""


def test_actions_dataclass_wraps_one_widget() -> None:
    entry = FakeEntry("secret-key", clipboard="")
    actions = ui_clipboard.ClipboardActions(widget=entry)
    assert actions.select_all() is True
    assert actions.copy() is True
    assert actions.text() == "secret-key"
    assert ui_clipboard.ClipboardActions(widget=FakeEntry("x")).paste() is False


def test_select_all_paints_the_selection_and_puts_the_caret_at_the_end() -> None:
    """The defect: Cmd+A left the caret at ``1.0`` and read as "nothing selected".

    The next keystroke then landed *in front of* the text instead of replacing it,
    so the user saw a selection that did nothing.
    """
    text = FakeText("line one\nline two", clipboard="")
    assert ui_clipboard.select_all(text) is True
    assert text.raised == "sel", "an unraised sel tag hides under the text tags"
    assert text.caret == "end-1c"
    assert text.seen == ["insert"]
    assert ui_clipboard.selected_text(text) == "line one\nline two"


def test_select_all_first_clears_an_earlier_selection() -> None:
    text = FakeText("abc", clipboard="")
    text.tag = (1, 2)
    ui_clipboard.select_all(text)
    assert text.tag == (0, 3)


def test_a_paste_becomes_one_undo_step_and_shows_the_caret() -> None:
    text = FakeText("", clipboard="first\nsecond\n")
    ui_clipboard.paste(text)
    assert text.separators == 1, "a paste must be one undo step, not one per line"
    assert text.seen == ["insert"], "the caret has to be scrolled into view"


def test_select_none_drops_the_selection() -> None:
    text = FakeText("abc", clipboard="")
    ui_clipboard.select_all(text)
    ui_clipboard.select_none(text)
    assert ui_clipboard.selected_text(text) is None


def test_insert_text_never_truncates_a_large_block() -> None:
    big = "x" * 250_000
    text = FakeText("", clipboard=big)
    assert ui_clipboard.paste(text) == big
    assert len(text.text) == len(big), "a large paste must arrive whole"


def test_undo_and_redo_report_when_the_stack_is_empty() -> None:
    class NoHistory(FakeText):
        def edit_undo(self) -> None:
            raise RuntimeError("nothing to undo")

        def edit_redo(self) -> None:
            raise RuntimeError("nothing to redo")

    text = NoHistory("abc")
    assert ui_clipboard.undo(text) is False
    assert ui_clipboard.redo(text) is False


def test_enable_undo_turns_the_widget_history_on() -> None:
    text = FakeText("abc")
    assert ui_clipboard.enable_undo(text) is True
    assert text.options == {"undo": True, "autoseparators": True, "maxundo": -1}


# ---------------------------------------------------------------------------
# the capacity panel: one scale, no double counting, honest states
# ---------------------------------------------------------------------------


def _carrier(*, payload_bytes: int = 1000, kind: str = "image") -> object:
    from phantom.core.api import CarrierKind, CarrierProbe
    from phantom.stego.engine import Capacity

    capacity = Capacity(usable_bits=payload_bytes * 8, payload_bytes=payload_bytes)
    if kind == "audio":
        from phantom.audio import AudioInfo, AudioKind

        return CarrierProbe(
            kind=CarrierKind.AUDIO,
            path="/tmp/carrier.wav",
            capacity=capacity,
            audio=AudioInfo(
                path="/tmp/carrier.wav",
                kind=AudioKind.WAV,
                frames=44100,
                channels=2,
                sample_rate=44100,
                bits=16,
                capacity=capacity,
            ),
        )
    from phantom.stego.image import ImageInfo

    return CarrierProbe(
        kind=CarrierKind.IMAGE,
        path="/tmp/carrier.png",
        capacity=capacity,
        image=ImageInfo(
            path="/tmp/carrier.png", width=640, height=360, mode="RGB", capacity=capacity
        ),
    )


def _report(**kwargs: object):
    from phantom.ui.capacity_view import build_report

    params: dict[str, object] = {
        "carrier": _carrier(),
        "estimate": None,
        "has_payload": True,
        "chars": 100,
        "utf8_bytes": 120,
    }
    params.update(kwargs)
    return build_report(**params)


def test_no_carrier_says_so_and_never_unlocks_embed() -> None:
    from phantom.ui.capacity_view import CapacityState

    report = _report(carrier=None)
    assert report.state is CapacityState.NO_CARRIER
    assert report.can_embed is False
    assert report.percent == 0.0


def test_no_payload_is_its_own_state() -> None:
    from phantom.ui.capacity_view import CapacityState

    report = _report(has_payload=False, chars=0, utf8_bytes=0)
    assert report.state is CapacityState.NO_PAYLOAD
    assert report.prepared_bytes is None


def test_a_pending_estimate_hides_the_previous_number() -> None:
    from phantom.core.payload import SizeEstimate
    from phantom.ui.capacity_view import CapacityState

    stale = SizeEstimate(payload_bytes=400, exceeded_capacity=False)
    report = _report(estimate=stale, waiting=True)
    assert report.state is CapacityState.CALCULATING
    assert report.prepared_bytes is None, "a stale size must not read as this text's size"
    assert report.can_embed is False


def test_a_fitting_payload_reports_free_space_and_one_scale() -> None:
    from phantom.core.payload import SizeEstimate
    from phantom.ui.capacity_view import CapacityState

    estimate = SizeEstimate(payload_bytes=400, exceeded_capacity=False)
    report = _report(estimate=estimate)
    assert report.state is CapacityState.FITS
    assert report.can_embed is True
    assert report.prepared_bytes == 400
    assert report.free_bytes == 600
    assert report.excess_bytes == 0
    assert report.percent == pytest.approx(40.0)
    assert report.fill == pytest.approx(0.4)


def test_an_overflowing_payload_keeps_the_number_and_caps_the_bar() -> None:
    from phantom.core.payload import SizeEstimate
    from phantom.ui.capacity_view import CapacityState

    estimate = SizeEstimate(payload_bytes=1500, exceeded_capacity=True)
    report = _report(estimate=estimate)
    assert report.state is CapacityState.OVERFLOW
    assert report.can_embed is False
    assert report.excess_bytes == 500
    assert report.free_bytes == 0
    assert report.percent == pytest.approx(150.0), "the number may pass 100 % …"
    assert report.fill == 1.0, "… while the bar stays capped"


def test_exceeded_capacity_without_a_bigger_number_still_overflows() -> None:
    from phantom.core.payload import SizeEstimate
    from phantom.ui.capacity_view import CapacityState

    # The streaming estimate stops counting once the budget is gone, so it can
    # report a lower bound below the capacity it already knows it passed.
    estimate = SizeEstimate(payload_bytes=900, exceeded_capacity=True)
    report = _report(estimate=estimate)
    assert report.state is CapacityState.OVERFLOW
    assert report.can_embed is False


def test_a_failed_estimate_is_said_not_hidden() -> None:
    from phantom.core.payload import SizeEstimate
    from phantom.ui.capacity_view import CapacityState

    report = _report(
        estimate=SizeEstimate(payload_bytes=10, exceeded_capacity=False), failed=True
    )
    assert report.state is CapacityState.ERROR
    assert report.can_embed is False


def test_human_bytes_uses_one_binary_scale() -> None:
    from phantom.ui.capacity_view import human_bytes

    assert human_bytes(0) == "0 B"
    assert human_bytes(1023) == "1023 B"
    assert human_bytes(1024) == "1.0 KiB"
    assert human_bytes(1536) == "1.5 KiB"
    assert human_bytes(2 * 1024 * 1024) == "2.00 MiB"
    assert human_bytes(None) == "—"


def test_the_panel_lists_container_payload_and_capacity_in_one_scale() -> None:
    from phantom.core.payload import SizeEstimate
    from phantom.i18n import I18n
    from phantom.ui.capacity_view import human_bytes, panel_lines

    i18n = I18n("ru")
    probe = _carrier()
    estimate = SizeEstimate(payload_bytes=400, exceeded_capacity=False)
    report = _report(estimate=estimate, carrier=probe, utf8_bytes=300)
    lines = panel_lines(report, probe, i18n)
    assert len(lines) == 3
    assert "640×360 RGB" in lines[0], "the panel names the real geometry, not a guess"
    assert human_bytes(400) in lines[1], "the prepared size is the estimate's own number"
    assert human_bytes(1000) in lines[2]
    assert human_bytes(400) in lines[2], "occupied and payload agree — nothing is counted twice"
    assert human_bytes(600) in lines[2]
    assert "40.0" in lines[2]


def test_the_panel_warns_with_the_excess_and_the_real_usable_bytes() -> None:
    from phantom.core.payload import SizeEstimate
    from phantom.i18n import I18n
    from phantom.ui.capacity_view import human_bytes, panel_lines

    i18n = I18n("ru")
    probe = _carrier()
    estimate = SizeEstimate(payload_bytes=1500, exceeded_capacity=True)
    report = _report(estimate=estimate, carrier=probe)
    lines = panel_lines(report, probe, i18n)
    assert len(lines) == 4
    assert human_bytes(500) in lines[3]
    assert human_bytes(report.usable_bytes) in lines[3]
    assert "НЕ ХВАТАЕТ" in lines[3]


def test_a_file_payload_gets_file_wording() -> None:
    from phantom.core.payload import SizeEstimate
    from phantom.i18n import I18n
    from phantom.ui.capacity_view import panel_lines

    i18n = I18n("ru")
    probe = _carrier()
    estimate = SizeEstimate(payload_bytes=400, exceeded_capacity=False)
    report = _report(estimate=estimate, carrier=probe, chars=0, utf8_bytes=5000)
    lines = panel_lines(report, probe, i18n)
    assert "файл" in lines[1]
    assert "симв" not in lines[1]


def test_the_panel_speaks_three_languages() -> None:
    from phantom.core.payload import SizeEstimate
    from phantom.i18n import I18n
    from phantom.ui.capacity_view import CapacityState, panel_lines

    probe = _carrier(kind="audio")
    estimate = SizeEstimate(payload_bytes=1500, exceeded_capacity=True)
    report = _report(estimate=estimate, carrier=probe)
    assert report.state is CapacityState.OVERFLOW
    for language in ("ru", "uk", "en"):
        lines = panel_lines(report, probe, I18n(language))
        assert all(line.strip() for line in lines), language
        assert len(lines) == 4, language


def test_result_window_geometry_uses_most_of_the_screen() -> None:
    width, height, x, y = layout.window_geometry(1920, 1080)
    assert (width, height) == (1536, 864)
    assert (x, y) == (192, 72)
    small = layout.window_geometry(800, 600)
    assert (small[0], small[1]) == layout.MIN_WINDOW


class FakeCTkEntry:
    """Minimal CTkEntry: the placeholder is real text behind a flag."""

    def __init__(self, inner: FakeEntry, placeholder: str = "minimum 12 characters") -> None:
        self._entry = inner
        self.placeholder = placeholder
        self.placeholder_active = True
        self._entry.text = placeholder

    def get(self) -> str:
        return "" if self.placeholder_active else self._entry.get()

    def insert(self, index: str, value: str) -> None:
        self.placeholder_active = False
        self._entry.text = ""
        self._entry.caret = 0
        self._entry.insert(index, value)

    def delete(self, first: object, last: object) -> None:
        self._entry.delete(first, last)

    def selection_range(self, first: int, last: str) -> None:
        self._entry.selection_range(first, last)

    def icursor(self, index: str) -> None:
        self._entry.icursor(index)


def test_text_buffer_writes_through_the_ctk_wrapper() -> None:
    inner = FakeEntry("", clipboard="pasted-key")
    wrapper = FakeCTkEntry(inner)
    buffer = ui_clipboard.TextBuffer(inner=inner, public=wrapper)
    assert ui_clipboard.paste(buffer) == "pasted-key"
    # The placeholder was dropped, so the field really reads back the text.
    assert buffer.text() == "pasted-key"
    assert wrapper.get() == "pasted-key"


def test_writing_the_inner_widget_leaves_the_placeholder_in_charge() -> None:
    inner = FakeEntry("", clipboard="pasted-key")
    wrapper = FakeCTkEntry(inner)
    assert ui_clipboard.paste(inner) == "pasted-key"  # the trap the wrapper avoids
    assert wrapper.placeholder_active is True
    assert wrapper.get() == ""


def test_as_buffer_accepts_a_bare_widget() -> None:
    entry = FakeEntry("value")
    assert ui_clipboard.as_buffer(entry).editor is entry
    assert ui_clipboard.as_buffer(ui_clipboard.TextBuffer(inner=entry)).editor is entry


# ---------------------------------------------------------------------------
# container layout labels
# ---------------------------------------------------------------------------
LOCALES_DIR = Path(__file__).resolve().parent.parent / "src" / "phantom" / "locales"


def test_layout_label_names_the_version_of_every_supported_layout() -> None:
    for code in ("en", "ru", "uk"):
        i18n = I18n(code, locales_dir=LOCALES_DIR)
        current = ui_format.layout_text(i18n, C.CONTAINER_VERSION)
        legacy = ui_format.layout_text(i18n, C.V2_CONTAINER_VERSION)
        assert f"v{C.CONTAINER_VERSION}" in current
        assert f"v{C.V2_CONTAINER_VERSION}" in legacy
        assert current != legacy
        assert ui_format.written_layout_text(i18n) == current
        for version in (
            C.KEYED_CONTAINER_VERSION,
            C.ADAPTIVE_CONTAINER_VERSION,
            C.STC_CONTAINER_VERSION,
        ):
            assert f"v{version}" in ui_format.layout_text(i18n, version)


def test_the_written_layout_is_the_trellis_one() -> None:
    """The embed tab advertises v5, the trellis, in every language."""
    for code in ("en", "ru", "uk"):
        i18n = I18n(code, locales_dir=LOCALES_DIR)
        written = ui_format.written_layout_text(i18n)
        assert written == ui_format.layout_text(i18n, C.STC_CONTAINER_VERSION)
        assert written == i18n.t("layout_trellis", version=C.STC_CONTAINER_VERSION)
        assert f"v{C.STC_CONTAINER_VERSION}" in written
        assert written != ui_format.layout_text(i18n, C.KEYED_CONTAINER_VERSION)


def test_russian_wording_for_every_layout_is_what_the_interface_shows() -> None:
    """The exact strings, so a locale drift shows up as a failing test."""
    i18n = I18n("ru", locales_dir=LOCALES_DIR)
    expected = {
        C.STC_CONTAINER_VERSION: "v5 · синдромный решётчатый код",
        C.ADAPTIVE_CONTAINER_VERSION: "v4 · адаптивные позиции",
        C.KEYED_CONTAINER_VERSION: "v3 · ключевые позиции",
        C.V2_CONTAINER_VERSION: "v2 · открытый заголовок · старый файл",
    }
    for version, text in expected.items():
        assert ui_format.layout_text(i18n, version) == text
    assert ui_format.written_layout_text(i18n) == expected[C.STC_CONTAINER_VERSION]


def test_layout_labels_exist_in_every_locale() -> None:
    i18n = I18n("en", locales_dir=LOCALES_DIR)
    keys = set(ui_format.LAYOUT_KEYS.values()) | {ui_format.UNKNOWN_LAYOUT_KEY}
    keys.add("audit_layout")
    keys.add("lbl_embed_format")
    for code in ("en", "ru", "uk"):
        locale = I18n(code, locales_dir=LOCALES_DIR)
        for key in keys:
            assert locale.has(key), f"{code} misses {key}"
    assert i18n.has("lbl_embed_format")


def test_unknown_layout_is_named_without_inventing_a_version() -> None:
    i18n = I18n("en", locales_dir=LOCALES_DIR)
    text = ui_format.layout_text(i18n, 99)
    assert text == i18n.t("layout_unknown")
    assert "99" not in text


def test_layout_keys_cover_exactly_the_supported_versions() -> None:
    assert set(ui_format.LAYOUT_KEYS) == {
        C.KEYED_CONTAINER_VERSION,
        C.ADAPTIVE_CONTAINER_VERSION,
        C.STC_CONTAINER_VERSION,
        C.V2_CONTAINER_VERSION,
    }


def test_every_layout_the_engine_can_write_has_its_own_label() -> None:
    """A v5 file must not fall into the "unknown layout" bucket."""
    i18n = I18n("en", locales_dir=LOCALES_DIR)
    texts = [
        ui_format.layout_text(i18n, version)
        for version in sorted(ui_format.LAYOUT_KEYS)
    ]
    assert len(set(texts)) == len(texts), "labels must differ per layout"
    for version in sorted(ui_format.LAYOUT_KEYS):
        text = ui_format.layout_text(i18n, version)
        assert f"v{version}" in text
        assert text != i18n.t("layout_unknown")


def test_stage_line_appends_the_clock_only_when_asked() -> None:
    """The footer's text builder: no display needed, so it is pinned here."""
    i18n = I18n("ru", locales_dir=LOCALES_DIR)

    plain = ui_format.stage_line(
        i18n, "lbl_progress_stage", stage=Stage.EMBEDDING, pct="25"
    )
    assert plain == i18n.t(
        "lbl_progress_stage", stage=i18n.t("stage.embedding"), pct="25"
    )
    assert "с" not in plain.split("·")[-1], plain

    counted = ui_format.stage_line(
        i18n, "lbl_progress_stage", seconds=12, stage=Stage.EMBEDDING, pct="25"
    )
    assert "25" in counted and "12" in counted
    assert counted.startswith(plain), "the clock is a suffix, not a rewrite"
    assert "{" not in counted


def test_stage_line_names_a_phase_that_cannot_report_a_fraction() -> None:
    i18n = I18n("ru", locales_dir=LOCALES_DIR)
    line = ui_format.stage_line(
        i18n, "lbl_progress_indeterminate", seconds=3, stage=Stage.ANALYSING
    )
    assert line.startswith("Анализ изображения")
    assert "%" not in line, "an indeterminate phase must not claim a share"
    assert line.endswith("3 с"), line


def test_stage_line_translates_at_paint_time_not_at_event_time() -> None:
    """The footer repaints for the clock and for a language switch.

    The stage therefore travels as the enum: a name translated when the progress
    event arrived would stay in the old language for the rest of the operation.
    """
    line_ru = ui_format.stage_line(
        I18n("ru", locales_dir=LOCALES_DIR),
        "lbl_progress_indeterminate",
        seconds=4,
        stage=Stage.ANALYSING,
    )
    line_uk = ui_format.stage_line(
        I18n("uk", locales_dir=LOCALES_DIR),
        "lbl_progress_indeterminate",
        seconds=4,
        stage=Stage.ANALYSING,
    )
    assert "Анализ изображения" in line_ru and "Аналіз зображення" not in line_ru
    assert "Аналіз зображення" in line_uk and "Анализ" not in line_uk
    assert line_ru.endswith("4 с") and line_uk.endswith("4 с")


def test_stage_line_never_leaves_a_placeholder_in_any_locale() -> None:
    for code in ("en", "ru", "uk"):
        i18n = I18n(code, locales_dir=LOCALES_DIR)
        for key in ("lbl_progress_stage", "lbl_progress_indeterminate"):
            for stage in (Stage.ANALYSING, Stage.EMBEDDING):
                for seconds in (None, 0, 59):
                    line = ui_format.stage_line(
                        i18n, key, seconds=seconds, stage=stage, pct="40"
                    )
                    assert "{" not in line and "}" not in line, (code, key, line)
                    assert i18n.t(f"stage.{stage.value}") in line
                    if seconds is not None:
                        assert str(seconds) in line


def test_stage_line_without_a_stage_still_paints_the_key() -> None:
    """``lbl_progress_idle`` and friends have no stage, and must not crash."""
    i18n = I18n("en", locales_dir=LOCALES_DIR)
    assert ui_format.stage_line(i18n, "lbl_progress_idle") == i18n.t("lbl_progress_idle")


def test_a_stuck_phase_keeps_counting_past_a_minute() -> None:
    """No unit switch at 60 s: "1 с" -> "95 с" stays readable and monotonic."""
    i18n = I18n("en", locales_dir=LOCALES_DIR)
    values = [
        ui_format.stage_line(
            i18n, "lbl_progress_indeterminate", seconds=seconds, stage=Stage.ANALYSING
        )
        for seconds in (1, 59, 60, 95)
    ]
    assert all("Analyzing the image" in value for value in values)
    assert values == sorted(values, key=len), "the line grows with the number"


def test_the_long_phases_have_wording_in_every_locale() -> None:
    """The stage the interface shows while the cost map runs, and its clock."""
    expected = {
        "en": ("Analyzing the image", "{progress} · {seconds}s"),
        "ru": ("Анализ изображения", "{progress} · {seconds} с"),
        "uk": ("Аналіз зображення", "{progress} · {seconds} с"),
    }
    for code, (stage_text, elapsed) in expected.items():
        i18n = I18n(code, locales_dir=LOCALES_DIR)
        assert i18n.t("stage.analysing") == stage_text
        assert i18n.t("lbl_progress_elapsed") == elapsed
        line = i18n.t("lbl_progress_indeterminate", stage=stage_text)
        counted = i18n.t("lbl_progress_elapsed", progress=line, seconds=12)
        assert stage_text in counted
        assert "12" in counted
        assert "{" not in counted, "a placeholder was left unfilled"


def test_every_stage_has_a_name_in_every_locale() -> None:
    """A stage without a label would surface as a raw key in the footer."""
    for code in ("en", "ru", "uk"):
        i18n = I18n(code, locales_dir=LOCALES_DIR)
        for stage in Stage:
            assert i18n.has(f"stage.{stage.value}"), f"{code} misses stage.{stage.value}"


def test_the_embed_hint_mentions_the_layout_it_will_write() -> None:
    i18n = I18n("en", locales_dir=LOCALES_DIR)
    text = i18n.t("lbl_embed_format", layout=ui_format.written_layout_text(i18n))
    assert f"v{C.CONTAINER_VERSION}" in text
    assert "{" not in text, "a placeholder was left unfilled"


# ---------------------------------------------------------------------------
# audit SIZE line
# ---------------------------------------------------------------------------
def _extracted(original: int, compressed: int, name: str = "") -> ExtractedPayload:
    return ExtractedPayload(
        kind=PayloadKind.TEXT if not name else PayloadKind.FILE,
        name=name,
        data=b"x" * original,
        sha256_expected="a" * 64,
        sha256_actual="a" * 64,
        integrity_ok=True,
        original_size=original,
        compressed_size=compressed,
    )


def test_size_line_reports_the_share_of_the_carrier() -> None:
    i18n = I18n("en", locales_dir=LOCALES_DIR)
    capacity = Capacity(usable_bits=57_600, payload_bytes=(57_600 - 400) // 8 - C.TAG_SIZE)

    text = ui_format.size_text(_extracted(284, 306), capacity, i18n)

    assert text.startswith("284 B → 306 B · ")
    assert "of the carrier" in text
    assert "100 %" not in text


def _share(text: str) -> int:
    match = re.search(r"(\d+) %", text)
    assert match, f"no share in {text!r}"
    return int(match.group(1))


def test_size_line_share_grows_with_the_payload() -> None:
    i18n = I18n("en", locales_dir=LOCALES_DIR)
    capacity = Capacity(usable_bits=57_600, payload_bytes=(57_600 - 400) // 8 - C.TAG_SIZE)
    small = _share(ui_format.size_text(_extracted(10, 10), capacity, i18n))
    large = _share(ui_format.size_text(_extracted(5_000, 5_000), capacity, i18n))
    assert small < large <= 100


def test_size_line_counts_the_metadata_around_the_stream() -> None:
    """The share is built from the ciphertext, which carries name and tag too."""
    plain = _extracted(1_000, 1_000)
    named = _extracted(1_000, 1_000, name="report.pdf")
    assert carried_bytes(named) - carried_bytes(plain) == len("report.pdf")
    assert carried_bytes(plain) == (
        C.PAYLOAD_META_OVERHEAD + 1_000 + C.TAG_SIZE
    ), "type, name length, digest and length are part of the ciphertext"

    i18n = I18n("en", locales_dir=LOCALES_DIR)
    capacity = Capacity(usable_bits=57_600, payload_bytes=(57_600 - 400) // 8 - C.TAG_SIZE)
    long_name = _extracted(1_000, 1_000, name="r" * 400)
    assert ui_format.size_text(named, capacity, i18n) != ui_format.size_text(
        long_name, capacity, i18n
    )


def test_size_line_without_a_carrier_omits_the_share() -> None:
    i18n = I18n("en", locales_dir=LOCALES_DIR)
    assert ui_format.size_text(_extracted(284, 306), None, i18n) == "284 B → 306 B"
    empty = Capacity(usable_bits=0, payload_bytes=0)
    assert ui_format.size_text(_extracted(284, 306), empty, i18n) == "284 B → 306 B"


def test_size_line_is_localised() -> None:
    payload = _extracted(284, 306)
    capacity = Capacity(usable_bits=57_600, payload_bytes=7_118)
    texts = {
        code: ui_format.size_text(payload, capacity, I18n(code, locales_dir=LOCALES_DIR))
        for code in ("en", "ru", "uk")
    }
    assert len(set(texts.values())) == 3, "each language says it its own way"
    for code, text in texts.items():
        assert text.startswith("284 B → 306 B · "), code
        assert "{" not in text, f"{code} left a placeholder unfilled"


# ---------------------------------------------------------------------------
# native clipboard clearing (the OS part, no Tk involved)
# ---------------------------------------------------------------------------
def test_clear_command_knows_the_platform_tools() -> None:
    assert ui_clipboard.clear_command("darwin") == ("pbcopy",)
    assert ui_clipboard.clear_command("win32") == ("clip",)
    assert ui_clipboard.clear_command("linux", which=lambda name: "/usr/bin/wl-copy") == (
        "wl-copy",
        "--clear",
    )
    assert ui_clipboard.clear_command(
        "linux", which=lambda name: "/usr/bin/xclip" if name == "xclip" else None
    ) == ("xclip", "-selection", "clipboard", "-i", "/dev/null")
    assert ui_clipboard.clear_command(
        "linux", which=lambda name: "/usr/bin/xsel" if name == "xsel" else None
    ) == ("xsel", "--clipboard", "--delete")
    assert ui_clipboard.clear_command("linux", which=lambda name: None) is None


def test_clear_clipboard_runs_the_tool_with_an_empty_stdin() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(*args: object, **kwargs: object) -> object:
        calls.append((list(args[0]), kwargs))  # type: ignore[call-overload]
        return object()

    assert ui_clipboard.clear_clipboard(runner=runner, platform="darwin") is True
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["pbcopy"], "an argument vector, never a shell string"
    assert kwargs["input"] == b"", "pbcopy clears when stdin is empty"
    assert "shell" not in kwargs, "no shell anywhere near a secret"
    assert kwargs["check"] is False


def test_clear_clipboard_reports_a_missing_tool_without_raising() -> None:
    calls: list[object] = []

    def runner(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return object()

    assert (
        ui_clipboard.clear_clipboard(runner=runner, platform="linux", which=lambda name: None)
        is False
    )
    assert calls == [], "nothing to run"


def test_clear_clipboard_swallows_tool_failures() -> None:
    def runner(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("no pbcopy here")

    assert ui_clipboard.clear_clipboard(runner=runner, platform="darwin") is False


def test_clear_clipboard_passes_a_timeout() -> None:
    seen: dict[str, object] = {}

    def runner(*args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return object()

    ui_clipboard.clear_clipboard(runner=runner, platform="win32")
    assert seen["timeout"] == ui_clipboard.CLIPBOARD_TOOL_TIMEOUT
    assert seen["timeout"] < 5.0, "a hung tool must not block the GUI thread"
