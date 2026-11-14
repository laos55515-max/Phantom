"""Authenticated payloads used by all carrier engines.

Version 2 authenticates its format marker as AES-GCM associated data.  Older
PHANTOM blobs have no marker and use 200,000 PBKDF2 iterations; they remain
readable, but are never written by the new implementation.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
import zlib
from pathlib import PurePosixPath
from typing import NamedTuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"PHNTM2\x00\x01"
KDF_ITERATIONS = 600_000
LEGACY_KDF_ITERATIONS = 200_000
SALT_SIZE = 16
NONCE_SIZE = 12
KEY_SIZE = 32
TAG_SIZE = 16
SHA256_SIZE = 32
TYPE_TEXT = 1
TYPE_FILE = 2
MAX_DATA_BYTES = 64 * 1024 * 1024
# zlib can expand incompressible input slightly; allow for that overhead.
MAX_BLOB_BYTES = MAX_DATA_BYTES + MAX_DATA_BYTES // 1000 + 131_072


class PhantomError(ValueError):
    """An expected validation, capacity or decryption failure."""


class CapacityError(PhantomError):
    pass


class CarrierError(PhantomError):
    pass


class AuthenticationError(PhantomError):
    """Wrong password or damaged/unsupported hidden data."""


class ExtractionResult(NamedTuple):
    payload_type: int
    filename: str
    data: bytes
    hash_expected: str
    hash_actual: str
    integrity_ok: bool
    original_size: int
    compressed_size: int


def derive_key(password: str, salt: bytes, iterations: int = KDF_ITERATIONS) -> bytes:
    if not isinstance(password, str) or not password:
        raise PhantomError("Password must not be empty.")
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=KEY_SIZE,
                      salt=salt, iterations=iterations).derive(password.encode("utf-8"))


def pack_payload(data: bytes, filename: str | None = None) -> bytes:
    if not isinstance(data, bytes):
        raise TypeError("Payload must be bytes")
    if len(data) > MAX_DATA_BYTES:
        raise CapacityError("Payload exceeds the 64 MiB safety limit.")
    kind = TYPE_TEXT if filename is None else TYPE_FILE
    # Strip paths on both Unix and Windows, even when the file was made abroad.
    name = "" if filename is None else PurePosixPath(filename.replace("\\", "/")).name
    if kind == TYPE_FILE and name in ("", ".", ".."):
        raise PhantomError("Invalid filename.")
    name_bytes = name.encode("utf-8")
    if len(name_bytes) > 65535:
        raise PhantomError("Filename is too long.")
    compressed = zlib.compress(data, 9)
    return (struct.pack(">BH", kind, len(name_bytes)) + name_bytes +
            hashlib.sha256(data).digest() + struct.pack(">I", len(compressed)) + compressed)


def unpack_payload(raw: bytes) -> ExtractionResult:
    try:
        if len(raw) < 39:
            raise ValueError("truncated header")
        kind, name_len = struct.unpack_from(">BH", raw)
        offset = 3 + name_len
        if kind not in (TYPE_TEXT, TYPE_FILE) or len(raw) < offset + 36:
            raise ValueError("invalid type or header")
        filename = raw[3:offset].decode("utf-8")
        if kind == TYPE_TEXT and filename:
            raise ValueError("text has a filename")
        if kind == TYPE_FILE:
            filename = PurePosixPath(filename.replace("\\", "/")).name
            if filename in ("", ".", ".."):
                raise ValueError("invalid filename")
        expected = raw[offset:offset + 32]
        compressed_len = struct.unpack_from(">I", raw, offset + 32)[0]
        compressed = raw[offset + 36:]
        if len(compressed) != compressed_len:
            raise ValueError("invalid compressed length")
        # Reject decompression bombs and trailing or incomplete streams.
        dec = zlib.decompressobj()
        data = dec.decompress(compressed, MAX_DATA_BYTES + 1)
        if len(data) > MAX_DATA_BYTES or dec.unconsumed_tail or not dec.eof or dec.unused_data:
            raise ValueError("invalid or excessive decompressed data")
        actual = hashlib.sha256(data).digest()
        if not hmac.compare_digest(expected, actual):
            raise ValueError("digest mismatch")
    except (ValueError, UnicodeDecodeError, zlib.error, struct.error) as exc:
        raise AuthenticationError("Hidden data is invalid or damaged.") from exc
    return ExtractionResult(kind, filename, data, expected.hex(), actual.hex(),
                            True, len(data), len(compressed))


def encrypt_payload(data: bytes, password: str, filename: str | None = None) -> bytes:
    packed = pack_payload(data, filename)
    salt, nonce = os.urandom(SALT_SIZE), os.urandom(NONCE_SIZE)
    key = derive_key(password, salt)
    return MAGIC + salt + nonce + AESGCM(key).encrypt(nonce, packed, MAGIC)


def decrypt_payload(blob: bytes, password: str) -> ExtractionResult:
    new_format = blob.startswith(MAGIC)
    offset = len(MAGIC) if new_format else 0
    if len(blob) < offset + SALT_SIZE + NONCE_SIZE + TAG_SIZE or len(blob) > MAX_BLOB_BYTES:
        raise AuthenticationError("Wrong password or hidden data is damaged.")
    salt = blob[offset:offset + SALT_SIZE]
    nonce = blob[offset + SALT_SIZE:offset + SALT_SIZE + NONCE_SIZE]
    ciphertext = blob[offset + SALT_SIZE + NONCE_SIZE:]
    key = derive_key(password, salt, KDF_ITERATIONS if new_format else LEGACY_KDF_ITERATIONS)
    try:
        raw = AESGCM(key).decrypt(nonce, ciphertext, MAGIC if new_format else None)
    except InvalidTag as exc:
        raise AuthenticationError("Wrong password or hidden data is damaged.") from exc
    return unpack_payload(raw)
