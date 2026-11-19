"""Data model shared by the inspector, the cleaner and the reports.

The point of the model is that every other module in the package works on plain
data: the parsers turn bytes into :class:`Section` objects, :mod:`inspector`
merges them into a :class:`MetadataReport`, :mod:`cleaner` plans removals on the
report and rewrites bytes afterwards.  Nothing here imports tkinter, PIL or
mutagen, so the whole pipeline can be exercised without a display.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Final

#: Values longer than this are cut for display and the cut is reported, so a
#: 10 MiB text chunk cannot freeze the list of fields.
MAX_DISPLAY_VALUE: Final = 400

#: Names of the six categories, in the order the interface lists them.
CATEGORY_ORDER: Final[tuple[str, ...]] = (
    "personal",
    "descriptive",
    "technical",
    "rights",
    "provenance",
    "unknown",
)


@unique
class MediaFormat(Enum):
    """Formats this module understands, detected from the bytes, not the name."""

    PNG = "png"
    WAV = "wav"
    FLAC = "flac"

    @property
    def i18n_key(self) -> str:
        return f"meta.format.{self.value}"

    @property
    def extension(self) -> str:
        return f".{self.value}"


@unique
class MetadataCategory(Enum):
    """How a field is classified, which decides whether the cleaner may touch it."""

    PERSONAL = "personal"
    DESCRIPTIVE = "descriptive"
    TECHNICAL = "technical"
    RIGHTS = "rights"
    PROVENANCE = "provenance"
    UNKNOWN = "unknown"

    @property
    def i18n_key(self) -> str:
        return f"meta.cat.{self.value}"

    @property
    def rank(self) -> int:
        return CATEGORY_ORDER.index(self.value)


@unique
class ProvenanceState(Enum):
    """What local analysis can honestly say about a content credential.

    Only the first three are reachable in this build: validating a C2PA signature
    needs the trust list and the certificate chain, which PHANTOM deliberately
    does not ship, so claiming :attr:`SIGNATURE_VALID` here would be a lie.
    """

    NONE = "none"
    MANIFEST_FOUND = "manifest"
    SIGNATURE_UNVERIFIED = "unverified"
    SIGNATURE_VALID = "valid"
    SIGNATURE_INVALID = "invalid"

    @property
    def i18n_key(self) -> str:
        return f"meta.prov.{self.value}"


@dataclass(frozen=True, slots=True)
class MetadataField:
    """One readable metadata entry, with everything the user needs to judge it."""

    key: str
    label: str
    value: str
    category: MetadataCategory
    container: str
    removable: bool = False
    #: The English sentence the interface falls back to, and the log uses.
    reason: str = ""
    #: Locale key of the same sentence, when the reader produced a structured
    #: reason: the panel and the report then show it in the user's language
    #: instead of that English sentence in the middle of a Ukrainian card.
    reason_key: str = ""
    #: Values for the placeholders of :attr:`reason_key`, e.g. the names of the
    #: protected properties that block the removal.
    reason_params: tuple[tuple[str, str], ...] = ()
    #: The same treatment for :attr:`value` when the value is a sentence this
    #: program wrote rather than data it read ("contains personal properties",
    #: "12 bytes"): data is never translated, prose always is.
    value_key: str = ""
    value_params: tuple[tuple[str, str], ...] = ()
    size: int = 0
    truncated: bool = False
    #: A view computed from other fields rather than read from the container
    #: (the decimal GPS position, the XMP properties of a packet).  It is shown so
    #: the user can read the file at a glance, but it is not an entry of its own:
    #: it is not counted, it is never removed by itself, and the cleaner does not
    #: expect it to survive a rewrite — removing the fields it is made of removes
    #: it.  Without that distinction a view and the fields behind it are counted
    #: twice, and every removal of the fields behind it looks like a lost field.
    derived: bool = False

    @property
    def sensitive(self) -> bool:
        """Personal data: the fields the cleaner exists for."""
        return self.category is MetadataCategory.PERSONAL

    @property
    def protected(self) -> bool:
        return not self.removable

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "category": self.category.value,
            "container": self.container,
            "removable": self.removable,
            "derived": self.derived,
            "reason": self.reason,
            "reason_key": self.reason_key,
            "value_key": self.value_key,
            "size": self.size,
            "truncated": self.truncated,
        }


@dataclass(frozen=True, slots=True)
class ContainerChunk:
    """One structural unit of the container: a PNG chunk, a RIFF chunk, a FLAC block."""

    kind: str
    offset: int
    size: int
    checksum_ok: bool | None = None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "offset": self.offset,
            "size": self.size,
            "checksum_ok": self.checksum_ok,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ProvenanceReport:
    """What was found about digital provenance, and what that does and does not mean."""

    state: ProvenanceState = ProvenanceState.NONE
    manifests: tuple[str, ...] = ()
    signed: bool = False
    details: tuple[tuple[str, str], ...] = ()

    @property
    def detected(self) -> bool:
        return self.state is not ProvenanceState.NONE

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "detected": self.detected,
            "manifests": list(self.manifests),
            "signed": self.signed,
            "details": [list(item) for item in self.details],
        }


@dataclass(frozen=True, slots=True)
class Section:
    """Everything one format parser found; the inspector merges sections."""

    fields: tuple[MetadataField, ...] = ()
    chunks: tuple[ContainerChunk, ...] = ()
    technical: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()
    manifests: tuple[tuple[str, bytes], ...] = ()
    signature_blobs: tuple[str, ...] = ()
    checksum_ok: bool | None = None


@dataclass(frozen=True, slots=True)
class MetadataCounts:
    """The numbers the results card shows at a glance."""

    total: int = 0
    personal: int = 0
    descriptive: int = 0
    technical: int = 0
    rights: int = 0
    provenance: int = 0
    unknown: int = 0
    removable: int = 0

    def of(self, category: MetadataCategory) -> int:
        return {
            MetadataCategory.PERSONAL: self.personal,
            MetadataCategory.DESCRIPTIVE: self.descriptive,
            MetadataCategory.TECHNICAL: self.technical,
            MetadataCategory.RIGHTS: self.rights,
            MetadataCategory.PROVENANCE: self.provenance,
            MetadataCategory.UNKNOWN: self.unknown,
        }[category]

    def to_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "personal": self.personal,
            "descriptive": self.descriptive,
            "technical": self.technical,
            "rights": self.rights,
            "provenance": self.provenance,
            "unknown": self.unknown,
            "removable": self.removable,
        }


@dataclass(frozen=True, slots=True)
class DerivedCounts:
    """Views computed from other fields, counted separately from the entries.

    The XMP properties of a packet and the decimal GPS position are ways of reading
    fields that are already counted, so they are not entries: adding them to
    ``total`` would count one position six times.  They are still *visible* in the
    list, and a card that said "unknown 0" next to a visible unknown property was
    simply confusing two different things.  Hence a second, small tally.
    """

    total: int = 0
    personal: int = 0
    unknown: int = 0

    def of(self, category: MetadataCategory) -> int:
        return {
            MetadataCategory.PERSONAL: self.personal,
            MetadataCategory.UNKNOWN: self.unknown,
        }.get(category, 0)

    def to_dict(self) -> dict[str, int]:
        return {"total": self.total, "personal": self.personal, "unknown": self.unknown}


def count_derived(fields: tuple[MetadataField, ...]) -> DerivedCounts:
    """Tally the derived views that ``count_fields`` deliberately leaves out."""
    total = personal = unknown = 0
    for item in fields:
        if not item.derived:
            continue
        total += 1
        personal += 1 if item.category is MetadataCategory.PERSONAL else 0
        unknown += 1 if item.category is MetadataCategory.UNKNOWN else 0
    return DerivedCounts(total=total, personal=personal, unknown=unknown)


def count_fields(fields: tuple[MetadataField, ...]) -> MetadataCounts:
    """Count fields per category, once, so the UI and the report cannot disagree.

    Derived views are left out: they are a second way of reading fields that are
    already counted, and adding them would make a file with one GPS position look
    like it carried six personal fields.  :func:`count_derived` tallies them.
    """
    totals = dict.fromkeys(CATEGORY_ORDER, 0)
    removable = 0
    counted = 0
    for item in fields:
        if item.derived:
            continue
        counted += 1
        totals[item.category.value] += 1
        removable += 1 if item.removable else 0
    return MetadataCounts(
        total=counted,
        personal=totals["personal"],
        descriptive=totals["descriptive"],
        technical=totals["technical"],
        rights=totals["rights"],
        provenance=totals["provenance"],
        unknown=totals["unknown"],
        removable=removable,
    )


@dataclass(frozen=True, slots=True)
class MetadataReport:
    """Result of reading one file: fields, structure, technical facts, provenance."""

    path: str
    fmt: MediaFormat
    size_bytes: int
    sha256: str
    section: Section
    provenance: ProvenanceReport
    counts: MetadataCounts = field(init=False)
    derived_counts: DerivedCounts = field(init=False)

    def __post_init__(self) -> None:
        # ``counts`` is derived, so it is computed once here instead of being
        # passed around (and eventually disagreeing with the fields).
        object.__setattr__(self, "counts", count_fields(self.section.fields))
        object.__setattr__(self, "derived_counts", count_derived(self.section.fields))

    @property
    def fields(self) -> tuple[MetadataField, ...]:
        return self.section.fields

    @property
    def chunks(self) -> tuple[ContainerChunk, ...]:
        return self.section.chunks

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.section.warnings

    @property
    def technical(self) -> dict[str, str]:
        return dict(self.section.technical)

    @property
    def checksum_ok(self) -> bool | None:
        return self.section.checksum_ok

    @property
    def personal_fields(self) -> tuple[MetadataField, ...]:
        return tuple(item for item in self.fields if item.sensitive)

    @property
    def removable_keys(self) -> frozenset[str]:
        return frozenset(item.key for item in self.fields if item.removable)

    def find(self, key: str) -> MetadataField | None:
        for item in self.fields:
            if item.key == key:
                return item
        return None

    def sorted_fields(self) -> tuple[MetadataField, ...]:
        """Category order first, then container and label, so lists are stable."""
        return tuple(
            sorted(
                self.fields,
                key=lambda item: (item.category.rank, item.container, item.label, item.key),
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "format": self.fmt.value,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "counts": self.counts.to_dict(),
            "derived_counts": self.derived_counts.to_dict(),
            "technical": self.technical,
            "checksum_ok": self.checksum_ok,
            "warnings": list(self.warnings),
            "fields": [item.to_dict() for item in self.sorted_fields()],
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "provenance": self.provenance.to_dict(),
        }


def short_hash(data: bytes) -> str:
    """First eight hex characters of a SHA-256, for log lines and row labels."""
    return hashlib.sha256(data).hexdigest()[:8]
