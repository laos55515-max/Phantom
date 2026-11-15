"""The v4 (adaptive) layout at engine level: round-trip, fallbacks, adaptivity.

v4 keeps the v3 header where it is and only changes where the payload goes, so
the checks here are about that one difference: the file still extracts, the
changes land on texture instead of on smooth areas, and anything the cost map
cannot reason about (alpha, audio, an oversized payload) quietly falls back to
the v3 layout rather than producing an unreadable file.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest

from phantom.core import constants as C
from phantom.core.errors import WrongPasswordOrNoData
from phantom.core.payload import Payload, pack_payload
from phantom.stego import adaptive, engine
from phantom.stego.samples import ArraySamples, ImageSamples

WIDTH = 192
HEIGHT = 128


def _two_regions(*, size: tuple[int, int] = (HEIGHT, WIDTH)) -> np.ndarray:
    """Left half sky, right half foliage: the adaptive map should tell them apart."""
    height, width = size
    rng = np.random.default_rng(21)
    yy, xx = np.mgrid[0:height, 0:width]
    sky = 118 + 26 * (xx / width) + 6 * np.cos(yy / 40.0)
    foliage = 108 + rng.normal(0, 32.0, (height, width))
    plane = np.where(xx < width // 2, sky, foliage)
    return np.clip(np.stack([plane, plane * 0.92 + 18, plane * 0.84 + 26], -1), 0, 255).astype(
        np.uint8
    )


def _opaque(*, size: tuple[int, int] = (HEIGHT, WIDTH)) -> np.ndarray:
    rng = np.random.default_rng(5)
    planes = [rng.integers(20, 236, size, dtype=np.uint8) for _ in range(3)]
    return np.stack(planes, axis=-1).astype(np.uint8)


def _noise_payload(size: int = 1200) -> bytes:
    """Incompressible bytes, so the payload is big enough to measure where it lands."""
    rng = np.random.default_rng(77)
    data = rng.integers(0, 256, size, dtype=np.uint8).tobytes()
    space_bits = 10**9
    packed = pack_payload(Payload.file("blob.bin", data), max_payload_bytes=space_bits)
    return packed.blob


def _embed(pixels: np.ndarray, text: str, password: str, version: int) -> tuple:
    space = ImageSamples(pixels)
    report = engine.embed_text(space, text, password, container_version=version)
    return space, report


def test_v4_roundtrip(password: str) -> None:
    pixels = _opaque()
    space, report = _embed(pixels, "адаптивная стеганография", password, 4)

    assert report.container_version == C.ADAPTIVE_CONTAINER_VERSION
    extracted = engine.extract_payload(space, password)
    assert extracted.integrity_ok
    assert extracted.data.decode("utf-8") == "адаптивная стеганография"


def test_v4_never_moves_a_sample_by_more_than_one(password: str) -> None:
    pixels = _two_regions()
    cover = pixels.copy()
    _embed(pixels, "one step at a time", password, 4)
    delta = np.abs(pixels.astype(np.int16) - cover.astype(np.int16))
    assert int(delta.max()) == 1


def test_v4_keeps_the_mask_group_of_every_sample(password: str) -> None:
    """What the extractor relies on: the coarse image it sees is the cover's."""
    pixels = _two_regions()
    cover = pixels.copy()
    _embed(pixels, "coarse image must not move", password, 4)
    assert np.array_equal(adaptive.masked_pixels(pixels), adaptive.masked_pixels(cover))


def _sky_share(pixels: np.ndarray, cover: np.ndarray) -> float:
    changed = np.flatnonzero((pixels != cover).any(axis=-1))
    return float(np.mean(changed % WIDTH < WIDTH // 2))


def test_v4_puts_the_payload_into_texture_not_into_the_sky(password: str) -> None:
    pixels = _two_regions()
    cover = pixels.copy()
    space = ImageSamples(pixels)
    engine.embed_payload(space, _noise_payload(), password, container_version=4)

    sky_share = _sky_share(pixels, cover)
    assert sky_share < 0.15, f"{sky_share:.1%} изменений попало в гладкую половину"


def test_v3_by_contrast_spreads_changes_evenly(password: str) -> None:
    """The control case for the test above: v3 does not look at the image."""
    pixels = _two_regions()
    cover = pixels.copy()
    space = ImageSamples(pixels)
    engine.embed_payload(space, _noise_payload(), password, container_version=3)

    sky_share = _sky_share(pixels, cover)
    assert 0.35 < sky_share < 0.65, f"v3 got {sky_share:.1%}"


def test_v4_falls_back_to_v3_for_audio_like_samples(password: str) -> None:
    rng = np.random.default_rng(3)
    samples = rng.integers(-2000, 2000, 40000, dtype=np.int16)
    space = ArraySamples(samples, min_value=-32768, max_value=32767)

    report = engine.embed_text(space, "audio carrier", password, container_version=4)

    assert report.container_version == C.CONTAINER_VERSION
    assert engine.extract_payload(space, password).data.decode("utf-8") == "audio carrier"


def test_v4_falls_back_when_part_of_the_image_is_transparent(password: str) -> None:
    rgba = np.dstack([_opaque(), np.full((HEIGHT, WIDTH), 255, dtype=np.uint8)])
    rgba[0:20, 0:40, 3] = 0
    space = ImageSamples(rgba)

    report = engine.embed_text(space, "alpha carrier", password, container_version=4)

    assert report.container_version == C.CONTAINER_VERSION
    assert engine.extract_payload(space, password).data.decode("utf-8") == "alpha carrier"


def test_v4_falls_back_when_the_payload_exceeds_its_guard(
    password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine, "ADAPTIVE_MAX_BITS", 512)
    pixels = _opaque()
    space, report = _embed(pixels, "too big for the adaptive layout", password, 4)

    assert report.container_version == C.CONTAINER_VERSION
    assert engine.extract_payload(space, password).data.decode("utf-8") == (
        "too big for the adaptive layout"
    )


def test_v4_capacity_boundary_still_extracts(password: str) -> None:
    pixels = _opaque()
    space = ImageSamples(pixels)
    capacity = engine.capacity_for(space)
    payload = Payload.text("x" * max(1, capacity.payload_bytes // 8))
    packed = pack_payload(
        payload, max_payload_bytes=capacity.payload_bytes, cancel=threading.Event()
    )

    report = engine.embed_payload(space, packed.blob, password, container_version=4)

    assert report.container_version == C.ADAPTIVE_CONTAINER_VERSION
    assert engine.extract_payload(space, password).integrity_ok


def test_v4_rejects_a_wrong_password(password: str) -> None:
    space, _report = _embed(_opaque(), "секрет", password, 4)
    with pytest.raises(WrongPasswordOrNoData):
        engine.extract_payload(space, password + "!")


def test_v3_files_are_unaffected_by_the_adaptive_branch(password: str) -> None:
    space, report = _embed(_opaque(), "обычный v3", password, 3)
    assert report.container_version == C.CONTAINER_VERSION
    assert engine.extract_payload(space, password).data.decode("utf-8") == "обычный v3"


def test_extraction_reports_which_layout_the_file_uses(password: str) -> None:
    space, _report = _embed(_opaque(), "header stays put", password, 4)
    extracted = engine.extract_payload(space, password)
    assert extracted.container_version == C.ADAPTIVE_CONTAINER_VERSION
    assert extracted.integrity_ok


def test_adaptive_layout_is_refused_for_tiny_images(password: str) -> None:
    """A 4×4 carrier has no room for the header, let alone a layout."""
    pixels = np.zeros((4, 4, 3), dtype=np.uint8)
    space = ImageSamples(pixels)
    with pytest.raises(Exception):  # noqa: B017 - capacity error family
        engine.embed_text(space, "no room", password, container_version=4)
