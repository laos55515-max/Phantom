"""Carrier-agnostic v3 stego engine.

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
keyed — are still read: :func:`extract_payload` tries the v3 layout first and
falls back to v2.  Writing v2 is possible through ``container_version`` for
carriers that an older build has to open.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
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

    def wipe(self) -> None:
        self.opened.wipe()


def _open_layout(
    space: SampleSpace,
    password: str,
    *,
    max_payload_len: int,
    cancel: threading.Event | None,
    progress: ProgressFn,
) -> OpenedCarrier:
    """Try the v3 layout, then the v2 one; identical error either way."""
    candidates: list[tuple[int, FeistelPermutation]] = [
        (C.CONTAINER_VERSION, bootstrap_for(space, password, cancel=cancel)),
        (C.V2_CONTAINER_VERSION, legacy_permutation(space)),
    ]
    for version, bootstrap in candidates:
        check_cancel(cancel)
        header = _read_header_bits(space, bootstrap)
        try:
            opened = container.open_header(
                header,
                password,
                max_payload_len=max_payload_len,
                expected_version=version,
                cancel=cancel,
                progress=progress,
            )
        except WrongPasswordOrNoData:
            continue
        return OpenedCarrier(
            header=header, opened=opened, bootstrap=bootstrap, version=version
        )
    raise WrongPasswordOrNoData("no container found for this password")


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

    ``container_version`` selects the layout: the current one places every bit
    at password-derived positions, ``C.V2_CONTAINER_VERSION`` reproduces the
    older public-header layout so a build that predates v3 can still open the
    file.  The default is always the current version.
    """
    if not blob:
        raise CapacityError("empty payload")
    capacity = capacity_for(space)
    container.header_capacity_check(len(blob) + C.TAG_SIZE, capacity.usable_bits)
    envelope = container.seal(
        blob,
        password,
        profile=profile,
        version=container_version,
        cancel=cancel,
        progress=progress,
    )
    try:
        usable = int(space.size)
        if envelope.version == C.CONTAINER_VERSION:
            bootstrap = bootstrap_for(space, password, cancel=cancel)
        else:
            bootstrap = legacy_permutation(space)
        keyed = keyed_permutation(bytes(envelope.keys.position_key), usable - C.HEADER_BITS)
        writer = BitWriter(space)

        check_cancel(cancel)
        header_bits = bytes_to_bits(envelope.header)
        writer.write(bootstrap.apply(np.arange(C.HEADER_BITS)), header_bits)
        progress(Stage.EMBEDDING, 0, len(envelope.ciphertext))

        ciphertext = envelope.ciphertext
        total = len(ciphertext)
        for offset in range(0, total, CHUNK_BITS // 8):
            check_cancel(cancel)
            block = ciphertext[offset : offset + CHUNK_BITS // 8]
            positions = _payload_positions(bootstrap, keyed, offset * 8, len(block) * 8)
            writer.write(positions, bytes_to_bits(block))
            progress(Stage.EMBEDDING, min(total, offset + len(block)), total)
        profile_used = envelope.profile
        payload_len = len(ciphertext)
    finally:
        envelope.wipe()
    return EmbedReport(
        payload_bytes=payload_len,
        usable_bits=capacity.usable_bits,
        profile=profile_used,
        fill_percent=capacity.fill_percent(payload_len),
    )


def extract_payload(
    space: SampleSpace,
    password: str,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> ExtractedPayload:
    """Full extraction: layout → header → keys → payload → AEAD → decompress.

    The v3 layout is tried first; a file written by PHANTOM 2.0.0 is then found
    through the v2 layout.  Both failures surface as the same error.
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
        keyed = keyed_permutation(bytes(opened.keys.position_key), usable - C.HEADER_BITS)
        total_bits = opened.payload_len * 8
        out = bytearray(opened.payload_len)
        progress(Stage.EXTRACTING, 0, opened.payload_len)
        for start_bit in range(0, total_bits, CHUNK_BITS):
            check_cancel(cancel)
            count = min(CHUNK_BITS, total_bits - start_bit)
            positions = _payload_positions(bootstrap, keyed, start_bit, count)
            bits = BitReader(space).read(positions)
            produced = bits_to_bytes(bits)
            byte_offset = start_bit // 8
            out[byte_offset : byte_offset + len(produced)] = produced
            progress(
                Stage.EXTRACTING,
                min(opened.payload_len, byte_offset + len(produced)),
                opened.payload_len,
            )
        plain = container.open_payload(header, opened, bytes(out))
        profile_label = opened.profile.label
        version = opened.version
    finally:
        found.wipe()
    result = unpack_payload(plain, cancel=cancel, progress=progress)
    return replace(result, kdf_label=profile_label, container_version=version)


def embed_text(
    space: SampleSpace,
    text: str,
    password: str,
    *,
    profile: KdfProfile | None = None,
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
        space, packed.blob, password, profile=profile, cancel=cancel, progress=progress
    )
