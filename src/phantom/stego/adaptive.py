"""Cost maps for adaptive embedding: where a ±1 change is least visible.

The idea behind S-UNIWARD and HILL is the same: hide the payload where the image
is already busy.  A flip in a smooth sky is a lone outlier; the same flip in
foliage, hair or noise is one more grain among thousands.

Why the cost reduces to a local energy
--------------------------------------
S-UNIWARD defines the cost of changing pixel ``(i, j)`` by ±1 as

    ρ_ij = Σ_k Σ_dirs  |W^(k)_ij(X) − W^(k)_ij(X ± 1)| / (|W^(k)_ij(X)| + σ)

with ``W^(k)`` the undecimated wavelet detail of level ``k``.  ``W`` is linear,
so a unit change of one pixel moves every coefficient inside the filter footprint
by exactly the impulse response of the filter — a number that does not depend on
the image.  The numerator is therefore a constant per position, and

    ρ_ij  ∝  1 / ( Σ_k Σ_dirs |W^(k)_ij(X)| + σ )

which is the local energy of the wavelet details: the thing this module computes.
HILL replaces the wavelet with a 3×3 high-pass and adds two box blurs; the second
blur (15×15) is what keeps the cost from flickering pixel by pixel, and it is here
too, because the extractor has to rebuild the same ordering from the stego image.

The filters are Daubechies-8: eight taps, orthogonal, vanishing moments up to
order four, which is why a smooth gradient produces almost no detail energy and a
texture produces a lot.

Reproducing the layout without the cover
----------------------------------------
The receiver has only the stego image, so the cost map it computes has to be the
one the sender used.  Computing it from the raw pixels is not good enough: a ±1
change nudges the wavelet details of a 15×15 neighbourhood, and neighbouring costs
then reorder — measured drift was anywhere from one to twelve hundred positions
depending on how coarsely the costs were quantised, and a single swapped position
corrupts the payload.

The fix is an invariant instead of a search.  Every sample is only ever changed
*inside its group of sixteen values* — see :data:`MASK_LOW_BITS` and
:func:`write_masked` — so ``value >> 4`` never moves, :func:`masked_pixels` returns
the same coarse image at both ends, and the costs derived from it are identical.
The layout is then a pure function of the key and the carrier, computed once and
reproduced exactly; the engine still re-derives it after writing and falls back to
the v3 layout if it ever disagrees.

What this module deliberately does not do
-----------------------------------------
Real S-UNIWARD embeds with syndrome-trellis codes, which decouple "where to
change" from "which bits are where".  Selection by cost without a code still leaks
the *set* of candidate positions to a detector that knows the scheme, and the
masked writer forces the direction on two of every sixteen samples, a small
asymmetry in the ±1 distribution.  Both are stated rather than papered over.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final, Protocol

import numpy as np

from phantom.stego.permute import keyed_permutation


class WritableSamples(Protocol):
    """The slice of a sample space adaptive embedding needs."""

    def gather(self, positions: np.ndarray) -> np.ndarray: ...

    def scatter(self, positions: np.ndarray, values: np.ndarray) -> None: ...


#: Daubechies-8 (db4) low-pass decomposition filter, normalised to Σh = √2.
_DAUBECHIES8_LOW: Final = np.array(
    [
        0.230377813308855,
        0.714846570552542,
        0.630880767929591,
        -0.027983769416984,
        -0.187034811718881,
        0.030841381835987,
        0.032883011666983,
        -0.010597401784997,
    ],
    dtype=np.float64,
)

#: Wavelet decomposition levels.  Two levels already reach 4 pixels around a
#: sample, which is the neighbourhood a ±1 change disturbs; a third level doubles
#: the work for a mask that looks the same.
LEVELS: Final = 2

#: HILL's second blur: without it the cost of neighbouring pixels is uncorrelated
#: and the selection is decided by noise rather than by texture.
SPREAD_RADIUS: Final = 7

#: Relative floor added to the energy before inverting it, so a perfectly flat
#: image gives a finite cost instead of a division by zero.
ENERGY_FLOOR: Final = 0.1

#: How many low bits a change is allowed to disturb.  A ±1 touches the LSB and,
#: when it crosses a group boundary, the bits above it — so if the *direction* is
#: chosen to stay inside the group, ``value >> MASK_LOW_BITS`` never moves.  That
#: coarse image is what the cost map is computed from, which makes the layout the
#: same for the sender (cover) and the receiver (stego): no guessing, no drift.
MASK_LOW_BITS: Final = 4

#: Cost is quantised into this many levels before ordering.  Coarse buckets are
#: what make the layout survive the embedding that created it: a ±1 change moves
#: a local energy by a fraction of a percent, which must not reshuffle the order.
COST_BUCKETS: Final = 6


def daubechies8() -> tuple[np.ndarray, np.ndarray]:
    """Low-pass and high-pass Daubechies-8 filters (``g`` is the QMF of ``h``)."""
    high = np.array(
        [(-1.0) ** index * _DAUBECHIES8_LOW[7 - index] for index in range(8)],
        dtype=np.float64,
    )
    return _DAUBECHIES8_LOW.copy(), high


def _filter_axis(
    plane: np.ndarray,
    taps: np.ndarray,
    step: int,
    axis: int,
) -> np.ndarray:
    """Dilated (à trous) FIR along one axis, symmetric borders, no decimation."""
    radius = (taps.size - 1) * step
    pad = [(0, 0), (0, 0)]
    pad[axis] = (radius, radius)
    padded = np.pad(plane.astype(np.float64, copy=False), pad, mode="symmetric")
    out = np.zeros(plane.shape, dtype=np.float64)
    for index, tap in enumerate(taps):
        if tap == 0.0:
            continue
        shift = index * step
        window = [slice(None), slice(None)]
        window[axis] = slice(shift, shift + plane.shape[axis])
        out += tap * padded[tuple(window)]
    return out


def detail_energy(plane: np.ndarray, *, levels: int = LEVELS) -> np.ndarray:
    """Σ|wavelet detail| over three orientations and ``levels`` scales.

    The result has the shape of ``plane``: one number per sample, the local
    "busyness" of the neighbourhood that a flip would disturb.
    """
    low, high = daubechies8()
    energy = np.zeros(plane.shape, dtype=np.float64)
    for level in range(levels):
        step = 1 << level
        horizontal = _filter_axis(_filter_axis(plane, high, step, axis=1), low, step, axis=0)
        vertical = _filter_axis(_filter_axis(plane, low, step, axis=1), high, step, axis=0)
        diagonal = _filter_axis(_filter_axis(plane, high, step, axis=1), high, step, axis=0)
        energy += np.abs(horizontal) + np.abs(vertical) + np.abs(diagonal)
    return energy


def _box_blur(plane: np.ndarray, radius: int) -> np.ndarray:
    """Mean over a ``(2r+1)²`` window, computed with a summed-area table."""
    size = 2 * radius + 1
    padded = np.pad(plane, radius, mode="symmetric")
    table = np.zeros((padded.shape[0] + 1, padded.shape[1] + 1), dtype=np.float64)
    table[1:, 1:] = padded.cumsum(axis=0).cumsum(axis=1)
    total = (
        table[size:, size:] - table[:-size, size:] - table[size:, :-size] + table[:-size, :-size]
    )
    return total / float(size * size)


def texture_energy(plane: np.ndarray) -> np.ndarray:
    """HILL-style spread of the detail energy: what the cost inverts."""
    energy = detail_energy(plane)
    return _box_blur(_box_blur(energy, 1), SPREAD_RADIUS)


def cost_map(plane: np.ndarray) -> np.ndarray:
    """Per-sample cost of a ±1 change: high on smooth areas, low on texture.

    Costs are normalised to a median of 1.0, so a threshold means the same thing
    on a flat sky and on a noisy forest — the engine reasons in relative terms.
    """
    energy = texture_energy(plane)
    floor = ENERGY_FLOOR * float(energy.mean()) + 1e-9
    cost = 1.0 / (energy + floor)
    median = float(np.median(cost))
    return cost / median


def mask_group(value: np.ndarray | int) -> np.ndarray:
    """The part of a sample a masked change is not allowed to touch."""
    return np.asarray(value) >> MASK_LOW_BITS


def masked_pixels(pixels: np.ndarray) -> np.ndarray:
    """The coarse image the cost map is derived from: invariant under our writes."""
    coarse: np.ndarray = pixels[:, :, :3] >> MASK_LOW_BITS
    return coarse


def rgb_cost_map(pixels: np.ndarray) -> np.ndarray:
    """Costs for an ``(h, w, 3|4)`` uint8 image, one per used channel sample.

    Each channel is scored on its own detail energy and normalised separately:
    a channel that happens to be flat must still get the same meaning of "cheap"
    as its noisier neighbours, otherwise the payload would pile into one channel.
    """
    if pixels.ndim != 3 or pixels.shape[2] not in (3, 4):
        raise ValueError("expected an (h, w, 3|4) image")
    channels = pixels[:, :, :3]
    costs = np.empty(channels.shape, dtype=np.float64)
    for index in range(3):
        costs[:, :, index] = cost_map(channels[:, :, index])
    return costs


def slot_ranks(count: int, key: bytes) -> np.ndarray:
    """Keyed pseudo-random rank in ``0..count-1`` for each candidate slot.

    The tie-breaker inside a cost bucket: without the password the order inside a
    bucket is not reproducible.  It reuses the Feistel permutation the layout
    already trusts, domain-separated from the position key so the two orders are
    independent.
    """
    if count <= 0:
        return np.zeros(0, dtype=np.int64)
    domain_key = hashlib.blake2b(key + b"|adaptive-rank", digest_size=32).digest()
    permutation = keyed_permutation(domain_key, count)
    return np.asarray(permutation.apply(np.arange(count)), dtype=np.int64)


def bucket_of(costs: np.ndarray, buckets: int = COST_BUCKETS) -> np.ndarray:
    """Quantise costs into log-spaced buckets, cheapest bucket 0.

    Log spacing matters: textures differ from each other by factors, not by
    constants, so a linear quantiser would put every textured sample in bucket 0
    and every smooth one in the last.
    """
    if buckets < 1:
        raise ValueError("need at least one bucket")
    positive = np.maximum(costs, 1e-12)
    low, high = float(positive.min()), float(positive.max())
    if high <= low:
        return np.zeros(costs.shape, dtype=np.int64)
    position = np.log(positive / low) / np.log(high / low)
    scaled = np.floor(position * buckets).astype(np.int64)
    quantised: np.ndarray = np.clip(scaled, 0, buckets - 1)
    return quantised


@dataclass(frozen=True, slots=True)
class Selection:
    """Payload slots in the order the bits are written, plus their costs."""

    slots: np.ndarray
    costs: np.ndarray

    def __len__(self) -> int:
        return int(self.slots.size)


def select_positions(
    costs: np.ndarray,
    candidates: np.ndarray,
    count: int,
    key: bytes,
    *,
    buckets: int = COST_BUCKETS,
) -> Selection:
    """Pick ``count`` of ``candidates``: cheapest buckets first, key inside them.

    ``costs`` is indexed by slot, ``candidates`` are the slots the payload may use
    (everything except the header slots).  The order is by cost bucket, then by a
    key-derived rank, so the result depends on the password but not on the
    payload: a receiver that computed the same ``costs`` reproduces the same list.
    """
    candidates = np.asarray(candidates, dtype=np.int64)
    if count < 0:
        raise ValueError("count must not be negative")
    if count > candidates.size:
        raise ValueError("not enough candidate slots for the payload")
    if count == 0:
        empty = np.zeros(0, dtype=np.int64)
        return Selection(slots=empty, costs=np.zeros(0, dtype=np.float64))
    local = np.asarray(costs[candidates], dtype=np.float64)
    ranks = slot_ranks(candidates.size, key)
    order = np.lexsort((ranks, bucket_of(local, buckets)))[:count]
    chosen = candidates[order]
    return Selection(slots=chosen, costs=local[order])


def is_stabler(previous: np.ndarray, current: np.ndarray) -> bool:
    """Two layouts agree bit for bit — the fixed point the engine looks for."""
    return previous.shape == current.shape and bool(np.array_equal(previous, current))


def layout_costs(pixels: np.ndarray) -> np.ndarray:
    """``(h, w, 3)`` costs from the mask-invariant part of the image.

    Because :func:`write_masked` never crosses a mask group, this array is the
    same before and after embedding, and the receiver computes it from the file
    it holds without needing the cover.
    """
    coarse = masked_pixels(pixels)
    return rgb_cost_map(coarse)


def direction_stream(count: int, key: bytes, *, counter: int = 0) -> np.ndarray:
    """Boolean rundown: ``True`` means "change upwards" where the choice is free.

    Seeded from the password-derived key, so the direction each sample takes is
    not predictable from the file, and reproducible in tests.
    """
    if count <= 0:
        return np.zeros(0, dtype=bool)
    digest = hashlib.blake2b(
        key + b"|adaptive-direction|" + counter.to_bytes(8, "big"), digest_size=32
    ).digest()
    generator = np.random.default_rng(int.from_bytes(digest, "big"))
    return np.asarray(generator.random(count) < 0.5)


def write_masked(
    space: WritableSamples,
    slots: np.ndarray,
    bits: np.ndarray,
    direction: np.ndarray,
) -> None:
    """Write ``bits`` into ``slots`` while keeping every sample in its mask group.

    LSB matching decides *whether* to change a sample; this decides *which way*.
    Whenever the low bits sit at either end of their group only one direction
    exists (low nibble ``0000`` goes up, ``1111`` goes down), otherwise
    ``direction`` picks.  The result is a normal ±1 matching write whose high bits
    are provably untouched — the property the extractor relies on to rebuild the
    same slots.
    """
    slots = np.asarray(slots, dtype=np.int64)
    if bits.size != slots.size or direction.size != slots.size:
        raise ValueError("bits, directions and slots must have the same length")
    if slots.size == 0:
        return
    values = np.asarray(space.gather(slots), dtype=np.uint8)
    target = np.asarray(bits, dtype=np.uint8) & 1
    low = values & np.uint8((1 << MASK_LOW_BITS) - 1)
    full = (1 << MASK_LOW_BITS) - 1
    upward = np.where(low == 0, True, np.where(low == full, False, direction))
    shifted = values.astype(np.int16) + np.where(upward, 1, -1).astype(np.int16)
    updated = np.clip(shifted, 0, 255).astype(np.uint8)
    needs_change = (values & 1) != target
    result = np.where(needs_change, updated, values).astype(np.uint8)
    space.scatter(slots, result)
