"""API edge cases: detection, file payloads, wiping and the self-check."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from phantom.core import api
from phantom.core import constants as C
from phantom.core.api import CarrierKind, CarrierProbe
from phantom.core.errors import (
    CapacityError,
    IntegrityError,
    InvalidInput,
    InvalidPassword,
    OperationCancelled,
    PhantomIOError,
    UnsupportedCarrier,
    WipeError,
)
from phantom.core.payload import Payload
from phantom.stego.engine import Capacity


def _png(path: Path, size: tuple[int, int] = (160, 120)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    Image.fromarray(
        rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8), "RGB"
    ).save(path, format="PNG")
    return path


# ---------------------------------------------------------------------------
# CarrierProbe / detection
# ---------------------------------------------------------------------------
def test_probe_description_for_image(rgb_carrier: Path) -> None:
    probe = api.probe_carrier(str(rgb_carrier))
    assert probe.description == "160x120 RGB"


def test_probe_description_for_audio(wav_factory) -> None:
    path = wav_factory(bits=16, channels=2, frames=1_000, kind="noise")
    probe = api.probe_carrier(str(path))
    assert "samples" in probe.description
    assert "2 ch" in probe.description
    assert "16 bit" in probe.description


def test_probe_description_falls_back_to_path() -> None:
    probe = CarrierProbe(
        kind=CarrierKind.IMAGE,
        path="/tmp/nothing.png",
        capacity=Capacity(usable_bits=0, payload_bytes=0),
    )
    assert probe.description == "/tmp/nothing.png"


def test_detection_prefers_content_over_extension(tmp_path: Path) -> None:
    disguised = _png(tmp_path / "picture.txt")
    assert api.detect_carrier_kind(str(disguised)) is CarrierKind.IMAGE


def test_detection_by_extension_when_header_is_unknown(tmp_path: Path) -> None:
    unknown = tmp_path / "body.png"
    unknown.write_bytes(b"NOTAPNG" + b"\x00" * 32)
    assert api.detect_carrier_kind(str(unknown)) is CarrierKind.IMAGE
    lossy = tmp_path / "song.mp3"
    lossy.write_bytes(b"\x00" * 32)
    assert api.detect_carrier_kind(str(lossy)) is CarrierKind.AUDIO


def test_detection_rejects_unknown_files(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(b"hello")
    with pytest.raises(UnsupportedCarrier) as info:
        api.detect_carrier_kind(str(path))
    assert info.value.detail == ".txt"
    no_ext = tmp_path / "blob"
    no_ext.write_bytes(b"hello")
    with pytest.raises(UnsupportedCarrier) as info:
        api.detect_carrier_kind(str(no_ext))
    assert info.value.detail == "no extension"


def test_detection_reports_unreadable_files(tmp_path: Path) -> None:
    with pytest.raises(PhantomIOError):
        api.detect_carrier_kind(str(tmp_path / "missing.png"))


def test_probe_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PhantomIOError):
        api.probe_carrier(str(tmp_path / "missing.png"))


def test_probe_honours_cancellation(rgb_carrier: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelled):
        api.probe_carrier(str(rgb_carrier), cancel=cancel)


# ---------------------------------------------------------------------------
# embed: argument validation
# ---------------------------------------------------------------------------
def test_embed_requires_exactly_one_payload_source(rgb_carrier: Path, tmp_path: Path, password: str) -> None:
    output = tmp_path / "out.png"
    with pytest.raises(InvalidInput):
        api.embed_payload(str(rgb_carrier), str(output), password=password)
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"x")
    with pytest.raises(InvalidInput):
        api.embed_payload(
            str(rgb_carrier),
            str(output),
            Payload.text("x"),
            secret_path=str(secret),
            password=password,
        )


def test_embed_rejects_missing_carrier(tmp_path: Path, password: str) -> None:
    with pytest.raises(PhantomIOError):
        api.embed_payload(
            str(tmp_path / "nope.png"), str(tmp_path / "out.png"), Payload.text("x"), password=password
        )


def test_embed_refuses_to_overwrite_the_carrier(rgb_carrier: Path, password: str) -> None:
    with pytest.raises(Exception) as info:
        api.embed_payload(str(rgb_carrier), str(rgb_carrier), Payload.text("x"), password=password)
    assert "carrier" in str(info.value).lower() or "output" in str(info.value).lower()


def test_embed_respects_forced_png_extension(rgb_carrier: Path, tmp_path: Path, password: str) -> None:
    outcome = api.embed_payload(
        str(rgb_carrier), str(tmp_path / "out.jpg"), Payload.text("x"), password=password
    )
    assert outcome.output_path.endswith(".png")


# ---------------------------------------------------------------------------
# embed: file payloads, verify and wipe
# ---------------------------------------------------------------------------
def test_file_payload_roundtrip_from_path(tmp_path: Path, password: str) -> None:
    carrier = _png(tmp_path / "carrier.png")
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"file payload" * 100)
    output = tmp_path / "stego.png"
    outcome = api.embed_payload(
        str(carrier), str(output), secret_path=str(secret), password=password
    )
    assert outcome.payload_kind.value == C.TYPE_FILE
    assert outcome.user_bytes == secret.stat().st_size
    assert outcome.verified is True
    result = api.extract_from_path(str(output), password)
    assert result.payload.data == secret.read_bytes()
    assert result.payload.name == "secret.bin"


def test_verify_flag_is_reported_honestly(tmp_path: Path, password: str) -> None:
    carrier = _png(tmp_path / "carrier.png")
    output = tmp_path / "stego.png"
    outcome = api.embed_payload(
        str(carrier), str(output), Payload.text("no verify"), password=password, verify=False
    )
    assert outcome.verified is False


def test_failed_self_check_removes_the_output(
    tmp_path: Path, password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    carrier = _png(tmp_path / "carrier.png")
    output = tmp_path / "stego.png"
    calls: list[dict[str, object]] = []

    def broken(*args: object, **kwargs: object) -> None:
        calls.append(kwargs)
        raise IntegrityError("self-check failed")

    monkeypatch.setattr(api, "_verify_output", broken)
    with pytest.raises(IntegrityError):
        api.embed_payload(str(carrier), str(output), Payload.text("x"), password=password)
    assert calls, "verification must actually run"
    assert not output.exists(), "a failed self-check must not leave an artifact"


def test_failed_self_check_that_cannot_delete_raises_wipe_error(
    tmp_path: Path, password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    carrier = _png(tmp_path / "carrier.png")
    output = tmp_path / "stego.png"

    def broken_verify(*args: object, **kwargs: object) -> None:
        raise IntegrityError("self-check failed")

    def broken_unlink(path: str) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(api, "_verify_output", broken_verify)
    monkeypatch.setattr(api.os, "unlink", broken_unlink)
    with pytest.raises(WipeError):
        api.embed_payload(str(carrier), str(output), Payload.text("x"), password=password)


def test_wipe_source_removes_the_secret_file(tmp_path: Path, password: str) -> None:
    carrier = _png(tmp_path / "carrier.png")
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"erase me" * 50)
    output = tmp_path / "stego.png"
    outcome = api.embed_payload(
        str(carrier),
        str(output),
        secret_path=str(secret),
        password=password,
        wipe_source=True,
    )
    assert outcome.wiped_source is True
    assert outcome.wipe_error is None
    assert not secret.exists()
    assert api.extract_from_path(str(output), password).payload.data == b"erase me" * 50


def test_wipe_failure_is_reported_not_raised(
    tmp_path: Path, password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from phantom.core import files as fsutil

    carrier = _png(tmp_path / "carrier.png")
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"keep me")

    def broken_wipe(path: str, **kwargs: object) -> None:
        raise PhantomIOError("cannot overwrite", detail=path)

    monkeypatch.setattr(fsutil, "secure_wipe", broken_wipe)
    outcome = api.embed_payload(
        str(carrier),
        str(tmp_path / "stego.png"),
        secret_path=str(secret),
        password=password,
        wipe_source=True,
    )
    assert outcome.wiped_source is False
    assert outcome.wipe_error is not None
    assert "cannot overwrite" in outcome.wipe_error


def test_wipe_without_a_secret_file_is_rejected(tmp_path: Path, password: str) -> None:
    carrier = _png(tmp_path / "carrier.png")
    with pytest.raises(InvalidInput):
        api.embed_payload(
            str(carrier),
            str(tmp_path / "out.png"),
            Payload.text("x"),
            password=password,
            wipe_source=True,
        )


def test_wipe_refuses_when_secret_equals_output(tmp_path: Path, password: str) -> None:
    carrier = _png(tmp_path / "carrier.png")
    secret = tmp_path / "secret.png"
    original = b"x" * 10
    secret.write_bytes(original)
    with pytest.raises(Exception):
        api.embed_payload(
            str(carrier),
            str(secret),
            secret_path=str(secret),
            password=password,
            wipe_source=True,
        )
    assert secret.read_bytes() == original, "the refusal must happen before writing"


def test_capacity_is_enforced_before_writing(tmp_path: Path, password: str) -> None:
    carrier = _png(tmp_path / "small.png", size=(40, 40))
    incompressible = Payload.file("blob.bin", os.urandom(5_000))
    with pytest.raises(CapacityError):
        api.embed_payload(
            str(carrier), str(tmp_path / "out.png"), incompressible, password=password
        )
    assert not (tmp_path / "out.png").exists()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def test_read_all_and_errors(tmp_path: Path, rgb_carrier: Path) -> None:
    assert api.read_all(str(rgb_carrier)) == rgb_carrier.read_bytes()
    with pytest.raises(PhantomIOError):
        api.read_all(str(tmp_path / "missing.bin"))


def test_ensure_password_strength() -> None:
    api.ensure_password_strength("x" * C.MIN_PASSWORD_LENGTH)
    with pytest.raises(InvalidPassword):
        api.ensure_password_strength("x" * (C.MIN_PASSWORD_LENGTH - 1))
    with pytest.raises(InvalidPassword):
        api.ensure_password_strength("x" * 5, minimum=10)


def test_estimate_fill_percent(rgb_carrier: Path) -> None:
    capacity = api.capacity_of(str(rgb_carrier)).payload_bytes
    assert api.estimate_fill_percent(str(rgb_carrier), 0) > 0
    assert api.estimate_fill_percent(str(rgb_carrier), capacity) == pytest.approx(100.0)
    assert api.estimate_fill_percent(str(rgb_carrier), capacity * 10) == pytest.approx(100.0)
    assert api.estimate_fill_percent(str(rgb_carrier), capacity // 2) < 60


def test_estimate_fill_percent_on_a_useless_carrier(tmp_path: Path) -> None:
    tiny = tmp_path / "tiny.png"
    Image.new("RGB", (4, 4), (0, 0, 0)).save(tiny)
    assert api.capacity_of(str(tiny)).payload_bytes == 0
    assert api.estimate_fill_percent(str(tiny), 0) == pytest.approx(100.0)


def test_extract_accepts_an_explicit_kind(wav_factory, password: str) -> None:
    import wave

    carrier = wav_factory(bits=16, channels=1, frames=3_000, kind="noise")
    output = Path(str(carrier)).with_name("explicit.wav")
    api.embed_payload(str(carrier), str(output), Payload.text("kind"), password=password)
    outcome = api.extract_from_path(str(output), password, kind=CarrierKind.AUDIO)
    assert outcome.payload.data == b"kind"
    with wave.open(str(output), "rb") as handle:
        assert handle.getnchannels() == 1


def test_extract_honours_cancellation(rgb_carrier: Path, password: str) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelled):
        api.extract_from_path(str(rgb_carrier), password, cancel=cancel)


def test_temporary_files_are_not_left_behind(rgb_carrier: Path, tmp_path: Path, password: str) -> None:
    output = tmp_path / "out.png"
    api.embed_payload(str(rgb_carrier), str(output), Payload.text("clean"), password=password)
    leftovers = [name for name in os.listdir(tmp_path) if name.startswith(".")]
    assert leftovers == []
