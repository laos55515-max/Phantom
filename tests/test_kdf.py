"""KDF: known vectors, normalisation (D5), profile constants (D1, R3)."""

from __future__ import annotations

import threading
import unicodedata

import pytest

from phantom.core import constants as C
from phantom.core.errors import InvalidPassword, OperationCancelled
from phantom.core.kdf import (
    derive_master_key,
    derive_message_keys,
    hkdf,
    normalize_password,
    password_variants,
)

# RFC 7914, section 11 (PBKDF2-HMAC-SHA256 known answers).
RFC7914_C1 = (
    "55ac046e56e3089fec1691c22544b605f94185216dde0465e68b9d57c20dacbc"
    "49ca9cccf179b645991664b39d77ef317c71b845b1e30bd509112041d3a19783"
)
RFC7914_C80000 = (
    "4ddcd8f60b98be21830cee5ef22701f9641a4418d04c0414aeff08876b34ab56"
    "a1d425a1225833549adb841b51c9b3176a272bdebba1d078478f62b397f33c8d"
)
# Argon2id cross-checked against the reference implementation (argon2-cffi /
# libargon2) with m=19456 KiB, t=2, p=1, 32-byte tag.
ARGON2ID_REFERENCE = "cdad8c21302c7cc5f6e6d0e803a58d0c7f34d5826ae6c27d1e32c212556e36eb"


def test_pbkdf2_rfc7914_vector_c1() -> None:
    """The RFC vector itself, plus equality of our wrapper for a valid salt."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    full = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=64, salt=b"salt", iterations=1
    ).derive(b"passwd")
    assert full.hex() == RFC7914_C1

    profile = C.KdfProfile(
        profile_id=99,
        label="test",
        kind=C.KdfKind.PBKDF2,
        memory_kib=0,
        iterations=4_096,
        lanes=0,
    )
    reference = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=b"0123456789abcdef", iterations=4_096
    ).derive(b"passwd")
    assert bytes(derive_master_key("passwd", b"0123456789abcdef", profile)) == reference


def test_pbkdf2_rfc7914_vector_80000() -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    full = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=64, salt=b"NaCl", iterations=80_000
    ).derive(b"Password")
    assert full.hex() == RFC7914_C80000


def test_argon2id_reference_vector() -> None:
    profile = C.KdfProfile(
        profile_id=98,
        label="argon2-test",
        kind=C.KdfKind.ARGON2ID,
        memory_kib=19_456,
        iterations=2,
        lanes=1,
    )
    key = derive_master_key(
        "phantom-test-vector", b"0123456789abcdef", profile, normalize=False
    )
    assert key.hex() == ARGON2ID_REFERENCE


def test_default_profile_is_above_owasp_minimum() -> None:
    profile = C.default_kdf_profile()
    assert profile.kind is C.KdfKind.ARGON2ID
    assert profile.memory_kib >= C.ARGON2_OWASP_MIN_MEMORY_KIB
    assert profile.iterations >= C.ARGON2_OWASP_MIN_ITERATIONS
    assert profile.lanes == 1
    assert profile.memory_kib == C.ARGON2_MEMORY_KIB
    assert profile.iterations == C.ARGON2_ITERATIONS


def test_pbkdf2_profile_meets_owasp_minimum() -> None:
    profile = C.KDF_PROFILES[C.KDF_PROFILE_PBKDF2_SHA256]
    assert profile.iterations >= C.PBKDF2_OWASP_MIN_ITERATIONS
    assert profile.iterations == C.PBKDF2_ITERATIONS
    assert profile.is_fips_compatible


def test_nfkc_normalisation_makes_variants_agree() -> None:
    nfc = unicodedata.normalize("NFC", "Пароль-ї-Ω")
    nfd = unicodedata.normalize("NFD", "Пароль-ї-Ω")
    assert nfc != nfd
    assert normalize_password(nfc) == normalize_password(nfd)
    salt = b"0123456789abcdef"
    profile = C.KDF_PROFILES[C.KDF_PROFILE_PBKDF2_SHA256]
    assert derive_master_key(nfc, salt, profile) == derive_master_key(nfd, salt, profile)


def test_legacy_variants_try_raw_then_normalised() -> None:
    nfd = unicodedata.normalize("NFD", "Пароль-ї")
    variants = password_variants(nfd)
    assert variants[0] == nfd
    assert unicodedata.normalize("NFC", nfd) in variants
    assert len(set(variants)) == len(variants)


def test_hkdf_domain_separation() -> None:
    master = b"\x11" * 32
    salt = b"\x22" * 16
    keys = derive_message_keys(master, salt)
    try:
        materials = {
            bytes(keys.length_key),
            bytes(keys.payload_key),
            bytes(keys.payload_nonce),
            bytes(keys.position_key),
        }
        assert len(materials) == 4
        assert len(keys.payload_nonce) == C.NONCE_SIZE
        assert hkdf(master, C.INFO_LEN_KEY, length=32, salt=salt) == keys.length_key
    finally:
        keys.wipe()


def test_message_keys_wipe_zeroes_material() -> None:
    keys = derive_message_keys(b"\x01" * 32, b"\x02" * 16)
    keys.wipe()
    assert bytes(keys.length_key) == b"\x00" * len(keys.length_key)
    assert bytes(keys.payload_key) == b"\x00" * len(keys.payload_key)
    assert bytes(keys.position_key) == b"\x00" * len(keys.position_key)


def test_empty_password_rejected() -> None:
    profile = C.KDF_PROFILES[C.KDF_PROFILE_PBKDF2_SHA256]
    with pytest.raises(InvalidPassword):
        derive_master_key("", b"0123456789abcdef", profile)


def test_short_salt_rejected() -> None:
    profile = C.KDF_PROFILES[C.KDF_PROFILE_PBKDF2_SHA256]
    with pytest.raises(Exception):
        derive_master_key("password", b"short", profile)


def test_cancellation_before_derivation() -> None:
    profile = C.KDF_PROFILES[C.KDF_PROFILE_PBKDF2_SHA256]
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelled):
        derive_master_key("password", b"0123456789abcdef", profile, cancel=cancel)
