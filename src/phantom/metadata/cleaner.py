"""Selective removal of personal metadata, with the original never touched.

The order of operations is the safety property:

1. read the source and inspect it (nothing is written yet);
2. refuse outright if the file carries a content credential, because rewriting it
   would leave a manifest that no longer matches the file;
3. build a plan — what goes, what stays, what the user asked for but cannot be
   removed — and show it before writing anything;
4. write the result to a staged sibling, not to the destination;
5. decode both files and compare the pixels or the PCM samples;
6. re-scan the staged file and confirm that the selected fields are gone and the
   protected ones are still there;
7. publish the staged file with one conditional operation — a rename when the
   caller explicitly asked to replace that name, a create/link that cannot
   replace anything otherwise — or discard it.

A failure at any step leaves the destination exactly as it was, which is the only
version of this feature worth shipping.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass, replace
from typing import Final

from phantom.core import files as fsutil
from phantom.core.errors import (
    MetadataConflictError,
    MetadataError,
    OutputExistsError,
    PhantomError,
    SignedMediaError,
    SourceOverwriteError,
    UnsupportedFormat,
)
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress
from phantom.metadata import flac as flac_mod
from phantom.metadata import inspector
from phantom.metadata import png as png_mod
from phantom.metadata import wav as wav_mod
from phantom.metadata.integrity import (
    ContentComparison,
    compare,
    fingerprint,
    png_checksums_ok,
)
from phantom.metadata.models import (
    MediaFormat,
    MetadataField,
    MetadataReport,
)

LOGGER: Final = logging.getLogger("phantom.metadata.cleaner")

#: ``flac.picture.2``, ``flac.application.0``: keys that shift when a sibling goes.
_INDEXED_KEY: Final = re.compile(r"\.\d+$")

#: Fields whose value is a structured block: removing them is all-or-nothing.
BLOCK_LEVEL_REASON: Final = "removed as a whole block"


@dataclass(frozen=True, slots=True)
class CleaningPlan:
    """What will happen to the file, ready to be shown before anything is written."""

    remove: tuple[MetadataField, ...] = ()
    keep: tuple[MetadataField, ...] = ()
    blocked: tuple[MetadataField, ...] = ()
    warnings: tuple[str, ...] = ()
    allowed: bool = True
    reason: str = ""

    @property
    def nothing_selected(self) -> bool:
        return not self.remove

    @property
    def remove_keys(self) -> frozenset[str]:
        return frozenset(item.key for item in self.remove)

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "remove": [item.to_dict() for item in self.remove],
            "keep": [item.to_dict() for item in self.keep],
            "blocked": [item.to_dict() for item in self.blocked],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class CleaningOutcome:
    """Everything the integrity card reports after a successful run."""

    source: str
    output: str
    removed: tuple[MetadataField, ...]
    kept: tuple[MetadataField, ...]
    comparison: ContentComparison
    source_sha256: str
    output_sha256: str
    report_before: MetadataReport
    report_after: MetadataReport
    checksum_ok: bool | None = None
    warnings: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return (
            self.comparison.identical
            and self.comparison.checked
            and self.checksum_ok is not False
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "output": self.output,
            "removed": [item.to_dict() for item in self.removed],
            "kept": [item.to_dict() for item in self.kept],
            "verified": self.verified,
            "checksum_ok": self.checksum_ok,
            "source_sha256": self.source_sha256,
            "output_sha256": self.output_sha256,
            "content": self.comparison.to_dict(),
            "warnings": list(self.warnings),
        }


def plan(report: MetadataReport, selection: frozenset[str]) -> CleaningPlan:
    """Decide what a selection means for this report, without touching the file."""
    if report.provenance.detected:
        return CleaningPlan(
            keep=report.fields,
            allowed=False,
            reason="signed_media",
            warnings=report.warnings,
        )
    remove: list[MetadataField] = []
    keep: list[MetadataField] = []
    blocked: list[MetadataField] = []
    for item in report.sorted_fields():
        if item.derived:
            # A view of other fields (the decimal position, the properties of an
            # XMP packet).  It is not an entry: it cannot be selected, it is not
            # counted, and it is not something the rewrite has to keep — removing
            # the fields it is made of removes it, which used to read as "a
            # protected field disappeared".
            continue
        if item.key in selection:
            if item.removable:
                remove.append(item)
            else:
                blocked.append(item)
                keep.append(item)
        else:
            keep.append(item)
    warnings = list(report.warnings)
    blocked_personal = [item for item in blocked if item.sensitive]
    if blocked_personal:
        warnings.append(
            f"{len(blocked_personal)} personal field(s) cannot be removed automatically"
        )
    return CleaningPlan(
        remove=tuple(remove),
        keep=tuple(keep),
        blocked=tuple(blocked),
        warnings=tuple(warnings),
        allowed=True,
        reason="",
    )


def default_selection(report: MetadataReport) -> frozenset[str]:
    """Personal fields that the cleaner is able to remove — the safe default."""
    return frozenset(item.key for item in report.fields if item.sensitive and item.removable)


def _rewrite(data: bytes, fmt: MediaFormat, keys: frozenset[str]) -> bytes:
    if fmt is MediaFormat.PNG:
        return png_mod.rewrite_png(data, png_mod.removal_from_keys(keys))
    if fmt is MediaFormat.WAV:
        return wav_mod.rewrite_wav(data, wav_mod.removal_from_keys(keys))
    return flac_mod.rewrite_flac(data, flac_mod.removal_from_keys(keys))


def _same_file(left: str, right: str) -> bool:
    """Identity of two paths, including symlinks and hard links.

    ``abspath`` catches the plain "same name" case, ``realpath`` catches a
    symlinked parent or a symlink that does not exist yet, and ``samefile``
    catches hard links and a symlink that does exist.  Checking only the string
    the user typed missed all of them.
    """
    if os.path.abspath(left) == os.path.abspath(right):
        return True
    if os.path.realpath(left) == os.path.realpath(right):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def final_output_path(output: str, fmt: MediaFormat) -> str:
    """The name the copy really gets: the chosen one, with the real format's extension.

    ``photo.wav`` for a PNG is not a destination the cleaner can honour, and
    silently writing ``photo.png`` instead made every check below look at the
    wrong file.  The extension is therefore decided once, here, before anything
    is compared or written.
    """
    stem, extension = os.path.splitext(output)
    if extension.lower() == fmt.extension:
        return output
    return f"{stem}{fmt.extension}"


def resolve_output(
    source: str, output: str, fmt: MediaFormat, *, overwrite: bool = False
) -> str:
    """The exact path :func:`clean` will write, or a typed refusal.

    Both questions are asked about the *final* path, after the extension has been
    replaced by the one the detected format uses: is it the source (directly, via
    a symlink, or via a hard link), and does something already live there.

    ``overwrite`` only covers the name the caller asked for.  When the extension
    had to be rewritten (``existing.wav`` → ``existing.png``), the caller has
    never been shown the file that would be replaced, so it is refused even with
    ``overwrite=True``.
    """
    final = final_output_path(output, fmt)
    if _same_file(source, final):
        raise SourceOverwriteError(
            "the cleaned copy must not replace the original", detail=final
        ).with_params(source=source, output=final)
    # A directory is not a file we may publish onto, and an extension rewrite
    # must not quietly turn "occupied/" into "occupied.png".
    for candidate in dict.fromkeys((output, final)):
        if os.path.isdir(candidate):
            raise OutputExistsError(
                "the destination is a directory", detail=candidate
            ).with_params(path=candidate)
    if os.path.lexists(final) and (final != output or not overwrite):
        raise OutputExistsError(
            "the destination already exists", detail=final
        ).with_params(path=final)
    return final


def output_path(source: str, output: str, *, overwrite: bool = False) -> str:
    """Answer where a clean would write, without writing: format first, then path."""
    fmt = inspector.detect_format(inspector.read_head(source))
    if fmt is None:
        raise UnsupportedFormat("only PNG, WAV and FLAC containers are cleaned")
    return resolve_output(source, output, fmt, overwrite=overwrite)


def clean(
    source: str,
    output: str,
    selection: frozenset[str],
    *,
    overwrite: bool = False,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> CleaningOutcome:
    """Remove the selected fields into ``output``; the source is only ever read."""
    progress(Stage.SCANNING, 0, 4)
    data = inspector.read_file(source, cancel=cancel)
    fmt = inspector.detect_format(data[:16])
    if fmt is None:
        raise UnsupportedFormat("only PNG, WAV and FLAC containers are cleaned")
    # Resolution happens after the format is known: checking the path the user
    # typed (``photo.wav``) is not checking the path that would be written
    # (``photo.png``), and that difference used to destroy the source.
    requested = output
    output = resolve_output(source, output, fmt, overwrite=overwrite)
    # Replacing is allowed only for the exact name the caller asked to replace;
    # an extension-rewritten destination is published exclusively.
    may_replace = overwrite and output == requested
    before = inspector.report_from_bytes(source, data, fmt)
    progress(Stage.SCANNING, 1, 4)
    if before.provenance.detected:
        raise SignedMediaError(
            "a content credential is attached; it cannot survive a rewrite",
            detail=before.provenance.state.value,
        ).with_params(state=before.provenance.state.value)
    decision = plan(before, selection)
    if decision.nothing_selected:
        raise MetadataError("no removable field was selected")
    check_cancel(cancel)
    progress(Stage.CLEANING, 2, 4)
    cleaned = _rewrite(data, fmt, decision.remove_keys)
    after = inspector.report_from_bytes(source, cleaned, fmt)
    _confirm_removal(decision, after)
    comparison = compare(fingerprint(data, fmt), fingerprint(cleaned, fmt))
    if not comparison.identical:
        raise MetadataError(
            "the decoded content changed; the cleaned copy was not published",
            detail="; ".join(comparison.differences),
        )
    checksum_ok: bool | None = None
    if fmt is MediaFormat.PNG:
        checksum_ok = png_checksums_ok(cleaned)
        if checksum_ok is False:
            raise MetadataError("the rewritten PNG fails its own CRC check")
    check_cancel(cancel)
    progress(Stage.VERIFYING, 3, 4)
    # Ask again immediately before publishing: a file created while the copy was
    # being verified must not be replaced, and a hard link created meanwhile must
    # not turn the destination into the source.  Even this check can lose a race,
    # so the publication itself is conditional (see ``_publish``).
    output = resolve_output(source, output, fmt, overwrite=overwrite)
    published = _publish(output, cleaned, replace=may_replace)
    # ``after`` describes the bytes that now live in ``published``: reporting it
    # under the source name made "analysis" and "file" name different files in the
    # same card, and the user could not tell which one carried the numbers.
    after = replace(after, path=published)
    progress(Stage.WRITING, 4, 4)
    return CleaningOutcome(
        source=source,
        output=published,
        removed=decision.remove,
        kept=decision.keep,
        comparison=comparison,
        source_sha256=before.sha256,
        output_sha256=after.sha256,
        report_before=before,
        report_after=after,
        checksum_ok=checksum_ok,
        warnings=before.warnings,
    )


def _stable_key(key: str) -> bool:
    """Keys that cannot be renumbered by a removal: no occurrence counter, no index.

    Removing the second ``flac.picture`` renumbers the third one, and removing an
    ID3 frame shifts ``wav.id3.…[#n]``.  Those keys are compared by count instead,
    so a rewrite that quietly drops a protected block is still caught.
    """
    return "#" not in key and not _INDEXED_KEY.search(key)


def _confirm_removal(decision: CleaningPlan, after: MetadataReport) -> None:
    """Prove the rewrite did exactly what the plan said, and nothing else."""
    # Pairs, not keys: removing one of two same-named chunks renumbers the other,
    # so only a field with both the same name and the same value is the same field.
    remaining = {(item.key, item.value) for item in after.fields if not item.derived}
    still_there = [
        item.key for item in decision.remove if (item.key, item.value) in remaining
    ]
    if still_there:
        raise MetadataError(
            "a selected field survived the rewrite",
            detail=", ".join(sorted(still_there)[:6]),
        )
    lost = [
        item
        for item in decision.keep
        if _stable_key(item.key) and (item.key, item.value) not in remaining
    ]
    if lost:
        # Its own code and its own sentence: "metadata error: png.gps.0x0000" told
        # the user nothing about what happened to the file, and this is a bug in
        # the policy, not something they did wrong.
        raise MetadataConflictError(
            "the rewrite dropped a field the plan promised to keep",
            detail=", ".join(sorted(item.key for item in lost)[:6]),
        ).with_params(fields=", ".join(item.label for item in lost[:4]))
    # Every kept field must still be there and nothing else: renumbering may
    # change keys, but it never changes how many fields a correct rewrite leaves.
    expected = len(decision.keep)
    found = len([item for item in after.fields if not item.derived])
    if found != expected:
        raise MetadataError(
            "the rewrite produced a different number of fields",
            detail=f"expected {expected}, found {found}",
        )


def _publish(output: str, cleaned: bytes, *, replace: bool) -> str:
    """Write the staged file, then publish it — or leave nothing behind.

    With ``replace=False`` the publication is conditional at the filesystem
    level, so two cleans that reach this point together cannot both succeed and
    neither can replace a file that appeared while it was verifying.
    """
    staged = fsutil.staged_path(output, suffix=".metadata")
    published = fsutil.published_path(output, staged)
    try:
        with open(staged, "wb") as handle:
            handle.write(cleaned)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            fsutil.publish_staged(staged, published)
        else:
            fsutil.publish_staged_exclusive(staged, published)
        return published
    except FileExistsError as exc:
        _discard_quietly(staged)
        raise OutputExistsError(
            "the destination already exists", detail=published
        ).with_params(path=published) from exc
    except PhantomError:
        _discard_quietly(staged)
        raise
    except OSError as exc:
        _discard_quietly(staged)
        raise MetadataError("the cleaned copy could not be written", detail=str(exc)) from exc


def _discard_quietly(staged: str) -> None:
    try:
        fsutil.discard_staged(staged)
    except PhantomError as exc:
        # The caller is already raising the error that matters; a temp file that
        # could not be removed is logged, never silently dropped.
        LOGGER.warning("cannot remove the staged copy %s: %s", staged, exc)


