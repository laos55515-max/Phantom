"""Container, file utilities, payload packing and locale error paths."""

from __future__ import annotations

import json
import os
import zlib
from pathlib import Path

import pytest

from phantom.core import constants as C
from phantom.core import container, files, kdf, payload
from phantom.core.container import (
    OpenedHeader,
    SealedEnvelope,
    open_header,
    open_payload,
    payload_aad,
    seal,
)
from phantom.core.errors import (
    CapacityError,
    IntegrityError,
    InvalidInput,
    InvalidPassword,
    OperationCancelled,
    PhantomIOError,
    WrongPasswordOrNoData,
)
from phantom.i18n import I18n, LocaleError, available_languages, load_locale, placeholders

PROFILE = C.default_kdf_profile()
PASSWORD = "core-edges-password"


# ---------------------------------------------------------------------------
# container
# ---------------------------------------------------------------------------
def test_seal_and_open_roundtrip() -> None:
    envelope = seal(b"hello container", PASSWORD)
    assert len(envelope.header) == C.HEADER_BYTES
    opened = open_header(envelope.header, PASSWORD, max_payload_len=1024)
    assert opened.payload_len == len(envelope.ciphertext)
    assert open_payload(envelope.header, opened, envelope.ciphertext) == b"hello container"
    opened.wipe()


def test_envelopes_wipe_keys_on_context_exit() -> None:
    envelope = seal(b"context", f"{PASSWORD}-1")
    with envelope as active:
        assert isinstance(active, SealedEnvelope)
        keys = active.keys
    assert bytes(keys.payload_key) == b"\x00" * len(keys.payload_key)

    opened = open_header(envelope.header, f"{PASSWORD}-1", max_payload_len=1024)
    with opened as active_open:
        assert isinstance(active_open, OpenedHeader)
        inner = active_open.keys
    assert bytes(inner.position_key) == b"\x00" * len(inner.position_key)


def test_seal_validates_inputs() -> None:
    from dataclasses import replace

    unknown = replace(PROFILE, profile_id=99, label="unknown")
    with pytest.raises(InvalidInput):
        seal(b"x", PASSWORD, profile=unknown)
    with pytest.raises(InvalidInput):
        seal("text", PASSWORD)  # type: ignore[arg-type]


def test_seal_accepts_bytearray() -> None:
    envelope = seal(bytearray(b"bytes-like"), f"{PASSWORD}-2")
    opened = open_header(envelope.header, f"{PASSWORD}-2", max_payload_len=64)
    assert open_payload(envelope.header, opened, envelope.ciphertext) == b"bytes-like"


def test_seal_wipes_keys_when_encryption_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[object] = []

    class ExplodingAESGCM:
        def __init__(self, key: bytes) -> None:
            captured.append(key)
            raise RuntimeError("hardware failure")

    monkeypatch.setattr(container, "AESGCM", ExplodingAESGCM)
    with pytest.raises(RuntimeError):
        seal(b"data", PASSWORD)
    assert captured


def test_open_header_validates_lengths() -> None:
    with pytest.raises(WrongPasswordOrNoData):
        open_header(b"short", PASSWORD, max_payload_len=10)
    envelope = seal(b"x", PASSWORD)
    with pytest.raises(InvalidInput):
        open_header(envelope.header, PASSWORD, max_payload_len=-1)


def test_open_header_rejects_declared_length_over_capacity() -> None:
    envelope = seal(b"payload" * 100, f"{PASSWORD}-3")
    with pytest.raises(WrongPasswordOrNoData):
        open_header(envelope.header, f"{PASSWORD}-3", max_payload_len=8)


def test_open_header_rejects_an_empty_payload() -> None:
    envelope = seal(b"", f"{PASSWORD}-4")
    with pytest.raises(WrongPasswordOrNoData):
        open_header(envelope.header, f"{PASSWORD}-4", max_payload_len=1024)


def test_open_payload_validates_length_and_prefix() -> None:
    envelope = seal(b"\x00starts-with-null", f"{PASSWORD}-5")
    opened = open_header(envelope.header, f"{PASSWORD}-5", max_payload_len=1024)
    with pytest.raises(WrongPasswordOrNoData):
        open_payload(envelope.header, opened, envelope.ciphertext[:-1])
    with pytest.raises(Exception) as info:
        open_payload(envelope.header, opened, envelope.ciphertext)
    assert "type byte" in str(info.value)


def test_open_payload_rejects_a_tampered_header() -> None:
    envelope = seal(b"bound to header", f"{PASSWORD}-6")
    opened = open_header(envelope.header, f"{PASSWORD}-6", max_payload_len=1024)
    tampered = bytearray(envelope.header)
    tampered[0] ^= 0x01
    with pytest.raises(WrongPasswordOrNoData):
        open_payload(bytes(tampered), opened, envelope.ciphertext)


def test_aad_binds_version_and_profile() -> None:
    first = payload_aad(b"header", 2, 1)
    assert first != payload_aad(b"header", 3, 1)
    assert first != payload_aad(b"header", 2, 2)
    assert first.startswith(C.PAYLOAD_AAD_PREFIX)


def test_header_capacity_check() -> None:
    container.header_capacity_check(0, C.HEADER_BITS)
    container.header_capacity_check(1, C.HEADER_BITS + 8)
    with pytest.raises(CapacityError):
        container.header_capacity_check(1, C.HEADER_BITS)


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------
def test_same_file_compares_paths_and_inodes(tmp_path: Path) -> None:
    first = tmp_path / "a.bin"
    first.write_bytes(b"x")
    link = tmp_path / "link.bin"
    os.link(first, link)
    assert files.same_file(str(first), str(first))
    assert files.same_file(str(first), str(link))
    assert not files.same_file(str(first), str(tmp_path / "b.bin"))


def test_same_file_survives_samefile_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"x")
    second.write_bytes(b"y")

    def broken(path_a: str, path_b: str) -> bool:
        raise OSError("no such device")

    monkeypatch.setattr(os.path, "samefile", broken)
    assert files.same_file(str(first), str(second)) is False


def test_ensure_distinct_and_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "out.png"
    with pytest.raises(Exception):
        files.ensure_distinct(str(target), str(target))
    with pytest.raises(PhantomIOError):
        files.ensure_output_dir(str(tmp_path / "missing" / "x.png"))

    def not_writable(path: str, mode: int) -> bool:
        return False

    monkeypatch.setattr(os, "access", not_writable)
    with pytest.raises(PhantomIOError):
        files.ensure_output_dir(str(target))


def test_atomic_path_replaces_the_target(tmp_path: Path) -> None:
    target = tmp_path / "atomic.txt"
    with files.atomic_path(str(target)) as temp:
        Path(temp).write_text("new", encoding="utf-8")
        assert not target.exists(), "the target must appear only after the context"
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_path_cleans_up_on_error(tmp_path: Path) -> None:
    target = tmp_path / "atomic.txt"
    with pytest.raises(RuntimeError):
        with files.atomic_path(str(target)):
            raise RuntimeError("writer failed")
    assert not target.exists()
    assert [entry.name for entry in tmp_path.iterdir()] == []


def test_fsync_dir_is_safe_on_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_open(path: str, flags: int) -> int:
        raise OSError("cannot open")

    monkeypatch.setattr(os, "open", broken_open)
    files._fsync_dir(str(tmp_path))  # no exception, just a debug log


def test_fsync_dir_handles_fsync_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_fsync(fd: int) -> None:
        raise OSError("fsync unsupported")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    files._fsync_dir(str(tmp_path))


def test_fsync_dir_is_a_noop_off_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files.os, "name", "nt")
    files._fsync_dir(str(tmp_path))


def test_secure_wipe_overwrites_and_removes(tmp_path: Path) -> None:
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"top secret" * 1_000)
    seen: list[tuple[str, int, int]] = []
    size = files.secure_wipe(
        str(secret), progress=lambda stage, done, total: seen.append((stage.value, done, total))
    )
    assert size == 10_000
    assert not secret.exists()
    assert seen and seen[-1][0] == "wiping"


def test_secure_wipe_refuses_directories(tmp_path: Path) -> None:
    from phantom.core.errors import UnsafePathError

    with pytest.raises(UnsafePathError):
        files.secure_wipe(str(tmp_path))


def test_secure_wipe_honours_cancellation(tmp_path: Path) -> None:
    import threading

    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"data")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelled):
        files.secure_wipe(str(secret), cancel=cancel)
    assert secret.exists(), "a cancelled wipe must not delete the file"


def test_secure_wipe_reports_open_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"data")

    def broken_open(path: str, flags: int) -> int:
        raise OSError("permission denied")

    monkeypatch.setattr(os, "open", broken_open)
    with pytest.raises(PhantomIOError):
        files.secure_wipe(str(secret))


def test_secure_wipe_reports_write_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"data")

    def broken_write(fd: int, data: bytes) -> int:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "write", broken_write)
    with pytest.raises(PhantomIOError):
        files.secure_wipe(str(secret))


def test_secure_wipe_reports_fsync_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"data")

    def broken_fsync(fd: int) -> None:
        raise OSError("fsync refused")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    with pytest.raises(PhantomIOError):
        files.secure_wipe(str(secret))


def test_secure_wipe_reports_unlink_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"data")

    def broken_replace(source: str, target: str) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(PhantomIOError):
        files.secure_wipe(str(secret))


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------
def test_iterators(tmp_path: Path) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(bytes(range(256)) * 10)
    chunks = list(payload.iter_file_chunks(str(path), chunk=1000))
    assert b"".join(chunks) == path.read_bytes()
    assert len(chunks) == 3
    assert list(payload.iter_bytes_chunks(b"abc", chunk=2)) == [b"ab", b"c"]


def test_iter_file_chunks_reports_errors(tmp_path: Path) -> None:
    with pytest.raises(PhantomIOError):
        list(payload.iter_file_chunks(str(tmp_path / "missing.bin")))


def test_pack_rejects_long_names() -> None:
    long_name = "x" * (C.MAX_FILENAME_BYTES + 1)
    with pytest.raises(InvalidInput):
        payload.pack_payload(payload.Payload.file(long_name, b"data"))
    with pytest.raises(InvalidInput):
        payload.Payload.file(long_name, b"data")


def test_pack_payload_enforces_capacity() -> None:
    data = payload.Payload.text("x" * 500)
    with pytest.raises(CapacityError):
        payload.pack_payload(data, max_payload_bytes=10)


def test_pack_file_roundtrip_and_limits(tmp_path: Path) -> None:
    source = tmp_path / "note.txt"
    source.write_bytes(b"file body" * 100)
    packed = payload.pack_file(str(source))
    extracted = payload.unpack_payload(packed.blob)
    assert extracted.data == b"file body" * 100
    assert extracted.name == "note.txt"
    assert extracted.integrity_ok is True
    with pytest.raises(CapacityError):
        payload.pack_file(str(source), max_payload_bytes=8)


def test_pack_file_reports_missing_source(tmp_path: Path) -> None:
    with pytest.raises(PhantomIOError):
        payload.pack_file(str(tmp_path / "missing.bin"))


def test_pack_file_rejects_long_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "short.bin"
    source.write_bytes(b"data")
    long_name = "n" * (C.MAX_FILENAME_BYTES + 1)
    monkeypatch.setattr(payload.os.path, "basename", lambda _path: long_name)
    with pytest.raises(InvalidInput):
        payload.pack_file(str(source))


def test_estimate_size_and_file(tmp_path: Path) -> None:
    text = payload.Payload.text("estimate me")
    estimate = payload.estimate_size(text)
    assert estimate.payload_bytes > 0
    assert estimate.exceeded_capacity is False
    assert payload.estimate_size(text, capacity_bytes=4).exceeded_capacity is True

    source = tmp_path / "big.bin"
    source.write_bytes(os.urandom(4_000))
    file_estimate = payload.estimate_file_size(str(source), capacity_bytes=1_000)
    assert file_estimate.exceeded_capacity is True
    with pytest.raises(PhantomIOError):
        payload.estimate_file_size(str(tmp_path / "missing.bin"))


def test_estimate_file_matches_pack(tmp_path: Path) -> None:
    source = tmp_path / "small.txt"
    source.write_bytes(b"compress me " * 50)
    packed = payload.pack_file(str(source))
    estimate = payload.estimate_file_size(str(source))
    assert estimate.payload_bytes == len(packed.blob)
    assert estimate.exceeded_capacity is False


def test_unpack_rejects_broken_streams(tmp_path: Path) -> None:
    packed = payload.pack_payload(payload.Payload.text("round trip"))
    with pytest.raises(IntegrityError):
        payload.unpack_payload(packed.blob[:-4] + b"\x00\x00\x00\x00")
    truncated = packed.blob[:-6]
    with pytest.raises(IntegrityError):
        payload.unpack_payload(truncated)
    with pytest.raises(IntegrityError):
        payload.unpack_payload(b"")


def test_unpack_rejects_decompression_bombs() -> None:
    import hashlib

    raw = b"a" * 4096
    blob = (
        payload._HEADER.pack(payload.PayloadKind.TEXT.value, 0)
        + hashlib.sha256(raw).digest()
        + payload._LEN.pack(len(zlib.compress(raw)))
        + zlib.compress(raw)
    )
    with pytest.raises(CapacityError):
        payload.unpack_payload(blob, max_plaintext=100)


def test_unpack_verifies_the_digest() -> None:
    packed = payload.pack_payload(payload.Payload.text("digest"))
    corrupted = bytearray(packed.blob)
    corrupted[20] ^= 0xFF
    with pytest.raises(IntegrityError):
        payload.unpack_payload(bytes(corrupted))


def test_payload_capacity_for_never_goes_negative() -> None:
    assert payload.payload_capacity_for(0) == 0
    assert payload.payload_capacity_for(1_000, "name.bin") < 1_000


# ---------------------------------------------------------------------------
# kdf helpers
# ---------------------------------------------------------------------------
def test_password_variants_order() -> None:
    variants = kdf.password_variants("Пароль-ї")
    assert variants[0] == "Пароль-ї"
    assert variants == tuple(dict.fromkeys(variants))
    assert kdf.password_variants(123)[0] == "123"  # type: ignore[arg-type]


def test_validate_and_normalize_password() -> None:
    with pytest.raises(InvalidPassword):
        kdf.validate_password(5)  # type: ignore[arg-type]
    with pytest.raises(InvalidPassword):
        kdf.validate_password("")
    with pytest.raises(InvalidPassword):
        kdf.normalize_password(None)  # type: ignore[arg-type]
    assert kdf.normalize_password("e\u0301") == "\u00e9"


def test_hkdf_separation_and_message_keys() -> None:
    master = b"m" * 32
    first = kdf.hkdf(master, b"label-a", length=32, salt=b"salt-123")
    second = kdf.hkdf(master, b"label-b", length=32, salt=b"salt-123")
    assert first != second
    keys = kdf.derive_message_keys(master, b"salt-123")
    assert len(keys.payload_key) == C.AES_KEY_SIZE
    assert len(keys.payload_nonce) == C.NONCE_SIZE
    assert bytes(keys.length_key) != bytes(keys.payload_key)
    with keys as active:
        assert active is keys
    assert bytes(keys.length_key) == b"\x00" * C.AES_KEY_SIZE


def test_reserved_added_gives_different_keys() -> None:
    master = b"m" * 32
    plain = kdf.derive_message_keys(master, b"salt-123")
    extended = kdf.derive_message_keys(master, b"salt-123", reserved_added=b"-v2")
    assert bytes(plain.payload_key) != bytes(extended.payload_key)


def test_wipe_all_zeroes_everything() -> None:
    first = bytearray(b"secret")
    second = bytearray(b"other")
    kdf.wipe_all(first, second)
    assert first == bytearray(6) and second == bytearray(5)


# ---------------------------------------------------------------------------
# i18n error paths
# ---------------------------------------------------------------------------
def test_available_languages_requires_a_directory(tmp_path: Path) -> None:
    with pytest.raises(LocaleError):
        available_languages(tmp_path / "missing")


def test_locale_directory_without_json(tmp_path: Path) -> None:
    with pytest.raises(LocaleError):
        I18n("en", locales_dir=tmp_path)


def test_locale_entries_must_be_strings(tmp_path: Path) -> None:
    (tmp_path / "en.json").write_text(json.dumps({"key": 5}), encoding="utf-8")
    with pytest.raises(LocaleError):
        load_locale("en", tmp_path)


def test_locale_must_be_an_object(tmp_path: Path) -> None:
    (tmp_path / "en.json").write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(LocaleError):
        I18n("en", locales_dir=tmp_path)


def test_i18n_introspection(tmp_path: Path) -> None:
    (tmp_path / "en.json").write_text(
        json.dumps({"a": "A", "with_param": "B {n}", "lang.en": "English"}), encoding="utf-8"
    )
    (tmp_path / "uk.json").write_text(
        json.dumps({"a": "А", "extra": "Е", "with_param": "Б {n} {m}", "lang.uk": "Українська"}),
        encoding="utf-8",
    )
    i18n = I18n("uk", locales_dir=tmp_path)
    assert i18n.languages == ("en", "uk")
    assert i18n.has("a") is True
    assert "a" in i18n.keys("uk")
    assert "with_param" in i18n.keys("en")
    assert list(i18n.missing_keys("en")) == []
    assert list(i18n.missing_keys("uk")) == ["lang.en"]
    assert list(i18n.extra_keys("uk")) == ["extra", "lang.uk"]
    assert list(i18n.extra_keys("en")) == []
    assert dict(i18n.placeholder_mismatches("uk")) == {"with_param": ("B {n}", "Б {n} {m}")}
    assert i18n.t("with_param", n=1) == "Б 1 {m}"
    assert i18n.t("a") == "А"
    assert i18n.language_name() == "Українська"


def test_i18n_unknown_language_uses_first_available(tmp_path: Path) -> None:
    (tmp_path / "de.json").write_text(json.dumps({"lang.de": "Deutsch"}), encoding="utf-8")
    i18n = I18n("zz", locales_dir=tmp_path)
    assert i18n.language == "de"
    assert i18n.default_language == "de"


def test_i18n_formatting_failures_are_contained() -> None:
    i18n = I18n("en")
    assert i18n.t("lbl_capacity_bits", **{"bad key": 1}) == i18n.t("lbl_capacity_bits")


def test_placeholders_helper() -> None:
    assert placeholders("a {one} b {two}") == {"one", "two"}
    assert placeholders("no params") == set()


# ---------------------------------------------------------------------------
# error objects
# ---------------------------------------------------------------------------
def test_error_str_and_repr() -> None:
    with_detail = CapacityError("does not fit", detail="12 > 8")
    assert str(with_detail) == "does not fit (12 > 8)"
    plain = CapacityError("plain failure")
    assert str(plain) == "plain failure"
    empty = CapacityError("")
    assert str(empty) == CapacityError.code.value
    assert "CapacityError" in repr(with_detail)
    assert with_detail.i18n_key == f"err.{CapacityError.code.value}"
    assert with_detail.with_params(needed=12) is with_detail
    assert with_detail.params == {"needed": 12}


def test_kdf_summary_for_both_profiles() -> None:
    argon = C.kdf_summary(C.default_kdf_profile())
    assert argon.startswith("Argon2id")
    pbkdf2 = C.kdf_summary(C.KDF_PROFILES[C.KDF_PROFILE_PBKDF2_SHA256])
    assert pbkdf2.startswith("PBKDF2-HMAC-SHA256")
    assert f"{C.PBKDF2_ITERATIONS:,}" in pbkdf2


def test_i18n_survives_a_broken_template(tmp_path: Path) -> None:
    (tmp_path / "en.json").write_text(json.dumps({"bad": "value {broken"}), encoding="utf-8")
    i18n = I18n("en", locales_dir=tmp_path)
    assert i18n.t("bad", x=1) == "value {broken"
    assert i18n.t("bad") == "value {broken"
