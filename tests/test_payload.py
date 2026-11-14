"""Payload codec: streaming, bounded decompression (D6), integrity, capacity."""

from __future__ import annotations

import os
import time
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from phantom.core import constants as C
from phantom.core.errors import CapacityError, IntegrityError, InvalidInput
from phantom.core.payload import (
    Payload,
    PayloadKind,
    _compress_stream,
    estimate_file_size,
    estimate_size,
    iter_bytes_chunks,
    pack_file,
    pack_payload,
    payload_capacity_for,
    unpack_payload,
)


def test_text_roundtrip() -> None:
    packed = pack_payload(Payload.text("Привіт, світ 🔐 hello"))
    result = unpack_payload(packed.blob)
    assert result.kind is PayloadKind.TEXT
    assert result.data.decode() == "Привіт, світ 🔐 hello"
    assert result.name == ""
    assert result.integrity_ok
    assert result.sha256_expected == packed.sha256_hex


def test_file_roundtrip_keeps_name(tmp_path: Path) -> None:
    source = tmp_path / "secret.bin"
    payload = bytes(range(256)) * 4
    source.write_bytes(payload)
    packed = pack_file(str(source))
    result = unpack_payload(packed.blob)
    assert result.kind is PayloadKind.FILE
    assert result.name == "secret.bin"
    assert result.data == payload


def test_integrity_failure_detected() -> None:
    packed = pack_payload(Payload.text("integrity matters"))
    blob = bytearray(packed.blob)
    blob[-1] ^= 0xFF
    with pytest.raises(IntegrityError):
        unpack_payload(bytes(blob))


def test_truncated_blob_detected() -> None:
    packed = pack_payload(Payload.text("truncate me"))
    for cut in (2, 10, len(packed.blob) - 1):
        with pytest.raises(IntegrityError):
            unpack_payload(packed.blob[:cut])


def test_unknown_payload_type_rejected() -> None:
    blob = bytearray(pack_payload(Payload.text("x")).blob)
    blob[0] = 0x42
    with pytest.raises(IntegrityError):
        unpack_payload(bytes(blob))


def test_capacity_limit_enforced_while_packing() -> None:
    import base64

    incompressible = base64.b64encode(os.urandom(4_000)).decode("ascii")
    with pytest.raises(CapacityError):
        pack_payload(Payload.text(incompressible), max_payload_bytes=256)


def test_payload_capacity_helper() -> None:
    assert payload_capacity_for(100, "") == 100 - C.PAYLOAD_META_OVERHEAD
    assert payload_capacity_for(10, "name.bin") == 0


def test_compression_stops_reading_when_capacity_is_exceeded() -> None:
    consumed: list[int] = []

    def chunks() -> Iterator[bytes]:
        for _ in range(10_000):
            block = os.urandom(1_024)
            consumed.append(len(block))
            yield block

    with pytest.raises(CapacityError):
        _compress_stream(
            chunks(),
            max_compressed=4_096,
            cancel=None,
            progress=lambda *args: None,
            total_hint=10_000 * 1_024,
        )
    assert sum(consumed) < 10_000 * 1_024, "the whole input was read despite the limit"


def test_estimate_exits_early_on_large_file(tmp_path: Path) -> None:
    source = tmp_path / "big.bin"
    source.write_bytes(os.urandom(8 * 1024 * 1024))
    started = time.perf_counter()
    estimate = estimate_file_size(str(source), capacity_bytes=4_096, chunk=65_536)
    elapsed = time.perf_counter() - started
    assert estimate.exceeded_capacity
    assert elapsed < 4.0, "estimate did not stop early"


def test_estimate_without_capacity_reads_everything(tmp_path: Path) -> None:
    source = tmp_path / "small.txt"
    source.write_bytes(b"hello " * 1_000)
    estimate = estimate_file_size(str(source))
    assert not estimate.exceeded_capacity
    assert estimate.payload_bytes > 0


def test_zlib_bomb_is_rejected() -> None:
    bomb = zlib.compress(b"\x00" * (64 * 1024 * 1024), 9)
    assert len(bomb) < 100_000
    header = (
        bytes([PayloadKind.TEXT.value]) + b"\x00\x00" + b"\x00" * C.SHA256_SIZE
    )
    import struct

    blob = header + struct.pack(">I", len(bomb)) + bomb
    with pytest.raises(CapacityError):
        unpack_payload(blob, max_plaintext=1_048_576)


def test_bomb_within_limit_but_wrong_hash_is_integrity_error() -> None:
    bomb = zlib.compress(b"\x00" * 100_000, 9)
    header = bytes([PayloadKind.TEXT.value]) + b"\x00\x00" + b"\x00" * C.SHA256_SIZE
    import struct

    blob = header + struct.pack(">I", len(bomb)) + bomb
    with pytest.raises(IntegrityError):
        unpack_payload(blob, max_plaintext=1_048_576)


def test_filename_length_limit() -> None:
    with pytest.raises(InvalidInput):
        Payload.file("x" * (C.MAX_FILENAME_BYTES + 1), b"data")


def test_text_payload_rejects_name() -> None:
    with pytest.raises(InvalidInput):
        Payload(kind=PayloadKind.TEXT, data=b"x", name="nope")


def test_file_payload_requires_name() -> None:
    with pytest.raises(InvalidInput):
        Payload(kind=PayloadKind.FILE, data=b"x")


def test_estimate_size_for_text() -> None:
    import base64

    text = base64.b64encode(os.urandom(400)).decode("ascii")
    packed = pack_payload(Payload.text(text))
    estimate = estimate_size(Payload.text(text))
    assert estimate.payload_bytes == len(packed.blob)
    assert not estimate.exceeded_capacity


def test_iter_bytes_chunks_covers_input() -> None:
    data = bytes(range(256)) * 100
    assert b"".join(iter_bytes_chunks(data, chunk=1_000)) == data
