"""Baseline steganalysis of the v3 and v4 layouts: how much payload shows up.

Run with ``.venv/bin/python tools/bench_steganalysis.py``.  Eight deterministic
512x512 carriers are generated, embedded through the real engine twice — once
with the v3 keyed layout (LSB matching, positions ignore the image) and once with
the v4 adaptive layout (S-UNIWARD-style cost map, changes go where the image is
busy) — written out as PNG, read back, extracted again to prove the file is still
readable, and handed to two classic blind detectors:

* **chi-square** (Westfeld & Pfitzmann, 1999) — tests whether the histogram
  pairs ``(2i, 2i+1)`` have equalised, which is what LSB randomisation does.
  Reported as the probability of embedding, 0..1.
* **RS analysis** (Fridrich, Goljan & Du, 2001) — compares regular and singular
  groups under LSB flipping and solves for an embedding rate ``p``.  The model
  was derived for LSB *replacement*; LSB matching (what PHANTOM writes) is known
  to bias it, so the number is reported as measured, not as truth.

Both detectors are run on the untouched carriers too: a detector that already
screams on a cover says nothing about the payload, and RS does exactly that on
noise-like textures.  A carrier therefore only counts as *detected* when its
stego is flagged while its cover was not.

The comparison table is the point of the run: the same payload, the same
carriers, two ways of choosing which samples to touch.  Distortion (PSNR) and the
number of changes are the same by construction — what differs is *where* they
land, and whether the detectors notice.  ``перцентиль`` / ``в гладких 50%``
show that directly: an adaptive embedder should pick changes from the cheapest
end of the cost map (percentile near 0) and put almost nothing into the smooth
half, while an image-blind one lands at 50 % / 50 % by definition.

SRM / rich-model features are deliberately out of scope: this is the cheap
baseline that says at which payload a PHANTOM file stops looking innocent.

``bpp`` is **bits per pixel** over the whole 512x512 frame, so 1 bpp equals
0.33 bit per channel; the engine writes at most one bit per channel sample.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from phantom.core import constants as C
from phantom.core.payload import Payload, pack_payload
from phantom.stego import adaptive, engine
from phantom.stego.samples import ImageSamples

SIZE: Final = 512
PASSWORD: Final = "benchmark password, not a secret"
LOAD_LEVELS: Final[tuple[float, ...]] = (0.05, 0.1, 0.2, 0.4)
LAYOUTS: Final[tuple[str, ...]] = ("v3", "v4")
LAYOUT_VERSIONS: Final[dict[str, int]] = {
    "v3": C.CONTAINER_VERSION,
    "v4": C.ADAPTIVE_CONTAINER_VERSION,
}

# Detector thresholds.  They are heuristics picked so that an ordinary cover
# stays out of the "suspect" bucket; they are not calibrated for a false
# positive rate on a real photo corpus.
CLEAN_CHI2: Final = 0.5
CLEAN_RS: Final = 0.05
FLAG_CHI2: Final = 0.9
FLAG_RS: Final = 0.25


# ---------------------------------------------------------------------------
# carriers
# ---------------------------------------------------------------------------
def _grid() -> tuple[np.ndarray, np.ndarray]:
    y, x = np.mgrid[0:SIZE, 0:SIZE]
    return y.astype(np.float64), x.astype(np.float64)


def _as_rgb(base: np.ndarray, noise: np.ndarray) -> np.ndarray:
    """One luminance field, a per-channel gain and shared noise -> uint8 RGB."""
    field = np.asarray(base, dtype=np.float64)
    gains = (1.0, 0.94, 1.06)
    channels = [np.clip(field * gain + noise, 0, 255) for gain in gains]
    return np.stack(channels, axis=-1).astype(np.uint8)


def carrier_gradient(rng: np.random.Generator) -> np.ndarray:
    _, x = _grid()
    return _as_rgb(40 + 0.34 * x, rng.normal(0, 1.5, (SIZE, SIZE)))


def carrier_waves(rng: np.random.Generator) -> np.ndarray:
    y, x = _grid()
    base = 128 + 55 * np.sin(x / 7.0) * np.sin(y / 11.0)
    return _as_rgb(base, rng.normal(0, 2.0, (SIZE, SIZE)))


def _blur(field: np.ndarray, rounds: int = 3) -> np.ndarray:
    """3x3 box blur: turns white noise into the smooth blobs a photo has."""
    out = field
    for _ in range(rounds):
        padded = np.pad(out, 1, mode="edge")
        out = (
            sum(
                padded[row : row + SIZE, column : column + SIZE]
                for row in range(3)
                for column in range(3)
            )
            / 9.0
        )
    return out


def carrier_photo(rng: np.random.Generator) -> np.ndarray:
    """Smooth luminance blobs plus mild sensor noise — the ordinary photo case."""
    blobs = _blur(rng.normal(0, 1.0, (SIZE, SIZE))) * 190
    return _as_rgb(128 + blobs, rng.normal(0, 5.0, (SIZE, SIZE)))


def carrier_fabric(rng: np.random.Generator) -> np.ndarray:
    y, x = _grid()
    base = 130 + 30 * np.sin(x * 0.22) + 20 * np.sin(y * 0.28 + 0.6)
    return _as_rgb(base, rng.normal(0, 4.0, (SIZE, SIZE)))


def carrier_mixed(rng: np.random.Generator) -> np.ndarray:
    _, x = _grid()
    detail = 128 + rng.normal(0, 35.0, (SIZE, SIZE))
    base = np.where(x < SIZE / 2, np.full((SIZE, SIZE), 196.0), detail)
    return _as_rgb(base, rng.normal(0, 1.2, (SIZE, SIZE)))


def carrier_clipped(rng: np.random.Generator) -> np.ndarray:
    base = np.clip(128 + rng.normal(0, 60.0, (SIZE, SIZE)), 0, 255)
    # Nothing extra on top: the histogram already piles up at both ends.
    return _as_rgb(base, np.zeros((SIZE, SIZE)))


def carrier_edges(rng: np.random.Generator) -> np.ndarray:
    y, x = _grid()
    base = 60 + 0.2 * x + 0.1 * y
    base = np.where(((x - 150) ** 2 + (y - 300) ** 2) < 90**2, 190, base)
    base = np.where((x > 330) & (x < 470) & (y > 60) & (y < 200), 30, base)
    return _as_rgb(base, rng.normal(0, 1.5, (SIZE, SIZE)))


def carrier_flat(rng: np.random.Generator) -> np.ndarray:
    base = np.full((SIZE, SIZE), 128.0)
    base[100:200, 100:300] += 8
    base[300:340, 50:460] -= 6
    return _as_rgb(base, rng.normal(0, 0.7, (SIZE, SIZE)))


CARRIERS: Final[tuple[tuple[str, Any], ...]] = (
    ("smooth gradient", carrier_gradient),
    ("sine waves", carrier_waves),
    ("photo noise", carrier_photo),
    ("fabric texture", carrier_fabric),
    ("flat + detail", carrier_mixed),
    ("clipped noise", carrier_clipped),
    ("edges and shapes", carrier_edges),
    ("near-flat", carrier_flat),
)


# ---------------------------------------------------------------------------
# chi-square attack
# ---------------------------------------------------------------------------
def _gammaincc(a: float, x: float) -> float:
    """Regularised upper incomplete gamma Q(a, x), series + continued fraction."""
    if x <= 0.0:
        return 1.0
    if x < a + 1.0:
        ap = a
        term = 1.0 / a
        total = term
        for _ in range(1000):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return 1.0 - total * math.exp(-x + a * math.log(x) - math.lgamma(a))
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = tiny if abs(d) < tiny else d
        c = b + an / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def chi2_sf(statistic: float, df: int) -> float:
    """Upper tail ``P(X > statistic)`` of a chi-square distribution."""
    if df <= 0:
        return 0.0
    return _gammaincc(df / 2.0, statistic / 2.0)


def chi2_probability(plane: np.ndarray) -> float:
    """Westfeld-Pfitzmann probability of embedding for one channel plane."""
    hist = np.bincount(plane.reshape(-1), minlength=256).astype(np.float64)
    even, odd = hist[0::2], hist[1::2]
    expected = (even + odd) / 2.0
    usable = expected > 0
    if int(usable.sum()) < 2:
        return 0.0
    deviation = (even - expected)[usable]
    statistic = float((deviation**2 / expected[usable]).sum())
    return chi2_sf(statistic, int(usable.sum()) - 1)


# ---------------------------------------------------------------------------
# RS analysis
# ---------------------------------------------------------------------------
RS_MASK: Final = (0, 1, 1, 0)
RS_NEGATIVE_MASK: Final = (0, -1, -1, 0)


def _smoothness(groups: np.ndarray) -> np.ndarray:
    return np.abs(np.diff(groups, axis=1)).sum(axis=1)


def _flip(values: np.ndarray, direction: int) -> np.ndarray:
    if direction == 0:
        return values
    if direction == 1:
        return values ^ 1
    # F(-1): -1<->0, 1<->2, ...; 255 has no partner and stays put.
    return np.where(values < 255, ((values + 1) ^ 1) - 1, 255)


def rs_counts(plane: np.ndarray, mask: tuple[int, int, int, int]) -> tuple[int, int]:
    """Regular and singular group counts for one mask."""
    flat = plane.reshape(-1)
    groups = flat[: (flat.size // 4) * 4].reshape(-1, 4).astype(np.int16)
    flipped = groups.copy()
    for column in (1, 2):
        flipped[:, column] = _flip(groups[:, column], mask[column])
    original = _smoothness(groups)
    changed = _smoothness(flipped)
    return int((changed > original).sum()), int((changed < original).sum())


def rs_estimate(plane: np.ndarray) -> float:
    """Estimated embedding rate in bits per sample, 0..1, or NaN.

    Solves ``2(d0 + d1)p^2 + (d_neg0 - d1 - d0)p + (d0 - d_neg0) = 0`` where
    ``d = R - S`` at p = 0 (original) and p = 1 (fully flipped image).  NaN means
    the quadratic has no usable root, typically a texture whose histograms are
    too degenerate for the model — an honest "no estimate", not a verdict.
    """
    regular, singular = rs_counts(plane, RS_MASK)
    neg_regular, neg_singular = rs_counts(plane, RS_NEGATIVE_MASK)
    flipped_regular, flipped_singular = rs_counts(plane ^ 1, RS_MASK)

    d0 = regular - singular
    d_neg0 = neg_regular - neg_singular
    d1 = flipped_regular - flipped_singular

    a = 2.0 * (d1 + d0)
    b = d_neg0 - d1 - d0
    c = d0 - d_neg0
    if abs(a) < 1e-12:
        return math.nan
    discriminant = b * b - 4.0 * a * c
    if discriminant < 0.0:
        return math.nan
    root = math.sqrt(discriminant)
    low, high = sorted(((-b + root) / (2.0 * a), (-b - root) / (2.0 * a)))
    return _select_root(low, high)


def _select_root(low: float, high: float) -> float:
    """Pick the rate the quadratic model actually allows.

    A root inside 0..1 is the answer.  When neither root is, the closest one is
    clamped - being a hair below zero is just estimation noise - unless it is
    far outside, which means the model does not describe this image at all
    (saturated highlights, flat blocks) and an honest NaN is better than 100 %.
    """
    for candidate in (low, high):
        if 0.0 <= candidate <= 1.0:
            return candidate
    nearest = min((low, high), key=lambda value: min(abs(value), abs(value - 1.0)))
    if nearest < -0.25 or nearest > 1.25:
        return math.nan
    return min(1.0, max(0.0, nearest))


# ---------------------------------------------------------------------------
# distortion metrics
# ---------------------------------------------------------------------------
def psnr(carrier: np.ndarray, stego: np.ndarray) -> float:
    difference = carrier.astype(np.float64) - stego.astype(np.float64)
    mse = float((difference**2).mean())
    return math.inf if mse == 0.0 else 10.0 * math.log10(255.0**2 / mse)


def changed_percent(carrier: np.ndarray, stego: np.ndarray) -> tuple[float, float]:
    """Changed pixels and changed channel samples, in percent."""
    samples = carrier != stego
    pixels = samples.any(axis=-1)
    return 100.0 * pixels.sum() / pixels.size, 100.0 * samples.sum() / samples.size


def _worst(values: list[float]) -> float:
    """Largest finite value, NaN when the detector produced nothing usable."""
    usable = [value for value in values if not math.isnan(value)]
    return max(usable) if usable else math.nan


def _detect(image: np.ndarray) -> tuple[float, float]:
    """(chi-square, RS) of the worst channel."""
    channels = range(image.shape[2])
    chi2 = _worst([chi2_probability(image[:, :, channel]) for channel in channels])
    rs = _worst([rs_estimate(image[:, :, channel]) for channel in channels])
    return chi2, rs


# ---------------------------------------------------------------------------
# measurements
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Measurement:
    carrier: str
    bpp: float
    psnr_db: float
    changed_pixels_pct: float
    changed_samples_pct: float
    chi2: float
    rs: float
    layout: str = "v3"
    version: int = C.CONTAINER_VERSION
    cheap_share: float = math.nan
    cost_percentile: float = math.nan
    smooth_share: float = math.nan
    verified: bool = False

    @property
    def is_cover(self) -> bool:
        return self.bpp <= 0.0

    @property
    def flagged(self) -> bool:
        """Either detector reacted, regardless of whether the cover did too."""
        chi2_hit = not math.isnan(self.chi2) and self.chi2 >= CLEAN_CHI2
        rs_hit = not math.isnan(self.rs) and self.rs >= CLEAN_RS
        return chi2_hit or rs_hit

    @property
    def screamed(self) -> bool:
        """The strong thresholds: an analyst would call this a find."""
        chi2_hit = not math.isnan(self.chi2) and self.chi2 >= FLAG_CHI2
        rs_hit = not math.isnan(self.rs) and self.rs >= FLAG_RS
        return chi2_hit or rs_hit


def adaptivity(cover: np.ndarray, stego: np.ndarray) -> tuple[float, float, float]:
    """Where the changes landed relative to the cost map of the untouched cover.

    Three numbers, all of them 20 / 50 / 50 for a change set chosen without looking
    at the image — which is exactly what v3 does:

    * share of changed samples in the cheapest fifth of the map;
    * median cost *percentile* of the changed samples (0 = always the cheapest);
    * share in the expensive half (sky, skin, flat fills).

    The percentile is the one to read when the payload is large enough to fill the
    cheap region completely: the share saturates at 100 %, the percentile does not.
    """
    costs = adaptive.layout_costs(cover).reshape(-1)
    changed = np.flatnonzero((cover != stego).reshape(-1))
    if changed.size == 0:
        return math.nan, math.nan, math.nan
    order = np.argsort(costs, kind="stable")
    ranks = np.empty(costs.size, dtype=np.float64)
    ranks[order] = np.arange(costs.size) * (100.0 / max(1, costs.size - 1))
    picked = costs[changed]
    cheap = float(np.quantile(costs, 0.2))
    smooth = float(np.quantile(costs, 0.5))
    return (
        100.0 * float(np.mean(picked <= cheap)),
        float(np.median(ranks[changed])),
        100.0 * float(np.mean(picked >= smooth)),
    )


def measure(
    name: str,
    cover: np.ndarray,
    image: np.ndarray,
    bpp: float,
    *,
    layout: str = "v3",
    version: int = C.CONTAINER_VERSION,
    verified: bool = False,
) -> Measurement:
    pixels_pct, samples_pct = changed_percent(cover, image)
    chi2, rs = _detect(image)
    cheap_share, cost_percentile, smooth_share = adaptivity(cover, image)
    return Measurement(
        carrier=name,
        bpp=bpp,
        psnr_db=psnr(cover, image),
        changed_pixels_pct=pixels_pct,
        changed_samples_pct=samples_pct,
        chi2=chi2,
        rs=rs,
        layout=layout,
        version=version,
        cheap_share=cheap_share,
        cost_percentile=cost_percentile,
        smooth_share=smooth_share,
        verified=verified,
    )


def verdict(row: Measurement, cover: Measurement) -> str:
    """Cover-relative verdict: a detector that fires on the cover proves nothing."""
    if row.screamed and not cover.flagged:
        return "detected"
    if row.flagged and not cover.flagged:
        return "suspect"
    if row.flagged:
        return "cover-blind"
    return "clean"


# ---------------------------------------------------------------------------
# calibration: prove the detectors can see anything at all
# ---------------------------------------------------------------------------
def calibration() -> dict[str, Any]:
    """Detector sanity check on synthetic LSB replacement, without the engine.

    Replacement is what chi-square and RS were designed for, so on a smooth
    image they must climb from ~0 to ~1 as the rate grows.  If this block is
    sane, any quiet result further down is the engine's doing, not a bug here.
    """
    rng = np.random.default_rng(5)
    y, x = np.mgrid[0:SIZE, 0:SIZE]
    base = np.clip(128 + 40 * np.sin(x / 37.0) + 25 * np.cos(y / 53.0), 0, 255)
    plane = (base + rng.normal(0, 2.0, (SIZE, SIZE))).astype(np.uint8)
    rows: list[dict[str, Any]] = []
    for rate in (0.0, 0.25, 0.5, 1.0):
        tested = plane.copy()
        if rate > 0.0:
            selected = rng.random((SIZE, SIZE)) < rate
            target = rng.integers(0, 2, (SIZE, SIZE)).astype(np.uint8)
            tested[selected] = (tested[selected] & 0xFE) | target[selected]
        rows.append(
            {
                "bit_per_channel": rate,
                "chi2": chi2_probability(tested),
                "rs": rs_estimate(tested),
            }
        )
    return {"method": "LSB replacement on a synthetic smooth plane", "rows": rows}


def print_calibration(block: dict[str, Any]) -> None:
    print("\ncalibration — LSB replacement on a smooth synthetic plane")
    print(f"  {'bit/channel':>12}{'chi2 p':>10}{'RS p̂':>9}")
    for row in block["rows"]:
        print(f"  {row['bit_per_channel']:>12.2f}{row['chi2']:>10.3f}{row['rs']:>9.3f}")


# ---------------------------------------------------------------------------
# embedding
# ---------------------------------------------------------------------------
def packed_blob_for(bpp: float, pixels: int) -> tuple[bytes, bytes]:
    """Packed payload whose ciphertext lands on the requested bpp, plus its plaintext.

    The blob has to come from ``pack_payload`` — extraction checks its metadata and
    digest, so a hand-built blob would report corruption even when the payload
    itself survived.  Compression of random bytes is close to 1:1, so the data
    length is nudged twice to hit the target size within a handful of bytes.
    """
    target_bits = round(bpp * pixels)
    target_bytes = (target_bits - C.HEADER_BITS) // 8 - C.TAG_SIZE
    if target_bytes < 64:
        raise SystemExit(f"{bpp} bpp does not fit into {pixels} pixels")
    rng = np.random.default_rng(round(bpp * 10_000))
    budget = target_bytes - 2  # a byte or two of slack for the capacity check
    length = max(1, budget - 48)
    data = b""
    blob = b""
    for _attempt in range(4):
        data = rng.integers(0, 256, length, dtype=np.uint8).tobytes()
        blob = pack_payload(Payload.file("bench.bin", data)).blob
        shortfall = budget - len(blob)
        if shortfall == 0:
            break
        length = max(1, length + shortfall)
    return blob, data


def embed_png(
    source: np.ndarray, bpp: float, target: Path, *, layout: str = "v3"
) -> tuple[np.ndarray, int, bool]:
    """Embed through the real engine, write a PNG, read it back, extract it again."""
    pixels = source.copy()
    space = ImageSamples(pixels)
    blob, data = packed_blob_for(bpp, int(source.shape[0] * source.shape[1]))
    report = engine.embed_payload(space, blob, PASSWORD, container_version=LAYOUT_VERSIONS[layout])
    Image.fromarray(pixels, mode="RGB").save(target, format="PNG", optimize=False)
    with Image.open(target) as written:
        stego = np.array(written.convert("RGB"), dtype=np.uint8)
    verified = False
    try:
        extracted = engine.extract_payload(ImageSamples(stego.copy()), PASSWORD)
        verified = extracted.integrity_ok and extracted.data == data
    except Exception:  # noqa: BLE001 - a broken file is a result, not a crash
        verified = False
    return stego, report.container_version, verified


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def _cell(value: float, width: int, digits: int = 3) -> str:
    body = "n/a" if math.isnan(value) else f"{value:.{digits}f}"
    return f"{body:>{width}}"


def print_per_carrier(rows: list[Measurement], covers: dict[str, Measurement]) -> None:
    print("\nper image (chi2 and RS are the worst channel)")
    header = (
        f"  {'carrier':<18}{'bpp':>6}{'PSNR dB':>10}{'changed px %':>14}"
        f"{'chi2 p':>9}{'RS p̂':>8}   verdict"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    current = ""
    for row in rows:
        cover = covers[row.carrier]
        if row.carrier != current:
            current = row.carrier
            print(
                f"  {row.carrier:<18}{'cover':>6}{'—':>10}"
                f"{'—':>14}{_cell(cover.chi2, 9)}{_cell(cover.rs, 8)}   (untouched)"
            )
        print(
            f"  {'':<18}{row.bpp:>6.2f}{row.psnr_db:>10.2f}"
            f"{row.changed_pixels_pct:>14.2f}{_cell(row.chi2, 9)}{_cell(row.rs, 8)}"
            f"   {verdict(row, cover)}"
        )


def summarise(rows: list[Measurement], covers: dict[str, Measurement]) -> list[dict[str, Any]]:
    """Per load level: distortion, detector readings and how many carriers fell.

    Carriers whose cover already trips a detector are left out of the ratio:
    their verdict says nothing about the payload.
    """
    usable = {name for name, cover in covers.items() if not cover.flagged}
    summary: list[dict[str, Any]] = []
    for bpp in sorted({row.bpp for row in rows if not row.is_cover}):
        group = [row for row in rows if abs(row.bpp - bpp) < 1e-9 and row.carrier in usable]
        hits = [row for row in group if verdict(row, covers[row.carrier]) == "detected"]
        suspects = [row for row in group if verdict(row, covers[row.carrier]) == "suspect"]
        summary.append(
            {
                "bpp": bpp,
                "psnr_db": float(np.median([row.psnr_db for row in group])),
                "changed_pixels_pct": float(np.median([row.changed_pixels_pct for row in group])),
                "changed_samples_pct": float(np.median([row.changed_samples_pct for row in group])),
                "chi2_median": float(np.median([row.chi2 for row in group])),
                "chi2_max": _worst([row.chi2 for row in group]),
                "rs_median": float(np.nanmedian([row.rs for row in group])),
                "rs_max": _worst([row.rs for row in group]),
                "detected": len(hits),
                "suspect": len(suspects),
                "carriers": len(group),
                "excluded": len(covers) - len(usable),
                "detection_rate": 100.0 * len(hits) / len(group) if group else 0.0,
            }
        )
    return summary


def print_summary(summary: list[dict[str, Any]], covers: dict[str, Measurement]) -> None:
    blind = [name for name, cover in covers.items() if cover.flagged]
    chi2_noise = _worst([cover.chi2 for cover in covers.values()])
    rs_noise = _worst([cover.rs for cover in covers.values()])
    width = 100
    print("\n" + "=" * width)
    print("сводка по носителям (медиана по 8 картинкам, детекторы — худший канал)")
    print("=" * width)
    print(
        f"{'Нагрузка (bpp)':>14} | {'PSNR (dB)':>9} | {'Изменено пикселей (%)':>21} | "
        f"{'χ² p':>6} | {'RS p̂':>6} | Вероятность обнаружения"
    )
    print("-" * width)
    for row in summary:
        detection = f"{row['detected']}/{row['carriers']} ({row['detection_rate']:.0f} %)"
        if row["suspect"]:
            detection += f" +{row['suspect']} question"
        print(
            f"{row['bpp']:>14.2f} | {row['psnr_db']:>9.2f} | {row['changed_pixels_pct']:>21.2f} | "
            f"{_cell(row['chi2_median'], 6)} | {_cell(row['rs_median'], 6)} | {detection}"
        )
    print("-" * width)
    print(
        "χ² p — вероятность встраивания по Westfeld-Pfitzmann (1.0 = пары гистограммы выровнены);\n"
        "RS p̂ — оценка доли изменённых бит (RS рассчитан на LSB replacement, matching её смещает,"
        " «n/a» = модель не сошлась);\n"
        "обнаружение считается только там, где чистый носитель молчал."
    )
    print(
        f"шум на чистых носителях: χ² ≤ {_cell(chi2_noise, 5, 3).strip()}, "
        f"RS ≤ {_cell(rs_noise, 5, 3).strip()} — это пол шкалы, ниже него разницы нет"
    )
    if blind:
        print(
            f"исключены из знаменателя ({len(blind)}/{len(covers)}): {', '.join(blind)} — "
            "детектор срабатывает на них ещё до встраивания («cover-blind»)"
        )


def print_comparison(rows: list[Measurement], covers: dict[str, Measurement]) -> None:
    """v3 against v4 at the same payload: distortion, placement, detector verdicts."""
    usable = {name for name, cover in covers.items() if not cover.flagged}
    loads = sorted({row.bpp for row in rows if not row.is_cover})
    width = 116
    print("\n" + "=" * width)
    print("v3 (LSB matching) против v4 (адаптивная раскладка) — те же носители, та же нагрузка")
    print("=" * width)
    header = (
        f"  {'bpp':>5} | {'раскладка':<10} | {'PSNR dB':>8} | {'изменено px %':>13} | "
        f"{'перцентиль':>10} | {'в гладких 50%':>13} | {'χ² p̂':>6} | {'RS p̂':>6} | "
        f"{'обнаружено':>10} | файл"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))
    for bpp in loads:
        for layout in LAYOUTS:
            group = [
                row
                for row in rows
                if abs(row.bpp - bpp) < 1e-9 and row.layout == layout and row.carrier in usable
            ]
            if not group:
                continue
            hits = [row for row in group if verdict(row, covers[row.carrier]) == "detected"]
            suspects = [row for row in group if verdict(row, covers[row.carrier]) == "suspect"]
            detected = f"{len(hits)}/{len(group)}"
            if suspects:
                detected += f" +{len(suspects)}?"
            fallbacks = sum(1 for row in group if row.version != LAYOUT_VERSIONS[layout])
            verified = sum(1 for row in group if row.verified)
            state = f"{verified}/{len(group)} ok"
            if layout == "v4" and fallbacks:
                state += f", {fallbacks} откат"
            print(
                f"  {bpp:>5.2f} | {layout + ' ' + _layout_note(layout):<10} | "
                f"{np.median([row.psnr_db for row in group]):>8.2f} | "
                f"{np.median([row.changed_pixels_pct for row in group]):>13.2f} | "
                f"{np.median([row.cost_percentile for row in group]):>10.1f} | "
                f"{np.median([row.smooth_share for row in group]):>13.1f} | "
                f"{_cell(float(np.median([row.chi2 for row in group])), 6)} | "
                f"{_cell(float(np.nanmedian([row.rs for row in group])), 6)} | "
                f"{detected:>10} | {state}"
            )
    print("  " + "-" * (len(header) - 2))
    print(
        "«перцентиль» — медианный перцентиль стоимости изменённых сэмплов (0 = самые дешёвые);\n"
        "«в гладких 50%» — доля изменений в самой дорогой половине карты (небо, кожа, заливки)."
        "\nСлепой выбор по построению даёт 50 % и 50 %: так выглядит v3."
        "\n«файл» — сколько стенограмм удалось извлечь обратно после записи в PNG."
    )


def _layout_note(layout: str) -> str:
    return "matching" if layout == "v3" else "adaptive"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, help="also write the numbers here")
    parser.add_argument("--keep", type=Path, help="write the generated PNGs into this directory")
    parser.add_argument(
        "--bpp",
        type=float,
        nargs="+",
        default=list(LOAD_LEVELS),
        help="payload sizes in bits per pixel",
    )
    parser.add_argument(
        "--layout",
        choices=(*LAYOUTS, "both"),
        default="both",
        help="which layout to run (default: both, for the comparison table)",
    )
    args = parser.parse_args(argv)
    layouts: tuple[str, ...] = LAYOUTS if args.layout == "both" else (args.layout,)
    loads: tuple[float, ...] = tuple(args.bpp)

    workdir = args.keep or Path(tempfile.mkdtemp(prefix="phantom-steganalysis-"))
    workdir.mkdir(parents=True, exist_ok=True)
    keep = args.keep is not None
    if not keep:
        print(f"carriers are written to {workdir} (temporary, use --keep to keep them)")

    print(
        f"PHANTOM v{C.CONTAINER_VERSION} (keyed LSB matching) "
        f"vs v{C.ADAPTIVE_CONTAINER_VERSION} (adaptive) · "
        f"{len(CARRIERS)} carriers of {SIZE}x{SIZE} RGB · bpp = bits per pixel"
    )
    print_calibration(calibration())

    rows: list[Measurement] = []
    covers: dict[str, Measurement] = {}
    started = time.perf_counter()
    try:
        for index, (name, factory) in enumerate(CARRIERS):
            carrier = factory(np.random.default_rng(1000 + index))
            slug = name.replace(" ", "-")
            cover_path = workdir / f"{index:02d}-{slug}.png"
            Image.fromarray(carrier, mode="RGB").save(cover_path, format="PNG")
            covers[name] = measure(name, carrier, carrier, 0.0)
            print(
                f"  cover {name:<18} chi2={_cell(covers[name].chi2, 6, 3)}"
                f" RS={_cell(covers[name].rs, 6, 3)}"
            )
            for layout in layouts:
                for bpp in loads:
                    stego_path = workdir / f"{index:02d}-{slug}-{layout}-{bpp:.2f}.png"
                    stego, version, verified = embed_png(carrier, bpp, stego_path, layout=layout)
                    rows.append(
                        measure(
                            name,
                            carrier,
                            stego,
                            bpp,
                            layout=layout,
                            version=version,
                            verified=verified,
                        )
                    )
                    print(
                        f"  embedded {name:<18} {layout} {bpp:.2f} bpp -> {stego_path.name}"
                        f"{'' if verified else '  (extract failed)'}"
                    )
    finally:
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)
    elapsed = time.perf_counter() - started

    for layout in layouts:
        layout_rows = [row for row in rows if row.layout == layout]
        print_per_carrier(layout_rows, covers)
        print_summary(summarise(layout_rows, covers), covers)
    if len(layouts) > 1:
        print_comparison(rows, covers)
    broken = [row for row in rows if not row.verified]
    print(f"\n{len(rows)} embeds in {elapsed:.1f} s, {len(broken)} unreadable")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "size": SIZE,
            "carriers": len(CARRIERS),
            "calibration": calibration(),
            "covers": {
                name: {
                    "chi2": cover.chi2,
                    "rs": cover.rs,
                    "flagged": cover.flagged,
                }
                for name, cover in covers.items()
            },
            "rows": [
                {
                    "carrier": row.carrier,
                    "bpp": row.bpp,
                    "psnr_db": row.psnr_db,
                    "changed_pixels_pct": row.changed_pixels_pct,
                    "changed_samples_pct": row.changed_samples_pct,
                    "chi2": row.chi2,
                    "rs": row.rs,
                    "layout": row.layout,
                    "version": row.version,
                    "cheap_share": row.cheap_share,
                    "cost_percentile": row.cost_percentile,
                    "smooth_share": row.smooth_share,
                    "verified": row.verified,
                    "verdict": verdict(row, covers[row.carrier]),
                }
                for row in rows
            ],
            "summary": {
                layout: summarise([row for row in rows if row.layout == layout], covers)
                for layout in layouts
            },
        }
        args.json.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"json written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
