"""Carrier-agnostic v2 stego engine.

Layout on the carrier (all values are bits, 1 bit per sample slot):

* ``HEADER_BITS`` slots taken from ``public_permutation(N)[:HEADER_BITS]`` —
  a password-independent permutation, so extraction can find the header before
  it knows the key.  Contents are random salt/nonce/ciphertext, i.e. they carry
  no magic bytes or version marks;
* the rest of the payload at ``public_permutation(N)[HEADER_BITS + π(j)]`` where
  ``π`` is a keyed permutation over ``N - HEADER_BITS`` slots.  Payload bits are
  spread over the entire carrier, never written as a prefix (D3).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Final, TypeVar

import numpy as np

from phantom.core import constants as C
from phantom.core import container
from phantom.core.errors import CapacityError, WrongPasswordOrNoData
from phantom.core.kdf import KdfProfile
from phantom.core.payload import (
    ExtractedPayload,
    Payload,
    pack_payload,
    payload_capacity_for,
    unpack_payload,
)
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress
from phantom.stego.lsb import BitReader, BitWriter, bits_to_bytes, bytes_to_bits
from phantom.stego.permute import FeistelPermutation, keyed_permutation, public_permutation
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
        return min(100.0, 100.0 * self.fill_ratio(payload_bytes))


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


T = TypeVar("T")


def _header_positions(space: SampleSpace) -> np.ndarray:
    if space.size < C.HEADER_BITS:
        raise WrongPasswordOrNoData("carrier too small to hold a header")
    return public_permutation(int(space.size)).apply(np.arange(C.HEADER_BITS))


def _payload_positions(
    public: FeistelPermutation,
    keyed: FeistelPermutation,
    start: int,
    count: int,
) -> np.ndarray:
    if count <= 0:
        return np.zeros(0, dtype=np.int64)
    slots = C.HEADER_BITS + keyed.apply(np.arange(start, start + count))
    return public.apply(slots)


def embed_payload(
    space: SampleSpace,
    blob: bytes,
    password: str,
    *,
    profile: KdfProfile | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> EmbedReport:
    """Seal ``blob`` and write it into ``space`` with LSB matching."""
    if not blob:
        raise CapacityError("empty payload")
    capacity = capacity_for(space)
    container.header_capacity_check(len(blob) + C.TAG_SIZE, capacity.usable_bits)
    envelope = container.seal(
        blob, password, profile=profile, cancel=cancel, progress=progress
    )
    try:
        usable = int(space.size)
        public = public_permutation(usable)
        keyed = keyed_permutation(bytes(envelope.keys.position_key), usable - C.HEADER_BITS)
        writer = BitWriter(space)

        check_cancel(cancel)
        header_bits = bytes_to_bits(envelope.header)
        writer.write(public.apply(np.arange(C.HEADER_BITS)), header_bits)
        progress(Stage.EMBEDDING, 0, len(envelope.ciphertext))

        ciphertext = envelope.ciphertext
        total = len(ciphertext)
        for offset in range(0, total, CHUNK_BITS // 8):
            check_cancel(cancel)
            block = ciphertext[offset : offset + CHUNK_BITS // 8]
            positions = _payload_positions(public, keyed, offset * 8, len(block) * 8)
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


def read_header(space: SampleSpace) -> bytes:
    positions = _header_positions(space)
    bits = BitReader(space).read(positions)
    return bits_to_bytes(bits)


def extract_payload(
    space: SampleSpace,
    password: str,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> ExtractedPayload:
    """Full v2 extraction: header → keys → payload → AEAD → decompress."""
    capacity = capacity_for(space)
    if capacity.usable_bits < C.HEADER_BITS:
        raise WrongPasswordOrNoData("carrier too small")
    header = read_header(space)
    opened = container.open_header(
        header,
        password,
        max_payload_len=capacity.max_ciphertext_bytes,
        cancel=cancel,
        progress=progress,
    )
    try:
        usable = int(space.size)
        public = public_permutation(usable)
        keyed = keyed_permutation(bytes(opened.keys.position_key), usable - C.HEADER_BITS)
        total_bits = opened.payload_len * 8
        out = bytearray(opened.payload_len)
        progress(Stage.EXTRACTING, 0, opened.payload_len)
        for start_bit in range(0, total_bits, CHUNK_BITS):
            check_cancel(cancel)
            count = min(CHUNK_BITS, total_bits - start_bit)
            positions = _payload_positions(public, keyed, start_bit, count)
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
        opened.wipe()
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
