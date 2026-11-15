"""Keyed pseudo-random permutations over ``[0, domain)`` — no O(N) tables.

A 4-round Feistel network (Luby-Rackoff) with AES-128 as the round PRF, plus
cycle-walking to map the power-of-two Feistel domain onto ``[0, domain)``.

Properties used by the engine:

* the output is a *bijection* of the input, so no two bits ever share a carrier
  slot and the header region can never collide with the payload region;
* memory is O(number of requested indices), not O(domain) — a 24 MP image needs
  no 72-million-entry position table;
* the round function is AES, so positions are unpredictable without the key and
  uniformly spread over the whole carrier (D3).

Two entry points matter to the engine: :func:`bootstrap_permutation`, keyed by
the password, places **every** bit of a v3 carrier (header first, payload after
it), and :func:`keyed_permutation`, keyed by the per-message position key,
scatters the payload ranks inside the non-header part.  :func:`public_permutation`
is the constant-seeded permutation of the v2 layout and exists only so files
written before v3 can still be read.
"""

from __future__ import annotations

import hashlib
from typing import Final

import numpy as np
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from phantom.core import constants as C

_MASK32: Final = 0xFFFF_FFFF


class FeistelPermutation:
    """Deterministic keyed permutation of ``range(domain)``."""

    __slots__ = (
        "_bits",
        "_domain",
        "_key",
        "_left_bits",
        "_right_bits",
        "_rounds",
    )

    def __init__(self, key: bytes, domain: int, *, rounds: int = C.FEISTEL_ROUNDS) -> None:
        if domain < 1:
            raise ValueError("domain must be >= 1")
        if domain.bit_length() > 32:
            raise ValueError("domain too large (max 2**32)")
        if len(key) < 16:
            raise ValueError("need at least 16 key bytes")
        if rounds < 3 or rounds % 2:
            # even round count keeps the half widths in their original places
            raise ValueError("rounds must be an even number >= 4")
        self._domain = domain
        self._bits = max(2, (domain - 1).bit_length())
        self._left_bits = self._bits // 2
        self._right_bits = self._bits - self._left_bits
        self._rounds = rounds
        self._key = bytes(key[:16])

    # -- public API --------------------------------------------------------
    @property
    def domain(self) -> int:
        return self._domain

    def __repr__(self) -> str:
        return f"FeistelPermutation(domain={self._domain}, rounds={self._rounds})"

    def apply(self, indices: np.ndarray) -> np.ndarray:
        """Permute an array of indices; the result is in ``[0, domain)``."""
        values = np.asarray(indices, dtype=np.int64)
        if values.size == 0:
            return values.copy()
        if values.min() < 0 or values.max() >= self._domain:
            raise ValueError("index out of domain")
        if self._domain <= 2:
            return values.copy()
        return self._cycle_walk(values)

    # -- internals ---------------------------------------------------------
    def _cycle_walk(self, values: np.ndarray) -> np.ndarray:
        result = np.empty(values.size, dtype=np.int64)
        pending = np.arange(values.size)
        current = values
        while pending.size:
            permuted = self._permute(current)
            inside = permuted < self._domain
            result[pending[inside]] = permuted[inside]
            pending = pending[~inside]
            current = permuted[~inside]
        return result

    def _permute(self, values: np.ndarray) -> np.ndarray:
        left_bits = self._left_bits
        right_bits = self._right_bits
        left = values >> right_bits
        right = values & ((1 << right_bits) - 1)
        for round_index in range(self._rounds):
            f_out = self._round_function(right, round_index) & ((1 << left_bits) - 1)
            left, right = right, (left ^ f_out) & ((1 << left_bits) - 1)
            left_bits, right_bits = right_bits, left_bits
        return (left << right_bits) | right

    def _round_function(self, values: np.ndarray, round_index: int) -> np.ndarray:
        count = values.size
        blocks = np.zeros((count, 16), dtype=np.uint8)
        blocks[:, 0:4] = (
            np.asarray(values, dtype=np.uint32).astype(np.uint32, copy=False).view(np.uint8)
        ).reshape(count, 4)
        blocks[:, 4] = round_index & 0xFF
        blocks[:, 5] = 0x50
        # ECB is used as a keyed pseudorandom function inside the Feistel round,
        # never as a confidentiality mode: the output is a permutation index.
        encryptor = Cipher(algorithms.AES(self._key), modes.ECB()).encryptor()  # noqa: S305
        ciphertext = encryptor.update(blocks.tobytes()) + encryptor.finalize()
        words = np.frombuffer(ciphertext, dtype=np.uint32).reshape(count, 4)[:, 0]
        return np.ascontiguousarray(words, dtype=np.int64)


def bootstrap_permutation(position_key: bytes, usable_bits: int) -> FeistelPermutation:
    """Permutation that decides where *every* bit of a v3 carrier lives.

    Header bits are its first ``HEADER_BITS`` ranks, payload bits are the ranks
    after them, so the two regions cannot overlap and nothing about the layout
    can be computed without the password-derived ``position_key``.
    """
    if len(position_key) < 16:
        raise ValueError("need at least 16 key bytes")
    return FeistelPermutation(position_key, usable_bits)


def keyed_permutation(position_key: bytes, domain: int) -> FeistelPermutation:
    """Permutation of the payload ranks inside the non-header part."""
    return FeistelPermutation(position_key, domain)


def public_permutation(usable_bits: int) -> FeistelPermutation:
    """v2 layout only: constant-seeded, password-independent positions.

    Files written by PHANTOM 2.0.0 placed their header at
    ``public_permutation(N)[:HEADER_BITS]``; keeping the factory lets those
    files stay readable.  Nothing written by this build uses it.
    """
    key = hashlib.sha256(C.PUBLIC_POSITION_SEED).digest()
    return FeistelPermutation(key, usable_bits)
