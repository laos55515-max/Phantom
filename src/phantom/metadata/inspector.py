"""Reading a file: format detection, parsing, hashing, provenance.

Detection is by content, never by extension: a ``.png`` that is really a JPEG is
reported as unsupported instead of being parsed with the wrong reader, and a WAV
whose ``RIFF`` header says ``AVI`` is refused the same way.
"""

from __future__ import annotations

import hashlib
import os
import threading
from typing import Final

from phantom.core.errors import MetadataError, PhantomIOError, UnsupportedFormat
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress
from phantom.metadata import flac as flac_mod
from phantom.metadata import png as png_mod
from phantom.metadata import provenance
from phantom.metadata import wav as wav_mod
from phantom.metadata.models import MediaFormat, MetadataReport, Section

#: Files larger than this are refused: the parsers need the container in memory,
#: and an inspector that quietly swaps a laptop to death is worse than an error.
MAX_INSPECT_BYTES: Final = 2 * 1024 * 1024 * 1024
_READ_BLOCK: Final = 1024 * 1024
_SNIFF_BYTES: Final = 16


def detect_format(head: bytes) -> MediaFormat | None:
    """Identify the format from the first bytes, or ``None`` when unsupported."""
    if png_mod.sniff(head):
        return MediaFormat.PNG
    if wav_mod.sniff(head):
        return MediaFormat.WAV
    if flac_mod.sniff(head):
        return MediaFormat.FLAC
    return None


def read_file(path: str, *, cancel: threading.Event | None = None) -> bytes:
    """Read a whole file, refusing anything the parsers cannot handle."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise PhantomIOError("cannot read the file", detail=str(exc)) from exc
    if size <= 0:
        raise MetadataError("the file is empty")
    if size > MAX_INSPECT_BYTES:
        raise MetadataError("the file is larger than the inspector accepts")
    data = bytearray()
    try:
        with open(path, "rb") as handle:
            while True:
                check_cancel(cancel)
                block = handle.read(_READ_BLOCK)
                if not block:
                    break
                data.extend(block)
    except OSError as exc:
        raise PhantomIOError("cannot read the file", detail=str(exc)) from exc
    return bytes(data)


def sha256_of(path: str, *, cancel: threading.Event | None = None) -> str:
    """SHA-256 of the whole file, streamed so a large carrier costs no memory."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                check_cancel(cancel)
                block = handle.read(_READ_BLOCK)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise PhantomIOError("cannot hash the file", detail=str(exc)) from exc
    return digest.hexdigest()


def parse(data: bytes, fmt: MediaFormat) -> Section:
    """Run the parser of one format over already-read bytes."""
    if fmt is MediaFormat.PNG:
        return png_mod.inspect_png(data)
    if fmt is MediaFormat.WAV:
        return wav_mod.inspect_wav(data)
    return flac_mod.inspect_flac(data)


def report_from_bytes(path: str, data: bytes, fmt: MediaFormat) -> MetadataReport:
    """Build a full report from bytes that are already in memory."""
    section = parse(data, fmt)
    return MetadataReport(
        path=path,
        fmt=fmt,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        section=section,
        provenance=provenance.classify(section),
    )


def inspect_path(
    path: str,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> MetadataReport:
    """Read one file and return everything the inspector found."""
    progress(Stage.SCANNING, 0, 3)
    data = read_file(path, cancel=cancel)
    progress(Stage.SCANNING, 1, 3)
    fmt = detect_format(data[:_SNIFF_BYTES])
    if fmt is None:
        raise UnsupportedFormat(
            "only PNG, WAV and FLAC containers are inspected",
            detail=os.path.splitext(path)[1] or "no extension",
        )
    progress(Stage.SCANNING, 2, 3)
    report = report_from_bytes(path, data, fmt)
    progress(Stage.SCANNING, 3, 3)
    return report


def technical_summary(report: MetadataReport) -> dict[str, str]:
    """The few numbers the file card shows, in a stable order."""
    technical = report.technical
    interesting = (
        "width",
        "height",
        "mode",
        "sample_rate",
        "channels",
        "bits_per_sample",
        "frames",
        "duration_seconds",
        "total_samples",
    )
    summary = {key: technical[key] for key in interesting if key in technical}
    if "width" in summary and "height" in summary:
        summary.setdefault("resolution", f"{summary['width']}x{summary['height']}")
    return summary
