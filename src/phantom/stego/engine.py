"""Carrier-agnostic stego engine: v5 by default, v3, v4 and v2 on request.

Layout on the carrier (all values are bits, 1 bit per sample slot):

* one keyed permutation ``B = bootstrap_permutation(position_key, N)`` decides
  where every bit lives.  ``position_key`` is derived from the password with a
  fixed domain salt, so the layout cannot be computed without the password;
* the ``HEADER_BITS`` header bits take the first ranks ``B(0) … B(HEADER_BITS-1)``
  — contents are random salt/nonce/ciphertext, there is no magic or version mark
  in clear text;
* payload bit ``j`` takes ``B(HEADER_BITS + π(j))``, where ``π`` is a second
  permutation keyed by the per-message position key over the remaining
  ``N - HEADER_BITS`` ranks.  Payload bits are spread over the whole carrier,
  never written as a prefix (D3);
* ``B`` is a bijection and the two regions use disjoint rank ranges, so the
  header and the payload can never collide, and no two bits share a slot.

Files written in the v2 layout — header at constant-seeded positions, payload
keyed — are still read: :func:`extract_payload` tries the keyed layouts first and
falls back to v2.  Writing v2 is possible through ``container_version`` for
carriers that an older build has to open.

Two more layouts share that header and change only where the payload goes, both
driven by the wavelet cost map of :mod:`phantom.stego.adaptive`.  One of them, v5,
is what this build writes by default:

* ``C.ADAPTIVE_CONTAINER_VERSION`` (v4) selects the cheapest samples greedily and
  writes one bit into each.  Its extractor has to recompute the cost map from the
  stego image, which is why :func:`phantom.stego.adaptive.write_masked` never
  leaves a sample's 16-value group — that is what makes the layout reproducible.
* ``C.STC_CONTAINER_VERSION`` (v5) gives the whole body and its costs to a
  syndrome-trellis code (:mod:`phantom.stego.stc`), which solves for the cheapest
  set of changes that carries the payload.  It needs no reproducibility trick at
  all: the trellis is built from the carrier size, the payload length and the
  password, so the extractor reproduces it exactly.  It is the layout the
  benchmark likes: same payload, about half the changes of v4 and three to four
  decibels more PSNR.  It is the default: :data:`phantom.core.constants.CONTAINER_VERSION`
  is v5, and v3 (``C.KEYED_CONTAINER_VERSION``) is the fallback when the trellis
  cannot be used.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import Final

import numpy as np

from phantom.core import constants as C
from phantom.core import container
from phantom.core.errors import CapacityError, WrongPasswordOrNoData
from phantom.core.kdf import KdfProfile, derive_position_key
from phantom.core.payload import (
    ExtractedPayload,
    Payload,
    pack_payload,
    payload_capacity_for,
    unpack_payload,
)
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress
from phantom.core.security import secure_zeroize
from phantom.stego import adaptive, stc
from phantom.stego.lsb import BitReader, BitWriter, bits_to_bytes, bytes_to_bits
from phantom.stego.permute import (
    FeistelPermutation,
    bootstrap_permutation,
    keyed_permutation,
    public_permutation,
)
from phantom.stego.samples import SampleSpace

#: Bits handled per chunk: bounds peak memory to a few MB even for huge payloads.
CHUNK_BITS: Final = 1 << 19
#: Slots per pass over the syndrome trellis.  The blocks are independent, so this
#: only bounds the working set — half a million slots is ~12 MB of indices and
#: costs — and keeps the AES buffers small enough to stay in cache, which is
#: measurably faster than one big sweep on a 24 MP carrier.
STC_CHUNK_SLOTS: Final = 1 << 19


@dataclass(frozen=True, slots=True)
class Capacity:
    """What fits into a carrier.

    ``payload_bytes`` is the largest **plaintext** payload record that fits;
    the ciphertext adds the GCM tag, which is why the capacity maths always
    keeps ``TAG_SIZE`` bytes in reserve.
    """

    usable_bits: int
    payload_bytes: int
    header_bits: int = C.HEADER_BITS

    @property
    def payload_area_bytes(self) -> int:
        return max(0, (self.usable_bits - self.header_bits) // 8)

    @property
    def max_ciphertext_bytes(self) -> int:
        return self.payload_bytes + C.TAG_SIZE

    def user_bytes(self, name: str = "") -> int:
        return payload_capacity_for(self.payload_bytes, name)

    def fill_ratio(self, payload_bytes: int) -> float:
        if self.payload_bytes <= 0:
            return 1.0 if payload_bytes > 0 else 0.0
        return payload_bytes / self.payload_bytes

    def fill_percent(self, payload_bytes: int) -> float:
        """Heuristic gauge for the fill bar (plaintext bytes against capacity)."""
        return min(100.0, 100.0 * self.fill_ratio(payload_bytes))

    def carrier_share(self, ciphertext_bytes: int) -> float:
        """Percent of the carrier's usable bits the container actually writes.

        Unlike :meth:`fill_percent` this is not a heuristic: it counts the header
        bits plus eight bits per ciphertext byte, so an extracted payload can be
        reported against the carrier it really sits in.
        """
        if self.usable_bits <= 0:
            return 0.0
        used = self.header_bits + 8 * max(0, ciphertext_bytes)
        return min(100.0, 100.0 * used / self.usable_bits)


@dataclass(frozen=True, slots=True)
class EmbedReport:
    payload_bytes: int
    usable_bits: int
    profile: KdfProfile
    fill_percent: float
    container_version: int = C.CONTAINER_VERSION


def capacity_for(space: SampleSpace) -> Capacity:
    """Payload capacity of a sample space (empty payload area clamped to 0)."""
    usable = int(space.size)
    plaintext_bytes = max(0, (usable - C.HEADER_BITS) // 8 - C.TAG_SIZE)
    return Capacity(usable_bits=usable, payload_bytes=plaintext_bytes)


def _require_room_for_header(space: SampleSpace) -> int:
    usable = int(space.size)
    if usable < C.HEADER_BITS:
        raise WrongPasswordOrNoData("carrier too small to hold a header")
    return usable


def bootstrap_for(
    space: SampleSpace,
    password: str,
    *,
    cancel: threading.Event | None = None,
) -> FeistelPermutation:
    """The v3 permutation that places every bit of ``space`` for ``password``."""
    usable = _require_room_for_header(space)
    key = derive_position_key(password, cancel=cancel)
    try:
        return bootstrap_permutation(bytes(key), usable)
    finally:
        secure_zeroize(key)


def header_positions(
    space: SampleSpace,
    password: str,
    *,
    cancel: threading.Event | None = None,
) -> np.ndarray:
    """Carrier slots that hold the header of a v3 file for ``password``.

    Two different passwords give two different sets of slots: the layout is a
    function of the password, not of the format.
    """
    return bootstrap_for(space, password, cancel=cancel).apply(np.arange(C.HEADER_BITS))


def legacy_permutation(space: SampleSpace) -> FeistelPermutation:
    """v2 layout: the constant-seeded permutation that placed the header.

    Files written by PHANTOM 2.0.0 live at these positions; nothing written by
    this build does.  Kept so those files still open.
    """
    return public_permutation(_require_room_for_header(space))


def _payload_positions(
    bootstrap: FeistelPermutation,
    keyed: FeistelPermutation,
    start: int,
    count: int,
) -> np.ndarray:
    if count <= 0:
        return np.zeros(0, dtype=np.int64)
    slots = C.HEADER_BITS + keyed.apply(np.arange(start, start + count))
    return bootstrap.apply(slots)


#: Adaptive selection sorts every candidate slot, so a payload this large would
#: cost more memory than the layout saves; v3 carries those.
ADAPTIVE_MAX_BITS: Final = 4_000_000

@dataclass(slots=True)
class _LayoutKeys:
    """Position material for one file: the bootstrap permutation and rank key."""

    bootstrap: FeistelPermutation
    rank_key: bytearray

    def wipe(self) -> None:
        secure_zeroize(self.rank_key)


def _layout_keys(
    space: SampleSpace,
    password: str,
    *,
    cancel: threading.Event | None = None,
) -> _LayoutKeys:
    usable = _require_room_for_header(space)
    key = derive_position_key(password, cancel=cancel)
    try:
        return _LayoutKeys(
            bootstrap=bootstrap_permutation(bytes(key), usable),
            rank_key=bytearray(key),
        )
    finally:
        secure_zeroize(key)


def _cost_pixels(space: SampleSpace) -> np.ndarray | None:
    """Pixels a wavelet cost map can be computed from, or ``None``.

    Audio carries no pixels and a partly transparent image has samples the writer
    must not touch, so neither can be analysed — and the caller must not announce
    an analysis pass it will not run.
    """
    pixels = getattr(space, "pixels", None)
    if pixels is None or not getattr(space, "is_opaque", False):
        return None
    if pixels.ndim != 3 or pixels.shape[2] not in (3, 4):
        return None
    analysed: np.ndarray = pixels
    return analysed


def _cost_map(space: SampleSpace, *, masked: bool) -> np.ndarray | None:
    """Per-slot cost of a ±1 change, or ``None`` when the carrier is not an image.

    ``masked`` asks for costs computed from the 16-value group the writer cannot
    leave (:mod:`phantom.stego.adaptive`), which is what makes the v4 layout
    reproducible without the cover.  v5 needs no such thing — its extractor never
    looks at costs — so it reads the finer, unmasked map.
    """
    pixels = _cost_pixels(space)
    if pixels is None:
        return None
    costs = adaptive.layout_costs(pixels) if masked else adaptive.embedding_costs(pixels)
    flat: np.ndarray = np.ascontiguousarray(costs.reshape(-1))
    return flat


def _adaptive_costs(space: SampleSpace) -> np.ndarray | None:
    return _cost_map(space, masked=True)


def _stc_costs(space: SampleSpace) -> np.ndarray | None:
    return _cost_map(space, masked=False)


def adaptive_payload_slots(
    space: SampleSpace,
    bootstrap: FeistelPermutation,
    rank_key: bytes,
    count: int,
) -> np.ndarray | None:
    """Payload slots in bit order for the v4 layout, or ``None`` if it cannot apply.

    Costs come from the carrier as it is *right now*: the embedder passes the
    cover, the extractor passes the file it received.  Both see the same thing
    because :mod:`phantom.stego.adaptive` only ever writes inside a sample's
    16-value group, so the coarse image the costs are derived from cannot move.
    The engine still re-derives the layout after writing and refuses a v4 header
    if it ever disagrees, which turns a silent corruption into a v3 file.
    """
    if count <= 0 or count > ADAPTIVE_MAX_BITS:
        return None
    costs = _adaptive_costs(space)
    if costs is None:
        return None
    usable = _require_room_for_header(space)
    body = bootstrap.apply(np.arange(C.HEADER_BITS, usable))
    selection = adaptive.select_positions(costs, body, count, rank_key)
    return selection.slots


def _write_masked(
    space: SampleSpace,
    slots: np.ndarray,
    data: bytes,
    *,
    rank_key: bytes,
    counter: int = 0,
    cancel: threading.Event | None,
    progress: ProgressFn,
    total: int | None = None,
) -> int:
    """Write ``data`` at ``slots`` without ever leaving a sample's mask group.

    Returns the counter to use for the next call, so the direction stream keeps
    advancing instead of repeating the same pattern.
    """
    step = CHUNK_BITS // 8
    written = 0
    for offset in range(0, len(data), step):
        check_cancel(cancel)
        block = data[offset : offset + step]
        bits = bytes_to_bits(block)
        chunk_slots = slots[offset * 8 : offset * 8 + bits.size]
        directions = adaptive.direction_stream(bits.size, rank_key, counter=counter)
        adaptive.write_masked(space, chunk_slots, bits, directions)
        counter += 1
        written += len(block)
        if total is not None:
            progress(Stage.EMBEDDING, min(total, written), total)
    return counter


def stc_layout(
    space: SampleSpace,
    rank_key: bytes,
    bits: int,
) -> tuple[stc.Trellis, np.ndarray] | None:
    """Trellis and keyed parity matrix for a payload of ``bits``, or ``None``.

    Neither depends on the image: the sender and the receiver derive the same
    structure from the carrier size, the payload length and the password, so the
    v5 layout cannot drift the way a cost-driven one can.
    """
    usable = _require_room_for_header(space)
    trellis = stc.plan(usable - C.HEADER_BITS, bits)
    if trellis is None:
        return None
    matrix = stc.parity_matrix(trellis.height, trellis.width, rank_key)
    return trellis, stc.columns_of(matrix)


def _stc_body(
    bootstrap: FeistelPermutation,
    trellis: stc.Trellis,
    start: int = 0,
    stop: int | None = None,
) -> np.ndarray:
    """The keyed slot order the trellis reads and writes, one block after another.

    ``bootstrap`` is a permutation of the usable slots, so its domain is exactly
    the room left after the header — the same number the plan was made for.

    ``start``/``stop`` pick a range of *blocks*.  A 24 MP carrier has 72 M usable
    slots, so materialising the whole body would cost 576 MB of int64 indices;
    blocks are independent, so callers walk it a chunk at a time instead.
    """
    first = C.HEADER_BITS + start * trellis.width
    last = C.HEADER_BITS + (trellis.blocks if stop is None else stop) * trellis.width
    return bootstrap.apply(np.arange(first, last))


def _stc_block_chunks(trellis: stc.Trellis) -> list[tuple[int, int]]:
    """Block ranges for one pass over the trellis, sized by :data:`STC_CHUNK_SLOTS`."""
    step = max(1, STC_CHUNK_SLOTS // max(1, trellis.width))
    return [
        (start, min(start + step, trellis.blocks)) for start in range(0, trellis.blocks, step)
    ]


def _embed_stc_payload(
    space: SampleSpace,
    bootstrap: FeistelPermutation,
    envelope: container.SealedEnvelope,
    *,
    trellis: stc.Trellis,
    columns: np.ndarray,
    costs: np.ndarray,
    cancel: threading.Event | None,
    progress: ProgressFn,
) -> None:
    """Write header, then flip the samples the trellis chose, and nothing else."""
    check_cancel(cancel)
    reader = BitReader(space)
    writer = BitWriter(space)
    message = bytes_to_bits(envelope.ciphertext)
    writer.write(bootstrap.apply(np.arange(C.HEADER_BITS)), bytes_to_bits(envelope.header))
    progress(Stage.EMBEDDING, 0, len(envelope.ciphertext))
    for start, stop in _stc_block_chunks(trellis):
        check_cancel(cancel)
        body = _stc_body(bootstrap, trellis, start, stop)
        cover_bits = reader.read(body)
        block = stc.sliced(trellis, start, stop)
        window = message[start * trellis.height : stop * trellis.height]
        changes = stc.encode(cover_bits, costs[body], window, trellis=block, columns=columns)
        changed = np.flatnonzero(changes)
        for offset in range(0, changed.size, CHUNK_BITS):
            picked = changed[offset : offset + CHUNK_BITS]
            writer.write(body[picked], 1 - cover_bits[picked])
        progress(
            Stage.EMBEDDING,
            min(len(envelope.ciphertext), stop * trellis.height // 8),
            len(envelope.ciphertext),
        )


def _read_stc_payload(
    space: SampleSpace,
    bootstrap: FeistelPermutation,
    payload_len: int,
    *,
    trellis: stc.Trellis,
    columns: np.ndarray,
    cancel: threading.Event | None,
    progress: ProgressFn,
) -> bytes:
    """Read the syndrome of the stego body and unpack the message out of it."""
    check_cancel(cancel)
    progress(Stage.EXTRACTING, 0, payload_len)
    reader = BitReader(space)
    message = np.zeros(trellis.capacity, dtype=np.uint8)
    for start, stop in _stc_block_chunks(trellis):
        check_cancel(cancel)
        body = _stc_body(bootstrap, trellis, start, stop)
        block = stc.sliced(trellis, start, stop)
        message[start * trellis.height : stop * trellis.height] = stc.decode(
            reader.read(body), trellis=block, columns=columns
        )
        # A whole-body trellis spends seconds per megapixel here; report per chunk
        # so the bar moves instead of sitting at zero until the payload appears.
        progress(
            Stage.EXTRACTING,
            min(payload_len, stop * trellis.height // 8),
            payload_len,
        )
    progress(Stage.EXTRACTING, payload_len, payload_len)
    return bits_to_bytes(message[: payload_len * 8])


def _read_slots(
    space: SampleSpace,
    slots: np.ndarray,
    payload_len: int,
    *,
    cancel: threading.Event | None,
    progress: ProgressFn,
) -> bytes:
    out = bytearray(payload_len)
    total_bits = payload_len * 8
    for start_bit in range(0, total_bits, CHUNK_BITS):
        check_cancel(cancel)
        count = min(CHUNK_BITS, total_bits - start_bit)
        bits = BitReader(space).read(slots[start_bit : start_bit + count])
        produced = bits_to_bytes(bits)
        byte_offset = start_bit // 8
        out[byte_offset : byte_offset + len(produced)] = produced
        progress(
            Stage.EXTRACTING,
            min(payload_len, byte_offset + len(produced)),
            payload_len,
        )
    return bytes(out)


def _read_header_bits(space: SampleSpace, bootstrap: FeistelPermutation) -> bytes:
    positions = bootstrap.apply(np.arange(C.HEADER_BITS))
    return bits_to_bytes(BitReader(space).read(positions))


@dataclass(slots=True)
class OpenedCarrier:
    """A carrier whose header authenticated, with the layout that found it."""

    header: bytes
    opened: container.OpenedHeader
    bootstrap: FeistelPermutation
    version: int
    rank_key: bytearray = field(default_factory=bytearray)

    def wipe(self) -> None:
        self.opened.wipe()
        secure_zeroize(self.rank_key)


def _open_layout(
    space: SampleSpace,
    password: str,
    *,
    max_payload_len: int,
    cancel: threading.Event | None,
    progress: ProgressFn,
) -> OpenedCarrier:
    """Try the keyed layouts (v3, v4 and v5 share header placement), then v2.

    The version lives inside the authenticated header, so one KDF run answers
    "v3, v4 or v5?" — the three differ in where the payload goes, not in how the
    header is placed or sealed.
    """
    keyed_versions = frozenset(
        {C.KEYED_CONTAINER_VERSION, C.ADAPTIVE_CONTAINER_VERSION, C.STC_CONTAINER_VERSION}
    )
    keys = _layout_keys(space, password, cancel=cancel)
    handed_over = False
    try:
        check_cancel(cancel)
        header = _read_header_bits(space, keys.bootstrap)
        try:
            opened = container.open_header(
                header,
                password,
                max_payload_len=max_payload_len,
                accepted_versions=keyed_versions,
                cancel=cancel,
                progress=progress,
            )
        except WrongPasswordOrNoData:
            opened = None
        if opened is not None:
            handed_over = True
            return OpenedCarrier(
                header=header,
                opened=opened,
                bootstrap=keys.bootstrap,
                version=opened.version,
                rank_key=keys.rank_key,
            )
    finally:
        if not handed_over:
            keys.wipe()

    check_cancel(cancel)
    legacy = legacy_permutation(space)
    header = _read_header_bits(space, legacy)
    opened = container.open_header(
        header,
        password,
        max_payload_len=max_payload_len,
        expected_version=C.V2_CONTAINER_VERSION,
        cancel=cancel,
        progress=progress,
    )
    return OpenedCarrier(
        header=header,
        opened=opened,
        bootstrap=legacy,
        version=opened.version,
    )


def _write_keyed_payload(
    space: SampleSpace,
    bootstrap: FeistelPermutation,
    keyed: FeistelPermutation,
    ciphertext: bytes,
    *,
    cancel: threading.Event | None,
    progress: ProgressFn,
) -> None:
    """v3/v2 payload placement: every bit at its keyed slot, chunked."""
    writer = BitWriter(space)
    total = len(ciphertext)
    for offset in range(0, total, CHUNK_BITS // 8):
        check_cancel(cancel)
        block = ciphertext[offset : offset + CHUNK_BITS // 8]
        positions = _payload_positions(bootstrap, keyed, offset * 8, len(block) * 8)
        writer.write(positions, bytes_to_bits(block))
        progress(Stage.EMBEDDING, min(total, offset + len(block)), total)


def _embed_masked_payload(
    space: SampleSpace,
    bootstrap: FeistelPermutation,
    slots: np.ndarray,
    envelope: container.SealedEnvelope,
    *,
    rank_key: bytes,
    cancel: threading.Event | None,
    progress: ProgressFn,
) -> int:
    """Write header and payload, and return the direction counter after them."""
    progress(Stage.EMBEDDING, 0, len(envelope.ciphertext))
    counter = _write_masked(
        space,
        bootstrap.apply(np.arange(C.HEADER_BITS)),
        envelope.header,
        rank_key=rank_key,
        cancel=cancel,
        progress=progress,
    )
    return _write_masked(
        space,
        slots,
        envelope.ciphertext,
        rank_key=rank_key,
        counter=counter,
        cancel=cancel,
        progress=progress,
        total=len(envelope.ciphertext),
    )


def embed_payload(
    space: SampleSpace,
    blob: bytes,
    password: str,
    *,
    profile: KdfProfile | None = None,
    container_version: int = C.CONTAINER_VERSION,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> EmbedReport:
    """Seal ``blob`` and write it into ``space`` with LSB matching.

    ``container_version`` selects the layout: the default is
    ``C.STC_CONTAINER_VERSION`` (syndrome trellis), ``C.KEYED_CONTAINER_VERSION``
    places every bit at password-derived positions,
    ``C.ADAPTIVE_CONTAINER_VERSION`` sends the
    payload to the samples a Daubechies-8 cost map calls cheapest (edges, noise,
    foliage) instead of spreading it evenly, ``C.STC_CONTAINER_VERSION`` — the
    default — embeds it with a syndrome-trellis code over the whole body, and
    ``C.V2_CONTAINER_VERSION`` reproduces the older public-header layout so a
    build that predates v3 can still open the file.  The default is always the
    current version, and every earlier layout stays readable.

    Both adaptive layouts fall back to v3 when the carrier is not an opaque
    image (audio, RGBA with transparent pixels) or when their structure does not
    fit the payload; the header then says v3 and the file is ordinary v3.  The
    v4 layout is additionally verified before it is used, because a layout that
    does not reproduce itself would be unreadable; v5 cannot drift, its extractor
    never looks at the image.
    """
    if not blob:
        raise CapacityError("empty payload")
    capacity = capacity_for(space)
    container.header_capacity_check(len(blob) + C.TAG_SIZE, capacity.usable_bits)
    usable = _require_room_for_header(space)
    total_bits = (len(blob) + C.TAG_SIZE) * 8
    keys = _layout_keys(space, password, cancel=cancel)
    try:
        version = container_version
        # v2 is the one layout whose header sits at public positions: everything
        # else, the adaptive and syndrome-trellis branches included, is keyed.
        bootstrap = (
            legacy_permutation(space) if version == C.V2_CONTAINER_VERSION else keys.bootstrap
        )
        slots: np.ndarray | None = None
        cover_values = np.zeros(0, dtype=np.uint8)
        trellis: stc.Trellis | None = None
        columns = np.zeros(0, dtype=np.int64)
        cost_map: np.ndarray = np.zeros(0, dtype=np.float64)
        if version == C.STC_CONTAINER_VERSION:
            layout = stc_layout(space, bytes(keys.rank_key), total_bits)
            candidate = None
            if layout is not None and _cost_pixels(space) is not None:
                # One opaque pass over the image: say so, so the interface shows a
                # spinner with the elapsed time instead of a frozen zero.
                progress(Stage.ANALYSING, 0, 0)
                candidate = _stc_costs(space)
            if layout is None or candidate is None:
                version = C.KEYED_CONTAINER_VERSION
            else:
                trellis, columns = layout
                cost_map = candidate
        elif version == C.ADAPTIVE_CONTAINER_VERSION:
            if _cost_pixels(space) is not None:
                progress(Stage.ANALYSING, 0, 0)
            slots = adaptive_payload_slots(
                space, bootstrap, bytes(keys.rank_key), total_bits
            )
            if slots is None:
                version = C.KEYED_CONTAINER_VERSION
            else:
                cover_values = np.asarray(space.gather(slots), dtype=np.uint8)
        envelope = container.seal(
            blob,
            password,
            profile=profile,
            version=version,
            cancel=cancel,
            progress=progress,
        )
        try:
            settled = True
            if trellis is not None:
                check_cancel(cancel)
                _embed_stc_payload(
                    space,
                    bootstrap,
                    envelope,
                    trellis=trellis,
                    columns=columns,
                    costs=cost_map,
                    cancel=cancel,
                    progress=progress,
                )
            if slots is not None:
                check_cancel(cancel)
                _embed_masked_payload(
                    space,
                    bootstrap,
                    slots,
                    envelope,
                    rank_key=bytes(keys.rank_key),
                    cancel=cancel,
                    progress=progress,
                )
                # The masked writer cannot move a sample's high bits, so this
                # check is a safety net rather than a fix-up: if it ever fails,
                # the payload is rewritten where v3 would put it.
                renewed = adaptive_payload_slots(
                    space, bootstrap, bytes(keys.rank_key), total_bits
                )
                settled = renewed is not None and adaptive.is_stabler(slots, renewed)
                if not settled:
                    space.scatter(slots, cover_values)
            if not settled:
                envelope.wipe()
                version = C.KEYED_CONTAINER_VERSION
                envelope = container.seal(
                    blob,
                    password,
                    profile=profile,
                    version=version,
                    cancel=cancel,
                    progress=progress,
                )
            if trellis is None and (slots is None or not settled):
                check_cancel(cancel)
                BitWriter(space).write(
                    bootstrap.apply(np.arange(C.HEADER_BITS)),
                    bytes_to_bits(envelope.header),
                )
                progress(Stage.EMBEDDING, 0, len(envelope.ciphertext))
                keyed = keyed_permutation(
                    bytes(envelope.keys.position_key), usable - C.HEADER_BITS
                )
                _write_keyed_payload(
                    space,
                    bootstrap,
                    keyed,
                    envelope.ciphertext,
                    cancel=cancel,
                    progress=progress,
                )
            profile_used = envelope.profile
            payload_len = len(envelope.ciphertext)
        finally:
            envelope.wipe()
    finally:
        keys.wipe()
    return EmbedReport(
        payload_bytes=payload_len,
        usable_bits=capacity.usable_bits,
        profile=profile_used,
        fill_percent=capacity.fill_percent(payload_len),
        container_version=version,
    )


def extract_payload(
    space: SampleSpace,
    password: str,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> ExtractedPayload:
    """Full extraction: layout → header → keys → payload → AEAD → decompress.

    The keyed layouts are tried first; a file written by PHANTOM 2.0.0 is then
    found through the v2 layout.  Both failures surface as the same error.
    """
    capacity = capacity_for(space)
    if capacity.usable_bits < C.HEADER_BITS:
        raise WrongPasswordOrNoData("carrier too small")
    found = _open_layout(
        space,
        password,
        max_payload_len=capacity.max_ciphertext_bytes,
        cancel=cancel,
        progress=progress,
    )
    header, opened = found.header, found.opened
    try:
        usable = int(space.size)
        bootstrap = found.bootstrap
        if opened.version == C.STC_CONTAINER_VERSION:
            layout = stc_layout(space, bytes(found.rank_key), opened.payload_len * 8)
            if layout is None:
                raise WrongPasswordOrNoData("syndrome layout does not fit the carrier")
            trellis, columns = layout
            body = _read_stc_payload(
                space,
                bootstrap,
                opened.payload_len,
                trellis=trellis,
                columns=columns,
                cancel=cancel,
                progress=progress,
            )
        elif opened.version == C.ADAPTIVE_CONTAINER_VERSION:
            slots = adaptive_payload_slots(
                space, bootstrap, bytes(found.rank_key), opened.payload_len * 8
            )
            if slots is None:
                raise WrongPasswordOrNoData("adaptive layout is not reproducible")
            progress(Stage.EXTRACTING, 0, opened.payload_len)
            body = _read_slots(
                space, slots, opened.payload_len, cancel=cancel, progress=progress
            )
        else:
            keyed = keyed_permutation(
                bytes(opened.keys.position_key), usable - C.HEADER_BITS
            )
            body = _read_keyed_payload(
                space,
                bootstrap,
                keyed,
                opened.payload_len,
                cancel=cancel,
                progress=progress,
            )
        plain = container.open_payload(header, opened, body)
        profile_label = opened.profile.label
        version = opened.version
    finally:
        found.wipe()
    result = unpack_payload(plain, cancel=cancel, progress=progress)
    return replace(result, kdf_label=profile_label, container_version=version)


def _read_keyed_payload(
    space: SampleSpace,
    bootstrap: FeistelPermutation,
    keyed: FeistelPermutation,
    payload_len: int,
    *,
    cancel: threading.Event | None,
    progress: ProgressFn,
) -> bytes:
    """v3/v2 payload reading, chunked like the writer."""
    total_bits = payload_len * 8
    out = bytearray(payload_len)
    progress(Stage.EXTRACTING, 0, payload_len)
    for start_bit in range(0, total_bits, CHUNK_BITS):
        check_cancel(cancel)
        count = min(CHUNK_BITS, total_bits - start_bit)
        positions = _payload_positions(bootstrap, keyed, start_bit, count)
        produced = bits_to_bytes(BitReader(space).read(positions))
        byte_offset = start_bit // 8
        out[byte_offset : byte_offset + len(produced)] = produced
        progress(
            Stage.EXTRACTING,
            min(payload_len, byte_offset + len(produced)),
            payload_len,
        )
    return bytes(out)


def embed_text(
    space: SampleSpace,
    text: str,
    password: str,
    *,
    profile: KdfProfile | None = None,
    container_version: int = C.CONTAINER_VERSION,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> EmbedReport:
    capacity = capacity_for(space)
    packed = pack_payload(
        Payload.text(text),
        max_payload_bytes=capacity.payload_bytes,
        cancel=cancel,
        progress=progress,
    )
    return embed_payload(
        space,
        packed.blob,
        password,
        profile=profile,
        container_version=container_version,
        cancel=cancel,
        progress=progress,
    )
