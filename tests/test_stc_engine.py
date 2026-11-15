"""The v5 (syndrome-trellis) layout at engine level.

v5 shares the keyed header with v3/v4 and differs only in how the payload reaches
the carrier: a convolutional code over the whole body, driven by the same wavelet
cost map.  What has to hold here is that the file reads back, that the changes are
fewer than the greedy layout spends, that nothing about extraction depends on the
image, and that every carrier the cost map cannot reason about falls back to v3
instead of producing an unreadable file.
"""

from __future__ import annotations

import numpy as np
import pytest

from phantom.core import constants as C
from phantom.core.errors import WrongPasswordOrNoData
from phantom.core.payload import Payload, pack_payload
from phantom.stego import engine, stc
from phantom.stego.samples import ArraySamples, ImageSamples

WIDTH = 192
HEIGHT = 128


def _opaque(*, size: tuple[int, int] = (HEIGHT, WIDTH), seed: int = 5) -> np.ndarray:
    rng = np.random.default_rng(seed)
    planes = [rng.integers(20, 236, size, dtype=np.uint8) for _ in range(3)]
    return np.stack(planes, axis=-1).astype(np.uint8)


def _two_regions(*, size: tuple[int, int] = (HEIGHT, WIDTH)) -> np.ndarray:
    height, width = size
    rng = np.random.default_rng(21)
    _yy, xx = np.mgrid[0:height, 0:width]
    sky = 118 + 26 * (xx / width)
    foliage = 108 + rng.normal(0, 32.0, (height, width))
    plane = np.where(xx < width // 2, sky, foliage)
    return np.clip(np.stack([plane, plane * 0.92 + 18, plane * 0.84 + 26], -1), 0, 255).astype(
        np.uint8
    )


def _payload_blob(size: int = 300) -> bytes:
    rng = np.random.default_rng(77)
    data = rng.integers(0, 256, size, dtype=np.uint8).tobytes()
    return pack_payload(Payload.file("blob.bin", data)).blob


def test_v5_roundtrip(password: str) -> None:
    pixels = _opaque()
    space = ImageSamples(pixels)
    report = engine.embed_text(space, "свёрточный код и Витерби", password, container_version=5)

    assert report.container_version == C.STC_CONTAINER_VERSION
    extracted = engine.extract_payload(space, password)
    assert extracted.integrity_ok
    assert extracted.data.decode("utf-8") == "свёрточный код и Витерби"
    assert extracted.container_version == C.STC_CONTAINER_VERSION


def test_v5_moves_samples_by_one_at_most(password: str) -> None:
    pixels = _two_regions()
    cover = pixels.copy()
    engine.embed_text(ImageSamples(pixels), "one step", password, container_version=5)
    assert int(np.abs(pixels.astype(np.int16) - cover.astype(np.int16)).max()) == 1


def test_v5_spends_fewer_changes_than_the_greedy_layouts(password: str) -> None:
    """The whole point: the same payload with roughly half the ±1 changes."""
    blob = _payload_blob(600)
    results: dict[int, int] = {}
    for version in (3, 4, 5):
        pixels = _two_regions()
        cover = pixels.copy()
        engine.embed_payload(ImageSamples(pixels), blob, password, container_version=version)
        results[version] = int((pixels != cover).any(axis=-1).sum())

    assert results[5] < results[4], f"v5 {results[5]} against greedy v4 {results[4]}"
    assert results[5] < results[3], f"v5 {results[5]} against blind v3 {results[3]}"


def test_v5_extraction_never_consults_the_image(password: str) -> None:
    """Trellis and matrix are built from the size, the length and the key — only."""
    first = _opaque(seed=1)
    second = _opaque(seed=2)
    key = b"\x5a" * 32
    left = engine.stc_layout(ImageSamples(first), key, 4_000)
    right = engine.stc_layout(ImageSamples(second), key, 4_000)

    assert left is not None and right is not None
    assert left[0] == right[0]
    assert np.array_equal(left[1], right[1])
    assert engine.stc_layout(ImageSamples(first), b"\x5b" * 32, 4_000)[0] == left[0]


def test_v5_falls_back_to_v3_for_audio_like_samples(password: str) -> None:
    rng = np.random.default_rng(3)
    samples = rng.integers(-2000, 2000, 40000, dtype=np.int16)
    space = ArraySamples(samples, min_value=-32768, max_value=32767)

    report = engine.embed_text(space, "audio carrier", password, container_version=5)

    assert report.container_version == C.CONTAINER_VERSION
    assert engine.extract_payload(space, password).data.decode("utf-8") == "audio carrier"


def test_v5_falls_back_when_part_of_the_image_is_transparent(password: str) -> None:
    rgba = np.dstack([_opaque(), np.full((HEIGHT, WIDTH), 255, dtype=np.uint8)])
    rgba[0:20, 0:40, 3] = 0
    space = ImageSamples(rgba)

    report = engine.embed_text(space, "alpha carrier", password, container_version=5)

    assert report.container_version == C.CONTAINER_VERSION
    assert engine.extract_payload(space, password).data.decode("utf-8") == "alpha carrier"


def test_v5_falls_back_when_no_trellis_fits(password: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stc, "plan", lambda *args, **kwargs: None)
    pixels = _opaque()
    space = ImageSamples(pixels)

    report = engine.embed_text(space, "no trellis for this", password, container_version=5)

    assert report.container_version == C.CONTAINER_VERSION
    assert engine.extract_payload(space, password).data.decode("utf-8") == "no trellis for this"


def test_v5_capacity_boundary_still_extracts(password: str) -> None:
    space = ImageSamples(_opaque())
    capacity = engine.capacity_for(space)
    payload = Payload.text("x" * max(1, capacity.payload_bytes // 8))
    packed = pack_payload(payload, max_payload_bytes=capacity.payload_bytes)

    report = engine.embed_payload(space, packed.blob, password, container_version=5)

    assert report.container_version == C.STC_CONTAINER_VERSION
    extracted = engine.extract_payload(space, password)
    assert extracted.integrity_ok
    assert extracted.data == payload.data


def test_v5_rejects_a_wrong_password(password: str) -> None:
    space = ImageSamples(_opaque())
    engine.embed_text(space, "секрет", password, container_version=5)
    with pytest.raises(WrongPasswordOrNoData):
        engine.extract_payload(space, password + "!")


def test_a_damaged_v5_file_is_refused(password: str) -> None:
    pixels = _opaque()
    space = ImageSamples(pixels)
    engine.embed_text(space, "не трогать", password, container_version=5)
    pixels.reshape(-1)[::97] ^= 1  # flip a scattering of LSBs

    with pytest.raises(WrongPasswordOrNoData):
        engine.extract_payload(space, password)


def test_older_layouts_still_extract_next_to_v5(password: str) -> None:
    for version in (C.CONTAINER_VERSION, C.ADAPTIVE_CONTAINER_VERSION):
        space = ImageSamples(_opaque())
        report = engine.embed_text(space, "старый файл", password, container_version=version)
        extracted = engine.extract_payload(space, password)
        assert extracted.container_version == report.container_version
        assert extracted.data.decode("utf-8") == "старый файл"
