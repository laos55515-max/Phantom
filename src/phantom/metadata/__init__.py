"""Metadata inspection and privacy cleaning for PNG, WAV and FLAC containers.

The package is deliberately separate from :mod:`phantom.stego`: it reads and
rewrites *containers*, and never touches the stego payload format, the cipher or
the key schedule.  A file that carries a PHANTOM payload is still inspected as an
ordinary carrier — the report shows its chunks, not a decrypted manifest.

Public surface
--------------
``inspect_path``          read a file, return a :class:`MetadataReport`
``detect_format``         format from the first bytes, never the extension
``plan``                  what a selection would remove, before writing anything
``default_selection``     the personal fields the cleaner can remove
``clean``                 write a cleaned copy, verify it, publish it atomically
``report_text``/``report_json``  the two exports

Guarantees
----------
* the source file is only ever opened for reading;
* the result is staged and published atomically, so a failure leaves the
  destination untouched;
* decoded pixels and PCM samples are compared before publishing, and a difference
  aborts the operation;
* a file with a content credential attached is not rewritten at all.

Limitations, stated once and repeated wherever they matter: unknown blocks are
listed but never removed automatically; a manifest is reported, never validated;
an embedded FLAC picture is removed as a whole and its own metadata is not read.
"""

from __future__ import annotations

from phantom.metadata.cleaner import (
    CleaningOutcome,
    CleaningPlan,
    clean,
    default_selection,
    plan,
)
from phantom.metadata.inspector import detect_format, inspect_path, technical_summary
from phantom.metadata.models import (
    CATEGORY_ORDER,
    ContainerChunk,
    MediaFormat,
    MetadataCategory,
    MetadataCounts,
    MetadataField,
    MetadataReport,
    ProvenanceReport,
    ProvenanceState,
)
from phantom.metadata.report import (
    REPORT_FORMAT_VERSION,
    cleaning_text,
    inspection_text,
    report_json,
    summary_line,
)

__all__ = [
    "CATEGORY_ORDER",
    "REPORT_FORMAT_VERSION",
    "CleaningOutcome",
    "CleaningPlan",
    "ContainerChunk",
    "MediaFormat",
    "MetadataCategory",
    "MetadataCounts",
    "MetadataField",
    "MetadataReport",
    "ProvenanceReport",
    "ProvenanceState",
    "clean",
    "cleaning_text",
    "default_selection",
    "detect_format",
    "inspect_path",
    "inspection_text",
    "plan",
    "report_json",
    "summary_line",
    "technical_summary",
]
