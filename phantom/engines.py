"""Lossless carriers. Progress callbacks receive (stage, completed, total).

A total of None means the underlying library does not expose granular progress.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
import math
import os
from pathlib import Path
import struct
import tempfile
import warnings

import numpy as np
from PIL import Image, UnidentifiedImageError
import soundfile as sf

from .crypto import (AuthenticationError, CapacityError, CarrierError, MAX_BLOB_BYTES,
                     decrypt_payload, encrypt_payload)

LENGTH_BYTES = 4
LENGTH_HEADER_BITS = LENGTH_BYTES * 8
MAX_IMAGE_PIXELS = 40_000_000
CHUNK_BITS = 262_144
AUDIO_BLOCK_FRAMES = 65_536


def _report(callback, stage, done=0, total=None):
    if callback:
        callback(stage, done, total)


def _same_path(a, b):
    if os.path.realpath(a) == os.path.realpath(b):
        return True
    try:
        return os.path.samefile(a, b)
    except (FileNotFoundError, OSError):
        return False


@contextmanager
def _atomic_target(source, target, extension):
    if Path(target).suffix.lower() != extension:
        raise CarrierError(f"Output must use the {extension} extension.")
    if _same_path(source, target):
        raise CarrierError("Output must be a different file from the carrier.")
    parent = Path(target).expanduser().resolve().parent
    fd, temporary = tempfile.mkstemp(prefix=".phantom-", suffix=extension, dir=parent)
    os.close(fd)
    try:
        yield temporary
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _payload_input(data, filename):
    if isinstance(data, str):
        if filename is not None:
            raise TypeError("Use bytes for file data")
        return data.encode("utf-8")
    if isinstance(data, bytes):
        return data
    raise TypeError("Data must be text or bytes")


def _bits_slice(data: bytes, offset: int, count: int) -> np.ndarray:
    start = offset // 8
    stop = (offset + count + 7) // 8
    bits = np.unpackbits(np.frombuffer(data[start:stop], dtype=np.uint8))
    return bits[offset % 8:offset % 8 + count]


class BaseSteganographyEngine(ABC):
    """Capacity is the maximum encrypted blob in bytes, excluding the 4-byte prefix."""

    @abstractmethod
    def check_capacity(self, carrier_path: str) -> int:
        raise NotImplementedError

    @abstractmethod
    def hide(self, carrier_path: str, output_path: str, data: str | bytes,
             password: str, *, filename: str | None = None, progress=None) -> None:
        raise NotImplementedError

    @abstractmethod
    def extract(self, carrier_path: str, password: str, *, progress=None):
        raise NotImplementedError


class ImageSteganographyEngine(BaseSteganographyEngine):
    """PNG RGB-channel LSB matching. Alpha is never altered."""

    @contextmanager
    def _open(self, path):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if image.format != "PNG":
                        raise CarrierError("Carrier must actually be a PNG image.")
                    if getattr(image, "is_animated", False):
                        raise CarrierError("Animated PNG files are not supported.")
                    if image.width * image.height > MAX_IMAGE_PIXELS:
                        raise CarrierError("Image exceeds the 40 megapixel safety limit.")
                    yield image
        except CarrierError:
            raise
        except (UnidentifiedImageError, Image.DecompressionBombError,
                Image.DecompressionBombWarning, OSError) as exc:
            raise CarrierError("Cannot open PNG image.") from exc

    def check_capacity(self, carrier_path):
        with self._open(carrier_path) as image:
            return max(0, (image.width * image.height * 3 - LENGTH_HEADER_BITS) // 8)

    def _pixels(self, image):
        # Materialize exactly one byte buffer, never Python tuples per pixel.
        mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
        converted = image.convert(mode)
        try:
            return mode, bytearray(converted.tobytes())
        finally:
            converted.close()

    def hide(self, carrier_path, output_path, data, password, *, filename=None, progress=None):
        capacity = self.check_capacity(carrier_path)
        _report(progress, "encrypt")
        blob = encrypt_payload(_payload_input(data, filename), password, filename)
        if len(blob) > capacity:
            raise CapacityError(f"Carrier capacity {capacity} bytes; encrypted data needs {len(blob)} bytes.")
        encoded = struct.pack(">I", len(blob)) + blob
        total = len(encoded) * 8
        with _atomic_target(carrier_path, output_path, ".png") as target:
            with self._open(carrier_path) as image:
                mode, buffer = self._pixels(image)
                channel_count = len(mode)
                pixels = np.frombuffer(buffer, dtype=np.uint8)
                for offset in range(0, total, CHUNK_BITS):
                    count = min(CHUNK_BITS, total - offset)
                    indexes = np.arange(offset, offset + count, dtype=np.int64)
                    positions = (indexes // 3) * channel_count + indexes % 3
                    values = pixels[positions]
                    bits = _bits_slice(encoded, offset, count)
                    mismatch = (values & 1) != bits
                    rand = np.frombuffer(os.urandom(count), dtype=np.uint8) & 1
                    direction = np.where(rand, 1, -1).astype(np.int16)
                    direction = np.where(values == 0, 1, np.where(values == 255, -1, direction))
                    pixels[positions] = (values.astype(np.int16) + mismatch * direction).astype(np.uint8)
                    _report(progress, "embed", offset + count, total)
                _report(progress, "save")
                output = Image.frombytes(mode, image.size, bytes(buffer))
                try:
                    output.save(target, format="PNG", compress_level=6)
                finally:
                    output.close()
        _report(progress, "done", 1, 1)

    def extract(self, carrier_path, password, *, progress=None):
        with self._open(carrier_path) as image:
            capacity = max(0, (image.width * image.height * 3 - LENGTH_HEADER_BITS) // 8)
            if image.width * image.height * 3 < LENGTH_HEADER_BITS:
                raise AuthenticationError("Image is too small to contain hidden data.")
            mode, buffer = self._pixels(image)
            channels = len(mode)
            pixels = np.frombuffer(buffer, dtype=np.uint8)

            def read_bits(offset, count):
                indexes = np.arange(offset, offset + count, dtype=np.int64)
                positions = (indexes // 3) * channels + indexes % 3
                return pixels[positions] & 1

            length = struct.unpack(">I", np.packbits(read_bits(0, 32)).tobytes())[0]
            if length > capacity or length > MAX_BLOB_BYTES or length < 44:
                raise AuthenticationError("No valid hidden data or carrier is damaged.")
            result = bytearray()
            total = length * 8
            for offset in range(0, total, CHUNK_BITS):
                count = min(CHUNK_BITS, total - offset)
                result.extend(np.packbits(read_bits(32 + offset, count)).tobytes())
                _report(progress, "extract", offset + count, total)
        _report(progress, "decrypt")
        decoded = decrypt_payload(bytes(result), password)
        _report(progress, "done", 1, 1)
        return decoded


_AUDIO_BITS = {"PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32}


class _AudioReader:
    def __init__(self, source, shift):
        self.source, self.shift = source, shift
        self.samples = np.empty(0, dtype=np.int32)
        self.offset = 0

    def read_bytes(self, size):
        bits = np.empty(size * 8, dtype=np.uint8)
        written = 0
        while written < len(bits):
            if self.offset == len(self.samples):
                self.samples = self.source.read(AUDIO_BLOCK_FRAMES, dtype="int32", always_2d=True).reshape(-1)
                self.offset = 0
                if len(self.samples) == 0:
                    raise AuthenticationError("Audio data is truncated.")
            count = min(len(bits) - written, len(self.samples) - self.offset)
            bits[written:written + count] = (self.samples[self.offset:self.offset + count] >> self.shift) & 1
            self.offset += count
            written += count
        return np.packbits(bits).tobytes()


class AudioSteganographyEngine(BaseSteganographyEngine):
    """WAV/FLAC integer PCM LSB substitution; sample values remain lossless."""

    def _info(self, path):
        try:
            info = sf.info(path)
        except (RuntimeError, OSError, ValueError) as exc:
            raise CarrierError("Cannot open WAV or FLAC audio.") from exc
        if info.format not in ("WAV", "WAVEX", "FLAC") or info.subtype not in _AUDIO_BITS:
            raise CarrierError("Use WAV or FLAC with 8-, 16-, 24-, or 32-bit integer PCM samples.")
        return info

    def check_capacity(self, carrier_path):
        info = self._info(carrier_path)
        return max(0, (info.frames * info.channels - LENGTH_HEADER_BITS) // 8)

    def hide(self, carrier_path, output_path, data, password, *, filename=None, progress=None):
        info = self._info(carrier_path)
        extension = ".wav" if info.format in ("WAV", "WAVEX") else ".flac"
        capacity = max(0, (info.frames * info.channels - LENGTH_HEADER_BITS) // 8)
        _report(progress, "encrypt")
        blob = encrypt_payload(_payload_input(data, filename), password, filename)
        if len(blob) > capacity:
            raise CapacityError(f"Carrier capacity {capacity} bytes; encrypted data needs {len(blob)} bytes.")
        encoded = struct.pack(">I", len(blob)) + blob
        total_bits = len(encoded) * 8
        # libsndfile returns PCM samples left-aligned in int32, including 24-bit PCM.
        shift = 32 - _AUDIO_BITS[info.subtype]
        offset = 0
        frames_done = 0
        with _atomic_target(carrier_path, output_path, extension) as target:
            with sf.SoundFile(carrier_path, mode="r") as src:
                with sf.SoundFile(target, mode="w", samplerate=info.samplerate,
                                  channels=info.channels, format=info.format,
                                  subtype=info.subtype, endian=info.endian) as dst:
                    while True:
                        block = src.read(AUDIO_BLOCK_FRAMES, dtype="int32", always_2d=True)
                        if not len(block):
                            break
                        flat = block.reshape(-1)
                        count = min(len(flat), total_bits - offset)
                        if count:
                            desired = _bits_slice(encoded, offset, count)
                            different = ((flat[:count] >> shift) & 1) != desired
                            flat[:count] ^= different.astype(np.int32) << shift
                            offset += count
                        dst.write(block)
                        frames_done += len(block)
                        _report(progress, "audio_write", frames_done, info.frames)
        _report(progress, "done", 1, 1)

    def extract(self, carrier_path, password, *, progress=None):
        info = self._info(carrier_path)
        capacity = max(0, (info.frames * info.channels - LENGTH_HEADER_BITS) // 8)
        if info.frames * info.channels < LENGTH_HEADER_BITS:
            raise AuthenticationError("Audio is too small to contain hidden data.")
        shift = 32 - _AUDIO_BITS[info.subtype]
        with sf.SoundFile(carrier_path, mode="r") as source:
            reader = _AudioReader(source, shift)
            length = struct.unpack(">I", reader.read_bytes(4))[0]
            if length > capacity or length > MAX_BLOB_BYTES or length < 44:
                raise AuthenticationError("No valid hidden data or carrier is damaged.")
            result = bytearray()
            while len(result) < length:
                result.extend(reader.read_bytes(min(65_536, length - len(result))))
                _report(progress, "extract", len(result), length)
        _report(progress, "decrypt")
        decoded = decrypt_payload(bytes(result), password)
        _report(progress, "done", 1, 1)
        return decoded
