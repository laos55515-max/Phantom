"""Legacy v1 compatibility pinned to golden fixtures from commit 4920c65 (R5)."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from phantom.core import api
from phantom.core import constants as C
from phantom.core.errors import WrongPasswordOrNoData
from phantom.core.payload import PayloadKind
from phantom.stego.legacy import decode_legacy_blob, extract_legacy_from_pixels

PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def manifest(legacy_dir: Path) -> dict:
    return json.loads((legacy_dir / "manifest.json").read_text(encoding="utf-8"))


def _pixels(path: Path) -> np.ndarray:
    with Image.open(path) as img:
        return np.array(img.convert("RGB"), dtype=np.uint8)


def test_fixtures_are_intact(legacy_dir: Path, manifest: dict) -> None:
    for name in ("carrier.png", "stego_text.png", "stego_file.png", "stego_text_unicode.png"):
        raw = (legacy_dir / name).read_bytes()
        assert raw.startswith(b"\x89PNG")
    assert hashlib.sha256((legacy_dir / "stego_text.png").read_bytes()).hexdigest() == manifest["text"]["stego_sha256"]
    assert hashlib.sha256((legacy_dir / manifest["unicode_text"]["stego"]).read_bytes()).hexdigest() == manifest["unicode_text"]["stego_sha256"]


def test_legacy_text_extraction(legacy_dir: Path, manifest: dict) -> None:
    pixels = _pixels(legacy_dir / "stego_text.png")
    result = extract_legacy_from_pixels(pixels, PASSWORD)
    assert result.kind is PayloadKind.TEXT
    assert result.data.decode("utf-8") == manifest["text"]["secret_utf8"]
    assert result.integrity_ok
    assert result.container_version == 1
    assert "legacy" in result.kdf_label


def test_legacy_file_extraction(legacy_dir: Path, manifest: dict) -> None:
    pixels = _pixels(legacy_dir / "stego_file.png")
    result = extract_legacy_from_pixels(pixels, PASSWORD)
    assert result.kind is PayloadKind.FILE
    assert result.name == manifest["file"]["name"]
    assert hashlib.sha256(result.data).hexdigest() == manifest["file"]["secret_sha256"]


def test_legacy_wrong_password(legacy_dir: Path) -> None:
    pixels = _pixels(legacy_dir / "stego_text.png")
    with pytest.raises(WrongPasswordOrNoData):
        extract_legacy_from_pixels(pixels, "definitely not the password")


def test_plain_carrier_has_no_legacy_payload(legacy_dir: Path) -> None:
    pixels = _pixels(legacy_dir / "carrier.png")
    with pytest.raises(WrongPasswordOrNoData):
        extract_legacy_from_pixels(pixels, PASSWORD)


def test_legacy_unicode_password_accepts_nfd(legacy_dir: Path, manifest: dict) -> None:
    entry = manifest["unicode_text"]
    nfd = unicodedata.normalize("NFD", entry["password_nfc"])
    assert nfd != entry["password_nfc"], "fixture must exercise real NFD differences"
    pixels = _pixels(legacy_dir / entry["stego"])
    result = extract_legacy_from_pixels(pixels, nfd)
    assert result.data.decode("utf-8") == entry["secret_utf8"]


def test_legacy_unicode_password_accepts_nfc(legacy_dir: Path, manifest: dict) -> None:
    entry = manifest["unicode_text"]
    pixels = _pixels(legacy_dir / entry["stego"])
    result = extract_legacy_from_pixels(pixels, entry["password_nfc"])
    assert result.data.decode("utf-8") == entry["secret_utf8"]


def test_raw_blob_decryption_matches_v2_payload_reader(legacy_dir: Path, manifest: dict) -> None:
    from phantom.stego.legacy import legacy_bit_stream

    pixels = _pixels(legacy_dir / "stego_text.png")
    bits = legacy_bit_stream(pixels)
    declared = int.from_bytes(np.packbits(bits[:32]).tobytes(), "big")
    assert declared == manifest["text"]["blob_size"]
    blob = np.packbits(bits[32 : 32 + declared * 8]).tobytes()
    result = decode_legacy_blob(blob, PASSWORD)
    assert result.data.decode("utf-8") == manifest["text"]["secret_utf8"]


def test_public_api_reads_v1_files(legacy_dir: Path, manifest: dict) -> None:
    outcome = api.extract_from_path(str(legacy_dir / "stego_text.png"), PASSWORD)
    assert outcome.payload.data.decode("utf-8") == manifest["text"]["secret_utf8"]
    assert outcome.payload.container_version == 1


def test_legacy_detection_does_not_break_v2(legacy_dir: Path, tmp_path: Path, password: str) -> None:
    output = tmp_path / "modern.png"
    api.embed_payload(
        str(legacy_dir / "carrier.png"),
        str(output),
        api.Payload.text("modern payload"),
        password=password,
    )
    outcome = api.extract_from_path(str(output), password)
    assert outcome.payload.container_version == C.CONTAINER_VERSION
    assert outcome.payload.data == b"modern payload"


def test_wrong_password_on_v2_never_falls_back_to_garbage( tmp_path: Path, rgb_carrier: Path) -> None:
    output = tmp_path / "v2.png"
    api.embed_payload(
        str(rgb_carrier), str(output), api.Payload.text("x" * 100), password="a" * 20
    )
    with pytest.raises(WrongPasswordOrNoData):
        api.extract_from_path(str(output), "b" * 20)
