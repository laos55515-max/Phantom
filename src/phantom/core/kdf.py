"""Key derivation: Argon2id by default, PBKDF2-HMAC-SHA256 for FIPS profiles.

Only primitives shipped by ``cryptography`` are used.  No custom crypto.
"""

from __future__ import annotations

import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Final

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from phantom.core import constants as C
from phantom.core.constants import KdfProfile
from phantom.core.errors import InvalidInput, InvalidPassword
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress
from phantom.core.security import secure_zeroize

__all__ = [
    "HKDF_SHA256",
    "KdfProfile",
    "MessageKeys",
    "derive_master_key",
    "derive_message_keys",
    "derive_position_key",
    "hkdf",
    "normalize_password",
    "password_variants",
    "validate_password",
    "wipe_all",
]

HKDF_SHA256: Final = hashes.SHA256()


def normalize_password(password: str) -> str:
    """NFKC-normalize a password so NFC/NFD/aesthetic variants agree (D5)."""
    if not isinstance(password, str):
        raise InvalidPassword("password must be a string")
    return unicodedata.normalize(C.PASSWORD_NORMALIZATION, password)


def password_variants(password: str) -> tuple[str, ...]:
    """Candidate spellings for *legacy* v1 blobs (written before NFKC existed).

    Order matters: the original bytes are always tried first, then NFC, then
    NFKC.  v2 blobs only ever use the NFKC form.
    """
    base = password if isinstance(password, str) else str(password)
    variants = [base]
    for form in ("NFC", "NFKC"):
        candidate = unicodedata.normalize(form, base)
        if candidate not in variants:
            variants.append(candidate)
    return tuple(variants)


def validate_password(password: str) -> None:
    """Structural validation only — strength policy belongs to the UI."""
    if not isinstance(password, str):
        raise InvalidPassword("password must be a string")
    if password == "":
        raise InvalidPassword("password is empty")


def _password_bytes(password: str, *, normalize: bool) -> bytearray:
    text = normalize_password(password) if normalize else password
    return bytearray(text.encode("utf-8"))


def derive_master_key(
    password: str,
    salt: bytes,
    profile: KdfProfile,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
    normalize: bool = True,
) -> bytearray:
    """Derive a 32-byte master key for ``profile``."""
    validate_password(password)
    if len(salt) < 8:
        raise InvalidInput("salt too short")
    check_cancel(cancel)
    progress(Stage.KDF, 0, 0)
    secret = _password_bytes(password, normalize=normalize)
    try:
        if profile.kind is C.KdfKind.ARGON2ID:
            kdf = Argon2id(
                salt=salt,
                length=C.AES_KEY_SIZE,
                iterations=profile.iterations,
                lanes=profile.lanes,
                memory_cost=profile.memory_kib,
            )
            raw = kdf.derive(bytes(secret))
        else:
            pbkdf2 = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=C.AES_KEY_SIZE,
                salt=salt,
                iterations=profile.iterations,
            )
            raw = pbkdf2.derive(bytes(secret))
    finally:
        _zero(secret)
    check_cancel(cancel)
    return bytearray(raw)


def derive_position_key(
    password: str,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> bytearray:
    """Bootstrap key that decides where every bit of a v3 carrier lives.

    The salt is the fixed public constant ``POSITION_KDF_SALT``: the random
    per-message salt lives *inside* the sealed header, and the header cannot be
    located before this key exists.  The trade-off is deliberate and documented
    in ``SECURITY.md``: the position set depends on the password and the carrier
    size, never on the payload, so two files hidden with the same password in
    carriers of the same size use the same positions.

    ``PBKDF2-HMAC-SHA256`` is used regardless of the KDF profile chosen for the
    message itself, because that profile is negotiated by the header the key is
    needed to find.  The output is HKDF-separated from the message keys so the
    two derivations can never collide.
    """
    validate_password(password)
    profile = C.KDF_PROFILES[C.POSITION_KDF_PROFILE_ID]
    master = derive_master_key(
        password, C.POSITION_KDF_SALT, profile, cancel=cancel, progress=progress
    )
    try:
        return hkdf(
            master,
            C.INFO_POSITION_BOOTSTRAP,
            length=C.AES_KEY_SIZE,
            salt=C.POSITION_KDF_SALT,
        )
    finally:
        _zero(master)


def hkdf(
    key: bytes | bytearray,
    info: bytes,
    *,
    length: int,
    salt: bytes | None = None,
) -> bytearray:
    """HKDF-SHA256 expansion with domain separation."""
    hk = HKDF(
        algorithm=HKDF_SHA256,
        length=length,
        salt=salt,
        info=info,
    )
    return bytearray(hk.derive(bytes(key)))


def _zero(buf: bytearray) -> None:
    secure_zeroize(buf)


@dataclass(slots=True)
class MessageKeys:
    """All key material derived for one message.

    Values are ``bytearray`` so :meth:`wipe` can zero them.  Python makes no
    hard guarantee (interpreter copies, GC, OS swap) — documented limitation.
    """

    length_key: bytearray = field(repr=False)
    payload_key: bytearray = field(repr=False)
    payload_nonce: bytearray = field(repr=False)
    position_key: bytearray = field(repr=False)

    def wipe(self) -> None:
        for buf in (self.length_key, self.payload_key, self.payload_nonce, self.position_key):
            _zero(buf)

    def __enter__(self) -> MessageKeys:
        return self

    def __exit__(self, *exc: object) -> None:
        self.wipe()


def derive_message_keys(
    master_key: bytes | bytearray,
    salt: bytes,
    *,
    reserved_added: bytes = b"",
) -> MessageKeys:
    """Split a master key into independent sub-keys (one per purpose).

    Distinct HKDF info labels mean the AES-GCM nonces used by the length and
    payload messages are never combined with the same key.
    """
    info_suffix = reserved_added
    return MessageKeys(
        length_key=hkdf(master_key, C.INFO_LEN_KEY + info_suffix, length=C.AES_KEY_SIZE, salt=salt),
        payload_key=hkdf(
            master_key, C.INFO_PAYLOAD_KEY + info_suffix, length=C.AES_KEY_SIZE, salt=salt
        ),
        payload_nonce=hkdf(
            master_key, C.INFO_PAYLOAD_NONCE + info_suffix, length=C.NONCE_SIZE, salt=salt
        ),
        position_key=hkdf(
            master_key, C.INFO_POSITION_KEY + info_suffix, length=C.AES_KEY_SIZE, salt=salt
        ),
    )


def wipe_all(*buffers: bytearray) -> None:
    for buf in buffers:
        _zero(buf)
