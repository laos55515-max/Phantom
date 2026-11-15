"""Cost maps, ordering and the mask-preserving writer of adaptive embedding.

The properties that matter are pinned here, because the engine's v4 layout leans
on all of them: the Daubechies-8 filters are a valid orthonormal pair, detail
energy says "smooth" about a gradient and "busy" about noise, the cost is the
inverse of that energy, ordering depends on the key and not on the payload, and a
masked write never moves a sample out of its high-bit group — which is what makes
the layout reproducible without the cover.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from phantom.stego import adaptive
from phantom.stego.samples import ArraySamples


def _smooth(shape: tuple[int, int] = (128, 128)) -> np.ndarray:
    y, x = np.mgrid[0 : shape[0], 0 : shape[1]]
    return (120 + 40 * np.sin(x / 17.0) + 25 * np.cos(y / 23.0)).astype(np.float64)


def _busy(shape: tuple[int, int] = (128, 128), seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return _smooth(shape) + rng.normal(0, 25.0, shape)


def _two_regions(size: int = 256) -> np.ndarray:
    """Left half is sky, right half is foliage — the picture from the docstring."""
    rng = np.random.default_rng(3)
    _y, x = np.mgrid[0:size, 0:size]
    plane = np.where(x < size // 2, 120 + 30 * (x / size), 110 + rng.normal(0, 30.0, (size, size)))
    stack = np.stack([plane, plane * 0.9 + 20, plane * 0.8 + 30], axis=-1)
    return np.clip(stack, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# filters
# ---------------------------------------------------------------------------
def test_daubechies8_is_an_orthonormal_pair() -> None:
    low, high = adaptive.daubechies8()
    assert low.size == high.size == 8
    assert float(low.sum()) == pytest.approx(math.sqrt(2), abs=1e-12)
    assert float((low**2).sum()) == pytest.approx(1.0, abs=1e-12)
    assert float((high**2).sum()) == pytest.approx(1.0, abs=1e-12)
    assert float(high.sum()) == pytest.approx(0.0, abs=1e-12)
    assert float((low * high).sum()) == pytest.approx(0.0, abs=1e-12)


def test_highpass_is_the_quadrature_mirror_of_the_lowpass() -> None:
    low, high = adaptive.daubechies8()
    expected = [(-1.0) ** index * low[7 - index] for index in range(8)]
    assert np.allclose(high, expected)


def test_returned_filters_cannot_corrupt_the_module() -> None:
    low, _high = adaptive.daubechies8()
    low[:] = 0.0
    again, _ = adaptive.daubechies8()
    assert float(again.sum()) == pytest.approx(math.sqrt(2), abs=1e-12)


# ---------------------------------------------------------------------------
# detail energy
# ---------------------------------------------------------------------------
def test_detail_energy_of_a_constant_is_zero() -> None:
    plane = np.full((64, 64), 137.0)
    assert float(np.abs(adaptive.detail_energy(plane)).max()) < 1e-9


def test_detail_energy_of_a_linear_ramp_stays_small() -> None:
    """Four vanishing moments: a straight gradient barely registers next to noise."""
    y, x = np.mgrid[0:64, 0:64]
    ramp = 50.0 + 0.5 * x + 0.25 * y
    noise = ramp + np.random.default_rng(11).normal(0, 30.0, ramp.shape)
    ramp_energy = float(adaptive.detail_energy(ramp).max())
    noise_energy = float(adaptive.detail_energy(noise).mean())
    assert ramp_energy < 0.01 * noise_energy, f"{ramp_energy:.4f} vs {noise_energy:.1f}"


def test_detail_energy_grows_with_texture() -> None:
    smooth = float(adaptive.detail_energy(_smooth()).mean())
    busy = float(adaptive.detail_energy(_busy()).mean())
    assert busy > 10 * smooth


def test_detail_energy_ignores_a_constant_offset() -> None:
    plane = _busy()
    assert np.allclose(adaptive.detail_energy(plane + 40.0), adaptive.detail_energy(plane))


def test_detail_energy_keeps_the_shape() -> None:
    plane = _smooth((37, 53))
    assert adaptive.detail_energy(plane).shape == plane.shape


# ---------------------------------------------------------------------------
# cost map
# ---------------------------------------------------------------------------
def test_cost_map_is_normalised_to_a_median_of_one() -> None:
    cost = adaptive.cost_map(_busy())
    assert float(np.median(cost)) == pytest.approx(1.0, rel=1e-9)
    assert bool(np.isfinite(cost).all())


def test_cost_map_inverts_the_energy() -> None:
    cost = adaptive.cost_map(_two_regions()[:, :, 0].astype(np.float64))
    assert float(np.median(cost[:, :128])) > float(np.median(cost[:, 128:]))


def test_cost_map_of_a_flat_image_is_uniform_and_finite() -> None:
    cost = adaptive.cost_map(np.full((32, 32), 90.0))
    assert bool(np.isfinite(cost).all())
    assert np.allclose(cost, cost.flat[0])


def test_cost_map_spreads_over_a_neighbourhood() -> None:
    """Without the 15×15 blur the ordering would be decided by pixel noise."""
    cost = adaptive.cost_map(_busy())
    right = cost[:, 1:]
    left = cost[:, :-1]
    assert float(np.corrcoef(left.ravel(), right.ravel())[0, 1]) > 0.95


def test_smooth_regions_cost_far_more_than_texture() -> None:
    cost = adaptive.cost_map(_two_regions()[:, :, 0].astype(np.float64))
    sky = float(np.median(cost[:, :128]))
    foliage = float(np.median(cost[:, 128:]))
    assert sky > 5 * foliage, f"sky {sky:.2f} vs foliage {foliage:.2f}"


def test_rgb_cost_map_scores_each_channel_separately() -> None:
    pixels = _two_regions()
    costs = adaptive.rgb_cost_map(pixels)
    assert costs.shape == (256, 256, 3)
    for channel in range(3):
        assert float(np.median(costs[:, :, channel])) == pytest.approx(1.0, rel=1e-9)
        assert float(np.median(costs[:, :128, channel])) > 5 * float(
            np.median(costs[:, 128:, channel])
        )


def test_rgb_cost_map_rejects_the_wrong_shape() -> None:
    with pytest.raises(ValueError):
        adaptive.rgb_cost_map(np.zeros((10, 10), dtype=np.uint8))
    with pytest.raises(ValueError):
        adaptive.rgb_cost_map(np.zeros((10, 10, 2), dtype=np.uint8))


# ---------------------------------------------------------------------------
# the mask invariant
# ---------------------------------------------------------------------------
def test_masked_pixels_drops_exactly_the_low_bits() -> None:
    pixels = np.array([[[0, 15, 255], [16, 31, 32]]], dtype=np.uint8)
    coarse = adaptive.masked_pixels(pixels)
    assert coarse.tolist() == [[[0, 0, 15], [1, 1, 2]]]


def _space(values: np.ndarray) -> ArraySamples:
    flat = np.array(values, dtype=np.uint8)
    return ArraySamples(flat, min_value=0, max_value=255)


def test_write_masked_sets_every_bit_it_was_given() -> None:
    rng = np.random.default_rng(1)
    values = rng.integers(0, 256, 512, dtype=np.uint8)
    space = _space(values)
    slots = np.arange(values.size)
    bits = rng.integers(0, 2, values.size).astype(np.uint8)
    direction = rng.random(values.size) < 0.5

    adaptive.write_masked(space, slots, bits, direction)

    written = space.gather(slots)
    assert np.array_equal(written & 1, bits)
    assert int(np.abs(written.astype(np.int16) - values.astype(np.int16)).max()) <= 1


def test_write_masked_never_leaves_the_high_bit_group() -> None:
    """The property the extractor depends on: ``value >> 4`` does not move."""
    rng = np.random.default_rng(2)
    values = rng.integers(0, 256, 4096, dtype=np.uint8)
    space = _space(values)
    slots = np.arange(values.size)
    bits = rng.integers(0, 2, values.size).astype(np.uint8)

    adaptive.write_masked(space, slots, bits, rng.random(values.size) < 0.5)

    written = space.gather(slots)
    assert np.array_equal(adaptive.mask_group(written), adaptive.mask_group(values))


def test_write_masked_only_touches_samples_that_must_change() -> None:
    values = np.array([10, 11, 12, 13], dtype=np.uint8)
    space = _space(values)
    bits = np.array([0, 1, 0, 1], dtype=np.uint8)  # already correct everywhere
    adaptive.write_masked(space, np.arange(4), bits, np.array([True] * 4))
    assert np.array_equal(space.gather(np.arange(4)), values)


def test_write_masked_forces_the_only_safe_direction_at_the_group_edges() -> None:
    values = np.array([16, 31], dtype=np.uint8)  # low nibble 0 and 15
    space = _space(values)
    bits = np.array([1, 0], dtype=np.uint8)  # 16 is even, 31 is odd: both must flip
    adaptive.write_masked(space, np.arange(2), bits, np.array([False, True]))

    written = space.gather(np.arange(2))
    assert written.tolist() == [17, 30], "low nibble 0000 may only go up, 1111 only down"
    assert np.array_equal(adaptive.mask_group(written), adaptive.mask_group(values))


def test_write_masked_follows_the_direction_stream_where_it_is_free() -> None:
    values = np.array([20, 20], dtype=np.uint8)  # low nibble 4: either way is safe
    space = _space(values)
    bits = np.array([1, 1], dtype=np.uint8)
    adaptive.write_masked(space, np.arange(2), bits, np.array([True, False]))
    assert space.gather(np.arange(2)).tolist() == [21, 19]


def test_write_masked_handles_the_value_edges() -> None:
    values = np.array([0, 255], dtype=np.uint8)
    space = _space(values)
    adaptive.write_masked(space, np.arange(2), np.array([1, 0], np.uint8), np.array([True, True]))
    written = space.gather(np.arange(2))
    assert written.tolist() == [1, 254]
    assert np.array_equal(adaptive.mask_group(written), adaptive.mask_group(values))


def test_write_masked_rejects_mismatched_arguments() -> None:
    space = _space(np.zeros(4, dtype=np.uint8))
    with pytest.raises(ValueError):
        adaptive.write_masked(space, np.arange(4), np.zeros(3, np.uint8), np.zeros(4, bool))
    with pytest.raises(ValueError):
        adaptive.write_masked(space, np.arange(4), np.zeros(4, np.uint8), np.zeros(3, bool))


@settings(max_examples=25, deadline=None)
@given(
    values=st.lists(st.integers(min_value=0, max_value=255), min_size=1, max_size=64),
    bits=st.lists(st.integers(min_value=0, max_value=1), min_size=1, max_size=64),
)
def test_masked_writes_are_always_reproducible(values: list[int], bits: list[int]) -> None:
    size = min(len(values), len(bits))
    values_array = np.array(values[:size], dtype=np.uint8)
    bits_array = np.array(bits[:size], dtype=np.uint8)
    space = _space(values_array)
    adaptive.write_masked(space, np.arange(size), bits_array, np.zeros(size, dtype=bool))
    written = space.gather(np.arange(size))
    assert np.array_equal(written & 1, bits_array)
    assert np.array_equal(adaptive.mask_group(written), adaptive.mask_group(values_array))


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------
def test_slot_ranks_are_a_permutation_and_keyed() -> None:
    first = adaptive.slot_ranks(1000, b"\x01" * 32)
    second = adaptive.slot_ranks(1000, b"\x02" * 32)
    assert sorted(first.tolist()) == list(range(1000))
    assert not np.array_equal(first, second)
    assert np.array_equal(first, adaptive.slot_ranks(1000, b"\x01" * 32))


def test_bucket_of_is_monotone_and_log_spaced() -> None:
    costs = np.array([1.0, 2.0, 4.0, 8.0, 16.0, 32.0])
    buckets = adaptive.bucket_of(costs, 3)
    assert list(buckets) == sorted(buckets)
    assert buckets[0] == 0 and buckets[-1] == 2


def test_bucket_of_flattens_a_uniform_map() -> None:
    assert set(adaptive.bucket_of(np.full(10, 3.5)).tolist()) == {0}


def test_select_positions_returns_unique_candidates_in_bit_order() -> None:
    rng = np.random.default_rng(4)
    costs = rng.random(520)
    candidates = np.arange(10, 510)
    selection = adaptive.select_positions(costs, candidates, 40, b"k" * 32)

    assert len(selection) == 40
    assert np.unique(selection.slots).size == 40
    assert set(selection.slots.tolist()) <= set(candidates.tolist())


def test_select_positions_prefers_the_cheap_bucket() -> None:
    costs = np.ones(1000)
    costs[:100] = 0.01
    selection = adaptive.select_positions(costs, np.arange(1000), 50, b"k" * 32)
    assert set(selection.slots.tolist()) <= set(range(100)), "the cheap slot wins"


def test_select_positions_is_deterministic_and_key_dependent() -> None:
    costs = np.ones(1000)  # one bucket, so only the key decides
    first = adaptive.select_positions(costs, np.arange(1000), 100, b"\x01" * 32)
    again = adaptive.select_positions(costs, np.arange(1000), 100, b"\x01" * 32)
    other = adaptive.select_positions(costs, np.arange(1000), 100, b"\x02" * 32)
    assert np.array_equal(first.slots, again.slots)
    assert not np.array_equal(first.slots, other.slots)


def test_select_positions_rejects_impossible_requests() -> None:
    costs = np.ones(10)
    with pytest.raises(ValueError):
        adaptive.select_positions(costs, np.arange(10), 11, b"k" * 32)
    with pytest.raises(ValueError):
        adaptive.select_positions(costs, np.arange(10), -1, b"k" * 32)


def test_select_positions_returns_nothing_for_an_empty_request() -> None:
    selection = adaptive.select_positions(np.ones(10), np.arange(10), 0, b"k" * 32)
    assert len(selection) == 0


def test_selection_lands_on_texture_not_on_the_sky() -> None:
    pixels = _two_regions()
    costs = adaptive.layout_costs(pixels).reshape(-1)
    selection = adaptive.select_positions(costs, np.arange(costs.size), 6000, b"k" * 32)
    columns = (selection.slots // 3) % 256
    sky_share = float(np.mean(columns < 128))
    assert sky_share < 0.05, f"{sky_share:.1%} of the payload went into the flat half"


def test_layout_costs_survive_a_masked_write() -> None:
    """The whole v4 layout rests on this: embedding must not move the costs."""
    pixels = _two_regions()
    before = adaptive.layout_costs(pixels)

    flat = pixels.reshape(-1).copy()
    space = ArraySamples(flat, min_value=0, max_value=255)
    rng = np.random.default_rng(5)
    slots = rng.choice(flat.size, size=5000, replace=False)
    bits = rng.integers(0, 2, slots.size).astype(np.uint8)
    adaptive.write_masked(space, slots, bits, rng.random(slots.size) < 0.5)

    after = adaptive.layout_costs(space.gather(np.arange(flat.size)).reshape(pixels.shape))
    assert np.array_equal(before, after)


# ---------------------------------------------------------------------------
# direction stream and stability check
# ---------------------------------------------------------------------------
def test_direction_stream_is_keyed_deterministic_and_balanced() -> None:
    first = adaptive.direction_stream(20000, b"k" * 32, counter=3)
    assert np.array_equal(first, adaptive.direction_stream(20000, b"k" * 32, counter=3))
    assert not np.array_equal(first, adaptive.direction_stream(20000, b"k" * 32, counter=4))
    assert not np.array_equal(first, adaptive.direction_stream(20000, b"o" * 32, counter=3))
    share = float(np.mean(first))
    assert 0.45 < share < 0.55


def test_direction_stream_is_empty_for_an_empty_request() -> None:
    assert adaptive.direction_stream(0, b"k" * 32).size == 0


def test_is_stabler_compares_layouts_element_by_element() -> None:
    assert adaptive.is_stabler(np.array([1, 2, 3]), np.array([1, 2, 3]))
    assert not adaptive.is_stabler(np.array([1, 2, 3]), np.array([1, 3, 2]))
    assert not adaptive.is_stabler(np.array([1, 2]), np.array([1, 2, 3]))
