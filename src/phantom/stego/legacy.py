"""Read-only support for the historical v1 containers (R5).

v1 layout, preserved exactly as shipped before this refactor:

* AES-256-GCM with a key from PBKDF2-HMAC-SHA256, 200 000 iterations, raw
  UTF-8 password bytes (no normalisation);
* blob = salt(16) || nonce(12) || ciphertext||tag;
* the blob length is written as 32 plain-text bits at the very start of the
  carrier, then the blob itself, sequentially, MSB-first, in RGB channel order;
* the plaintext is the same payload record used by v2
  (type | name | sha256 | len | zlib data), so v2 code can unpack it.

Nothing here is ever used to *write* new data: v1 can only be read, and the
tests pin the behaviour against golden fixtures generated from the original
commit.
"""

from __future__ import annotations

import struct
import threading
from dataclasses import replace
from typing import Final

import numpy as np
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from phantom.core import constants as C
from phantom.core.errors import IntegrityError, WrongPasswordOrNoData
from phantom.core.kdf import password_variants
from phantom.core.payload import ExtractedPayload, unpack_payload
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress
from phantom.stego.lsb import bits_to_bytes

_HEADER_BITS: Final = 32
_LENGTH_STRUCT: Final = struct.Struct(">I")
_SALT: Final = 16
_NONCE: Final = 12


def _legacy_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=C.AES_KEY_SIZE,
        salt=salt,
        iterations=C.LEGACY_PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def _decrypt_legacy(blob: bytes, password: str) -> bytes:
    if len(blob) < _SALT + _NONCE + C.TAG_SIZE:
        raise WrongPasswordOrNoData("legacy blob too short")
    salt = blob[:_SALT]
    nonce = blob[_SALT : _SALT + _NONCE]
    ciphertext = blob[_SALT + _NONCE :]
    last_error: Exception | None = None
    for variant in password_variants(password):
        key = _legacy_key(variant, salt)
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:  # try the next normalisation form
            last_error = exc
    raise WrongPasswordOrNoData("legacy authentication failed") from last_error


def decode_legacy_blob(blob: bytes, password: str) -> ExtractedPayload:
    """Decrypt + unpack a raw v1 blob (also used by the golden-fixture tests)."""
    plain = _decrypt_legacy(blob, password)
    return unpack_payload(plain)


def legacy_bit_stream(pixels: np.ndarray) -> np.ndarray:
    """Flatten an RGB(A) array into the sequential v1 bit order."""
    if pixels.ndim != 3 or pixels.shape[2] < 3:
        raise WrongPasswordOrNoData("image has no RGB channels")
    flat = np.ascontiguousarray(pixels[:, :, :3]).reshape(-1)
    return np.asarray(flat & 1, dtype=np.uint8)


def extract_legacy_from_pixels(
    pixels: np.ndarray,
    password: str,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> ExtractedPayload:
    """Extract a v1 payload; raises ``WrongPasswordOrNoData`` if there is none."""
    bits = legacy_bit_stream(pixels)
    if bits.size < _HEADER_BITS:
        raise WrongPasswordOrNoData("carrier too small for a v1 header")
    check_cancel(cancel)
    progress(Stage.EXTRACTING, 0, 1)
    (declared,) = _LENGTH_STRUCT.unpack(bits_to_bytes(bits[:_HEADER_BITS]))
    total_bits = _HEADER_BITS + declared * 8
    if declared <= 0 or total_bits > bits.size:
        raise WrongPasswordOrNoData("v1 length header does not fit the carrier")
    blob = bits_to_bytes(bits[_HEADER_BITS:total_bits])
    progress(Stage.EXTRACTING, 1, 1)
    check_cancel(cancel)
    try:
        result = decode_legacy_blob(blob, password)
    except IntegrityError as exc:
        # A corrupted v1 blob is indistinguishable from a wrong password.
        raise WrongPasswordOrNoData("legacy payload corrupt") from exc
    return replace(
        result,
        kdf_label="legacy-v1 · pbkdf2-hmac-sha256 200k",
        container_version=1,
    )
