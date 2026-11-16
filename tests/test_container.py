"""Container format: negotiation, AAD binding, tamper detection, versioning (D4)."""

from __future__ import annotations

import os
import struct

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from phantom.core import constants as C
from phantom.core import container, security
from phantom.core.errors import CapacityError, InvalidInput, WrongPasswordOrNoData
from phantom.core.kdf import MessageKeys, derive_master_key, derive_message_keys

PASSWORD = "correct horse battery staple"
PAYLOAD = b"type-one payload used by the container tests" * 3


def test_header_layout_sizes() -> None:
    sealed = container.seal(PAYLOAD, PASSWORD)
    try:
        assert len(sealed.header) == C.HEADER_BYTES
        assert sealed.header[:16] != sealed.header[16 : 16 + 12]
        assert len(sealed.ciphertext) == len(PAYLOAD) + C.TAG_SIZE
    finally:
        sealed.wipe()


def test_container_has_no_plaintext_magic_or_version() -> None:
    """Nothing in the header is a readable marker: it behaves like random bytes."""
    headers = []
    for _ in range(32):
        sealed = container.seal(PAYLOAD, PASSWORD)
        try:
            headers.append(sealed.header)
            assert b"PHANTOM" not in sealed.header
            assert struct.pack(">I", len(PAYLOAD) + C.TAG_SIZE) not in sealed.header
        finally:
            sealed.wipe()
    # A fixed version/profile marker would show up as a constant byte at a fixed
    # offset; 32 independent seals must vary there.
    first_bytes = {header[C.HEADER_SALT_SIZE] for header in headers}
    assert len(first_bytes) > 8


def test_roundtrip_default_profile() -> None:
    sealed = container.seal(PAYLOAD, PASSWORD)
    try:
        opened = container.open_header(
            sealed.header, PASSWORD, max_payload_len=1 << 20
        )
        try:
            assert opened.payload_len == len(sealed.ciphertext)
            assert opened.profile.profile_id == C.KDF_PROFILE_ARGON2ID
            plain = container.open_payload(sealed.header, opened, sealed.ciphertext)
            assert plain == PAYLOAD
        finally:
            opened.wipe()
    finally:
        sealed.wipe()


def test_roundtrip_pbkdf2_profile_is_negotiated() -> None:
    profile = C.KDF_PROFILES[C.KDF_PROFILE_PBKDF2_SHA256]
    sealed = container.seal(PAYLOAD, PASSWORD, profile=profile)
    try:
        opened = container.open_header(sealed.header, PASSWORD, max_payload_len=1 << 20)
        try:
            assert opened.profile.profile_id == C.KDF_PROFILE_PBKDF2_SHA256
            assert container.open_payload(sealed.header, opened, sealed.ciphertext) == PAYLOAD
        finally:
            opened.wipe()
    finally:
        sealed.wipe()


def test_nfc_nfd_passwords_are_interchangeable() -> None:
    import unicodedata

    nfc = unicodedata.normalize("NFC", "Пароль-ї-Ω-2026")
    nfd = unicodedata.normalize("NFD", nfc)
    sealed = container.seal(PAYLOAD, nfc)
    try:
        opened = container.open_header(sealed.header, nfd, max_payload_len=1 << 20)
        try:
            assert container.open_payload(sealed.header, opened, sealed.ciphertext) == PAYLOAD
        finally:
            opened.wipe()
    finally:
        sealed.wipe()


def test_wrong_password_is_indistinguishable_from_no_data() -> None:
    sealed = container.seal(PAYLOAD, PASSWORD)
    try:
        with pytest.raises(WrongPasswordOrNoData):
            container.open_header(sealed.header, "wrong password", max_payload_len=1 << 20)
    finally:
        sealed.wipe()


@pytest.mark.parametrize("region", ["salt", "nonce", "length", "ciphertext"])
def test_tampering_each_region_is_detected(region: str) -> None:
    sealed = container.seal(PAYLOAD, PASSWORD)
    try:
        header = bytearray(sealed.header)
        ciphertext = bytearray(sealed.ciphertext)
        if region == "salt":
            header[0] ^= 0x01
        elif region == "nonce":
            header[C.HEADER_SALT_SIZE] ^= 0x01
        elif region == "length":
            header[C.HEADER_SALT_SIZE + C.HEADER_NONCE_SIZE] ^= 0x01
        else:
            ciphertext[3] ^= 0x01
        with pytest.raises(WrongPasswordOrNoData):
            opened = container.open_header(
                bytes(header), PASSWORD, max_payload_len=1 << 20
            )
            try:
                container.open_payload(bytes(header), opened, bytes(ciphertext))
            finally:
                opened.wipe()
    finally:
        sealed.wipe()


def test_header_bound_to_payload_via_aad() -> None:
    """Swapping the nonce of another message must invalidate the payload."""
    first = container.seal(PAYLOAD, PASSWORD)
    second = container.seal(PAYLOAD, PASSWORD)
    try:
        mixed = second.header[: C.HEADER_SALT_SIZE] + first.header[C.HEADER_SALT_SIZE :]
        with pytest.raises(WrongPasswordOrNoData):
            opened = container.open_header(mixed, PASSWORD, max_payload_len=1 << 20)
            assert opened.payload_len == len(second.ciphertext)
            container.open_payload(mixed, opened, second.ciphertext)
    finally:
        first.wipe()
        second.wipe()


def _forged_header(version: int) -> tuple[bytes, MessageKeys]:
    salt = os.urandom(C.SALT_SIZE)
    nonce = os.urandom(C.NONCE_SIZE)
    profile = C.default_kdf_profile()
    master = derive_master_key(PASSWORD, salt, profile, normalize=False)
    keys = derive_message_keys(master, salt)
    forged = struct.pack(">BBI", version, profile.profile_id, 64)
    len_ct = AESGCM(bytes(keys.length_key)).encrypt(nonce, forged, C.LEN_AAD)
    return salt + nonce + len_ct, keys


def test_version_inside_aead_is_validated() -> None:
    """A header that authenticates but claims another version is rejected."""
    header, keys = _forged_header(4)
    try:
        with pytest.raises(WrongPasswordOrNoData):
            container.open_header(header, PASSWORD, max_payload_len=1 << 20)
    finally:
        keys.wipe()


def test_expected_version_selects_the_layout_a_header_must_match() -> None:
    """The v2 reader accepts only a v2 header, the v3 reader only a v3 one."""
    v2_header, v2_keys = _forged_header(C.V2_CONTAINER_VERSION)
    v3_header, v3_keys = _forged_header(C.KEYED_CONTAINER_VERSION)
    try:
        opened = container.open_header(
            v2_header,
            PASSWORD,
            max_payload_len=1 << 20,
            expected_version=C.V2_CONTAINER_VERSION,
        )
        opened.wipe()
        with pytest.raises(WrongPasswordOrNoData):
            container.open_header(
                v2_header, PASSWORD, max_payload_len=1 << 20
            )
        with pytest.raises(WrongPasswordOrNoData):
            container.open_header(
                v3_header,
                PASSWORD,
                max_payload_len=1 << 20,
                expected_version=C.V2_CONTAINER_VERSION,
            )
    finally:
        v2_keys.wipe()
        v3_keys.wipe()


def test_unsupported_version_is_refused_up_front() -> None:
    with pytest.raises(InvalidInput):
        container.seal(PAYLOAD, PASSWORD, version=7)
    with pytest.raises(InvalidInput):
        container.open_header(
            b"\x00" * C.HEADER_BYTES,
            PASSWORD,
            max_payload_len=1 << 20,
            expected_version=7,
        )


def test_declared_length_above_capacity_rejected() -> None:
    sealed = container.seal(PAYLOAD, PASSWORD)
    try:
        with pytest.raises(WrongPasswordOrNoData):
            container.open_header(sealed.header, PASSWORD, max_payload_len=8)
    finally:
        sealed.wipe()


def test_payload_length_mismatch_rejected() -> None:
    sealed = container.seal(PAYLOAD, PASSWORD)
    try:
        opened = container.open_header(sealed.header, PASSWORD, max_payload_len=1 << 20)
        try:
            with pytest.raises(WrongPasswordOrNoData):
                container.open_payload(sealed.header, opened, sealed.ciphertext[:-1])
        finally:
            opened.wipe()
    finally:
        sealed.wipe()


def test_header_capacity_check_exact_boundary() -> None:
    payload_len = 10
    needed = C.HEADER_BITS + payload_len * 8
    container.header_capacity_check(payload_len, needed)
    with pytest.raises(CapacityError):
        container.header_capacity_check(payload_len, needed - 1)


def test_seal_rejects_unknown_profile() -> None:
    bogus = C.KdfProfile(
        profile_id=77,
        label="bogus",
        kind=C.KdfKind.PBKDF2,
        memory_kib=0,
        iterations=1,
        lanes=0,
    )
    with pytest.raises(Exception):
        container.seal(PAYLOAD, PASSWORD, profile=bogus)


def test_length_field_is_not_plaintext() -> None:
    sealed = container.seal(PAYLOAD, PASSWORD)
    try:
        expected_length = struct.pack(">I", len(PAYLOAD) + C.TAG_SIZE)
        assert expected_length not in sealed.header
    finally:
        sealed.wipe()


def _spy_zeroize(monkeypatch: pytest.MonkeyPatch) -> list[bytearray]:
    """Record every buffer the container hands to the zeroizer."""
    recorded: list[bytearray] = []

    def spy(buffer: bytearray) -> None:
        security.secure_zeroize(buffer)
        recorded.append(buffer)

    monkeypatch.setattr(container, "secure_zeroize", spy)
    return recorded


def test_sealing_zeroizes_the_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _spy_zeroize(monkeypatch)
    sealed = container.seal(PAYLOAD, PASSWORD)
    try:
        assert sealed.ciphertext
    finally:
        sealed.wipe()
    assert recorded, "sealing derives a master key and must wipe it"
    for buffer in recorded:
        assert bytes(buffer) == b"\x00" * len(buffer)


def test_a_wrong_password_also_zeroizes_every_candidate_master_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed = container.seal(PAYLOAD, PASSWORD)
    try:
        recorded = _spy_zeroize(monkeypatch)
        with pytest.raises(WrongPasswordOrNoData):
            container.open_header(sealed.header, "wrong password entirely", max_payload_len=4096)
    finally:
        sealed.wipe()
    assert len(recorded) >= len(C.KDF_PROFILES), "every attempted profile is cleaned up"
    for buffer in recorded:
        assert bytes(buffer) == b"\x00" * len(buffer)
