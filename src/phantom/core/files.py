"""Safe filesystem primitives: atomic writes and honest secure deletion.

Two jobs:

``atomic_path`` — every output file is written to a sibling temp file, fsynced
and then ``os.replace``d, so a crash or a full disk never leaves a half-written
stego file and never destroys the original (D8).

``publish_staged_exclusive`` — publish only while the name is free: ``link(2)``
where hard links exist, otherwise the platform's atomic no-replace rename
(``renameat2(RENAME_NOREPLACE)`` on Linux, ``renamex_np(RENAME_EXCL)`` on macOS,
``os.rename`` on Windows).  Two verified copies racing for one destination
produce one file and one ``EEXIST``, never a silent replacement; a folder that
offers neither primitive is refused rather than filled with a partial copy.

``staged_path``/``publish_staged`` — the same idea one level up, for the verify
step: a writer that must be *checked* before it may replace anything writes into
a staged sibling, the check runs on that file, and only a passing check publishes
it.  A rejected output therefore costs a temp file, never the file that was
already sitting at the destination.

``secure_wipe`` — one CSPRNG pass + fsync + rename + unlink, chunked, refusing
symlinks and non-regular files.  It cannot beat SSD wear levelling, copy-on-write
snapshots or journaling: the UI says exactly that (D7).
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import logging
import os
import secrets
import stat
import subprocess
import sys
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

from phantom.core import constants as C
from phantom.core.errors import (
    IntegrityError,
    OutputIsCarrierError,
    PhantomIOError,
    PublishUnavailableError,
    SourceOverwriteError,
    UnsafePathError,
    WipeError,
)
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress

LOGGER: Final = logging.getLogger("phantom.files")

#: Mode for files restored from a container: the owner reads and writes, nobody
#: else gets anything.  The payload is the user's secret, not a shared download.
EXTRACTED_FILE_MODE: Final = 0o600

#: Characters Windows refuses inside a file name; replaced, never silently dropped.
_UNSAFE_NAME_CHARS: Final = frozenset('<>:"|?*')

#: DOS device names that are still special on every modern Windows.
_RESERVED_NAMES: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)

#: Stand-in when a container stores an unusable name (or none at all).
FALLBACK_FILE_NAME: Final = "extracted.bin"

#: Built-in resources shipped with the app.  They are read-only examples and
#: must never be overwritten or deleted by any PHANTOM operation.
RESOURCES_DIR: Final = Path(__file__).resolve().parent.parent / "resources"


def _is_builtin_resource(path: str) -> bool:
    """True if ``path`` lives inside the read-only resources directory."""
    try:
        resolved = Path(path).resolve()
        resources = RESOURCES_DIR.resolve()
        return resources in resolved.parents or resolved == resources
    except (OSError, ValueError):
        return False


def absolute(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def reveal_command(
    path: str | os.PathLike[str], *, platform: str | None = None
) -> list[str] | None:
    """Command that shows ``path`` in the platform file manager, or ``None``.

    macOS          ``open -R <file>`` (``open <dir>`` for a directory)
    Windows        ``explorer /select,<file>``
    Linux/other    ``xdg-open <containing directory>``

    The path must exist: revealing a file the user has not saved yet would open
    an empty window, so the caller gets ``None`` and can say so in the UI.
    """
    target = os.path.abspath(os.fspath(path))
    if not os.path.exists(target):
        return None
    name = (platform or sys.platform).lower()
    is_directory = os.path.isdir(target)
    if name.startswith("darwin"):
        return ["open", target] if is_directory else ["open", "-R", target]
    if name.startswith("win"):
        return ["explorer", target] if is_directory else ["explorer", f"/select,{target}"]
    directory = target if is_directory else (os.path.dirname(target) or ".")
    return ["xdg-open", directory]


def reveal_in_file_manager(path: str | os.PathLike[str]) -> bool:
    """Open the platform file manager at ``path``; ``False`` when unavailable.

    Best effort by design: a headless session, a missing ``xdg-open`` or a
    locked-down desktop simply returns ``False`` and the UI reports it.  No
    shell is involved and nothing is awaited, so a slow file manager cannot
    freeze the Tk main loop.
    """
    command = reveal_command(path)
    if command is None:
        LOGGER.info("nothing to reveal: %s", path)
        return False
    try:
        subprocess.Popen(  # fixed argv, no shell, the path comes from the UI
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        LOGGER.warning("cannot open the file manager: %s", exc)
        return False
    return True


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


def ensure_output_not_source(source: str, candidates: Iterable[str]) -> None:
    """Refuse every output name that would replace the hidden source file.

    Extension forcing means one request can land on several real paths
    (``a.jpg`` becomes ``a.png``), so the caller passes them all: an output that
    overwrites the source would destroy the only copy before it is wiped.
    """
    for candidate in candidates:
        if same_file(source, candidate):
            raise SourceOverwriteError(
                "output file would overwrite the source file",
                detail=f"{absolute(source)}",
            )


def sanitise_filename(name: str) -> str:
    """Reduce a container-supplied name to one harmless path segment.

    A container is attacker-controlled input: ``name`` can be ``../../etc/passwd``,
    ``C:\\Windows\\system32\\x.dll``, an absolute POSIX path, an empty string or a
    Windows device name.  Only the last segment survives; separators are never
    interpreted, control characters and Windows-reserved characters are replaced,
    and a name that reduces to ``""``, ``"."`` or ``".."`` is rejected outright.
    """
    segment = (name or "").replace("\\", "/").split("/")[-1]
    cleaned = "".join(
        "_" if char in _UNSAFE_NAME_CHARS or not char.isprintable() else char
        for char in segment
    )
    # Windows silently drops trailing dots and spaces, which would let ".." and
    # "x. ." become something else again after the join.
    cleaned = cleaned.strip().rstrip(". ")
    if cleaned in {"", ".", ".."}:
        raise UnsafePathError(
            "refusing an empty or relative file name", detail=repr(name)
        )
    if cleaned.split(".")[0].lower() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def safe_output_path(directory: str, name: str) -> str:
    """Join ``directory`` with a sanitised name and prove the result stays there.

    The sanitised name cannot contain a separator, but the check is repeated on
    the resolved path: a pre-existing symlink at the destination must not turn a
    save into a write somewhere else entirely.
    """
    root = absolute(directory)
    target = os.path.join(root, sanitise_filename(name))
    resolved = absolute(target)
    try:
        inside = os.path.commonpath([root, resolved]) == root
    except ValueError:  # different drives on Windows
        inside = False
    if not inside or resolved == root:
        raise UnsafePathError(
            "extracted file would leave the output directory", detail=resolved
        )
    return target


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


def staged_path(target: str, *, suffix: str = C.ATOMIC_TEMP_SUFFIX) -> str:
    """A hidden sibling of ``target`` for a writer that must not publish yet.

    Same directory as ``target`` — ``os.replace`` only renames within one
    filesystem — and the target's own extension, because the stego writers force
    an extension of their own and that forcing has to survive.
    """
    target_abs = os.path.abspath(target)
    directory = os.path.dirname(target_abs) or "."
    ensure_output_dir(target_abs)
    stem, extension = os.path.splitext(os.path.basename(target_abs))
    token = secrets.token_hex(6)
    return os.path.join(directory, f".{stem}.{token}{suffix}{extension}")


def published_path(target: str, staged: str) -> str:
    """The real name ``target`` takes once ``staged`` (from :func:`staged_path`) is in.

    The writer is free to rewrite the extension — ``a.jpg`` becomes ``a.png``,
    ``a.ogg`` becomes ``a.flac`` — so the published name keeps the requested stem
    and takes the extension the writer actually produced.
    """
    target_abs = os.path.abspath(target)
    directory = os.path.dirname(target_abs) or "."
    stem = os.path.splitext(os.path.basename(target_abs))[0]
    extension = os.path.splitext(staged)[1]
    return os.path.join(directory, f"{stem}{extension}")


def publish_staged(staged: str, target: str) -> None:
    """fsync ``staged`` and rename it onto ``target``: the old file dies only now.

    Everything that can go wrong happens before the rename, which is what makes
    this safe to call after a verification: either the destination is untouched,
    or it holds the complete, checked file.
    """
    try:
        _fsync_file(staged)
        os.replace(staged, target)
    except OSError as exc:
        raise PhantomIOError("cannot publish the output file", detail=str(exc)) from exc
    _fsync_dir(os.path.dirname(os.path.abspath(target)) or ".")


#: Errors that mean "this filesystem cannot hard-link" rather than "something
#: is wrong": exFAT/SMB/FUSE mounts are the common ones.  Only those fall back to
#: an exclusive copy, which still never replaces an existing file.
_NO_HARD_LINK: Final = frozenset({errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV})


def publish_staged_exclusive(staged: str, target: str) -> None:
    """Publish ``staged`` as ``target`` only while the name is still free.

    :func:`publish_staged` uses ``os.replace``: atomic, but unconditional.  Two
    verified copies that reach the last step at the same time would each replace
    the other, and the loser would still be reported as a success.  Here the
    "is it free?" decision is made by the filesystem, in the same operation that
    makes the file visible:

    * POSIX with hard links: ``link(2)`` the staged file onto the target; it
      fails with ``EEXIST`` instead of replacing, and the target appears with
      the complete contents in one step;
    * filesystems without hard links: the platform's atomic no-replace rename —
      ``renameat2(..., RENAME_NOREPLACE)`` on Linux and
      ``renamex_np(..., RENAME_EXCL)`` on macOS — moves the verified file into
      place in one step;
    * Windows: ``os.rename``, which refuses an existing destination.

    A folder that offers none of these is refused with
    :class:`PublishUnavailableError`: the user is asked to choose another folder,
    and neither the source nor an existing file is touched.  There is no copy
    into the destination, so a partially written file can never appear under the
    final name.

    Raises the built-in ``FileExistsError`` when the destination is occupied;
    the existing file is untouched and the caller decides what to do with the
    staged copy.
    """
    directory = os.path.dirname(os.path.abspath(target)) or "."
    try:
        _fsync_file(staged)
        if os.name == "nt":
            os.rename(staged, target)
        else:
            try:
                os.link(staged, target)
            except FileExistsError:
                raise
            except OSError as exc:
                if exc.errno not in _NO_HARD_LINK:
                    raise
                if not _rename_no_replace(staged, target):
                    raise PublishUnavailableError(
                        "this folder cannot receive the copy atomically without "
                        "replacing an existing file; choose another folder",
                        detail=target,
                    ).with_params(path=target) from exc
            else:
                _unlink_staged_after_publish(staged)
    except (FileExistsError, PublishUnavailableError):
        raise
    except OSError as exc:
        raise PhantomIOError("cannot publish the output file", detail=str(exc)) from exc
    _fsync_dir(directory)


#: ``renameat2`` flags: fail instead of replacing an existing destination.
_RENAME_NOREPLACE = 1
#: ``renamex_np`` flag with the same meaning on macOS.
_RENAME_EXCL = 4
_AT_FDCWD = -100
#: ``str`` so a type checker does not narrow this dispatch to one OS and call the
#: other branches unreachable.
_PLATFORM: str = sys.platform
#: Errors that mean "this platform/filesystem does not support the operation"
#: rather than "the operation failed".
_RENAME_UNSUPPORTED: Final = frozenset(
    {
        errno.EINVAL,
        errno.ENOSYS,
        errno.EOPNOTSUPP,
        getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
    }
)


def _rename_no_replace(staged: str, target: str) -> bool:
    """Rename ``staged`` onto ``target`` atomically, refusing an existing name.

    Returns ``False`` when the platform or this filesystem has no such
    operation, so the caller can refuse instead of leaving a partially written
    copy under the final name.  Raises ``FileExistsError`` when the name is
    taken.
    """
    if _PLATFORM.startswith("linux"):
        return _linux_rename_no_replace(staged, target)
    if _PLATFORM == "darwin":
        return _macos_rename_no_replace(staged, target)
    return False


def _linux_rename_no_replace(staged: str, target: str) -> bool:
    try:
        library = ctypes.CDLL(None, use_errno=True)
    except OSError:  # pragma: no cover - no libc to talk to
        return False
    function = getattr(library, "renameat2", None)
    if function is None:  # glibc < 2.28: the symbol is not exported
        return False
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    if (
        function(
            _AT_FDCWD,
            os.fsencode(staged),
            _AT_FDCWD,
            os.fsencode(target),
            _RENAME_NOREPLACE,
        )
        == 0
    ):
        return True
    return _rename_result(target)


def _macos_rename_no_replace(staged: str, target: str) -> bool:
    try:
        library = ctypes.CDLL(None, use_errno=True)
    except OSError:  # pragma: no cover - no libc to talk to
        return False
    function = getattr(library, "renamex_np", None)
    if function is None:  # pragma: no cover - macOS always exports it
        return False
    function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(os.fsencode(staged), os.fsencode(target), _RENAME_EXCL) == 0:
        return True
    return _rename_result(target)


def _rename_result(target: str) -> bool:
    """Translate the platform rename's errno: taken, unsupported, or a real error."""
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), target)
    if error in _RENAME_UNSUPPORTED:
        return False
    raise OSError(error, os.strerror(error), target)


def _unlink_staged_after_publish(staged: str) -> None:
    try:
        os.unlink(staged)
    except OSError as exc:
        # The copy is published, but its verified bytes still sit in a hidden
        # sibling.  A WipeError says exactly that instead of a silent leak.
        raise WipeError(
            "the staged copy could not be removed after publishing", detail=str(exc)
        ) from exc


def discard_staged(staged: str) -> None:
    """Delete a staged file that will never be published.

    A failure here is a :class:`WipeError`, never a shrug: the caller is already
    reporting a failed operation, and leaving a temp file behind silently would
    keep the user's payload in a directory they believe they cleaned up.
    """
    try:
        os.unlink(staged)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WipeError(
            "the staged output could not be removed", detail=str(exc)
        ) from exc


def _fsync_file(path: str) -> None:
    with open(path, "rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def write_bytes_atomic(target: str, data: bytes) -> None:
    with atomic_path(target) as tmp, open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _is_posix() -> bool:
    """Filesystem permissions are a POSIX concept; Windows only has read-only."""
    return os.name == "posix"


def _harden_permissions(path: str, mode: int) -> None:
    """Force ``mode`` on POSIX, where ``chmod`` can actually express it."""
    if not _is_posix():
        return
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise PhantomIOError(
            "cannot restrict the permissions of the written file", detail=str(exc)
        ) from exc


def write_bytes_verified(
    target: str,
    data: bytes,
    *,
    expected_sha256: str | None = None,
    mode: int = EXTRACTED_FILE_MODE,
) -> None:
    """Write ``data`` to ``target`` atomically, then read it back and check it.

    The bytes go to a sibling temp file which is verified *before* ``os.replace``
    makes it visible, so a short write, a full disk or a hostile filesystem leaves
    whatever already lived at ``target`` exactly as it was.
    """
    with atomic_path(target, new_mode=mode) as tmp:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Permissions first, then the read-back check: the secret must never be
        # published world-readable, and a failing check must never publish at all.
        _harden_permissions(tmp, mode)
        with open(tmp, "rb") as handle:
            written = handle.read()
        if written != data or (
            expected_sha256 is not None
            and hashlib.sha256(written).hexdigest() != expected_sha256.lower()
        ):
            raise IntegrityError(
                "written file does not match the recovered data", detail=target
            )


def write_extracted_file(
    directory: str,
    name: str,
    data: bytes,
    *,
    expected_sha256: str | None = None,
) -> str:
    """Restore a payload as ``directory/name``, owner-only, verified; return the path."""
    target = safe_output_path(directory, name)
    write_bytes_verified(target, data, expected_sha256=expected_sha256)
    return target


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
    if _is_builtin_resource(path):
        raise PhantomIOError(
            "refusing to wipe a built-in resource", detail=path
        )
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
