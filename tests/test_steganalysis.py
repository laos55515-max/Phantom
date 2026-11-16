"""The baseline detectors in ``tools/bench_steganalysis.py`` must be trustworthy.

A steganalysis table is only worth reading if the detectors provably work: the
tests below pin their behaviour on synthetic data where the answer is known -
LSB *replacement* has to light them up, LSB *matching* (what the engine writes)
has to leave them cold, and a degenerate texture has to say "no estimate"
instead of inventing a rate.
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from phantom.core import constants as C

TOOL = Path(__file__).resolve().parent.parent / "tools" / "bench_steganalysis.py"


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_steganalysis", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses needs the module in sys.modules to resolve its annotations.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _plane(bench: ModuleType, seed: int = 1000) -> np.ndarray:
    """One channel of the tool's own smooth-gradient carrier.

    The fixtures are the carriers the report actually runs on, not a hand-made
    toy: a histogram with many small bins behaves nothing like a synthetic ramp
    and the detectors notice the difference.
    """
    return bench.carrier_gradient(np.random.default_rng(seed))[:, :, 0]


def _rgb(bench: ModuleType, seed: int = 1000, size: int = 256) -> np.ndarray:
    return bench.carrier_gradient(np.random.default_rng(seed))[:size, :size]


def _replace(plane: np.ndarray, rate: float, seed: int = 3) -> np.ndarray:
    """Classic LSB replacement: the thing chi-square and RS were built for."""
    rng = np.random.default_rng(seed)
    out = plane.copy()
    selected = rng.random(out.shape) < rate
    target = rng.integers(0, 2, out.shape).astype(np.uint8)
    out[selected] = (out[selected] & 0xFE) | target[selected]
    return out


def _match(plane: np.ndarray, rate: float, seed: int = 3) -> np.ndarray:
    """LSB matching: ±1 instead of overwriting the bit, histogram left intact."""
    rng = np.random.default_rng(seed)
    out = plane.copy()
    selected = rng.random(out.shape) < rate
    sign = np.where(rng.random(out.shape) < 0.5, -1, 1)
    moved = out[selected].astype(np.int16) + sign[selected]
    out[selected] = np.clip(moved, 0, 255).astype(np.uint8)
    return out


# ---------------------------------------------------------------------------
# chi-square
# ---------------------------------------------------------------------------
def test_chi2_tail_matches_the_published_tables(bench: ModuleType) -> None:
    assert bench.chi2_sf(3.841, 1) == pytest.approx(0.05, abs=1e-3)
    assert bench.chi2_sf(18.307, 10) == pytest.approx(0.05, abs=1e-3)
    assert bench.chi2_sf(0.0, 5) == 1.0
    assert bench.chi2_sf(5.0, 0) == 0.0


def test_chi2_tail_falls_with_the_statistic(bench: ModuleType) -> None:
    values = [bench.chi2_sf(statistic, 64) for statistic in (20.0, 60.0, 120.0, 400.0)]
    assert values == sorted(values, reverse=True)
    assert values[-1] < 1e-6


def test_chi2_calls_a_balanced_histogram_embedded(bench: ModuleType) -> None:
    """Equal even/odd counts are exactly what a saturated bit plane looks like."""
    plane = np.repeat(np.arange(256, dtype=np.uint8), 64)
    assert bench.chi2_probability(plane.reshape(-1, 1)) > 0.999


def test_chi2_calls_a_spiky_histogram_clean(bench: ModuleType) -> None:
    plane = np.repeat((np.arange(128) * 2).astype(np.uint8), 64)
    assert bench.chi2_probability(plane.reshape(-1, 1)) < 1e-6


def test_chi2_reacts_to_lsb_replacement(bench: ModuleType) -> None:
    plane = _plane(bench)
    clean = bench.chi2_probability(plane)
    replaced = bench.chi2_probability(_replace(plane, 1.0))
    assert clean < 0.01
    assert replaced > 0.99, "a fully replaced bit plane must be obvious"


def test_chi2_is_blind_to_lsb_matching(bench: ModuleType) -> None:
    """Matching keeps the histogram, so pairs never equalise: the point of the design."""
    plane = _plane(bench)
    matched = bench.chi2_probability(_match(plane, 1.0))
    assert matched < 0.01


# ---------------------------------------------------------------------------
# RS
# ---------------------------------------------------------------------------
def test_rs_counts_a_smooth_cover_as_regular(bench: ModuleType) -> None:
    regular, singular = bench.rs_counts(_plane(bench), bench.RS_MASK)
    assert regular > singular * 1.5, "flipping must roughen a smooth cover"


def test_rs_is_near_zero_on_a_clean_cover(bench: ModuleType) -> None:
    assert bench.rs_estimate(_plane(bench)) < 0.05


def test_rs_grows_with_lsb_replacement(bench: ModuleType) -> None:
    plane = _plane(bench)
    quiet = bench.rs_estimate(plane)
    half = bench.rs_estimate(_replace(plane, 0.5))
    full = bench.rs_estimate(_replace(plane, 1.0))
    assert quiet < half < full
    assert half > 0.5
    assert full > 0.9


def test_rs_stays_quiet_on_lsb_matching(bench: ModuleType) -> None:
    """The model assumes replacement; matching must not fake a rate."""
    assert bench.rs_estimate(_match(_plane(bench), 0.5)) < 0.1


def test_rs_refuses_saturated_planes(bench: ModuleType) -> None:
    """A plane with only 0 and 255 has no smoothness to reason about."""
    plane = np.where(np.arange(64 * 64).reshape(64, 64) % 2, 255, 0).astype(np.uint8)
    assert math.isnan(bench.rs_estimate(plane))


def test_rs_root_selection_prefers_the_allowed_range(bench: ModuleType) -> None:
    assert bench._select_root(0.02, 29.4) == 0.02
    assert bench._select_root(-18.9, 2.1) != bench._select_root(-18.9, 2.1), "NaN for nonsense"
    assert bench._select_root(-0.017, -5.7) == 0.0, "a hair below zero is estimation noise"
    assert bench._select_root(0.996, 14.2) == 0.996


# ---------------------------------------------------------------------------
# payload sizing and the real engine
# ---------------------------------------------------------------------------
def test_blob_for_hits_the_requested_load(bench: ModuleType) -> None:
    from phantom.core import constants as C

    pixels = 512 * 512
    blob, data = bench.packed_blob_for(0.4, pixels)
    assert len(data) > 12_000, "the payload itself must be big, not just its metadata"
    ciphertext = len(blob) + C.TAG_SIZE
    written_bits = C.HEADER_BITS + 8 * ciphertext
    assert written_bits / pixels == pytest.approx(0.4, abs=1e-3)


def test_blob_for_rejects_a_load_that_does_not_fit(bench: ModuleType) -> None:
    with pytest.raises(SystemExit):
        bench.packed_blob_for(0.5, 64)


@pytest.mark.parametrize(
    ("layout", "version"),
    [
        ("v5", C.STC_CONTAINER_VERSION),
        ("v4", C.ADAPTIVE_CONTAINER_VERSION),
        ("v3", C.KEYED_CONTAINER_VERSION),
    ],
)
def test_embed_png_writes_matching_and_reads_back_the_same_pixels(
    bench: ModuleType, tmp_path: Path, layout: str, version: int
) -> None:
    """The tool must drive the real engine, so only ±1 moves and nothing is lost."""
    cover = _rgb(bench)
    target = tmp_path / "stego.png"

    stego, written, verified = bench.embed_png(cover, 0.3, target, layout=layout)

    assert written == version, f"the bench must write the layout it was asked for: {layout}"
    assert verified, "the bench must prove the file it wrote can be read back"
    assert target.stat().st_size > 0
    delta = stego.astype(np.int16) - cover.astype(np.int16)
    assert set(np.unique(delta).tolist()) <= {-1, 0, 1}, "LSB matching never jumps further"
    changed = float((delta != 0).mean())
    assert 0.0 < changed < 0.25, f"0.3 bpp touches a minority of samples, got {changed:.3f}"
    histogram_gap = np.abs(
        np.bincount(cover.reshape(-1), minlength=256)
        - np.bincount(stego.reshape(-1), minlength=256)
    ).sum()
    assert histogram_gap < 0.05 * cover.size, "matching exists to keep the histogram"


def test_embed_png_output_is_detectable_only_when_replacement_is_used(
    bench: ModuleType, tmp_path: Path
) -> None:
    """Same pixels, two writing styles: the detectors separate them cleanly."""
    cover = _rgb(bench)
    stego, _version, _verified = bench.embed_png(cover, 0.3, tmp_path / "stego.png")
    matched_chi2, matched_rs = bench._detect(stego)
    assert matched_chi2 < 0.1
    assert math.isnan(matched_rs) or matched_rs < 0.1

    replaced = cover.copy()
    plane = _replace(replaced[:, :, 0], 0.6)
    replaced[:, :, 0] = plane
    chi2, rs = bench._detect(replaced)
    assert chi2 > 0.5 or rs > 0.25


# ---------------------------------------------------------------------------
# distortion metrics, verdicts and the report
# ---------------------------------------------------------------------------
def test_psnr_of_an_identical_image_is_infinite(bench: ModuleType) -> None:
    cover = _rgb(bench)
    assert math.isinf(bench.psnr(cover, cover))
    assert bench.changed_percent(cover, cover) == (0.0, 0.0)


def test_psnr_falls_as_more_samples_move(bench: ModuleType) -> None:
    cover = _rgb(bench)
    light = cover.copy()
    heavy = cover.copy()
    light.reshape(-1)[:100] ^= np.uint8(1)
    heavy.reshape(-1)[:1000] ^= np.uint8(1)
    assert bench.psnr(cover, light) > bench.psnr(cover, heavy)


def test_changed_percent_counts_pixels_and_samples_separately(bench: ModuleType) -> None:
    cover = np.zeros((2, 2, 3), dtype=np.uint8)
    stego = cover.copy()
    stego[0, 0, 0] = 1
    stego[0, 0, 1] = 1
    pixels, samples = bench.changed_percent(cover, stego)
    assert pixels == pytest.approx(25.0), "one pixel of four"
    assert samples == pytest.approx(100.0 * 2 / 12), "two samples of twelve"


def _two_regions(size: int = 256) -> np.ndarray:
    """Sky on the left, foliage on the right: a cost map needs both to be useful."""
    rng = np.random.default_rng(29)
    _yy, xx = np.mgrid[0:size, 0:size]
    plane = np.where(
        xx < size // 2, 120 + 20 * (xx / size), 110 + rng.normal(0, 30.0, (size, size))
    )
    return np.clip(np.stack([plane, plane * 0.9 + 20, plane * 0.8 + 30], -1), 0, 255).astype(
        np.uint8
    )


def test_embed_png_can_write_the_adaptive_layout(bench: ModuleType, tmp_path: Path) -> None:
    """The bench drives v4 through the engine too, and the file still extracts."""
    cover = _two_regions()
    stego, version, verified = bench.embed_png(cover, 0.2, tmp_path / "v4.png", layout="v4")

    assert version == 4
    assert verified
    costs = bench.adaptive.rgb_cost_map(cover).reshape(-1)
    _cheap, percentile, smooth = bench.adaptivity(costs, cover, stego)
    assert percentile < 25.0, f"v4 should sit low on the cost scale, got {percentile:.1f}"
    assert smooth < 20.0, f"v4 should avoid the smooth half, got {smooth:.1f} %"


def test_adaptivity_shows_an_image_blind_layout_at_its_defaults(bench: ModuleType) -> None:
    """The metric's own control: v3 changes are spread evenly, so ~20 % and ~50 %."""
    cover = _two_regions()
    stego = cover.copy()
    rng = np.random.default_rng(4)
    picked = rng.choice(cover.size, size=cover.size // 10, replace=False)
    stego.reshape(-1)[picked] ^= 1

    costs = bench.adaptive.rgb_cost_map(cover).reshape(-1)
    cheap, percentile, smooth = bench.adaptivity(costs, cover, stego)
    assert cheap == pytest.approx(20.0, abs=6.0)
    assert percentile == pytest.approx(50.0, abs=6.0)
    assert smooth == pytest.approx(50.0, abs=6.0)


def test_adaptivity_of_an_untouched_image_is_undefined(bench: ModuleType) -> None:
    cover = _rgb(bench)
    costs = bench.adaptive.rgb_cost_map(cover).reshape(-1)
    cheap, percentile, smooth = bench.adaptivity(costs, cover, cover.copy())
    assert math.isnan(cheap) and math.isnan(percentile) and math.isnan(smooth)


def _measure(bench: ModuleType, chi2: float, rs: float, bpp: float = 0.2) -> object:
    return bench.Measurement(
        carrier="test",
        bpp=bpp,
        psnr_db=60.0,
        changed_pixels_pct=5.0,
        changed_samples_pct=2.0,
        chi2=chi2,
        rs=rs,
    )


def test_verdict_ignores_detectors_that_fire_on_the_cover(bench: ModuleType) -> None:
    cover = _measure(bench, 0.0, 0.9, bpp=0.0)
    stego = _measure(bench, 0.0, 0.95)
    assert bench.verdict(stego, cover) == "cover-blind"


def test_verdict_separates_suspect_from_detected(bench: ModuleType) -> None:
    quiet_cover = _measure(bench, 0.0, 0.0, bpp=0.0)
    assert bench.verdict(_measure(bench, 0.0, 0.0), quiet_cover) == "clean"
    assert bench.verdict(_measure(bench, 0.6, 0.0), quiet_cover) == "suspect"
    assert bench.verdict(_measure(bench, 0.95, 0.0), quiet_cover) == "detected"


def test_nan_readings_never_count_as_a_detection(bench: ModuleType) -> None:
    row = _measure(bench, math.nan, math.nan)
    assert not row.flagged and not row.screamed


def test_calibration_shows_the_detectors_are_alive(bench: ModuleType) -> None:
    block = bench.calibration()
    chi2 = [row["chi2"] for row in block["rows"]]
    rs = [row["rs"] for row in block["rows"]]
    assert chi2 == sorted(chi2), "chi-square must climb with the replacement rate"
    assert rs == sorted(rs), "RS must climb with the replacement rate"
    assert chi2[-1] > 0.99 and rs[-1] > 0.9


def test_main_writes_a_json_report(bench: ModuleType, tmp_path: Path, monkeypatch) -> None:
    """End-to-end: two carriers, one load level, a JSON file that explains itself."""
    monkeypatch.setattr(bench, "CARRIERS", bench.CARRIERS[:2])
    report = tmp_path / "report.json"
    code = bench.main(
        ["--bpp", "0.3", "--json", str(report), "--keep", str(tmp_path / "pngs")]
    )

    assert code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["carriers"] == 2
    assert len(payload["covers"]) == 2
    assert set(payload["summary"]) == {"v3", "v4", "v5"}, "every layout is summarised"
    for layout in ("v3", "v4", "v5"):
        block = payload["summary"][layout]
        assert len(block) == 1
        assert block[0]["bpp"] == pytest.approx(0.3)
        assert block[0]["carriers"] <= 2
    assert {row["layout"] for row in payload["rows"]} == {"v3", "v4", "v5"}
    assert all("cost_percentile" in row for row in payload["rows"])
    assert payload["rows"] and all(row["verdict"] for row in payload["rows"])
    assert (tmp_path / "pngs").is_dir(), "--keep must leave the carriers on disk"
    for name in payload["covers"]:
        assert any(row["carrier"] == name for row in payload["rows"])
