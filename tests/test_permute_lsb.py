"""Keyed permutation and LSB Matching primitives (D3, D2)."""

from __future__ import annotations

import numpy as np
import pytest

from phantom.stego.lsb import (
    BitReader,
    BitWriter,
    bits_to_bytes,
    bytes_to_bits,
    lsb_match_values,
    random_sign_bits,
)
from phantom.stego.permute import FeistelPermutation, keyed_permutation, public_permutation
from phantom.stego.samples import ArraySamples

KEY = b"\x42" * 32


@pytest.mark.parametrize("domain", [1, 2, 3, 4, 5, 17, 100, 1_000, 4_096, 72_000])
def test_permutation_is_a_bijection(domain: int) -> None:
    permutation = FeistelPermutation(KEY, domain)
    out = permutation.apply(np.arange(domain))
    assert out.min() == 0
    assert out.max() == domain - 1
    assert len(set(out.tolist())) == domain


def test_permutation_is_deterministic_for_a_key() -> None:
    first = FeistelPermutation(KEY, 5_000).apply(np.arange(500))
    second = FeistelPermutation(KEY, 5_000).apply(np.arange(500))
    np.testing.assert_array_equal(first, second)


def test_permutation_differs_between_keys() -> None:
    first = FeistelPermutation(KEY, 5_000).apply(np.arange(500))
    second = FeistelPermutation(b"\x43" * 32, 5_000).apply(np.arange(500))
    assert not np.array_equal(first, second)


def test_permutation_spreads_indices_uniformly() -> None:
    domain = 100_000
    permutation = FeistelPermutation(KEY, domain)
    out = permutation.apply(np.arange(domain))
    bins = np.histogram(out, bins=10, range=(0, domain))[0]
    expected = domain / 10
    assert bins.min() > expected * 0.95
    assert bins.max() < expected * 1.05


def test_permutation_rejects_out_of_domain() -> None:
    permutation = FeistelPermutation(KEY, 100)
    with pytest.raises(ValueError):
        permutation.apply(np.array([0, 100]))
    with pytest.raises(ValueError):
        permutation.apply(np.array([-1]))


def test_permutation_rejects_huge_domain() -> None:
    with pytest.raises(ValueError):
        FeistelPermutation(KEY, 1 << 33)


def test_public_permutation_is_password_independent() -> None:
    first = public_permutation(1_000).apply(np.arange(100))
    second = public_permutation(1_000).apply(np.arange(100))
    np.testing.assert_array_equal(first, second)
    keyed = keyed_permutation(KEY, 1_000).apply(np.arange(100))
    assert not np.array_equal(first, keyed)


def test_lsb_match_always_produces_the_requested_bit() -> None:
    values = np.arange(256, dtype=np.int64)
    for bit in (0, 1):
        updated = lsb_match_values(values, np.full(256, bit, dtype=np.uint8))
        assert ((updated & 1) == bit).all()
        assert updated.min() >= 0
        assert updated.max() <= 255
        assert (np.abs(updated - values) <= 1).all()


def test_lsb_match_boundaries_move_inwards() -> None:
    values = np.array([0, 255], dtype=np.int64)
    updated = lsb_match_values(values, np.array([1, 0], dtype=np.uint8))
    assert updated.tolist() == [1, 254]


def test_lsb_match_sign_is_random_but_bounded() -> None:
    values = np.full(2_000, 128, dtype=np.int64)
    bits = np.ones(2_000, dtype=np.uint8)
    updated = lsb_match_values(values, bits, sign_bits=random_sign_bits(2_000))
    deltas = updated - values
    assert set(np.unique(deltas).tolist()) <= {-1, 1}
    assert (deltas == 1).any() and (deltas == -1).any()


def test_lsb_match_uses_a_csprng_not_numpy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_token_bytes(count: int) -> bytes:
        calls.append(count)
        return b"\xff" * count

    monkeypatch.setattr("phantom.stego.lsb.secrets.token_bytes", fake_token_bytes)
    values = np.full(8, 10, dtype=np.int64)
    updated = lsb_match_values(values, np.ones(8, dtype=np.uint8))
    assert calls, "secrets.token_bytes was not used for the ±1 sign"
    assert (updated - values == 1).all()


def test_lsb_match_rejects_shape_mismatch() -> None:
    with pytest.raises(ValueError):
        lsb_match_values(np.zeros(4, dtype=np.int64), np.zeros(3, dtype=np.uint8))


def test_lsb_match_rejects_impossible_range() -> None:
    with pytest.raises(ValueError):
        lsb_match_values(
            np.array([5], dtype=np.int64),
            np.array([0], dtype=np.uint8),
            min_value=5,
            max_value=5,
        )


def test_bit_conversion_roundtrip() -> None:
    data = bytes(range(256))
    assert bits_to_bytes(bytes_to_bits(data)) == data
    assert bytes_to_bits(b"").size == 0


def test_bit_conversion_rejects_partial_bytes() -> None:
    with pytest.raises(ValueError):
        bits_to_bytes(np.zeros(7, dtype=np.uint8))


def test_bit_writer_reader_roundtrip() -> None:
    data = np.arange(4_000, dtype=np.int32)
    space = ArraySamples(data, min_value=0, max_value=3_999)
    bits = bytes_to_bits(b"round trip through a linear carrier")
    positions = np.arange(bits.size)
    BitWriter(space).write(positions, bits)
    assert BitReader(space).read(positions).tolist() == bits.tolist()
    assert np.abs(data - np.arange(4_000)).max() <= 1


def test_lsb_matching_never_leaves_the_range_for_int8() -> None:
    values = np.array([-128, 127, -1, 0, 1], dtype=np.int64)
    updated = lsb_match_values(values, np.array([1, 0, 1, 1, 0], dtype=np.uint8), min_value=-128, max_value=127)
    assert updated.min() >= -128
    assert updated.max() <= 127
    assert ((updated & 1) == np.array([1, 0, 1, 1, 0])).all()
