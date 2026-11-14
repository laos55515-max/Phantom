"""Safe filesystem primitives: atomic writes and honest secure deletion.

Two jobs:

``atomic_path`` — every output file is written to a sibling temp file, fsynced
and then ``os.replace``d, so a crash or a full disk never leaves a half-written
stego file and never destroys the original (D8).

``secure_wipe`` — one CSPRNG pass + fsync + rename + unlink, chunked, refusing
symlinks and non-regular files.  It cannot beat SSD wear levelling, copy-on-write
snapshots or journaling: the UI says exactly that (D7).
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from phantom.core import constants as C
from phantom.core.errors import OutputIsCarrierError, PhantomIOError, UnsafePathError
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress

LOGGER: Final = logging.getLogger("phantom.files")


def absolute(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def same_file(first: str, second: str) -> bool:
    """True if both paths resolve to the same filesystem object."""
    if absolute(first) == absolute(second):
        return True
    try:
        if os.path.exists(first) and os.path.exists(second):
            return os.path.samefile(first, second)
    except OSError:
        return False
    return False


def ensure_distinct(carrier: str, output: str) -> None:
    """Refuse an output path that would overwrite the carrier (D8)."""
    if same_file(carrier, output):
        raise OutputIsCarrierError(
            "output file would overwrite the carrier",
            detail=f"{absolute(carrier)}",
        )


def ensure_output_dir(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(directory):
        raise PhantomIOError("output directory does not exist", detail=directory)
    if not os.access(directory, os.W_OK):
        raise PhantomIOError("output directory is not writable", detail=directory)


def _fsync_dir(directory: str) -> None:
    """Best-effort directory fsync: durable renames on POSIX (no-op elsewhere)."""
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        LOGGER.debug("cannot open directory for fsync (%s): %s", directory, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        LOGGER.debug("directory fsync failed (%s): %s", directory, exc)
    finally:
        os.close(fd)


@contextmanager
def atomic_path(target: str, *, new_mode: int = 0o644) -> Iterator[str]:
    """Yield a temp path in the target directory; replace ``target`` on success."""
    target_abs = os.path.abspath(target)
    directory = os.path.dirname(target_abs) or "."
    ensure_output_dir(target_abs)
    token = secrets.token_hex(6)
    tmp_path = os.path.join(
        directory, f".{os.path.basename(target_abs)}.{token}" + C.ATOMIC_TEMP_SUFFIX
    )
    created = False
    try:
        fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, new_mode)
        os.close(fd)
        created = True
        yield tmp_path
        _fsync_file(tmp_path)
        os.replace(tmp_path, target_abs)
        created = False
        _fsync_dir(directory)
    except OSError as exc:
        raise PhantomIOError("cannot write output file", detail=str(exc)) from exc
    finally:
        if created or os.path.exists(tmp_path):
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


def _fsync_file(path: str) -> None:
    with open(path, "rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def write_bytes_atomic(target: str, data: bytes) -> None:
    with atomic_path(target) as tmp, open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _lstat_regular(path: str) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise PhantomIOError("cannot stat file", detail=str(exc)) from exc
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePathError("refusing to wipe a symlink", detail=path)
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePathError("refusing to wipe a non-regular file", detail=path)
    return info


def secure_wipe(
    path: str,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> int:
    """Overwrite ``path`` with CSPRNG bytes, fsync, rename and unlink it.

    Returns the number of bytes overwritten.  Raises on symlinks, directories,
    devices and FIFOs, and on any I/O error: silently swallowing a failed wipe
    would leave the user believing a file is gone when it is not (D12).
    """
    info = _lstat_regular(path)
    size = int(info.st_size)
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PhantomIOError("cannot open file for wiping", detail=str(exc)) from exc
    try:
        check_cancel(cancel)
        progress(Stage.WIPING, 0, size or 1)
        written = 0
        while written < size:
            check_cancel(cancel)
            chunk = min(C.WIPE_CHUNK_BYTES, size - written)
            try:
                os.write(fd, secrets.token_bytes(chunk))
            except OSError as exc:
                raise PhantomIOError("wipe write failed", detail=str(exc)) from exc
            written += chunk
            progress(Stage.WIPING, written, size or 1)
        try:
            if hasattr(os, "ftruncate"):
                os.ftruncate(fd, 0)
            os.fsync(fd)
        except OSError as exc:
            raise PhantomIOError("wipe fsync failed", detail=str(exc)) from exc
    finally:
        os.close(fd)

    renamed = os.path.join(directory, f".phantom-wiped-{secrets.token_hex(8)}")
    try:
        os.replace(path, renamed)
        _fsync_dir(directory)
        os.unlink(renamed)
        _fsync_dir(directory)
    except OSError as exc:
        # The data has been overwritten, but the directory entry may remain.
        raise PhantomIOError("wipe unlink failed", detail=str(exc)) from exc
    progress(Stage.WIPING, size or 1, size or 1)
    return size
