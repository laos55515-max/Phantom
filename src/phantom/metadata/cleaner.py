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
7. publish the staged file onto the destination with one rename, or discard it.

A failure at any step leaves the destination exactly as it was, which is the only
version of this feature worth shipping.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Final

from phantom.core import files as fsutil
from phantom.core.errors import (
    MetadataError,
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
    if os.path.abspath(left) == os.path.abspath(right):
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


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
    if _same_file(source, output):
        raise SourceOverwriteError(
            "the cleaned copy must not replace the original", detail=output
        )
    output = _natural_extension(output)
    if os.path.exists(output) and not overwrite:
        raise MetadataError("the output file already exists", detail=output)
    progress(Stage.SCANNING, 0, 4)
    data = inspector.read_file(source, cancel=cancel)
    fmt = inspector.detect_format(data[:16])
    if fmt is None:
        raise UnsupportedFormat("only PNG, WAV and FLAC containers are cleaned")
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
    output = _with_extension(output, fmt)
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
    published = _publish(output, cleaned)
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
    remaining = {(item.key, item.value) for item in after.fields}
    still_there = [
        item.key for item in decision.remove if (item.key, item.value) in remaining
    ]
    if still_there:
        raise MetadataError(
            "a selected field survived the rewrite",
            detail=", ".join(sorted(still_there)[:6]),
        )
    lost = [
        item.key
        for item in decision.keep
        if _stable_key(item.key) and (item.key, item.value) not in remaining
    ]
    if lost:
        raise MetadataError(
            "a protected field disappeared in the rewrite",
            detail=", ".join(sorted(lost)[:6]),
        )
    # Every kept field must still be there and nothing else: renumbering may
    # change keys, but it never changes how many fields a correct rewrite leaves.
    expected = len(decision.keep)
    if len(after.fields) != expected:
        raise MetadataError(
            "the rewrite produced a different number of fields",
            detail=f"expected {expected}, found {len(after.fields)}",
        )


def _publish(output: str, cleaned: bytes) -> str:
    """Write the staged file, then rename it into place — or leave nothing behind."""
    staged = fsutil.staged_path(output, suffix=".metadata")
    try:
        with open(staged, "wb") as handle:
            handle.write(cleaned)
            handle.flush()
            os.fsync(handle.fileno())
        published = fsutil.published_path(output, staged)
        fsutil.publish_staged(staged, published)
        return published
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


def _with_extension(path: str, fmt: MediaFormat) -> str:
    """The output path with the extension of the real format, not of the file name."""
    stem, extension = os.path.splitext(path)
    if extension.lower() == fmt.extension:
        return path
    return f"{stem}{fmt.extension}"


def _natural_extension(path: str) -> str:
    """Lower-case the extension so ``PHOTO.PNG`` and ``photo.png`` are one name."""
    stem, extension = os.path.splitext(path)
    return f"{stem}{extension.lower()}" if extension else path
