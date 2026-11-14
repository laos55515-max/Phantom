"""Vectorized LSB Matching (±1) and bit packing (D2, D3).

LSB matching never produces a visible artifact larger than ±1 and never leaves
the valid range:

* a value that already carries the wanted bit is untouched;
* otherwise the value moves by ±1, sign drawn from a CSPRNG;
* at the boundaries it can only move inwards (0 → 1, 255 → 254), which still
  flips the LSB, so the embedded bit is always recoverable.
"""

from __future__ import annotations

import secrets
from typing import Final

import numpy as np

from phantom.stego.samples import SampleSpace

_BOUNDARY_ALLOWED: Final = (0, 255)


def bytes_to_bits(data: bytes) -> np.ndarray:
    """MSB-first bit expansion, shape (len(data) * 8,), dtype uint8."""
    if not data:
        return np.zeros(0, dtype=np.uint8)
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def bits_to_bytes(bits: np.ndarray) -> bytes:
    """Inverse of :func:`bytes_to_bits`; requires a whole number of bytes."""
    arr = np.asarray(bits, dtype=np.uint8)
    if arr.size % 8:
        raise ValueError("bit count is not a multiple of 8")
    if arr.size == 0:
        return b""
    return np.packbits(arr).tobytes()


def random_sign_bits(count: int) -> np.ndarray:
    """CSPRNG bits used to choose the ±1 direction (never ``numpy.random``)."""
    if count <= 0:
        return np.zeros(0, dtype=bool)
    raw = secrets.token_bytes((count + 7) // 8)
    return np.unpackbits(np.frombuffer(raw, dtype=np.uint8))[:count].astype(bool)


def lsb_match_values(
    current: np.ndarray,
    wanted_bits: np.ndarray,
    *,
    min_value: int = 0,
    max_value: int = 255,
    sign_bits: np.ndarray | None = None,
) -> np.ndarray:
    """Return new sample values with ``value & 1 == wanted_bit``."""
    values = np.asarray(current).astype(np.int64, copy=True)
    bits = np.asarray(wanted_bits, dtype=np.uint8)
    if values.shape != bits.shape:
        raise ValueError("value/bit shape mismatch")
    if values.size == 0:
        return values
    need = (values & 1) != bits
    signs = random_sign_bits(int(values.size)) if sign_bits is None else sign_bits
    delta = np.where(signs, 1, -1).astype(np.int64)
    candidate = values + np.where(need, delta, 0)
    below = need & (candidate < min_value)
    above = need & (candidate > max_value)
    if below.any():
        candidate[below] = values[below] + 1
    if above.any():
        candidate[above] = values[above] - 1
    # Guarantee correctness even for degenerate ranges (min==max).
    still_wrong = (candidate & 1) != bits
    if still_wrong.any():
        candidate[still_wrong] = np.clip(
            candidate[still_wrong], min_value, max_value
        )
        if ((candidate & 1) != bits).any():
            raise ValueError("carrier range cannot represent the requested bits")
    if candidate.min() < min_value or candidate.max() > max_value:
        raise ValueError("LSB matching left the valid range")
    return candidate


class BitWriter:
    """Writes bits into a carrier through a sample-space adapter."""

    def __init__(self, sample_space: SampleSpace) -> None:
        self._space = sample_space

    def write(self, positions: np.ndarray, bits: np.ndarray) -> None:
        if positions.size != bits.size:
            raise ValueError("position/bit count mismatch")
        values = self._space.gather(positions)
        updated = lsb_match_values(
            values,
            bits,
            min_value=self._space.min_value,
            max_value=self._space.max_value,
        )
        self._space.scatter(positions, updated)


class BitReader:
    """Reads LSBs from a carrier through a sample-space adapter."""

    def __init__(self, sample_space: SampleSpace) -> None:
        self._space = sample_space

    def read(self, positions: np.ndarray) -> np.ndarray:
        values = self._space.gather(positions)
        return (np.asarray(values) & 1).astype(np.uint8)

