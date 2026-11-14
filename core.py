"""Compatibility API for PHANTOM's original image GUI.

New integrations should use the engines and exceptions in :mod:`phantom`.
"""
from __future__ import annotations

import hashlib
import os
import stat
import zlib

from phantom.crypto import (AuthenticationError, CapacityError, CarrierError, PhantomError,
    ExtractionResult, TYPE_TEXT, TYPE_FILE, SALT_SIZE, NONCE_SIZE, KEY_SIZE,
    KDF_ITERATIONS, SHA256_SIZE, MAX_DATA_BYTES, MAGIC, derive_key, decrypt_payload,
    encrypt_payload)
from phantom.engines import (BaseSteganographyEngine, ImageSteganographyEngine,
    AudioSteganographyEngine, LENGTH_HEADER_BITS, _same_path)

ZLIB_LEVEL = 9
WIPE_PASSES = 3
_IMAGE = ImageSteganographyEngine()


def encrypt_message(plaintext, password):
    return encrypt_payload(plaintext.encode("utf-8"), password)


def _read_file(filepath, progress=None):
    if os.path.getsize(filepath) > MAX_DATA_BYTES:
        raise CapacityError("Payload exceeds the 64 MiB safety limit.")
    size = os.path.getsize(filepath)
    chunks = []
    read = 0
    with open(filepath, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            read += len(chunk)
            if read > MAX_DATA_BYTES:
                raise CapacityError("Payload exceeds the 64 MiB safety limit.")
            if progress:
                progress("read", read, max(size, read))
    data = b"".join(chunks)
    if len(data) > MAX_DATA_BYTES:
        raise CapacityError("Payload exceeds the 64 MiB safety limit.")
    return data


def encrypt_file(filepath, password):
    return encrypt_payload(_read_file(filepath), password, os.path.basename(filepath))


def decrypt_message(blob, password):
    result = decrypt_payload(blob, password)
    if result.payload_type != TYPE_TEXT:
        raise PhantomError("Payload contains a file, not text.")
    return result.data.decode("utf-8")


def _estimated_blob_size(raw_size, filename_size=0):
    # zlib's documented conservative compressBound expression. This is an
    # estimate only: the true size is checked after compression in the worker.
    bound = raw_size + (raw_size >> 12) + (raw_size >> 14) + (raw_size >> 25) + 13
    return len(MAGIC) + 16 + 12 + 16 + 39 + filename_size + bound


def estimate_encrypted_size_text(text):
    return _estimated_blob_size(len(text.encode("utf-8")))


def estimate_encrypted_size_file(filepath):
    return _estimated_blob_size(os.path.getsize(filepath), len(os.path.basename(filepath).encode("utf-8")))


def embed_data_in_image(input_path, output_path, plaintext, password, progress=None):
    _IMAGE.hide(input_path, output_path, plaintext, password, progress=progress)


def embed_file_in_image(input_image, output_image, filepath, password, progress=None):
    if _same_path(filepath, output_image) or _same_path(filepath, input_image):
        raise CarrierError("The secret, carrier and output must be different files.")
    _IMAGE.hide(input_image, output_image, _read_file(filepath, progress), password,
                filename=os.path.basename(filepath), progress=progress)


def extract_raw_from_image(stego_path, password, progress=None):
    return _IMAGE.extract(stego_path, password, progress=progress)


def extract_data_from_image(stego_path, password):
    result = extract_raw_from_image(stego_path, password)
    if result.payload_type != TYPE_TEXT:
        raise PhantomError("Payload contains a file, not text.")
    return result.data.decode("utf-8")


def image_capacity_bytes(image_path):
    # Approximate available uncompressed payload, excluding new-format overhead.
    return max(0, _IMAGE.check_capacity(image_path) - _estimated_blob_size(0))


def image_capacity_bits(image_path):
    # Preserve exact total bits, including the prefix and fractional last byte.
    with _IMAGE._open(image_path) as img:
        return img.width * img.height * 3


def secure_delete(filepath, passes=WIPE_PASSES):
    """Best effort only; cannot guarantee erasure on SSDs, snapshots or backups."""
    if passes < 1:
        raise ValueError("passes must be positive")
    if not hasattr(os, "O_NOFOLLOW"):
        raise PhantomError("Safe overwrite is unavailable on this platform.")
    before = os.stat(filepath, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise PhantomError("Cannot overwrite a non-regular or hard-linked file.")
    flags = os.O_RDWR | os.O_NOFOLLOW
    fd = os.open(filepath, flags)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)):
            raise PhantomError("Cannot overwrite a non-regular or hard-linked file.")
        for _ in range(passes):
            os.lseek(fd, 0, os.SEEK_SET)
            remaining = info.st_size
            while remaining:
                n = min(remaining, 1024 * 1024)
                block = os.urandom(n)
                view = memoryview(block)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                remaining -= n
            os.fsync(fd)
        current = os.stat(filepath, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise PhantomError("The source file changed; it was not deleted.")
        os.unlink(filepath)
    finally:
        os.close(fd)
