"""
core.py — Ядро офлайн-програми стеганографії.

Payload format (before encryption):
  [ type (1B) | name_len (2B, BE) | name (UTF-8) | sha256 (32B) | data_len (4B, BE) | compressed_data ]

type:  0x01 = TEXT,  0x02 = FILE

Encryption : AES-256-GCM via PBKDF2-HMAC-SHA256
Steganography: LSB Matching (±1) in RGB channels of PNG
Compression: zlib (level 9) — transparent
Integrity: SHA-256 hash of original uncompressed data, verified on extraction
"""

from __future__ import annotations

import hashlib
import os
import secrets
import struct
import zlib
from typing import Iterable, List, NamedTuple, Tuple

from PIL import Image
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# =====================================================================
# Constants
# =====================================================================
SALT_SIZE = 16
NONCE_SIZE = 12
KEY_SIZE = 32
KDF_ITERATIONS = 200_000
LENGTH_HEADER_BITS = 32
SHA256_SIZE = 32

TYPE_TEXT: int = 0x01
TYPE_FILE: int = 0x02

ZLIB_LEVEL = 9  # maximum compression
WIPE_PASSES = 3  # number of overwrite passes for secure delete


# =====================================================================
# Extraction result (returned to GUI)
# =====================================================================
class ExtractionResult(NamedTuple):
    payload_type: int           # TYPE_TEXT or TYPE_FILE
    filename: str               # original filename ("" for text)
    data: bytes                 # raw uncompressed data
    hash_expected: str          # SHA-256 stored inside payload
    hash_actual: str            # SHA-256 of extracted data
    integrity_ok: bool          # True if hashes match
    original_size: int          # size before compression
    compressed_size: int        # size after compression (informational)


# =====================================================================
# 1. CRYPTOGRAPHY
# =====================================================================
def derive_key(password: str, salt: bytes,
               iterations: int = KDF_ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(password.encode("utf-8"))


def _encrypt_raw(plaindata: bytes, password: str) -> bytes:
    """Encrypt raw bytes → salt ‖ nonce ‖ ciphertext+tag."""
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    key = derive_key(password, salt)
    ct = AESGCM(key).encrypt(nonce, plaindata, associated_data=None)
    return salt + nonce + ct


def _decrypt_raw(blob: bytes, password: str) -> bytes:
    """Decrypt blob → raw bytes."""
    if len(blob) < SALT_SIZE + NONCE_SIZE + 16:
        raise ValueError("Payload too short for AES-GCM.")
    salt = blob[:SALT_SIZE]
    nonce = blob[SALT_SIZE:SALT_SIZE + NONCE_SIZE]
    ct = blob[SALT_SIZE + NONCE_SIZE:]
    key = derive_key(password, salt)
    return AESGCM(key).decrypt(nonce, ct, associated_data=None)


# =====================================================================
# 2. PAYLOAD FORMAT (with compression + integrity hash)
# =====================================================================
def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def _pack_text(text: str) -> bytes:
    """Pack text payload with compression and SHA-256."""
    raw = text.encode("utf-8")
    digest = _sha256(raw)
    compressed = zlib.compress(raw, ZLIB_LEVEL)
    # type(1) | name_len(2)=0 | sha256(32) | data_len(4) | compressed
    return (struct.pack(">B H", TYPE_TEXT, 0) +
            digest +
            struct.pack(">I", len(compressed)) +
            compressed)


def _pack_file(filepath: str) -> bytes:
    """Pack file payload with original filename, compression, and SHA-256."""
    filename = os.path.basename(filepath)
    name_bytes = filename.encode("utf-8")
    if len(name_bytes) > 0xFFFF:
        raise ValueError("Filename too long (max 65535 UTF-8 bytes).")
    with open(filepath, "rb") as fh:
        raw = fh.read()
    digest = _sha256(raw)
    compressed = zlib.compress(raw, ZLIB_LEVEL)
    return (struct.pack(">B H", TYPE_FILE, len(name_bytes)) +
            name_bytes +
            digest +
            struct.pack(">I", len(compressed)) +
            compressed)


def _unpack_payload(raw: bytes) -> ExtractionResult:
    """
    Unpack serialized payload → ExtractionResult with integrity check.
    Format: type(1) | name_len(2) | name(name_len) | sha256(32) | data_len(4) | compressed(data_len)
    """
    if len(raw) < 3:
        raise ValueError("Corrupted payload (< 3 bytes).")

    offset = 0
    type_byte, name_len = struct.unpack_from(">B H", raw, offset)
    offset += 3

    if len(raw) < offset + name_len:
        raise ValueError("Corrupted payload (name truncated).")
    name = raw[offset:offset + name_len].decode("utf-8") if name_len else ""
    offset += name_len

    if len(raw) < offset + SHA256_SIZE:
        raise ValueError("Corrupted payload (hash missing).")
    stored_hash = raw[offset:offset + SHA256_SIZE]
    offset += SHA256_SIZE

    if len(raw) < offset + 4:
        raise ValueError("Corrupted payload (data length missing).")
    compressed_len = struct.unpack_from(">I", raw, offset)[0]
    offset += 4

    if len(raw) < offset + compressed_len:
        raise ValueError("Corrupted payload (data truncated).")
    compressed = raw[offset:offset + compressed_len]

    # Decompress
    try:
        data = zlib.decompress(compressed)
    except zlib.error as e:
        raise ValueError(f"Decompression failed: {e}")

    # Verify integrity
    actual_hash = _sha256(data)
    hash_expected_hex = stored_hash.hex()
    hash_actual_hex = actual_hash.hex()
    integrity_ok = (stored_hash == actual_hash)

    return ExtractionResult(
        payload_type=type_byte,
        filename=name,
        data=data,
        hash_expected=hash_expected_hex,
        hash_actual=hash_actual_hex,
        integrity_ok=integrity_ok,
        original_size=len(data),
        compressed_size=compressed_len,
    )


# =====================================================================
# 3. PUBLIC ENCRYPT / DECRYPT
# =====================================================================
def encrypt_message(plaintext: str, password: str) -> bytes:
    """Encrypt text → blob (backward compatible entry point)."""
    return _encrypt_raw(_pack_text(plaintext), password)


def encrypt_file(filepath: str, password: str) -> bytes:
    """Encrypt file (with name) → blob."""
    return _encrypt_raw(_pack_file(filepath), password)


def decrypt_payload(blob: bytes, password: str) -> ExtractionResult:
    """Decrypt blob → ExtractionResult (type, name, data, integrity)."""
    raw = _decrypt_raw(blob, password)
    return _unpack_payload(raw)


def decrypt_message(blob: bytes, password: str) -> str:
    """Backward compatibility: decrypt text only."""
    result = decrypt_payload(blob, password)
    if result.payload_type != TYPE_TEXT:
        raise ValueError("Payload contains a file, not text.")
    return result.data.decode("utf-8")


# =====================================================================
# 4. PAYLOAD SIZE ESTIMATION (for capacity bar)
# =====================================================================
def estimate_encrypted_size_text(text: str) -> int:
    """Estimate total blob size for a text message."""
    raw = text.encode("utf-8")
    compressed = zlib.compress(raw, ZLIB_LEVEL)
    # header: type(1) + name_len(2) + sha256(32) + data_len(4) + compressed
    payload_size = 1 + 2 + SHA256_SIZE + 4 + len(compressed)
    # encryption overhead: salt(16) + nonce(12) + tag(16)
    return payload_size + SALT_SIZE + NONCE_SIZE + 16


def estimate_encrypted_size_file(filepath: str) -> int:
    """Estimate total blob size for a file."""
    filename = os.path.basename(filepath)
    name_bytes = filename.encode("utf-8")
    with open(filepath, "rb") as fh:
        raw = fh.read()
    compressed = zlib.compress(raw, ZLIB_LEVEL)
    payload_size = 1 + 2 + len(name_bytes) + SHA256_SIZE + 4 + len(compressed)
    return payload_size + SALT_SIZE + NONCE_SIZE + 16


# =====================================================================
# 5. BIT UTILITIES
# =====================================================================
def bytes_to_bits(data: bytes) -> List[int]:
    bits: List[int] = []
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    return bits


def bits_to_bytes(bits: Iterable[int]) -> bytes:
    bits = list(bits)
    if len(bits) % 8:
        raise ValueError("Bit count not multiple of 8.")
    out = bytearray()
    for i in range(0, len(bits), 8):
        b = 0
        for bit in bits[i:i + 8]:
            b = (b << 1) | (bit & 1)
        out.append(b)
    return bytes(out)


def _length_prefix_bits(n: int) -> List[int]:
    return bytes_to_bits(struct.pack(">I", n))


# =====================================================================
# 6. STEGANOGRAPHY — LSB Matching (±1)
# =====================================================================
def _lsb_match(value: int, secret_bit: int) -> int:
    if (value & 1) == secret_bit:
        return value
    if value == 0:
        return 1
    if value == 255:
        return 254
    return value + (1 if secrets.randbits(1) else -1)


def embed_data_in_image(input_path: str, output_path: str,
                        plaintext: str, password: str) -> None:
    """Embed TEXT into PNG (backward compatible)."""
    payload = encrypt_message(plaintext, password)
    _embed_blob(input_path, output_path, payload)


def embed_file_in_image(input_image: str, output_image: str,
                        filepath: str, password: str) -> None:
    """Embed FILE into PNG."""
    payload = encrypt_file(filepath, password)
    _embed_blob(input_image, output_image, payload)


def _embed_blob(input_path: str, output_path: str,
                payload: bytes) -> None:
    bits = _length_prefix_bits(len(payload)) + bytes_to_bits(payload)
    img = Image.open(input_path)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in (img.mode or "") else "RGB")
    pixels = list(img.getdata())
    capacity = len(pixels) * 3
    if len(bits) > capacity:
        raise ValueError(
            f"Not enough capacity: need {len(bits)} bits, available {capacity}.")
    new_pixels = []
    idx = 0
    total = len(bits)
    for px in pixels:
        px_list = list(px)
        for c in range(3):
            if idx < total:
                px_list[c] = _lsb_match(px_list[c], bits[idx])
                idx += 1
        new_pixels.append(tuple(px_list))
    out = Image.new(img.mode, img.size)
    out.putdata(new_pixels)
    out.save(output_path, format="PNG", optimize=False, compress_level=6)


def extract_raw_from_image(stego_path: str,
                           password: str) -> ExtractionResult:
    """Extract payload from stego image → ExtractionResult."""
    img = Image.open(stego_path)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in (img.mode or "") else "RGB")
    flat: List[int] = []
    for px in img.getdata():
        for c in range(3):
            flat.append(px[c])
    if len(flat) < LENGTH_HEADER_BITS:
        raise ValueError("Image too small for header.")
    header = [flat[i] & 1 for i in range(LENGTH_HEADER_BITS)]
    payload_len = struct.unpack(">I", bits_to_bytes(header))[0]
    total_needed = LENGTH_HEADER_BITS + payload_len * 8
    if total_needed > len(flat):
        raise ValueError("Declared length exceeds image capacity.")
    payload_bits = [flat[i] & 1
                    for i in range(LENGTH_HEADER_BITS, total_needed)]
    blob = bits_to_bytes(payload_bits)
    return decrypt_payload(blob, password)


def extract_data_from_image(stego_path: str, password: str) -> str:
    """Backward compatibility: extract text only."""
    result = extract_raw_from_image(stego_path, password)
    if result.payload_type != TYPE_TEXT:
        raise ValueError("Payload contains a file, not text.")
    return result.data.decode("utf-8")


# =====================================================================
# 7. CAPACITY ESTIMATION
# =====================================================================
def image_capacity_bytes(image_path: str) -> int:
    """Max useful payload bytes (after all overheads)."""
    img = Image.open(image_path)
    w, h = img.size
    total_bits = w * h * 3
    usable = max(0, (total_bits - LENGTH_HEADER_BITS) // 8)
    # overhead: salt + nonce + tag + type + name_len + sha256 + data_len
    overhead = SALT_SIZE + NONCE_SIZE + 16 + 1 + 2 + SHA256_SIZE + 4
    return max(0, usable - overhead)


def image_capacity_bits(image_path: str) -> int:
    """Total embeddable bits (raw, before any overhead)."""
    img = Image.open(image_path)
    w, h = img.size
    return w * h * 3


# =====================================================================
# 8. SECURE DELETE
# =====================================================================
def secure_delete(filepath: str, passes: int = WIPE_PASSES) -> None:
    """
    Overwrite a file with random bytes multiple times, then delete.
    Not guaranteed on SSDs with wear-levelling, but provides
    reasonable protection on traditional storage.
    """
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")
    file_size = os.path.getsize(filepath)
    for _ in range(passes):
        with open(filepath, "r+b") as fh:
            fh.seek(0)
            fh.write(os.urandom(file_size))
            fh.flush()
            os.fsync(fh.fileno())
    os.remove(filepath)