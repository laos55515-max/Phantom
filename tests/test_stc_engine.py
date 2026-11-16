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
from phantom.core.progress import INDETERMINATE_STAGES, Stage
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

    assert report.container_version == C.KEYED_CONTAINER_VERSION
    assert engine.extract_payload(space, password).data.decode("utf-8") == "audio carrier"


def test_v5_falls_back_when_part_of_the_image_is_transparent(password: str) -> None:
    rgba = np.dstack([_opaque(), np.full((HEIGHT, WIDTH), 255, dtype=np.uint8)])
    rgba[0:20, 0:40, 3] = 0
    space = ImageSamples(rgba)

    report = engine.embed_text(space, "alpha carrier", password, container_version=5)

    assert report.container_version == C.KEYED_CONTAINER_VERSION
    assert engine.extract_payload(space, password).data.decode("utf-8") == "alpha carrier"


def test_v5_falls_back_when_no_trellis_fits(password: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stc, "plan", lambda *args, **kwargs: None)
    pixels = _opaque()
    space = ImageSamples(pixels)

    report = engine.embed_text(space, "no trellis for this", password, container_version=5)

    assert report.container_version == C.KEYED_CONTAINER_VERSION
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
    for version in (C.KEYED_CONTAINER_VERSION, C.ADAPTIVE_CONTAINER_VERSION):
        space = ImageSamples(_opaque())
        report = engine.embed_text(space, "старый файл", password, container_version=version)
        extracted = engine.extract_payload(space, password)
        assert extracted.container_version == report.container_version
        assert extracted.data.decode("utf-8") == "старый файл"


# ---------------------------------------------------------------------------
# the default layout
# ---------------------------------------------------------------------------


def test_the_default_layout_is_the_syndrome_trellis() -> None:
    """This build writes v5 unless the caller names another layout."""
    assert C.CONTAINER_VERSION == C.STC_CONTAINER_VERSION
    assert C.KEYED_CONTAINER_VERSION != C.CONTAINER_VERSION


def test_embedding_without_a_version_writes_v5(password: str) -> None:
    space = ImageSamples(_opaque())
    report = engine.embed_text(space, "по умолчанию", password)

    assert report.container_version == C.STC_CONTAINER_VERSION

    extracted = engine.extract_payload(space, password)
    assert extracted.container_version == C.STC_CONTAINER_VERSION
    assert extracted.data.decode("utf-8") == "по умолчанию"


def test_the_default_layout_spends_fewer_changes_than_the_keyed_one(password: str) -> None:
    """The whole point of the switch: same payload, quieter carrier."""
    default_space = ImageSamples(_opaque())
    keyed_space = ImageSamples(_opaque())
    cover = _opaque()

    engine.embed_text(default_space, "тише", password)
    engine.embed_text(keyed_space, "тише", password, container_version=C.KEYED_CONTAINER_VERSION)

    default_changes = int(np.count_nonzero(default_space.pixels[:, :, :3] != cover))
    keyed_changes = int(np.count_nonzero(keyed_space.pixels[:, :, :3] != cover))
    assert default_changes < keyed_changes


@pytest.mark.parametrize(
    "version",
    [
        C.V2_CONTAINER_VERSION,
        C.KEYED_CONTAINER_VERSION,
        C.ADAPTIVE_CONTAINER_VERSION,
    ],
)
def test_every_older_layout_is_still_readable(password: str, version: int) -> None:
    """Files written before the switch must open, and report their own version."""
    space = ImageSamples(_opaque())
    report = engine.embed_text(space, "старый файл", password, container_version=version)

    assert report.container_version == version

    extracted = engine.extract_payload(space, password)
    assert extracted.container_version == version
    assert extracted.integrity_ok
    assert extracted.data.decode("utf-8") == "старый файл"


def test_the_default_is_what_the_trellis_falls_back_from(password: str) -> None:
    """Falling back lowers the header to v3 — it never claims the trellis."""
    samples = np.random.default_rng(4).integers(-2000, 2000, 40000, dtype=np.int16)
    space = ArraySamples(samples, min_value=-32768, max_value=32767)

    report = engine.embed_text(space, "звук", password)

    assert report.container_version == C.KEYED_CONTAINER_VERSION
    assert engine.extract_payload(space, password).container_version == C.KEYED_CONTAINER_VERSION


# ---------------------------------------------------------------------------
# progress reporting: a 24 MP embed spends seconds in phases nobody can guess
# ---------------------------------------------------------------------------
class _Recorder:
    """Collects ``(stage, done, total)`` triples from the engine's callbacks."""

    def __init__(self) -> None:
        self.events: list[tuple[Stage, int, int]] = []

    def __call__(self, stage: Stage, done: int, total: int) -> None:
        self.events.append((stage, done, total))

    def stages(self) -> list[Stage]:
        return [stage for stage, _done, _total in self.events]


def test_v5_embed_announces_the_cost_map_before_anything_else(password: str) -> None:
    """The cost map is one opaque pass: it must show up as an indeterminate stage."""
    space = ImageSamples(_opaque())
    recorder = _Recorder()

    engine.embed_text(
        space, "фаза анализа", password, container_version=C.STC_CONTAINER_VERSION,
        progress=recorder,
    )

    stages = recorder.stages()
    assert stages, "the engine reported nothing at all"
    assert Stage.ANALYSING in stages
    assert stages.index(Stage.ANALYSING) < stages.index(Stage.EMBEDDING), (
        f"the analysis must be announced before the first bit is written: {stages}"
    )
    announced = recorder.events[stages.index(Stage.ANALYSING)]
    assert announced[1:] == (0, 0), "no fraction can be honest here"
    assert announced[0] in INDETERMINATE_STAGES


def test_v5_extract_reports_progress_while_it_reads(password: str) -> None:
    """Extraction used to report 0 % and then 100 %; the bar has to move."""
    rng = np.random.default_rng(15)
    pixels = rng.integers(0, 256, (512, 512, 3), dtype=np.uint8)
    space = ImageSamples(pixels)
    blob = _payload_blob(4_000)
    engine.embed_payload(space, blob, password, container_version=C.STC_CONTAINER_VERSION)

    recorder = _Recorder()
    engine.extract_payload(ImageSamples(space.pixels.copy()), password, progress=recorder)

    reporting = [done for stage, done, total in recorder.events if stage is Stage.EXTRACTING]
    assert len(reporting) >= 3, f"only {reporting} — the bar would stall"
    assert reporting == sorted(reporting), "progress must never go backwards"
    assert reporting[0] == 0
    assert reporting[-1] == max(reporting)
    fractions = {round(done / 4000, 2) for done in reporting if done}
    assert len(fractions) >= 2, "at least two distinct non-zero positions"


def test_analysing_is_skipped_when_the_trellis_cannot_be_used(password: str) -> None:
    """Audio falls back to v3 before any image analysis happens."""
    samples = np.random.default_rng(4).integers(-2000, 2000, 40000, dtype=np.int16)
    space = ArraySamples(samples, min_value=-32768, max_value=32767)
    recorder = _Recorder()

    engine.embed_text(space, "звук", password, container_version=C.STC_CONTAINER_VERSION,
                      progress=recorder)

    assert Stage.ANALYSING not in recorder.stages()
