"""Syndrome-trellis coding: the code is correct, optimal, and keyed.

Three things have to hold or the layout is worthless.  The code must *round-trip*
through any carrier (embed then read back gives the message), it must be *optimal*
for the additive distortion it is given (a brute-force search over a small block
must not find anything cheaper), and the trellis must be *reproducible from the
key alone*, because the extractor never sees the cover.
"""

from __future__ import annotations

import itertools

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from phantom.stego import stc

KEY = b"\x2a" * 32


def _code(width: int, height: int, key: bytes = KEY) -> tuple[stc.Trellis, np.ndarray]:
    matrix = stc.parity_matrix(height, width, key)
    return stc.Trellis(width=width, height=height, blocks=1), stc.columns_of(matrix)


def _brute_force(
    cover: np.ndarray, costs: np.ndarray, message: np.ndarray, width: int, height: int
) -> float:
    """Cheapest total cost of a change vector reaching ``message``, by exhaustion."""
    trellis, columns = _code(width, height)
    target = int(sum(int(bit) << index for index, bit in enumerate(message)))
    best = np.inf
    for combo in itertools.product([0, 1], repeat=width):
        changes = np.array(combo, dtype=np.uint8)
        if stc.syndromes(cover ^ changes, trellis, columns)[0] == target:
            best = min(best, float((costs * changes).sum()))
    return float(best)


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------
def test_plan_respects_capacity_and_budget() -> None:
    trellis = stc.plan(100_000, 20_000)
    assert trellis is not None
    assert trellis.capacity >= 20_000
    assert trellis.samples <= 100_000
    assert trellis.height <= stc.MAX_HEIGHT
    assert trellis.width == stc.WIDTH_LADDER[stc.WIDTH_LADDER.index(trellis.width)]


def test_plan_takes_the_widest_block_it_can_afford() -> None:
    """A wider block carries a bit with fewer changes, so it wins while it fits."""
    narrow = stc.plan(10_000, 5_000)
    wide = stc.plan(10_000, 500)
    assert narrow is not None and wide is not None
    assert wide.width > narrow.width, "a small payload can afford a long block"


def test_plan_is_impossible_for_more_bits_than_samples() -> None:
    assert stc.plan(1_000, 1_001) is None
    assert stc.plan(1_000, 0) is None
    assert stc.plan(0, 10) is None
    assert stc.plan(4, 10) is None, "narrower than the smallest block"


def test_plan_of_a_full_carrier_still_works() -> None:
    trellis = stc.plan(4_096, 4_096)
    assert trellis is not None
    assert trellis.capacity >= 4_096


def test_plan_is_deterministic() -> None:
    assert stc.plan(50_000, 7_000) == stc.plan(50_000, 7_000)


# ---------------------------------------------------------------------------
# the parity matrix
# ---------------------------------------------------------------------------
def test_matrix_is_binary_of_the_right_shape_and_full_rank() -> None:
    matrix = stc.parity_matrix(5, 12, KEY)
    assert matrix.shape == (5, 12)
    assert set(np.unique(matrix).tolist()) <= {0, 1}
    assert stc._gf2_rank(matrix) == 5
    assert not np.any(matrix.sum(axis=0) == 0), "a zero column would be unreachable"


def test_matrix_is_keyed_and_stable() -> None:
    first = stc.parity_matrix(6, 10, KEY)
    assert np.array_equal(first, stc.parity_matrix(6, 10, KEY))
    assert not np.array_equal(first, stc.parity_matrix(6, 10, b"\x2b" * 32))


def test_matrix_prefers_distinct_columns_when_it_can() -> None:
    columns = stc.columns_of(stc.parity_matrix(8, 40, KEY))
    assert np.unique(columns).size == 40, "no duplicates while the space allows it"


def test_matrix_rejects_an_impossible_shape() -> None:
    with pytest.raises(ValueError):
        stc.parity_matrix(0, 8, KEY)
    with pytest.raises(ValueError):
        stc.parity_matrix(9, 8, KEY)


def test_columns_of_packs_rows_into_bits() -> None:
    matrix = np.array([[1, 0, 1], [0, 1, 1], [1, 1, 0]], dtype=np.uint8)
    assert stc.columns_of(matrix).tolist() == [1 + 4, 2 + 4, 1 + 2]


# ---------------------------------------------------------------------------
# the syndrome, which is the whole contract between the two sides
# ---------------------------------------------------------------------------
def test_syndrome_is_linear_in_the_bits() -> None:
    trellis, columns = _code(10, 4)
    rng = np.random.default_rng(1)
    first = rng.integers(0, 2, 10).astype(np.uint8)
    second = rng.integers(0, 2, 10).astype(np.uint8)
    combined = first ^ second
    left = stc.syndromes(first, trellis, columns) ^ stc.syndromes(second, trellis, columns)
    assert int(left[0]) == int(stc.syndromes(combined, trellis, columns)[0])


def test_bits_of_inverts_the_block_packing() -> None:
    trellis = stc.Trellis(width=6, height=3, blocks=4)
    rng = np.random.default_rng(2)
    values = rng.integers(0, 1 << 3, trellis.blocks)
    bits = stc.bits_of(values, trellis)
    assert bits.size == trellis.capacity
    assert stc._pack_bits(bits, trellis).tolist() == values.tolist()


# ---------------------------------------------------------------------------
# embedding and reading back
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("samples", "bits"), [(2_048, 128), (2_048, 1_024), (4_096, 205), (600, 30)]
)
def test_roundtrip_recovers_the_message(samples: int, bits: int) -> None:
    trellis = stc.plan(samples, bits)
    assert trellis is not None
    columns = stc.columns_of(stc.parity_matrix(trellis.height, trellis.width, KEY))
    rng = np.random.default_rng(3)
    cover = rng.integers(0, 2, samples).astype(np.uint8)
    costs = rng.random(samples) + 0.01
    message = rng.integers(0, 2, bits).astype(np.uint8)

    changes = stc.encode(cover, costs, message, trellis=trellis, columns=columns)
    stego = cover.copy()
    stego[: trellis.samples] ^= changes.astype(np.uint8)

    recovered = stc.decode(stego, trellis=trellis, columns=columns)
    assert np.array_equal(recovered[:bits], message)


def test_encoding_leaves_the_carrier_tail_alone() -> None:
    trellis = stc.plan(1_001, 100)
    assert trellis is not None
    columns = stc.columns_of(stc.parity_matrix(trellis.height, trellis.width, KEY))
    rng = np.random.default_rng(4)
    cover = rng.integers(0, 2, 1_001).astype(np.uint8)
    changes = stc.encode(
        cover,
        np.ones(1_001),
        rng.integers(0, 2, 100).astype(np.uint8),
        trellis=trellis,
        columns=columns,
    )
    assert changes.size == trellis.samples
    assert changes.size < 1_001


def test_blocks_past_the_message_are_not_touched() -> None:
    """The extra syndrome bits are set to what the cover already has, so cost 0."""
    trellis = stc.Trellis(width=6, height=2, blocks=4)  # capacity 8, message 3
    columns = stc.columns_of(stc.parity_matrix(2, 6, KEY))
    rng = np.random.default_rng(5)
    cover = rng.integers(0, 2, trellis.samples).astype(np.uint8)
    message = rng.integers(0, 2, 3).astype(np.uint8)

    changes = stc.encode(cover, np.ones(trellis.samples), message, trellis=trellis, columns=columns)

    assert changes.reshape(4, 6)[1:].sum() == 0, "blocks 1..3 carry nothing"


def test_encode_refuses_a_message_the_trellis_cannot_hold() -> None:
    trellis = stc.Trellis(width=6, height=2, blocks=1)
    columns = stc.columns_of(stc.parity_matrix(2, 6, KEY))
    with pytest.raises(ValueError):
        stc.encode(
            np.zeros(6, dtype=np.uint8),
            np.ones(6),
            np.zeros(3, dtype=np.uint8),
            trellis=trellis,
            columns=columns,
        )


def test_a_different_key_moves_the_changes() -> None:
    _trellis, columns = _code(24, 4, KEY)
    other = stc.columns_of(stc.parity_matrix(4, 24, b"\x2c" * 32))
    trellis_wide = stc.Trellis(width=24, height=4, blocks=1)
    rng = np.random.default_rng(6)
    cover = rng.integers(0, 2, 24).astype(np.uint8)
    costs = rng.random(24) + 0.01
    message = rng.integers(0, 2, 4).astype(np.uint8)

    first = stc.encode(cover, costs, message, trellis=trellis_wide, columns=columns)
    second = stc.encode(cover, costs, message, trellis=trellis_wide, columns=other)
    assert not np.array_equal(first, second)
    assert (
        stc.decode(cover ^ second.astype(np.uint8), trellis=trellis_wide, columns=other)[
            :4
        ].tolist()
        == message.tolist()
    )  # width == samples here, so no tail to skip


# ---------------------------------------------------------------------------
# optimality — the reason the code exists
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("trial", range(6))
def test_uniform_costs_give_the_minimum_number_of_changes(trial: int) -> None:
    width, height = 12, 4
    trellis, columns = _code(width, height)
    rng = np.random.default_rng(100 + trial)
    cover = rng.integers(0, 2, width).astype(np.uint8)
    message = rng.integers(0, 2, height).astype(np.uint8)
    costs = np.ones(width)

    changes = stc.encode(cover, costs, message, trellis=trellis, columns=columns)

    assert float(changes.sum()) == pytest.approx(_brute_force(cover, costs, message, width, height))


@pytest.mark.parametrize("trial", range(6))
def test_skewed_costs_give_the_cheapest_combination_not_the_shortest(trial: int) -> None:
    """Two cheap flips may beat one expensive one, and the trellis must find that."""
    width, height = 10, 3
    trellis, columns = _code(width, height)
    rng = np.random.default_rng(200 + trial)
    cover = rng.integers(0, 2, width).astype(np.uint8)
    message = rng.integers(0, 2, height).astype(np.uint8)
    costs = np.exp(rng.normal(0, 2.0, width))  # a few very expensive samples

    changes = stc.encode(cover, costs, message, trellis=trellis, columns=columns)

    assert float((costs * changes).sum()) == pytest.approx(
        _brute_force(cover, costs, message, width, height)
    )


def test_the_code_beats_the_greedy_choice_of_one_bit_per_sample() -> None:
    """Same payload, both correct: the trellis reaches it with a lower total cost."""
    rng = np.random.default_rng(7)
    samples, bits = 4_096, 1_024
    trellis = stc.plan(samples, bits)
    assert trellis is not None
    columns = stc.columns_of(stc.parity_matrix(trellis.height, trellis.width, KEY))
    cover = rng.integers(0, 2, samples).astype(np.uint8)
    costs = np.exp(rng.normal(0, 1.5, samples))
    message = rng.integers(0, 2, bits).astype(np.uint8)

    changes = stc.encode(cover, costs, message, trellis=trellis, columns=columns)
    trellis_cost = float((costs[: trellis.samples] * changes).sum())

    cheapest = np.sort(costs)[:bits]
    forced = np.where(cover[:bits] == message, 0.0, 1.0)  # one bit per sample, greedy
    greedy_cost = float((cheapest * forced).sum()) if forced.sum() else 0.0

    assert trellis_cost < greedy_cost, f"{trellis_cost:.1f} vs greedy {greedy_cost:.1f}"


# ---------------------------------------------------------------------------
# property: any small request embeds and reads back
# ---------------------------------------------------------------------------
@settings(max_examples=25, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=10_000),
    samples=st.integers(min_value=60, max_value=400),
    share=st.floats(min_value=0.05, max_value=0.6),
)
def test_small_requests_always_round_trip(seed: int, samples: int, share: float) -> None:
    bits = max(1, int(samples * share))
    trellis = stc.plan(samples, bits)
    assert trellis is not None
    columns = stc.columns_of(stc.parity_matrix(trellis.height, trellis.width, KEY))
    rng = np.random.default_rng(seed)
    cover = rng.integers(0, 2, samples).astype(np.uint8)
    costs = rng.random(samples) + 0.001
    message = rng.integers(0, 2, bits).astype(np.uint8)

    changes = stc.encode(cover, costs, message, trellis=trellis, columns=columns)
    stego = cover.copy()
    stego[: trellis.samples] ^= changes.astype(np.uint8)

    assert np.array_equal(stc.decode(stego, trellis=trellis, columns=columns)[:bits], message)
