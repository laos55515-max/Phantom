"""Versioned v2 container: AES-256-GCM with KDF profile negotiation and AAD.

On-carrier layout (bits, not bytes — no magic, no version in clear text):

* fixed-size public header region (``HEADER_BITS``), placed at positions from a
  constant-seeded permutation::

      salt(16) || nonce(12) || AESGCM(length_key, nonce, version|profile|len)

  The ciphertext authenticates the KDF profile, so the extractor simply tries
  every known profile — the carrier never advertises which one was used.
* payload region, placed at positions from a key-derived permutation::

      AESGCM(payload_key, payload_nonce, payload_bytes, aad = label|header|ver|profile)

All sub-keys come from HKDF over the KDF master key, so the random header nonce
is never reused with the same key.
"""

from __future__ import annotations

import os
import struct
import threading
from dataclasses import dataclass, field
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from phantom.core import constants as C
from phantom.core.errors import (
    CapacityError,
    InternalError,
    InvalidInput,
    WrongPasswordOrNoData,
)
from phantom.core.kdf import (
    KdfProfile,
    MessageKeys,
    derive_master_key,
    derive_message_keys,
    normalize_password,
)
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress

_LEN_STRUCT: Final = struct.Struct(">BBI")


@dataclass(slots=True)
class SealedEnvelope:
    """Result of sealing a payload: public header + ciphertext + key material."""

    header: bytes
    ciphertext: bytes
    keys: MessageKeys = field(repr=False)
    profile: KdfProfile

    def wipe(self) -> None:
        self.keys.wipe()

    def __enter__(self) -> SealedEnvelope:
        return self

    def __exit__(self, *exc: object) -> None:
        self.wipe()


@dataclass(slots=True)
class OpenedHeader:
    """Parsed header: negotiated profile, trusted payload length, keys."""

    version: int
    profile: KdfProfile
    payload_len: int
    keys: MessageKeys = field(repr=False)

    def wipe(self) -> None:
        self.keys.wipe()

    def __enter__(self) -> OpenedHeader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.wipe()


def payload_aad(header: bytes, version: int, profile_id: int) -> bytes:
    """Associated data binding the ciphertext to the exact header bytes."""
    return C.PAYLOAD_AAD_PREFIX + b"|" + header + bytes((version, profile_id))


def seal(
    payload: bytes,
    password: str,
    *,
    profile: KdfProfile | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> SealedEnvelope:
    """Encrypt ``payload`` into a fresh v2 container."""
    profile = profile or C.default_kdf_profile()
    if profile.profile_id not in C.KDF_PROFILES:
        raise InvalidInput("unknown KDF profile")
    if not isinstance(payload, (bytes, bytearray)):
        raise InvalidInput("payload must be bytes")
    normalized = normalize_password(password)
    if len(payload) > 0xFFFFFFFF:
        raise InvalidInput("payload too large for the container")

    salt = os.urandom(C.SALT_SIZE)
    nonce = os.urandom(C.NONCE_SIZE)
    master = derive_master_key(
        normalized, salt, profile, cancel=cancel, progress=progress, normalize=False
    )
    keys = derive_message_keys(master, salt)
    master[:] = b"\x00" * len(master)
    try:
        # Store the *ciphertext* length: it includes the GCM tag and is exactly
        # the number of bytes the carrier must hold.
        length_plain = _LEN_STRUCT.pack(
            C.CONTAINER_VERSION, profile.profile_id, len(payload) + C.TAG_SIZE
        )
        len_ct = AESGCM(bytes(keys.length_key)).encrypt(
            nonce, length_plain, C.LEN_AAD
        )
        header = salt + nonce + len_ct
        aad = payload_aad(header, C.CONTAINER_VERSION, profile.profile_id)
        check_cancel(cancel)
        progress(Stage.ENCRYPTING, 0, 1)
        ciphertext = AESGCM(bytes(keys.payload_key)).encrypt(
            bytes(keys.payload_nonce), bytes(payload), aad
        )
        progress(Stage.ENCRYPTING, 1, 1)
    except BaseException:
        keys.wipe()
        raise
    return SealedEnvelope(header=header, ciphertext=ciphertext, keys=keys, profile=profile)


def open_header(
    header: bytes,
    password: str,
    *,
    max_payload_len: int,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> OpenedHeader:
    """Find the KDF profile that authenticates ``header`` and return its keys."""
    if len(header) != C.HEADER_BYTES:
        raise WrongPasswordOrNoData("header length mismatch")
    if max_payload_len < 0:
        raise InvalidInput("max_payload_len must be >= 0")
    salt = header[: C.HEADER_SALT_SIZE]
    nonce = header[C.HEADER_SALT_SIZE : C.HEADER_SALT_SIZE + C.HEADER_NONCE_SIZE]
    len_ct = header[C.HEADER_SALT_SIZE + C.HEADER_NONCE_SIZE :]
    normalized = normalize_password(password)

    for index, profile_id in enumerate(sorted(C.KDF_PROFILES)):
        check_cancel(cancel)
        profile = C.KDF_PROFILES[profile_id]
        master = derive_master_key(
            normalized, salt, profile, cancel=cancel, progress=progress, normalize=False
        )
        try:
            keys = derive_message_keys(master, salt)
            master[:] = b"\x00" * len(master)
            try:
                plain = AESGCM(bytes(keys.length_key)).decrypt(nonce, len_ct, C.LEN_AAD)
            except InvalidTag:
                keys.wipe()
                continue
            try:
                version, embedded_profile, payload_len = _LEN_STRUCT.unpack(plain)
            except struct.error:
                keys.wipe()
                continue
            if version != C.CONTAINER_VERSION or embedded_profile != profile_id:
                keys.wipe()
                continue
            if payload_len > max_payload_len:
                keys.wipe()
                raise WrongPasswordOrNoData(
                    "declared payload length exceeds carrier capacity"
                )
            if payload_len <= C.TAG_SIZE:
                keys.wipe()
                continue
            progress(Stage.KDF, index + 1, len(C.KDF_PROFILES))
            return OpenedHeader(
                version=version, profile=profile, payload_len=payload_len, keys=keys
            )
        except BaseException:
            master[:] = b"\x00" * len(master)
            raise
    raise WrongPasswordOrNoData("no authenticating KDF profile")


def open_payload(header: bytes, opened: OpenedHeader, ciphertext: bytes) -> bytes:
    """Authenticate and decrypt the payload region."""
    if len(ciphertext) != opened.payload_len:
        raise WrongPasswordOrNoData("payload length mismatch")
    aad = payload_aad(header, opened.version, opened.profile.profile_id)
    try:
        plain = AESGCM(bytes(opened.keys.payload_key)).decrypt(
            bytes(opened.keys.payload_nonce), ciphertext, aad
        )
    except InvalidTag as exc:
        raise WrongPasswordOrNoData("payload authentication failed") from exc
    if plain and plain[0] == 0x00:
        # Defensive: v2 payloads start with a type byte, never 0.
        raise InternalError("unexpected payload type byte")
    return plain


def header_capacity_check(payload_len: int, usable_bits: int) -> None:
    """Structural guard shared by both carriers."""
    needed = C.HEADER_BITS + payload_len * 8
    if needed > usable_bits:
        raise CapacityError(
            "payload does not fit", detail=f"need {needed} bits, have {usable_bits}"
        )
