"""Payload packing: type | name | sha256 | compressed stream.

Design notes
------------
* compression is **streamed**: as soon as the compressed size would exceed the
  carrier capacity we stop reading the input (no gigabyte reads, no gigabyte
  compression for a hopeless file) — D10;
* decompression is **bounded** with ``zlib.decompressobj`` and a hard ceiling,
  so a 299 KiB zlib bomb cannot allocate 300 MiB — D6;
* the SHA-256 of the *uncompressed* data is stored inside the AEAD-protected
  payload and re-checked on extraction.
"""

from __future__ import annotations

import hashlib
import os
import struct
import threading
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final

from phantom.core import constants as C
from phantom.core.errors import (
    CapacityError,
    IntegrityError,
    InvalidInput,
    PhantomIOError,
    UnsafePathError,
)
from phantom.core.files import FALLBACK_FILE_NAME, sanitise_filename
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress

_HEADER: Final = struct.Struct(">BH")
_LEN: Final = struct.Struct(">I")
_CHUNK: Final = C.COMPRESS_CHUNK


@unique
class PayloadKind(Enum):
    TEXT = C.TYPE_TEXT
    FILE = C.TYPE_FILE

    @classmethod
    def from_byte(cls, value: int) -> PayloadKind:
        try:
            return cls(value)
        except ValueError as exc:
            raise IntegrityError("unknown payload type", detail=str(value)) from exc


@dataclass(frozen=True, slots=True)
class Payload:
    """What the user wants to hide."""

    kind: PayloadKind
    data: bytes
    name: str = ""

    def __post_init__(self) -> None:
        if self.kind is PayloadKind.TEXT and self.name:
            raise InvalidInput("text payloads have no file name")
        if self.kind is PayloadKind.FILE and not self.name:
            raise InvalidInput("file payloads need a name")
        if len(self.name.encode("utf-8")) > C.MAX_FILENAME_BYTES:
            raise InvalidInput("file name too long")

    @classmethod
    def text(cls, text: str) -> Payload:
        return cls(kind=PayloadKind.TEXT, data=text.encode("utf-8"))

    @classmethod
    def file(cls, name: str, data: bytes) -> Payload:
        return cls(kind=PayloadKind.FILE, data=data, name=name)


@dataclass(frozen=True, slots=True)
class PackedPayload:
    blob: bytes
    original_size: int
    compressed_size: int
    sha256_hex: str


@dataclass(frozen=True, slots=True)
class ExtractedPayload:
    kind: PayloadKind
    name: str
    data: bytes
    sha256_expected: str
    sha256_actual: str
    integrity_ok: bool
    original_size: int
    compressed_size: int
    kdf_label: str = ""
    container_version: int = 0


def carried_bytes(payload: ExtractedPayload) -> int:
    """Bytes this payload occupies on a carrier: packed blob plus the GCM tag.

    ``compressed_size`` is only the zlib stream, so the metadata the container
    stores around it (type, name, digest, length) and the authentication tag are
    added back to get the ciphertext length the engine actually wrote.
    """
    name_bytes = len(payload.name.encode("utf-8"))
    return C.PAYLOAD_META_OVERHEAD + name_bytes + payload.compressed_size + C.TAG_SIZE


@dataclass(frozen=True, slots=True)
class SizeEstimate:
    """Result of a streaming size estimate."""

    payload_bytes: int
    exceeded_capacity: bool

    def fits(self, capacity: int) -> bool:
        return not self.exceeded_capacity and self.payload_bytes <= capacity


def iter_file_chunks(path: str, *, chunk: int = _CHUNK) -> Iterator[bytes]:
    try:
        with open(path, "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    return
                yield block
    except OSError as exc:
        raise PhantomIOError("cannot read file", detail=str(exc)) from exc


def iter_bytes_chunks(data: bytes, *, chunk: int = _CHUNK) -> Iterator[bytes]:
    for offset in range(0, len(data), chunk):
        yield data[offset : offset + chunk]


def _compress_stream(
    chunks: Iterable[bytes],
    *,
    max_compressed: int | None,
    cancel: threading.Event | None,
    progress: ProgressFn,
    total_hint: int,
    max_plaintext: int | None = None,
) -> tuple[bytes, bytes, int]:
    """Compress ``chunks`` with early abort. Returns (compressed, sha256, raw_len).

    Two ceilings, not one.  ``max_compressed`` keeps the container inside the
    carrier; ``max_plaintext`` keeps it inside what :func:`unpack_payload` is
    willing to inflate again.  Without the second the writer happily produced a
    container that the same build refused to open — a payload the user could
    embed and never extract.  ``None`` means the module default, read here rather
    than bound as a default argument so the limit can be inspected and patched.
    """
    limit = C.MAX_PLAINTEXT_BYTES if max_plaintext is None else max_plaintext
    compressor = zlib.compressobj(C.ZLIB_LEVEL)
    out = bytearray()
    digest = hashlib.sha256()
    read = 0
    progress(Stage.COMPRESSING, 0, total_hint)
    for block in chunks:
        check_cancel(cancel)
        read += len(block)
        if read > limit:
            raise CapacityError(
                "payload exceeds the safety limit",
                detail=f"{read} > {limit} bytes of plaintext",
            )
        digest.update(block)
        out += compressor.compress(block)
        if max_compressed is not None and len(out) > max_compressed:
            raise CapacityError(
                "payload does not fit the carrier",
                detail=f"compressed stream already {len(out)} > {max_compressed}",
            )
        progress(Stage.COMPRESSING, read, total_hint if total_hint else read)
    out += compressor.flush()
    if read > limit:  # the last block can cross the ceiling as well
        raise CapacityError(
            "payload exceeds the safety limit",
            detail=f"{read} > {limit} bytes of plaintext",
        )
    if max_compressed is not None and len(out) > max_compressed:
        raise CapacityError(
            "payload does not fit the carrier",
            detail=f"compressed payload {len(out)} > {max_compressed}",
        )
    progress(Stage.COMPRESSING, read, read or 1)
    return bytes(out), digest.digest(), read


def pack_payload(
    payload: Payload,
    *,
    max_payload_bytes: int | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> PackedPayload:
    """Serialize + compress ``payload`` into the canonical v2 plaintext blob."""
    # ``Payload`` validates the name length on construction, so no re-check here.
    name_bytes = payload.name.encode("utf-8")
    meta = _HEADER.size + len(name_bytes) + C.SHA256_SIZE + _LEN.size
    compressed_budget = (
        None if max_payload_bytes is None else max(0, max_payload_bytes - meta)
    )
    chunks = iter_bytes_chunks(payload.data)
    compressed, digest, raw_len = _compress_stream(
        chunks,
        max_compressed=compressed_budget,
        cancel=cancel,
        progress=progress,
        total_hint=len(payload.data),
    )
    blob = (
        _HEADER.pack(payload.kind.value, len(name_bytes))
        + name_bytes
        + digest
        + _LEN.pack(len(compressed))
        + compressed
    )
    if max_payload_bytes is not None and len(blob) > max_payload_bytes:
        raise CapacityError(
            "payload does not fit the carrier",
            detail=f"payload {len(blob)} > capacity {max_payload_bytes}",
        )
    return PackedPayload(
        blob=blob,
        original_size=raw_len,
        compressed_size=len(compressed),
        sha256_hex=digest.hex(),
    )


def pack_file(
    path: str,
    *,
    max_payload_bytes: int | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> PackedPayload:
    """Pack a file without loading it into memory."""
    display_name = os.path.basename(path)
    name_bytes = display_name.encode("utf-8")
    if len(name_bytes) > C.MAX_FILENAME_BYTES:
        raise InvalidInput("file name too long")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise PhantomIOError("cannot stat file", detail=str(exc)) from exc
    meta = _HEADER.size + len(name_bytes) + C.SHA256_SIZE + _LEN.size
    compressed_budget = (
        None if max_payload_bytes is None else max(0, max_payload_bytes - meta)
    )
    compressed, digest, raw_len = _compress_stream(
        iter_file_chunks(path),
        max_compressed=compressed_budget,
        cancel=cancel,
        progress=progress,
        total_hint=size,
    )
    blob = (
        _HEADER.pack(PayloadKind.FILE.value, len(name_bytes))
        + name_bytes
        + digest
        + _LEN.pack(len(compressed))
        + compressed
    )
    if max_payload_bytes is not None and len(blob) > max_payload_bytes:
        raise CapacityError(
            "payload does not fit the carrier",
            detail=f"payload {len(blob)} > capacity {max_payload_bytes}",
        )
    return PackedPayload(
        blob=blob,
        original_size=raw_len,
        compressed_size=len(compressed),
        sha256_hex=digest.hex(),
    )


def estimate_size(
    payload: Payload,
    *,
    capacity_bytes: int | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
    chunk: int = _CHUNK,
) -> SizeEstimate:
    """Streaming size estimate with early exit — never scans a whole 100 MB file."""
    meta = _HEADER.size + len(payload.name.encode("utf-8")) + C.SHA256_SIZE + _LEN.size
    budget = None if capacity_bytes is None else max(0, capacity_bytes - meta)
    return _estimate_chunks(
        iter_bytes_chunks(payload.data, chunk=chunk),
        meta=meta,
        budget=budget,
        capacity=capacity_bytes,
        cancel=cancel,
        progress=progress,
        total=len(payload.data),
    )


def estimate_file_size(
    path: str,
    *,
    capacity_bytes: int | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
    chunk: int = _CHUNK,
) -> SizeEstimate:
    """Streaming estimate for a file payload (early exit at capacity)."""
    name = os.path.basename(path)
    meta = _HEADER.size + len(name.encode("utf-8")) + C.SHA256_SIZE + _LEN.size
    budget = None if capacity_bytes is None else max(0, capacity_bytes - meta)
    try:
        total = os.path.getsize(path)
    except OSError as exc:
        raise PhantomIOError("cannot stat file", detail=str(exc)) from exc
    return _estimate_chunks(
        iter_file_chunks(path, chunk=chunk),
        meta=meta,
        budget=budget,
        capacity=capacity_bytes,
        cancel=cancel,
        progress=progress,
        total=total,
    )


def _estimate_chunks(
    chunks: Iterable[bytes],
    *,
    meta: int,
    budget: int | None,
    capacity: int | None,
    cancel: threading.Event | None,
    progress: ProgressFn,
    total: int,
) -> SizeEstimate:
    compressor = zlib.compressobj(C.ZLIB_LEVEL)
    produced = 0
    consumed = 0
    progress(Stage.ESTIMATING, 0, total)
    for block in chunks:
        check_cancel(cancel)
        consumed += len(block)
        produced += len(compressor.compress(block))
        if budget is not None and produced > budget:
            return SizeEstimate(payload_bytes=meta + produced, exceeded_capacity=True)
        progress(Stage.ESTIMATING, consumed, total or consumed)
    produced += len(compressor.flush())
    estimate = meta + produced
    if capacity is not None and estimate > capacity:
        return SizeEstimate(payload_bytes=estimate, exceeded_capacity=True)
    return SizeEstimate(payload_bytes=estimate, exceeded_capacity=False)


def _safe_payload_name(kind: PayloadKind, stored: str) -> str:
    """Turn a container-supplied name into one harmless segment.

    The stored name is attacker-controlled: it may be ``../../.bashrc``, an
    absolute path, ``".."`` or empty.  Nothing outside this function ever sees
    the raw value, so a hostile container cannot steer a save out of its folder
    even if a caller forgets to sanitise again.
    """
    if kind is not PayloadKind.FILE:
        return ""
    try:
        return sanitise_filename(stored)
    except UnsafePathError:
        # An empty name, "." or ".." is not worth failing the whole extraction
        # for: the data is intact, only the file name was unusable.
        return FALLBACK_FILE_NAME


def unpack_payload(
    blob: bytes,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
    max_plaintext: int | None = None,
) -> ExtractedPayload:
    """Parse a decrypted payload blob and decompress it under a hard limit."""
    limit = C.MAX_PLAINTEXT_BYTES if max_plaintext is None else max_plaintext
    if len(blob) < _HEADER.size:
        raise IntegrityError("payload truncated (header)")
    kind_byte, name_len = _HEADER.unpack_from(blob, 0)
    kind = PayloadKind.from_byte(kind_byte)
    offset = _HEADER.size
    if len(blob) < offset + name_len + C.SHA256_SIZE + _LEN.size:
        raise IntegrityError("payload truncated (metadata)")
    name = _safe_payload_name(
        kind, blob[offset : offset + name_len].decode("utf-8", errors="replace")
    )
    offset += name_len
    stored_hash = blob[offset : offset + C.SHA256_SIZE]
    offset += C.SHA256_SIZE
    (compressed_len,) = _LEN.unpack_from(blob, offset)
    offset += _LEN.size
    if len(blob) < offset + compressed_len:
        raise IntegrityError("payload truncated (data)")
    compressed = blob[offset : offset + compressed_len]

    data = _decompress_bounded(
        compressed, limit=limit, cancel=cancel, progress=progress
    )
    actual = hashlib.sha256(data).digest()
    integrity_ok = actual == stored_hash
    if not integrity_ok:
        raise IntegrityError("payload hash mismatch")
    return ExtractedPayload(
        kind=kind,
        name=name,
        data=data,
        sha256_expected=stored_hash.hex(),
        sha256_actual=actual.hex(),
        integrity_ok=True,
        original_size=len(data),
        compressed_size=compressed_len,
    )


def _decompress_bounded(
    compressed: bytes,
    *,
    limit: int,
    cancel: threading.Event | None,
    progress: ProgressFn,
) -> bytes:
    """Streaming decompression with an absolute output ceiling (anti-bomb)."""
    decompressor = zlib.decompressobj()
    out = bytearray()
    cursor = 0
    total_in = len(compressed)
    step = max(1024, total_in // 64)
    progress(Stage.DECOMPRESSING, 0, total_in)
    try:
        while cursor < total_in:
            check_cancel(cancel)
            chunk = compressed[cursor : cursor + step]
            cursor += len(chunk)
            produced = decompressor.decompress(chunk, limit - len(out) + 1)
            out += produced
            if len(out) > limit:
                raise CapacityError(
                    "decompressed payload exceeds the safety limit",
                    detail=f"limit {limit} bytes",
                )
            progress(Stage.DECOMPRESSING, cursor, total_in)
        tail = decompressor.flush()
        out += tail
        if len(out) > limit:
            raise CapacityError(
                "decompressed payload exceeds the safety limit",
                detail=f"limit {limit} bytes",
            )
    except zlib.error as exc:
        raise IntegrityError("decompression failed", detail=str(exc)) from exc
    if not decompressor.eof:
        raise IntegrityError("compressed stream truncated")
    progress(Stage.DECOMPRESSING, total_in, total_in)
    return bytes(out)


def payload_capacity_for(carrier_payload_bytes: int, name: str = "") -> int:
    """Usable bytes of *user data* for a carrier that can hold N payload bytes."""
    meta = _HEADER.size + len(name.encode("utf-8")) + C.SHA256_SIZE + _LEN.size
    return max(0, carrier_payload_bytes - meta)
