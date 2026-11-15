"""Regression tests for the audit findings: overwrite, traversal, false "verified".

Each test reproduces one concrete way the product used to lose or expose data:

* an output path that only collides with the source after the extension is forced;
* a container-supplied file name that climbs out of the output directory;
* a "successful" embed whose self-check never ran, followed by a source wipe;
* a failed write that leaves the previous file truncated instead of intact;
* extracted files that are readable by every account on the machine.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from phantom.core import api
from phantom.core import files as fs
from phantom.core.errors import (
    IntegrityError,
    InvalidInput,
    PhantomIOError,
    SourceOverwriteError,
    UnsafePathError,
)
from phantom.core.payload import (
    ExtractedPayload,
    Payload,
    PayloadKind,
    pack_payload,
    unpack_payload,
)


def _png(path: Path, size: tuple[int, int] = (160, 120)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(11)
    Image.fromarray(
        rng.integers(0, 256, (size[1], size[0], 3), dtype=np.uint8), "RGB"
    ).save(path, format="PNG")
    return path


def _hostile_blob(name_bytes: bytes, data: bytes = b"payload") -> bytes:
    """A container payload blob whose stored name is fully attacker-controlled."""
    compressed = zlib.compress(data)
    return (
        struct.pack(">BH", PayloadKind.FILE.value, len(name_bytes))
        + name_bytes
        + hashlib.sha256(data).digest()
        + struct.pack(">I", len(compressed))
        + compressed
    )


# ---------------------------------------------------------------------------
# An output must never replace the file the user is hiding
# ---------------------------------------------------------------------------
def test_extension_change_cannot_overwrite_the_secret(
    tmp_path: Path, password: str
) -> None:
    """``photo.jpg`` is written as ``photo.png`` — which is the secret's path."""
    carrier = _png(tmp_path / "carrier.png")
    secret = tmp_path / "photo.png"
    original = b"the only copy of this file"
    secret.write_bytes(original)

    with pytest.raises(SourceOverwriteError):
        api.embed_payload(
            str(carrier),
            str(tmp_path / "photo.jpg"),
            secret_path=str(secret),
            password=password,
        )

    assert secret.read_bytes() == original, "the refusal must happen before writing"


def test_output_variants_cover_every_forced_suffix() -> None:
    image = api.output_path_variants("/tmp/a.jpg", api.CarrierKind.IMAGE)
    audio = api.output_path_variants("/tmp/a.ogg", api.CarrierKind.AUDIO)
    assert image == ("/tmp/a.jpg", "/tmp/a.png")
    assert audio == ("/tmp/a.ogg", "/tmp/a.wav", "/tmp/a.flac")


def test_symlinked_output_cannot_reach_the_secret(
    tmp_path: Path, password: str
) -> None:
    """``same_file`` resolves symlinks, so a link to the secret is refused too."""
    carrier = _png(tmp_path / "carrier.png")
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"original bytes")
    link = tmp_path / "innocent.png"
    link.symlink_to(secret)

    with pytest.raises(SourceOverwriteError):
        api.embed_payload(
            str(carrier),
            str(link),
            secret_path=str(secret),
            password=password,
        )
    assert secret.read_bytes() == b"original bytes"
    assert link.is_symlink(), "the link must not be replaced either"


def test_output_equal_to_the_secret_is_refused_before_writing(
    tmp_path: Path, password: str
) -> None:
    carrier = _png(tmp_path / "carrier.png")
    secret = tmp_path / "secret.hidden"
    secret.write_bytes(b"abc")
    with pytest.raises(SourceOverwriteError):
        api.embed_payload(
            str(carrier),
            str(secret),
            secret_path=str(secret),
            password=password,
            wipe_source=True,
        )
    assert secret.read_bytes() == b"abc"


# ---------------------------------------------------------------------------
# A container-supplied name is attacker input, never a path
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("../../evil.txt", "evil.txt"),
        ("..\\..\\evil.txt", "evil.txt"),
        ("/etc/passwd", "passwd"),
        ("C:\\Windows\\System32\\drivers\\etc\\hosts", "hosts"),
        ("a\x00b.txt", "a_b.txt"),
        ("report.pdf", "report.pdf"),
    ],
)
def test_container_names_reduce_to_one_segment(stored: str, expected: str) -> None:
    assert fs.sanitise_filename(stored) == expected


@pytest.mark.parametrize("stored", ["", ".", "..", "/", "\\", "   ", ". ."])
def test_unusable_container_names_are_rejected(stored: str) -> None:
    with pytest.raises(UnsafePathError):
        fs.sanitise_filename(stored)


@pytest.mark.parametrize("stored", ["CON", "nul.txt", "com1.bin", "LPT9.dat"])
def test_windows_device_names_are_defused(stored: str) -> None:
    assert fs.sanitise_filename(stored).startswith("_")


def test_path_traversal_stays_inside_the_output_directory(tmp_path: Path) -> None:
    target = fs.safe_output_path(str(tmp_path), "../evil.txt")
    assert target == str(tmp_path.resolve() / "evil.txt")
    assert os.path.realpath(os.path.dirname(target)) == os.path.realpath(tmp_path)


def test_traversal_through_a_symlinked_destination_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"do not touch")
    output = tmp_path / "output"
    output.mkdir()
    (output / "evil.txt").symlink_to(outside / "secret.txt")

    with pytest.raises(UnsafePathError):
        fs.safe_output_path(str(output), "evil.txt")
    assert (outside / "secret.txt").read_bytes() == b"do not touch"


def test_unpacked_payload_name_is_sanitised() -> None:
    """A hostile container cannot steer the save even through the payload layer."""
    recovered = unpack_payload(_hostile_blob(b"../../evil.txt"))
    assert recovered.name == "evil.txt"
    assert recovered.data == b"payload"


@pytest.mark.parametrize("stored", [b"", b".", b"..", b"/", b"../../"])
def test_unusable_container_name_falls_back_to_a_safe_name(stored: bytes) -> None:
    """The data is intact; only the name was unusable, so the default is used."""
    recovered = unpack_payload(_hostile_blob(stored))
    assert recovered.name == fs.FALLBACK_FILE_NAME
    assert recovered.integrity_ok is True


def test_extracted_file_is_written_inside_the_chosen_directory(tmp_path: Path) -> None:
    written = fs.write_extracted_file(str(tmp_path), "../../escape.txt", b"data")
    assert Path(written) == tmp_path.resolve() / "escape.txt"
    assert Path(written).read_bytes() == b"data"
    assert not (tmp_path.parent / "escape.txt").exists()


def test_packed_name_round_trips_unchanged_for_normal_files() -> None:
    blob = pack_payload(Payload.file("report.pdf", b"data")).blob
    assert unpack_payload(blob).name == "report.pdf"


# ---------------------------------------------------------------------------
# "Verified" is a claim about work that really happened
# ---------------------------------------------------------------------------
def test_unverified_embed_never_wipes_the_source(
    tmp_path: Path, password: str
) -> None:
    carrier = _png(tmp_path / "carrier.png")
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"erase me" * 10)

    with pytest.raises(InvalidInput):
        api.embed_payload(
            str(carrier),
            str(tmp_path / "stego.png"),
            secret_path=str(secret),
            password=password,
            wipe_source=True,
            verify=False,
        )
    assert secret.read_bytes() == b"erase me" * 10


def test_verify_flag_is_reported_as_verified_false(
    tmp_path: Path, password: str
) -> None:
    carrier = _png(tmp_path / "carrier.png")
    outcome = api.embed_payload(
        str(carrier),
        str(tmp_path / "stego.png"),
        Payload.text("no self-check"),
        password=password,
        verify=False,
    )
    assert outcome.verified is False
    assert outcome.wiped_source is False


def test_verified_embed_reports_true_and_may_wipe(
    tmp_path: Path, password: str
) -> None:
    carrier = _png(tmp_path / "carrier.png")
    secret = tmp_path / "secret.bin"
    secret.write_bytes(b"erase me")
    outcome = api.embed_payload(
        str(carrier),
        str(tmp_path / "stego.png"),
        secret_path=str(secret),
        password=password,
        wipe_source=True,
    )
    assert outcome.verified is True
    assert outcome.wiped_source is True
    assert not secret.exists()


@pytest.mark.parametrize(("ok", "expected"), [(True, True), (False, False)])
def test_extract_outcome_verified_tracks_the_hash_check(
    ok: bool, expected: bool
) -> None:
    payload = ExtractedPayload(
        kind=PayloadKind.TEXT,
        name="",
        data=b"x",
        sha256_expected="a" * 64,
        sha256_actual="a" * 64 if ok else "b" * 64,
        integrity_ok=ok,
        original_size=1,
        compressed_size=1,
    )
    outcome = api.ExtractOutcome(
        payload=payload, carrier_kind=api.CarrierKind.IMAGE, duration_seconds=0.0
    )
    assert outcome.verified is expected


# ---------------------------------------------------------------------------
# A failed write keeps the old file, and secrets are owner-only
# ---------------------------------------------------------------------------
def test_failed_verification_leaves_the_previous_file_intact(tmp_path: Path) -> None:
    target = tmp_path / "report.pdf"
    target.write_bytes(b"previous content")

    with pytest.raises(IntegrityError):
        fs.write_bytes_verified(target, b"new content", expected_sha256="0" * 64)

    assert target.read_bytes() == b"previous content"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.pdf"]


def test_failed_io_leaves_the_previous_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "report.pdf"
    target.write_bytes(b"previous content")

    def broken_replace(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(fs.os, "replace", broken_replace)
    with pytest.raises(PhantomIOError):
        fs.write_bytes_verified(target, b"new content")
    assert target.read_bytes() == b"previous content"


def test_verified_write_matches_the_sealed_hash(tmp_path: Path) -> None:
    data = b"restored payload"
    target = fs.write_extracted_file(
        str(tmp_path),
        "payload.bin",
        data,
        expected_sha256=hashlib.sha256(data).hexdigest(),
    )
    assert Path(target).read_bytes() == data


def test_saved_write_keeps_an_existing_file_when_the_hash_differs(
    tmp_path: Path,
) -> None:
    target = tmp_path / "payload.bin"
    target.write_bytes(b"the user's old file")
    with pytest.raises(IntegrityError):
        fs.write_extracted_file(
            str(tmp_path), "payload.bin", b"new", expected_sha256="f" * 64
        )
    assert target.read_bytes() == b"the user's old file"


@pytest.mark.skipif(os.name != "posix", reason="chmod is only meaningful on POSIX")
def test_extracted_files_are_owner_only(tmp_path: Path) -> None:
    written = fs.write_extracted_file(str(tmp_path), "secret.bin", b"private")
    mode = stat.S_IMODE(os.stat(written).st_mode)
    assert mode == fs.EXTRACTED_FILE_MODE
    assert mode & 0o077 == 0


def test_permission_hardening_is_skipped_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows only the read-only bit exists: no chmod, no false promise."""
    calls: list[str] = []

    def record_chmod(path: str, mode: int) -> None:
        calls.append(path)

    monkeypatch.setattr(fs, "_is_posix", lambda: False)
    monkeypatch.setattr(fs.os, "chmod", record_chmod)
    written = fs.write_extracted_file(str(tmp_path), "secret.bin", b"private")
    assert calls == []
    assert Path(written).read_bytes() == b"private"


def test_permission_failure_is_reported_and_nothing_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "secret.bin"
    target.write_bytes(b"old")

    def broken_chmod(path: str, mode: int) -> None:
        raise OSError("operation not permitted")

    monkeypatch.setattr(fs.os, "chmod", broken_chmod)
    with pytest.raises(PhantomIOError):
        fs.write_extracted_file(str(tmp_path), "secret.bin", b"new")
    assert target.read_bytes() == b"old", "a rejected write must not publish"
