"""WAV (RIFF/WAVE) carrier: PCM 8/16/24/32-bit, any channel count.

The file is edited in place at the byte level: only the bytes *inside* the
``data`` chunk change.  ``RIFF``/``fmt ``/``LIST``/``bext``/``iXML`` and every
other chunk, including its padding byte, are copied byte-for-byte, so the output
size equals the input size (asserted by tests).

8-bit PCM is unsigned (0..255, offset 128); every other width is two's
complement little-endian.  IEEE-float and companded formats are refused — a
single LSB there is a different thing entirely and would not survive re-encoding.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Final

import numpy as np

from phantom.core import constants as C
from phantom.core.errors import AudioFormatError, UnsupportedCarrier
from phantom.core.files import atomic_path

_FMT_PCM: Final = 0x0001
_FMT_FLOAT: Final = 0x0003
_FMT_EXTENSIBLE: Final = 0xFFFE


@dataclass(frozen=True, slots=True)
class WavLayout:
    data_offset: int
    data_size: int
    channels: int
    sample_rate: int
    bits: int
    block_align: int
    format_tag: int
    file_size: int

    @property
    def frames(self) -> int:
        if self.block_align <= 0:
            return 0
        return self.data_size // self.block_align


def parse_wav(raw: bytes) -> WavLayout:
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise UnsupportedCarrier("not a RIFF/WAVE file")
    fmt: tuple[int, int, int, int, int] | None = None
    data_offset = -1
    data_size = 0
    pos = 12
    data_chunks = 0
    while pos + 8 <= len(raw):
        chunk_id = raw[pos : pos + 4]
        (size,) = struct.unpack_from("<I", raw, pos + 4)
        body = pos + 8
        if body + size > len(raw):
            # Some writers leave a stale size on the last chunk; clamp to EOF.
            size = max(0, len(raw) - body)
        if chunk_id == b"fmt ":
            if size < 16:
                raise AudioFormatError("fmt chunk too small", detail=str(size))
            tag, channels, rate, _byte_rate, align, bits = struct.unpack_from("<HHIIHH", raw, body)
            if tag == _FMT_EXTENSIBLE:
                if size < 40:
                    raise AudioFormatError("extensible fmt chunk too small")
                (sub_format,) = struct.unpack_from("<H", raw, body + 24)
                tag = sub_format
            fmt = (tag, channels, rate, align, bits)
        elif chunk_id == b"data":
            data_chunks += 1
            if data_offset < 0:
                data_offset = body
                data_size = size
        pos = body + size + (size & 1)
    if fmt is None:
        raise AudioFormatError("missing fmt chunk")
    if data_offset < 0:
        raise AudioFormatError("missing data chunk")
    if data_chunks > 1:
        raise AudioFormatError("multiple data chunks are not supported")
    tag, channels, rate, align, bits = fmt
    if tag == _FMT_FLOAT:
        raise AudioFormatError("float WAV is not supported")
    if tag != _FMT_PCM:
        raise AudioFormatError("unsupported WAV codec", detail=f"format tag {tag}")
    if channels <= 0 or channels > 64:
        raise AudioFormatError("unsupported channel count", detail=str(channels))
    if bits not in C.WAV_SUPPORTED_BITS:
        raise AudioFormatError("unsupported bit depth", detail=str(bits))
    expected_align = channels * bits // 8
    if align != expected_align:
        align = expected_align
    if data_size % align:
        raise AudioFormatError("data chunk is not sample aligned")
    return WavLayout(
        data_offset=data_offset,
        data_size=data_size,
        channels=channels,
        sample_rate=rate,
        bits=bits,
        block_align=align,
        format_tag=tag,
        file_size=len(raw),
    )


def decode_samples(payload: bytes, layout: WavLayout) -> np.ndarray:
    """Decode interleaved PCM into a flat int64 array of signed sample values."""
    bits = layout.bits
    if bits == 8:
        unsigned = np.frombuffer(payload, dtype=np.uint8).astype(np.int32)
        return unsigned - C.WAV_UNSIGNED_8BIT_OFFSET
    if bits == 16:
        return np.frombuffer(payload, dtype="<i2").astype(np.int32)
    if bits == 24:
        triplets = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = (
            triplets[:, 0]
            | (triplets[:, 1] << 8)
            | (triplets[:, 2] << 16)
        )
        signed = np.where(values >= (1 << 23), values - (1 << 24), values)
        return signed.astype(np.int32)
    if bits == 32:
        return np.frombuffer(payload, dtype="<i4").astype(np.int64)
    raise AudioFormatError("unsupported bit depth", detail=str(bits))


def encode_samples(samples: np.ndarray, layout: WavLayout) -> bytes:
    """Inverse of :func:`decode_samples`; refuses values outside the bit depth."""
    bits = layout.bits
    values = np.asarray(samples)
    if bits == 8:
        unsigned = values + C.WAV_UNSIGNED_8BIT_OFFSET
        if int(unsigned.min()) < 0 or int(unsigned.max()) > 255:
            raise AudioFormatError("8-bit sample out of range")
        return unsigned.astype(np.uint8).tobytes()
    if bits == 16:
        if int(values.min()) < -32768 or int(values.max()) > 32767:
            raise AudioFormatError("16-bit sample out of range")
        return values.astype("<i2").tobytes()
    if bits == 24:
        if int(values.min()) < -(1 << 23) or int(values.max()) > (1 << 23) - 1:
            raise AudioFormatError("24-bit sample out of range")
        unsigned = (values & 0xFFFFFF).astype(np.uint32)
        out = np.empty((unsigned.size, 3), dtype=np.uint8)
        out[:, 0] = (unsigned & 0xFF).astype(np.uint8)
        out[:, 1] = ((unsigned >> 8) & 0xFF).astype(np.uint8)
        out[:, 2] = ((unsigned >> 16) & 0xFF).astype(np.uint8)
        return out.tobytes()
    if bits == 32:
        low, high = -(1 << 31), (1 << 31) - 1
        if int(values.min()) < low or int(values.max()) > high:
            raise AudioFormatError("32-bit sample out of range")
        return values.astype("<i4").tobytes()
    raise AudioFormatError("unsupported bit depth", detail=str(bits))


def splice_data(raw: bytes, layout: WavLayout, new_payload: bytes) -> bytes:
    """Replace the data-chunk payload, keeping every other byte identical."""
    if len(new_payload) != layout.data_size:
        raise AudioFormatError(
            "embedded audio must keep the exact data size",
            detail=f"{len(new_payload)} != {layout.data_size}",
        )
    start = layout.data_offset
    end = start + layout.data_size
    return raw[:start] + new_payload + raw[end:]


def write_wav_atomic(path: str, raw: bytes) -> None:
    with atomic_path(path) as tmp, open(tmp, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
