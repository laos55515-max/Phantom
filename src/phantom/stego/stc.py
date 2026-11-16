"""Syndrome-trellis coding: choose *which* samples to change by minimizing cost.

Why a code instead of a sorted list
-----------------------------------
An adaptive embedder knows how much a ±1 change costs at every sample.  Picking
the cheapest samples and writing one message bit into each is a *greedy* answer:
it never asks whether one change could satisfy several parity constraints at once.
A syndrome-trellis code (Filler, Judas & Fridrich, 2011) does, and it is optimal
for an additive distortion: it looks for the change vector ``e ∈ {0,1}^n`` that
minimises ``Σ ρ_i e_i`` subject to ``H e = σ``, where ``σ`` is the syndrome that
carries the message.

The rate is what makes it pay.  One bit per sample needs about half the samples
changed; a block of ``w`` samples carrying ``h`` syndrome bits needs about
``h / log2(w)`` changes — for ``w = 160``, ``h = 8`` that is roughly six times
fewer ±1 changes for the same payload, and fewer changes is what every detector
in the benchmark measures.

The trellis and why it is block-wise
------------------------------------
``Ĥ`` is a ``height × width`` binary matrix; the full parity-check matrix ``H``
places copies of it on the diagonal, ``height`` rows and ``width`` columns per
block, so the syndrome of one block is ``Ĥ · x_block`` over GF(2) and blocks do
not interact.  Inside a block the problem is a shortest path through ``width``
layers of ``2**height`` partial-syndrome states, which is exactly what the
Viterbi pass below computes — the "syndrome trellis" of the literature, whose
states hold the partial syndrome of the block.

The published trellis slides by one sample instead of by one block, which couples
neighbouring blocks and buys a little more efficiency near block edges.  That
version is sequential over the whole carrier and, in pure Python, hundreds of
times slower; here every block is an independent trellis, so all blocks advance
in lockstep inside one NumPy operation.  The cost of that choice is the samples
at block boundaries, which cannot help their neighbours — with blocks of tens of
samples it is a fraction of a percent of the distortion, and the benchmark shows
the resulting change count is far below the greedy layout's.

What it does not hide
---------------------
The change vector still has to satisfy public-length constraints, so the *number*
of changes is not secret; only their placement is keyed, because ``Ĥ`` itself is
derived from the password.  Without it the syndromes cannot even be computed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

import numpy as np

#: Trellis states are ``2 ** height``.  Eight bits keeps the batched Viterbi at a
#: few hundred million element operations for a 512x512 frame, which is the point
#: where the distortion saving stops being worth the wait.
MAX_HEIGHT: Final = 8

#: Block widths to try, narrowest first.  A wider block carries its bits with
#: fewer changes (roughly ``1 / log2(width)`` per bit), but at a fixed payload it
#: needs a taller trellis, so the widest block that stays inside
#: :data:`MAX_HEIGHT` wins.
WIDTH_LADDER: Final = (6, 8, 10, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128, 160, 192, 256)

#: Peak memory for backpointers, which cost ``width * blocks * 2**height`` bytes.
MEMORY_BUDGET: Final = 24 << 20

#: Costs are used as weights only, but a zero would let a change be free and the
#: trellis would happily change every sample it can reach.
MIN_COST: Final = 1e-9

#: How many keyed matrices to try before giving up on finding a full-rank one.
MATRIX_ATTEMPTS: Final = 64


@dataclass(frozen=True, slots=True)
class Trellis:
    """``blocks`` independent trellises of ``width`` samples each."""

    width: int
    height: int
    blocks: int

    @property
    def samples(self) -> int:
        """How many samples the layout uses; the carrier tail stays untouched."""
        return self.width * self.blocks

    @property
    def capacity(self) -> int:
        """Message bits the layout can carry."""
        return self.height * self.blocks

    @property
    def states(self) -> int:
        return 1 << self.height


def sliced(trellis: Trellis, start: int, stop: int) -> Trellis:
    """The same layout restricted to blocks ``[start, stop)``.

    Blocks are independent, so a caller that must not hold the whole body in
    memory can walk the trellis a chunk of blocks at a time and get exactly the
    answer the full trellis would have given for those blocks.
    """
    return Trellis(width=trellis.width, height=trellis.height, blocks=stop - start)


def plan(samples: int, bits: int) -> Trellis | None:
    """Widest block layout that carries ``bits`` over ``samples``, or ``None``.

    ``None`` means the request is impossible (more bits than samples) or would
    need a trellis taller than :data:`MAX_HEIGHT` even with the narrowest block.
    """
    if samples <= 0 or bits <= 0:
        return None
    chosen: Trellis | None = None
    for width in WIDTH_LADDER:
        blocks = samples // width
        if blocks <= 0:
            break
        height = -(-bits // blocks)  # ceil: every block must carry its share
        if height > MAX_HEIGHT:
            break
        if height > width:
            # A rate-1 block has no redundancy left; a wider one may still fit,
            # so skip this width instead of giving up on the plan.
            continue
        chosen = Trellis(width=width, height=height, blocks=blocks)
    return chosen


def parity_matrix(height: int, width: int, key: bytes) -> np.ndarray:
    """Keyed ``height × width`` binary matrix of full rank over GF(2).

    Columns are drawn without replacement while the width allows it, so single
    changes reach as many distinct syndromes as the trellis can represent; beyond
    that duplicates are unavoidable and only the rank matters.  The matrix is part
    of the key: without the password the syndromes cannot be computed at all.
    """
    if not 1 <= height <= width:
        raise ValueError("height must be between 1 and width")
    exclusive = (1 << height) - 1
    for attempt in range(MATRIX_ATTEMPTS):
        label = (
            b"|stc-matrix|"
            + height.to_bytes(1, "big")
            + width.to_bytes(2, "big")
            + attempt.to_bytes(1, "big")
        )
        seed = hashlib.blake2b(key + label, digest_size=32).digest()
        generator = np.random.default_rng(int.from_bytes(seed, "big"))
        if width <= exclusive:
            picks = generator.choice(np.arange(1, exclusive + 1), size=width, replace=False)
        else:
            picks = generator.integers(1, exclusive + 1, size=width)
        matrix = ((picks[None, :] >> np.arange(height)[:, None]) & 1).astype(np.uint8)
        if _gf2_rank(matrix) == height:
            return matrix
    raise ValueError("could not build a full-rank parity matrix")


def _gf2_rank(matrix: np.ndarray) -> int:
    """Rank over GF(2), one Python integer per row (width ≤ 256 bits)."""
    basis: list[int] = []
    for row in matrix:
        value = 0
        for bit in row:
            value = (value << 1) | int(bit)
        for kept in basis:
            value = min(value, value ^ kept)
        if value:
            basis.append(value)
            basis.sort(reverse=True)
    return len(basis)


def columns_of(matrix: np.ndarray) -> np.ndarray:
    """Each column of ``matrix`` packed into one integer, LSB = row 0."""
    height = matrix.shape[0]
    weights = (np.int64(1) << np.arange(height, dtype=np.int64)).reshape(height, 1)
    packed: np.ndarray = (matrix.astype(np.int64) * weights).sum(axis=0)
    return packed


def syndromes(bits: np.ndarray, trellis: Trellis, columns: np.ndarray) -> np.ndarray:
    """Syndrome of every block, as an integer of ``height`` bits."""
    block_bits = np.asarray(bits[: trellis.samples], dtype=np.int64).reshape(
        trellis.blocks, trellis.width
    )
    out = np.zeros(trellis.blocks, dtype=np.int64)
    for column in range(trellis.width):
        out ^= block_bits[:, column] * columns[column]
    return out


def bits_of(values: np.ndarray, trellis: Trellis) -> np.ndarray:
    """Unpack per-block syndromes into the message bit order (block-major)."""
    width = trellis.height
    bits = np.zeros((trellis.blocks, width), dtype=np.uint8)
    for row in range(width):
        bits[:, row] = (values >> row) & 1
    return bits.reshape(-1)


def encode(
    cover_bits: np.ndarray,
    costs: np.ndarray,
    message: np.ndarray,
    *,
    trellis: Trellis,
    columns: np.ndarray,
) -> np.ndarray:
    """Change mask that embeds ``message`` at minimal total cost.

    ``message`` is embedded in block order; blocks the message does not reach keep
    the syndrome the cover already has there, so the trellis leaves them alone
    instead of paying for bits nobody reads.
    """
    if message.size > trellis.capacity:
        raise ValueError("message does not fit the trellis")
    # The extractor computes the syndrome of the *stego* bits, so the trellis has
    # to reach H·x = message.  It walks the change vector instead, whose syndrome
    # must therefore be H·x ⊕ H·cover: the cover part is where the layout starts.
    present = syndromes(cover_bits, trellis, columns)
    wanted_bits = bits_of(present, trellis)
    wanted_bits[: message.size] = np.asarray(message, dtype=np.uint8)
    wanted = _pack_bits(wanted_bits, trellis) ^ present
    supplied = np.asarray(costs)
    # Follow the caller's precision: the embedder hands over single-precision costs
    # on large carriers, and the trellis only compares sums, so float64 would buy
    # nothing but memory.
    dtype = np.float32 if supplied.dtype == np.float32 else np.float64
    weights = np.asarray(costs[: trellis.samples], dtype=dtype).reshape(
        trellis.blocks, trellis.width
    )
    return _viterbi(np.maximum(weights, MIN_COST), wanted, trellis, columns)


def _pack_bits(bits: np.ndarray, trellis: Trellis) -> np.ndarray:
    """Inverse of :func:`bits_of`: message bits into per-block integers."""
    shaped = np.asarray(bits, dtype=np.int64).reshape(trellis.blocks, trellis.height)
    weights = np.int64(1) << np.arange(trellis.height, dtype=np.int64)
    packed: np.ndarray = (shaped * weights).sum(axis=1)
    return packed


def _viterbi(
    weights: np.ndarray,
    targets: np.ndarray,
    trellis: Trellis,
    columns: np.ndarray,
) -> np.ndarray:
    """Shortest path per block through ``width`` layers of ``2**height`` states."""
    states = trellis.states
    width = trellis.width
    xor_index = {int(value): np.arange(states) ^ int(value) for value in np.unique(columns)}
    changes = np.zeros((trellis.blocks, width), dtype=bool)
    chunk = max(1, MEMORY_BUDGET // max(1, width * states))
    for start in range(0, trellis.blocks, chunk):
        stop = min(start + chunk, trellis.blocks)
        size = stop - start
        best = np.full((size, states), np.inf, dtype=weights.dtype)
        best[:, 0] = 0.0
        back = np.zeros((width, size, states), dtype=bool)
        for column in range(width):
            cost = weights[start:stop, column][:, None]
            flipped = best[:, xor_index[int(columns[column])]] + cost
            better = flipped < best
            back[column] = better
            best = np.where(better, flipped, best)
        state = targets[start:stop].astype(np.int64).copy()
        local_rows = np.arange(size)
        for column in range(width - 1, -1, -1):
            decision = back[column][local_rows, state]
            changes[start:stop, column] = decision
            state = state ^ np.where(decision, columns[column], 0)
        if np.any(state):
            raise AssertionError("trellis backtracking did not reach the start state")
    return changes.reshape(-1)


def decode(bits: np.ndarray, *, trellis: Trellis, columns: np.ndarray) -> np.ndarray:
    """Message bits read out of a carrier: the syndromes, in message order."""
    return bits_of(syndromes(bits, trellis, columns), trellis)
