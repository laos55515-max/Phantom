"""Filesystem safety: atomic writes (D8), honest wiping (D7, D12), path guards."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from phantom.core import files as fs
from phantom.core.errors import (
    OutputIsCarrierError,
    PhantomIOError,
    UnsafePathError,
    WipeError,
)


def test_atomic_path_replaces_target(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    target.write_bytes(b"old")
    with fs.atomic_path(str(target)) as tmp:
        Path(tmp).write_bytes(b"new")
    assert target.read_bytes() == b"new"
    assert not list(tmp_path.glob(".*phantom-tmp"))


def test_atomic_path_leaves_no_partial_file_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    with pytest.raises(RuntimeError), fs.atomic_path(str(target)) as tmp:
        Path(tmp).write_bytes(b"partial")
        raise RuntimeError("simulated crash")
    assert not target.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".")]


def test_atomic_path_reports_os_errors(tmp_path: Path) -> None:
    missing_dir = tmp_path / "missing" / "out.bin"
    with pytest.raises(PhantomIOError), fs.atomic_path(str(missing_dir)):
        pass


def test_atomic_path_keeps_original_when_replace_fails(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "out.bin"
    target.write_bytes(b"original")

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(fs.os, "replace", boom)
    with pytest.raises(PhantomIOError), fs.atomic_path(str(target)) as tmp:
        Path(tmp).write_bytes(b"new data")
    assert target.read_bytes() == b"original"


def test_write_bytes_atomic(tmp_path: Path) -> None:
    target = tmp_path / "x.bin"
    fs.write_bytes_atomic(str(target), b"hello")
    assert target.read_bytes() == b"hello"


def test_same_file_detects_identity(tmp_path: Path) -> None:
    target = tmp_path / "a.bin"
    target.write_bytes(b"x")
    assert fs.same_file(str(target), str(target))
    assert not fs.same_file(str(target), str(tmp_path / "b.bin"))


def test_ensure_distinct_rejects_carrier(tmp_path: Path) -> None:
    carrier = tmp_path / "carrier.png"
    carrier.write_bytes(b"x")
    with pytest.raises(OutputIsCarrierError):
        fs.ensure_distinct(str(carrier), str(carrier))
    with pytest.raises(OutputIsCarrierError):
        fs.ensure_distinct(str(carrier), str(tmp_path / "." / "carrier.png"))


def test_ensure_distinct_allows_other_paths(tmp_path: Path) -> None:
    carrier = tmp_path / "carrier.png"
    carrier.write_bytes(b"x")
    fs.ensure_distinct(str(carrier), str(tmp_path / "stego.png"))


def test_secure_wipe_removes_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "secret.bin"
    target.write_bytes(os.urandom(5_000))
    wiped = fs.secure_wipe(str(target))
    assert wiped == 5_000
    assert not target.exists()
    assert not [p for p in tmp_path.iterdir()]


def test_secure_wipe_overwrites_before_unlinking(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "secret.bin"
    target.write_bytes(b"A" * 1_000)
    observed: list[bytes] = []
    real_replace = os.replace

    def spy_replace(src: str, dst: str) -> None:
        observed.append(Path(src).read_bytes())
        real_replace(src, dst)

    monkeypatch.setattr(fs.os, "replace", spy_replace)
    fs.secure_wipe(str(target))
    # The random pass ran and the file was truncated before the rename.
    assert observed and observed[0] == b""


def test_secure_wipe_refuses_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim.bin"
    victim.write_bytes(b"do not touch")
    link = tmp_path / "link.bin"
    os.symlink(victim, link)
    with pytest.raises(UnsafePathError):
        fs.secure_wipe(str(link))
    assert victim.read_bytes() == b"do not touch"


def test_secure_wipe_refuses_directory(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        fs.secure_wipe(str(tmp_path))


def test_secure_wipe_refuses_fifo(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("mkfifo is not available on this platform")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(UnsafePathError):
        fs.secure_wipe(str(fifo))


def test_secure_wipe_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PhantomIOError):
        fs.secure_wipe(str(tmp_path / "nope.bin"))


def test_wipe_error_is_a_localizable_phantom_error() -> None:
    assert WipeError.code.value == "wipe_failed"
    assert WipeError("x").i18n_key == "err.wipe_failed"
