"""Benchmarks with generous bounds: they catch regressions, not micro-noise."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from phantom.core import api
from phantom.core import constants as C
from phantom.core.payload import Payload, pack_payload
from phantom.stego import engine
from phantom.stego.samples import ImageSamples

pytestmark = pytest.mark.slow

#: Bounds are ~4x the measured warm numbers on the development machine.
EMBED_LIMIT = 60.0
EXTRACT_LIMIT = 40.0


@pytest.fixture(scope="module")
def big_carrier(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("perf")
    path = directory / "24mp.png"
    rng = np.random.default_rng(2026)
    height, width = 4000, 6000
    data = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    Image.fromarray(data, "RGB").save(path, format="PNG", compress_level=3)
    return path


def test_100kib_in_24mp_carrier(big_carrier: Path, tmp_path: Path, password: str) -> None:
    secret = bytes(range(256)) * 400  # 102 400 B
    payload = Payload.file("secret.bin", secret)
    output = tmp_path / "out.png"

    started = time.perf_counter()
    outcome = api.embed_payload(str(big_carrier), str(output), payload, password=password)
    embed_seconds = time.perf_counter() - started
    assert outcome.verified
    assert embed_seconds < EMBED_LIMIT, f"embed took {embed_seconds:.1f}s"

    started = time.perf_counter()
    result = api.extract_from_path(str(output), password)
    extract_seconds = time.perf_counter() - started
    assert result.payload.data == secret
    assert extract_seconds < EXTRACT_LIMIT, f"extract took {extract_seconds:.1f}s"

    ratio = output.stat().st_size / big_carrier.stat().st_size
    assert 0.9 < ratio < 1.1, f"PNG size moved by {ratio:.3f}x"


def test_stc_layout_stays_within_budget(password: str) -> None:
    """The trellis is the slowest layout by far: keep it away from the cliff.

    A 1024x1024 frame at 0.4 bpp means roughly three million trellis steps across
    157 000 blocks with 256 states each; the bound is generous, the point is that
    it is linear in the carrier rather than quadratic.
    """
    rng = np.random.default_rng(11)
    pixels = rng.integers(0, 256, (1024, 1024, 3), dtype=np.uint8)
    space = ImageSamples(pixels)
    blob = pack_payload(
        Payload.file("bench.bin", rng.integers(0, 256, 52_400, dtype=np.uint8).tobytes())
    ).blob

    started = time.perf_counter()
    report = engine.embed_payload(space, blob, password, container_version=C.STC_CONTAINER_VERSION)
    embed_seconds = time.perf_counter() - started
    assert report.container_version == C.STC_CONTAINER_VERSION
    assert embed_seconds < EMBED_LIMIT, f"stc embed took {embed_seconds:.1f}s"

    started = time.perf_counter()
    extracted = engine.extract_payload(space, password)
    extract_seconds = time.perf_counter() - started
    assert extracted.integrity_ok
    assert extract_seconds < EXTRACT_LIMIT / 2, f"stc extract took {extract_seconds:.1f}s"


def test_probe_is_fast_on_24mp(big_carrier: Path) -> None:
    started = time.perf_counter()
    probe = api.probe_carrier(str(big_carrier))
    seconds = time.perf_counter() - started
    assert probe.capacity.payload_bytes > 1_000_000
    assert seconds < 15.0, f"probe took {seconds:.1f}s"


def test_wav_three_minutes(tmp_path: Path, wav_factory, password: str) -> None:
    frames = 44_100 * 180  # 3 minutes, mono
    carrier = wav_factory(bits=16, channels=1, frames=frames, kind="noise")
    payload = Payload.text("benchmark " * 500)
    output = tmp_path / "out.wav"

    started = time.perf_counter()
    outcome = api.embed_payload(str(carrier), str(output), payload, password=password)
    embed_seconds = time.perf_counter() - started
    assert outcome.verified
    assert embed_seconds < EMBED_LIMIT, f"embed took {embed_seconds:.1f}s"

    started = time.perf_counter()
    result = api.extract_from_path(str(output), password)
    extract_seconds = time.perf_counter() - started
    assert result.payload.data == payload.data
    assert extract_seconds < EXTRACT_LIMIT, f"extract took {extract_seconds:.1f}s"


def test_kdf_cost_is_in_the_expected_band() -> None:
    """A cheaper KDF would be a silent security regression."""
    from phantom.core.kdf import derive_master_key

    started = time.perf_counter()
    derive_master_key(
        "benchmark-password", b"0123456789abcdef", C.default_kdf_profile()
    )
    seconds = time.perf_counter() - started
    assert 0.05 < seconds < 3.0, f"Argon2id took {seconds:.3f}s"


def test_capacity_scales_linearly(rgb_carrier: Path, tmp_path: Path) -> None:
    small = api.capacity_of(str(rgb_carrier)).payload_bytes
    doubled = tmp_path / "double.png"
    with Image.open(rgb_carrier) as img:
        img.resize((img.width * 2, img.height * 2), Image.Resampling.NEAREST).save(
            doubled, format="PNG", compress_level=1
        )
    doubled_capacity = api.capacity_of(str(doubled)).payload_bytes
    # Four times the bits, one unchanged header, one unchanged GCM tag.
    delta = doubled_capacity - 4 * small
    assert 0 < delta <= (3 * C.HEADER_BITS) // 8 + 3 * C.TAG_SIZE
