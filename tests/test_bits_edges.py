"""Bit plumbing, permutations, sample adapters and the text shortcut."""

from __future__ import annotations

import numpy as np
import pytest

from phantom.core import constants as C
from phantom.core.errors import CapacityError, WrongPasswordOrNoData
from phantom.stego import lsb, permute
from phantom.stego.engine import (
    Capacity,
    capacity_for,
    embed_payload,
    embed_text,
    extract_payload,
    header_positions,
)
from phantom.stego.samples import ArraySamples, ImageSamples, SampleSpace


# ---------------------------------------------------------------------------
# lsb
# ---------------------------------------------------------------------------
def test_bytes_to_bits_roundtrip() -> None:
    data = bytes(range(37))
    bits = lsb.bytes_to_bits(data)
    assert bits.size == len(data) * 8
    assert lsb.bits_to_bytes(bits) == data
    assert lsb.bytes_to_bits(b"").size == 0
    assert lsb.bits_to_bytes(np.zeros(0, dtype=np.uint8)) == b""


def test_bits_to_bytes_rejects_partial_bytes() -> None:
    with pytest.raises(ValueError):
        lsb.bits_to_bytes(np.ones(7, dtype=np.uint8))


def test_random_sign_bits_are_bits() -> None:
    assert lsb.random_sign_bits(0).size == 0
    signs = lsb.random_sign_bits(1000)
    assert signs.dtype == bool
    assert signs.size == 1000
    assert 300 < int(signs.sum()) < 700, "coin flips must not be biased"
    assert lsb.random_sign_bits(9).size == 9


def test_lsb_matching_at_the_edges() -> None:
    current = np.array([0, 255, 1, 254], dtype=np.int64)
    wanted = np.array([1, 0, 0, 1], dtype=np.uint8)
    updated = lsb.lsb_match_values(current, wanted)
    assert ((updated & 1) == wanted).all()
    assert updated.min() >= 0 and updated.max() <= 255
    assert np.abs(updated - current).max() <= 1


def test_lsb_matching_detects_impossible_ranges() -> None:
    with pytest.raises(ValueError):
        lsb.lsb_match_values(np.array([5]), np.array([0], dtype=np.uint8), min_value=5, max_value=5)


def test_lsb_matching_validates_shapes() -> None:
    with pytest.raises(ValueError):
        lsb.lsb_match_values(np.zeros(3, dtype=np.int64), np.zeros(2, dtype=np.uint8))


def test_lsb_matching_empty_input() -> None:
    result = lsb.lsb_match_values(np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.uint8))
    assert result.size == 0


def test_lsb_matching_uses_provided_signs() -> None:
    current = np.array([10, 20], dtype=np.int64)
    wanted = np.array([1, 1], dtype=np.uint8)
    signs = np.array([False, False])
    updated = lsb.lsb_match_values(current, wanted, sign_bits=signs)
    assert updated.tolist() == [9, 19]
    up = lsb.lsb_match_values(current, wanted, sign_bits=np.array([True, True]))
    assert up.tolist() == [11, 21]


def test_bit_writer_and_reader_agree() -> None:
    array = np.zeros(64, dtype=np.int16)
    space = ArraySamples(array)
    positions = np.arange(16)
    bits = np.array([1, 0, 1, 1] * 4, dtype=np.uint8)
    lsb.BitWriter(space).write(positions, bits)
    assert (lsb.BitReader(space).read(positions) == bits).all()


def test_bit_writer_rejects_mismatched_lengths() -> None:
    space = ArraySamples(np.zeros(16, dtype=np.int16))
    with pytest.raises(ValueError):
        lsb.BitWriter(space).write(np.arange(4), np.zeros(3, dtype=np.uint8))


# ---------------------------------------------------------------------------
# permutation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("domain", [1, 2, 3, 7, 100, 1000])
def test_permutation_is_a_bijection(domain: int) -> None:
    permutation = permute.FeistelPermutation(b"k" * 16, domain, rounds=4)
    mapped = permutation.apply(np.arange(domain))
    assert sorted(mapped.tolist()) == list(range(domain))


def test_permutation_validation() -> None:
    with pytest.raises(ValueError):
        permute.FeistelPermutation(b"k" * 16, 0)
    with pytest.raises(ValueError):
        permute.FeistelPermutation(b"k" * 16, 1 << 33)
    with pytest.raises(ValueError):
        permute.FeistelPermutation(b"short", 100)
    with pytest.raises(ValueError):
        permute.FeistelPermutation(b"k" * 16, 100, rounds=3)
    with pytest.raises(ValueError):
        permute.FeistelPermutation(b"k" * 16, 100, rounds=5)


def test_permutation_rejects_out_of_domain_indices() -> None:
    permutation = permute.FeistelPermutation(b"k" * 16, 10)
    with pytest.raises(ValueError):
        permutation.apply(np.array([10]))
    with pytest.raises(ValueError):
        permutation.apply(np.array([-1]))


def test_permutation_empty_and_tiny_domains() -> None:
    assert permute.FeistelPermutation(b"k" * 16, 10).apply(np.zeros(0, dtype=np.int64)).size == 0
    for domain in (1, 2):
        identity = permute.FeistelPermutation(b"k" * 16, domain)
        assert identity.apply(np.arange(domain)).tolist() == list(range(domain))


def test_permutation_properties_and_key_sensitivity() -> None:
    first = permute.FeistelPermutation(b"a" * 16, 512)
    second = permute.FeistelPermutation(b"b" * 16, 512)
    assert first.domain == 512
    assert "domain=512" in repr(first)
    assert not np.array_equal(first.apply(np.arange(512)), second.apply(np.arange(512)))


def test_public_and_keyed_permutation_factories() -> None:
    public = permute.public_permutation(256)
    assert sorted(public.apply(np.arange(256)).tolist()) == list(range(256))
    keyed = permute.keyed_permutation(b"k" * 32, 256)
    assert keyed.domain == 256
    assert not np.array_equal(public.apply(np.arange(256)), keyed.apply(np.arange(256)))
    bootstrap = permute.bootstrap_permutation(b"k" * 32, 256)
    assert sorted(bootstrap.apply(np.arange(256)).tolist()) == list(range(256))
    assert not np.array_equal(bootstrap.apply(np.arange(256)), public.apply(np.arange(256)))


def test_permutation_handles_large_domain() -> None:
    big = 1 << 20
    permutation = permute.FeistelPermutation(b"z" * 16, big)
    sample = np.array([0, 1, big - 1, big // 2], dtype=np.int64)
    assert (permutation.apply(sample) < big).all()
    assert (permutation.apply(sample) >= 0).all()


# ---------------------------------------------------------------------------
# sample adapters
# ---------------------------------------------------------------------------
def test_array_samples_validation() -> None:
    with pytest.raises(ValueError):
        ArraySamples(np.zeros((2, 2), dtype=np.int16))
    with pytest.raises(TypeError):
        ArraySamples(np.zeros(4, dtype=np.float32))  # type: ignore[arg-type]


def test_array_samples_range_and_scatter() -> None:
    array = np.zeros(8, dtype=np.int16)
    space = ArraySamples(array, min_value=-5, max_value=5)
    assert space.min_value == -5 and space.max_value == 5
    space.scatter(np.array([0, 1]), np.array([-5, 5]))
    assert array[:2].tolist() == [-5, 5]
    with pytest.raises(ValueError):
        space.scatter(np.array([0]), np.array([6]))
    assert isinstance(space, SampleSpace)


def test_image_samples_validation() -> None:
    with pytest.raises(TypeError):
        ImageSamples(np.zeros((2, 2, 3), dtype=np.int16))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ImageSamples(np.zeros((2, 2, 2), dtype=np.uint8))
    readonly = np.zeros((2, 2, 3), dtype=np.uint8)
    readonly.flags.writeable = False
    with pytest.raises(ValueError):
        ImageSamples(readonly)


def test_image_samples_linear_mapping() -> None:
    pixels = np.zeros((4, 4, 3), dtype=np.uint8)
    space = ImageSamples(pixels)
    assert space.size == 48
    assert space.transparent_pixels == 0
    assert space.gather_pixels(np.array([0, 3, 5])).tolist() == [0, 1, 1]
    space.scatter(np.arange(3), np.array([10, 11, 12]))
    assert pixels[0, 0].tolist() == [10, 11, 12]
    with pytest.raises(ValueError):
        space.scatter(np.array([0]), np.array([256]))


def test_image_samples_skip_transparent_pixels() -> None:
    pixels = np.zeros((2, 2, 4), dtype=np.uint8)
    pixels[..., 3] = 255
    pixels[0, 0, 3] = 0
    space = ImageSamples(pixels)
    assert space.transparent_pixels == 1
    assert space.size == 3 * 3
    space.scatter(np.arange(space.size), np.arange(space.size) % 200)
    assert pixels[0, 0, :3].tolist() == [0, 0, 0], "transparent pixel must stay untouched"
    assert space.gather(np.arange(space.size)).max() > 0


# ---------------------------------------------------------------------------
# engine: capacity and the text helper
# ---------------------------------------------------------------------------
def test_capacity_helpers() -> None:
    capacity = Capacity(usable_bits=84_000, payload_bytes=10_000)
    assert capacity.payload_area_bytes == (84_000 - C.HEADER_BITS) // 8
    assert capacity.max_ciphertext_bytes == 10_000 + C.TAG_SIZE
    assert capacity.fill_ratio(5_000) == pytest.approx(0.5)
    assert capacity.fill_percent(20_000) == pytest.approx(100.0)
    empty = Capacity(usable_bits=0, payload_bytes=0)
    assert empty.payload_area_bytes == 0
    assert empty.fill_ratio(0) == 0.0
    assert empty.fill_ratio(10) == 1.0


def test_capacity_for_reserves_the_tag() -> None:
    space = ArraySamples(np.zeros(10_000, dtype=np.int16))
    capacity = capacity_for(space)
    assert capacity.usable_bits == 10_000
    assert capacity.payload_bytes == (10_000 - C.HEADER_BITS) // 8 - C.TAG_SIZE


def test_engine_rejects_tiny_spaces() -> None:
    tiny = ArraySamples(np.zeros(C.HEADER_BITS - 1, dtype=np.int16))
    with pytest.raises(WrongPasswordOrNoData):
        header_positions(tiny, "password-123456")
    with pytest.raises(WrongPasswordOrNoData):
        extract_payload(tiny, "password-123456")


def test_embed_rejects_an_empty_blob() -> None:
    space = ArraySamples(np.zeros(5_000, dtype=np.int16))
    with pytest.raises(CapacityError):
        embed_payload(space, b"", "password-123456")


def test_embed_text_roundtrip() -> None:
    samples = np.full(8_000, 7, dtype=np.int16)
    space = ArraySamples(samples)
    report = embed_text(space, "engine text", "password-123456")
    assert report.payload_bytes > 0
    extracted = extract_payload(space, "password-123456")
    assert extracted.data == b"engine text"
    assert samples.min() >= -2**15 and samples.max() <= 2**15 - 1


def test_embed_text_reports_capacity_error() -> None:
    space = ArraySamples(np.zeros(1_000, dtype=np.int16))
    with pytest.raises(CapacityError):
        embed_text(space, "x" * 10_000, "password-123456")


def test_extract_with_wrong_password() -> None:
    space = ArraySamples(np.zeros(8_000, dtype=np.int16))
    embed_text(space, "secret", "right-password-1")
    with pytest.raises(WrongPasswordOrNoData):
        extract_payload(space, "wrong-password-1")


def test_embed_reports_progress_and_cancels() -> None:
    import threading

    from phantom.core.errors import OperationCancelled

    space = ArraySamples(np.zeros(9_000, dtype=np.int16))
    seen: list[tuple[str, int, int]] = []
    embed_payload(
        space,
        b"\x02" + b"payload" * 20,
        "password-123456",
        progress=lambda stage, done, total: seen.append((stage.value, done, total)),
    )
    assert seen, "progress must be reported"
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelled):
        embed_payload(space, b"\x02data", "password-123456", cancel=cancel)


def test_lsb_matching_clamps_degenerate_ranges() -> None:
    """A one-value range can only satisfy one bit value; the other must raise."""
    with pytest.raises(ValueError):
        lsb.lsb_match_values(
            np.array([5]), np.array([0], dtype=np.uint8), min_value=5, max_value=5
        )
    assert lsb.lsb_match_values(
        np.array([5]), np.array([1], dtype=np.uint8), min_value=5, max_value=5
    ).tolist() == [5]


def test_payload_positions_without_bits() -> None:
    from phantom.stego.engine import _payload_positions

    bootstrap = permute.bootstrap_permutation(b"k" * 32, 1_000)
    keyed = permute.keyed_permutation(b"k" * 32, 600)
    assert _payload_positions(bootstrap, keyed, 0, 0).size == 0
