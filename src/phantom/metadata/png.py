"""PNG container: chunk walk, text/EXIF/XMP/ICC reading, and chunk-level rewriting.

Two rules shape this module.

*Nothing is decoded through Pillow here.*  The structural report has to survive a
file Pillow refuses (a broken CRC, a truncated chunk, an unknown critical chunk),
because that is exactly the file a user is worried about.  Pixel comparison lives
in :mod:`phantom.metadata.integrity`, where Pillow belongs.

*Removal never touches pixel data.*  ``IDAT`` chunks are copied byte for byte and
the chunk list is otherwise kept in its original order, so the decoded image
cannot change; the eXIf chunk is the only one rebuilt, and even then every
surviving TIFF entry keeps its exact value bytes.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Final

from phantom.metadata.models import (
    MAX_DISPLAY_VALUE,
    ContainerChunk,
    MetadataCategory,
    MetadataField,
    Section,
)

PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_CHUNK_HEADER: Final = struct.Struct(">I4s")
_CHUNK_CRC: Final = struct.Struct(">I")

#: Text chunks larger than this are listed but not decoded: a metadata block is
#: never a reason for the inspector to allocate a gigabyte.
MAX_TEXT_BYTES: Final = 8 * 1024 * 1024
#: A PNG with more chunks than this is either broken or hostile; stop walking.
MAX_CHUNKS: Final = 50_000
#: Exceeding this many EXIF entries means the IFD is lying about its size.
MAX_IFD_ENTRIES: Final = 4096

TEXT_CHUNKS: Final[frozenset[str]] = frozenset({"tEXt", "zTXt", "iTXt"})
XMP_KEYWORD: Final = "XML:com.adobe.xmp"

#: Chunks that are part of the image itself and are never metadata to remove.
STRUCTURAL_CHUNKS: Final[frozenset[str]] = frozenset(
    {"IHDR", "PLTE", "IDAT", "IEND", "tRNS", "bKGD", "sBIT", "hIST", "sPLT"}
)
#: Chunks carrying a colour profile or physical resolution: technical, kept.
TECHNICAL_CHUNKS: Final[frozenset[str]] = frozenset(
    {"iCCP", "sRGB", "gAMA", "cHRM", "pHYs", "cICP", "mDCV", "cLLI", "eXIf"}
)
#: Chunks that declare a digital signature or a content credential.
SIGNATURE_CHUNKS: Final[frozenset[str]] = frozenset({"dSIG", "caBX", "jumb", "jumbf"})

#: PNG text keywords of the specification, and how each one is classified.
_TEXT_KEYWORDS: Final[dict[str, MetadataCategory]] = {
    "title": MetadataCategory.DESCRIPTIVE,
    "description": MetadataCategory.DESCRIPTIVE,
    "author": MetadataCategory.RIGHTS,
    "copyright": MetadataCategory.RIGHTS,
    "disclaimer": MetadataCategory.RIGHTS,
    "warning": MetadataCategory.RIGHTS,
    "source": MetadataCategory.PROVENANCE,
    "software": MetadataCategory.TECHNICAL,
    "comment": MetadataCategory.PERSONAL,
    "creation time": MetadataCategory.PERSONAL,
}

#: Attribution fields: a name is personal, but stripping the author by default
#: would silently rewrite someone's credit, so the cleaner offers them unticked.
_ATTRIBUTION_TAGS: Final[frozenset[int]] = frozenset({0x013B})  # Artist
_ATTRIBUTION_KEYWORDS: Final[frozenset[str]] = frozenset({"author"})

_IFD_POINTERS: Final[dict[int, str]] = {0x8769: "exif", 0x8825: "gps", 0xA005: "interop"}
#: Tags that point at bytes rather than describing anything a user decides about.
_STRUCTURAL_TAGS: Final[frozenset[int]] = frozenset({0x0201, 0x0202})

_TYPE_SIZES: Final[dict[int, int]] = {
    1: 1,  # BYTE
    2: 1,  # ASCII
    3: 2,  # SHORT
    4: 4,  # LONG
    5: 8,  # RATIONAL
    6: 1,  # SBYTE
    7: 1,  # UNDEFINED
    8: 2,  # SSHORT
    9: 4,  # SLONG
    10: 8,  # SRATIONAL
    11: 4,  # FLOAT
    12: 8,  # DOUBLE
    13: 4,  # IFD
}

#: Curated EXIF tags.  A tag that is not here is still shown — as an unknown
#: field with its number — because "we do not know this tag" is not "no data".
_EXIF_TAGS: Final[dict[int, tuple[str, MetadataCategory]]] = {
    0x0100: ("ImageWidth", MetadataCategory.TECHNICAL),
    0x0101: ("ImageLength", MetadataCategory.TECHNICAL),
    0x0102: ("BitsPerSample", MetadataCategory.TECHNICAL),
    0x010E: ("ImageDescription", MetadataCategory.DESCRIPTIVE),
    0x010F: ("Make", MetadataCategory.PERSONAL),
    0x0110: ("Model", MetadataCategory.PERSONAL),
    0x0112: ("Orientation", MetadataCategory.TECHNICAL),
    0x011A: ("XResolution", MetadataCategory.TECHNICAL),
    0x011B: ("YResolution", MetadataCategory.TECHNICAL),
    0x0128: ("ResolutionUnit", MetadataCategory.TECHNICAL),
    0x0131: ("Software", MetadataCategory.TECHNICAL),
    0x0132: ("DateTime", MetadataCategory.PERSONAL),
    0x013B: ("Artist", MetadataCategory.RIGHTS),
    0x013E: ("WhitePoint", MetadataCategory.TECHNICAL),
    0x0213: ("YCbCrPositioning", MetadataCategory.TECHNICAL),
    0x8298: ("Copyright", MetadataCategory.RIGHTS),
    0x829A: ("ExposureTime", MetadataCategory.TECHNICAL),
    0x829D: ("FNumber", MetadataCategory.TECHNICAL),
    0x8827: ("ISOSpeedRatings", MetadataCategory.TECHNICAL),
    0x9000: ("ExifVersion", MetadataCategory.TECHNICAL),
    0x9003: ("DateTimeOriginal", MetadataCategory.PERSONAL),
    0x9004: ("DateTimeDigitized", MetadataCategory.PERSONAL),
    0x9010: ("OffsetTime", MetadataCategory.PERSONAL),
    0x9011: ("OffsetTimeOriginal", MetadataCategory.PERSONAL),
    0x9012: ("OffsetTimeDigitized", MetadataCategory.PERSONAL),
    0x9201: ("ShutterSpeedValue", MetadataCategory.TECHNICAL),
    0x9202: ("ApertureValue", MetadataCategory.TECHNICAL),
    0x9209: ("Flash", MetadataCategory.TECHNICAL),
    0x920A: ("FocalLength", MetadataCategory.TECHNICAL),
    0x927C: ("MakerNote", MetadataCategory.UNKNOWN),
    0x9286: ("UserComment", MetadataCategory.PERSONAL),
    0xA002: ("PixelXDimension", MetadataCategory.TECHNICAL),
    0xA003: ("PixelYDimension", MetadataCategory.TECHNICAL),
    0xA420: ("ImageUniqueID", MetadataCategory.TECHNICAL),
    0xA430: ("CameraOwnerName", MetadataCategory.PERSONAL),
    0xA431: ("BodySerialNumber", MetadataCategory.PERSONAL),
    0xA433: ("LensMake", MetadataCategory.TECHNICAL),
    0xA434: ("LensModel", MetadataCategory.TECHNICAL),
    0xA435: ("LensSerialNumber", MetadataCategory.PERSONAL),
}

_GPS_TAGS: Final[dict[int, tuple[str, MetadataCategory]]] = {
    0x0000: ("GPSVersionID", MetadataCategory.TECHNICAL),
    0x0001: ("GPSLatitudeRef", MetadataCategory.PERSONAL),
    0x0002: ("GPSLatitude", MetadataCategory.PERSONAL),
    0x0003: ("GPSLongitudeRef", MetadataCategory.PERSONAL),
    0x0004: ("GPSLongitude", MetadataCategory.PERSONAL),
    0x0005: ("GPSAltitudeRef", MetadataCategory.PERSONAL),
    0x0006: ("GPSAltitude", MetadataCategory.PERSONAL),
    0x0007: ("GPSTimeStamp", MetadataCategory.PERSONAL),
    0x0008: ("GPSSatellites", MetadataCategory.PERSONAL),
    0x0009: ("GPSStatus", MetadataCategory.PERSONAL),
    0x000D: ("GPSSpeed", MetadataCategory.PERSONAL),
    0x0010: ("GPSImgDirectionRef", MetadataCategory.PERSONAL),
    0x0011: ("GPSImgDirection", MetadataCategory.PERSONAL),
    0x001D: ("GPSDateStamp", MetadataCategory.PERSONAL),
}

#: XMP properties the reader pulls out of the packet, with their classification.
_XMP_PROPERTIES: Final[tuple[tuple[str, str, MetadataCategory], ...]] = (
    ("dc:title", "XMP dc:title", MetadataCategory.DESCRIPTIVE),
    ("dc:description", "XMP dc:description", MetadataCategory.DESCRIPTIVE),
    ("dc:creator", "XMP dc:creator", MetadataCategory.RIGHTS),
    ("dc:rights", "XMP dc:rights", MetadataCategory.RIGHTS),
    ("xmp:CreatorTool", "XMP xmp:CreatorTool", MetadataCategory.TECHNICAL),
    ("xmp:CreateDate", "XMP xmp:CreateDate", MetadataCategory.PERSONAL),
    ("xmp:ModifyDate", "XMP xmp:ModifyDate", MetadataCategory.PERSONAL),
    ("photoshop:DateCreated", "XMP photoshop:DateCreated", MetadataCategory.PERSONAL),
    ("photoshop:City", "XMP photoshop:City", MetadataCategory.PERSONAL),
    ("photoshop:Country", "XMP photoshop:Country", MetadataCategory.PERSONAL),
    ("photoshop:State", "XMP photoshop:State", MetadataCategory.PERSONAL),
    ("Iptc4xmpCore:Location", "XMP Iptc4xmpCore:Location", MetadataCategory.PERSONAL),
    ("exif:GPSLatitude", "XMP exif:GPSLatitude", MetadataCategory.PERSONAL),
    ("exif:GPSLongitude", "XMP exif:GPSLongitude", MetadataCategory.PERSONAL),
)

_REASON_PERSONAL_BLOCK = "personal data inside a structured packet"


@dataclass(frozen=True, slots=True)
class PngChunk:
    """One chunk, with the raw bytes the rewriter copies."""

    kind: str
    data: bytes = field(repr=False)
    offset: int = 0
    crc_ok: bool | None = None


@dataclass(frozen=True, slots=True)
class PngRemoval:
    """What the cleaner was asked to take out of one PNG."""

    keywords: frozenset[str] = frozenset()
    exif_tags: frozenset[int] = frozenset()
    gps_tags: frozenset[int] = frozenset()
    drop_exif: bool = False
    drop_xmp: bool = False
    chunk_kinds: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        return not (
            self.keywords
            or self.exif_tags
            or self.gps_tags
            or self.drop_exif
            or self.drop_xmp
            or self.chunk_kinds
        )


def sniff(data: bytes) -> bool:
    """True when the bytes start with the PNG signature."""
    return data.startswith(PNG_SIGNATURE)


# ---------------------------------------------------------------------------
# chunk walk
# ---------------------------------------------------------------------------
def iter_chunks(data: bytes) -> list[PngChunk]:
    """Every chunk that can be read, even when one of them is broken.

    A chunk whose CRC is wrong is kept and flagged: refusing to read the rest of
    the file because one metadata block is corrupt would hide the very problem the
    report is supposed to show.
    """
    if not sniff(data):
        return []
    chunks: list[PngChunk] = []
    offset = len(PNG_SIGNATURE)
    total = len(data)
    while offset + 8 <= total and len(chunks) < MAX_CHUNKS:
        (length, raw_kind) = _CHUNK_HEADER.unpack_from(data, offset)
        kind = raw_kind.decode("latin-1")
        body = offset + 8
        end = body + length
        if end + 4 > total:
            # Truncated chunk: record what is there and stop; the report says so.
            chunks.append(PngChunk(kind=kind, data=data[body:total], offset=offset, crc_ok=None))
            break
        payload = data[body:end]
        (stored_crc,) = _CHUNK_CRC.unpack_from(data, end)
        computed = zlib.crc32(raw_kind + payload) & 0xFFFFFFFF
        chunks.append(
            PngChunk(kind=kind, data=payload, offset=offset, crc_ok=computed == stored_crc)
        )
        offset = end + 4
        if kind == "IEND":
            break
    return chunks


def _decode_text(chunk: PngChunk) -> tuple[str, str, list[str]]:  # noqa: PLR0911 - one return per chunk layout
    """``(keyword, text, warnings)`` for a tEXt/zTXt/iTXt chunk."""
    warnings: list[str] = []
    kind = chunk.kind
    data = chunk.data
    if len(data) > MAX_TEXT_BYTES:
        return "", f"<{len(data)} bytes, too large to display>", [f"{kind} chunk over the size cap"]
    try:
        if kind == "tEXt":
            keyword, _, text = data.partition(b"\x00")
            return (
                keyword.decode("latin-1"),
                text.decode("latin-1", errors="replace"),
                warnings,
            )
        if kind == "zTXt":
            keyword, _, rest = data.partition(b"\x00")
            method = rest[:1]
            if method != b"\x00":
                return keyword.decode("latin-1"), "<unknown compression>", [
                    f"zTXt {keyword!r}: unsupported compression method {method!r}"
                ]
            raw = zlib.decompress(rest[1:])
            return keyword.decode("latin-1"), _safe_utf8(raw, warnings), warnings
        keyword, _, rest = data.partition(b"\x00")
        compressed = rest[:1] == b"\x01"
        method = rest[1:2]
        remainder = rest[2:]
        language, _, remainder = remainder.partition(b"\x00")
        _translated, _, text = remainder.partition(b"\x00")
        if compressed:
            if method != b"\x00":
                return keyword.decode("latin-1"), "<unknown compression>", [
                    f"iTXt {keyword!r}: unsupported compression method {method!r}"
                ]
            text = zlib.decompress(text)
        if language:
            warnings.append(f"iTXt {keyword.decode('latin-1')!r}: language {language!r}")
        return keyword.decode("latin-1"), _safe_utf8(text, warnings), warnings
    except (zlib.error, IndexError) as exc:
        return "", "<unreadable text chunk>", [f"{kind} chunk cannot be decoded: {exc}"]


def _keyword(data: bytes) -> str:
    """The keyword of a text chunk, without decompressing the text itself."""
    return data.split(b"\x00", 1)[0].decode("latin-1")


def _safe_utf8(raw: bytes, warnings: list[str]) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        warnings.append(f"text is not valid UTF-8 ({exc.reason}); replacing bad bytes")
        return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# EXIF (TIFF) reader
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class _TiffEntry:
    tag: int
    type: int
    count: int
    value: bytes


@dataclass(slots=True)
class _TiffIfd:
    entries: list[_TiffEntry]

    def get(self, tag: int) -> _TiffEntry | None:
        for entry in self.entries:
            if entry.tag == tag:
                return entry
        return None


@dataclass(slots=True)
class _Tiff:
    byte_order: bytes
    ifds: dict[str, _TiffIfd]
    thumbnail: bytes = b""
    warnings: list[str] = field(default_factory=list)


def _unpack_short(blob: bytes, order: str) -> int:
    return int(struct.unpack_from(f"{order}H", blob)[0])


def _parse_tiff(blob: bytes, warnings: list[str]) -> _Tiff | None:
    """Read IFD0, the Exif IFD, the GPS IFD and the Interop IFD of an eXIf blob."""
    if len(blob) < 8:
        warnings.append("eXIf chunk is shorter than a TIFF header")
        return None
    order = ">" if blob[:2] == b"MM" else "<"
    (magic,) = struct.unpack_from(f"{order}H", blob, 2)
    if magic != 42:
        warnings.append(f"eXIf chunk is not TIFF (magic {magic})")
        return None
    (first_ifd,) = struct.unpack_from(f"{order}I", blob, 4)
    ifds: dict[str, _TiffIfd] = {}
    pending: list[tuple[str, int]] = [("0th", first_ifd)]
    seen: set[int] = set()
    while pending:
        name, offset = pending.pop()
        if offset in seen or offset <= 0 or offset + 2 > len(blob):
            continue
        seen.add(offset)
        ifd, next_ifd = _read_ifd(blob, offset, order, warnings)
        if ifd is None:
            continue
        ifds[name] = ifd
        for entry in ifd.entries:
            if entry.tag in _IFD_POINTERS:
                pointer = _first_int(entry, order)
                if pointer:
                    pending.append((_IFD_POINTERS[entry.tag], pointer))
        if name == "0th" and next_ifd and next_ifd not in seen:
            pending.append(("ifd1", next_ifd))
    if "0th" not in ifds:
        warnings.append("eXIf chunk has no readable IFD0")
        return None
    return _Tiff(byte_order=blob[:2], ifds=ifds, thumbnail=_thumbnail(blob, ifds, order))


def _read_ifd(
    blob: bytes, offset: int, order: str, warnings: list[str]
) -> tuple[_TiffIfd | None, int]:
    if offset + 2 > len(blob):
        return None, 0
    (count,) = struct.unpack_from(f"{order}H", blob, offset)
    if count > MAX_IFD_ENTRIES:
        warnings.append(f"IFD at {offset} declares {count} entries; stopped reading")
        return None, 0
    entries: list[_TiffEntry] = []
    cursor = offset + 2
    for _index in range(count):
        if cursor + 12 > len(blob):
            warnings.append("IFD is truncated")
            return _TiffIfd(entries), 0
        tag, kind, amount = struct.unpack_from(f"{order}HHI", blob, cursor)
        size = _TYPE_SIZES.get(kind)
        if size is None:
            warnings.append(f"EXIF tag 0x{tag:04x} has unknown type {kind}; skipped")
        else:
            length = size * amount
            if length <= 4:
                raw = blob[cursor + 8 : cursor + 8 + length]
            else:
                (value_offset,) = struct.unpack_from(f"{order}I", blob, cursor + 8)
                if value_offset + length > len(blob):
                    warnings.append(f"EXIF tag 0x{tag:04x} points outside the chunk")
                    raw = b""
                else:
                    raw = blob[value_offset : value_offset + length]
            entries.append(_TiffEntry(tag=tag, type=kind, count=amount, value=raw))
        cursor += 12
    next_ifd = 0
    if cursor + 4 <= len(blob):
        (next_ifd,) = struct.unpack_from(f"{order}I", blob, cursor)
    return _TiffIfd(entries), next_ifd


def _first_int(entry: _TiffEntry, order: str) -> int:
    if not entry.value:
        return 0
    if entry.type in (3, 8) and len(entry.value) >= 2:
        return int(struct.unpack_from(f"{order}H", entry.value)[0])
    if entry.type in (4, 9, 13) and len(entry.value) >= 4:
        return int(struct.unpack_from(f"{order}I", entry.value)[0])
    return 0


def _thumbnail(blob: bytes, ifds: dict[str, _TiffIfd], order: str) -> bytes:
    """The embedded JPEG of IFD1, which lives at an offset two tags point at."""
    ifd1 = ifds.get("ifd1")
    if ifd1 is None:
        return b""
    start_entry, length_entry = ifd1.get(0x0201), ifd1.get(0x0202)
    if start_entry is None or length_entry is None:
        return b""
    start = _first_int(start_entry, order)
    length = _first_int(length_entry, order)
    if start <= 0 or length <= 0 or start + length > len(blob):
        return b""
    return blob[start : start + length]


def _rational(entry: _TiffEntry, order: str) -> list[tuple[int, int]]:
    pairs: list[tuple[int, int]] = []
    signed = entry.type == 10
    code = "i" if signed else "I"
    step = 8
    for index in range(0, min(len(entry.value), 24 * step), step):
        numerator, denominator = struct.unpack_from(f"{order}{code}{code}", entry.value, index)
        pairs.append((numerator, denominator))
    return pairs


def _render_value(entry: _TiffEntry, order: str) -> str:  # noqa: PLR0911 - one return per TIFF type
    """A short, printable rendering of one TIFF value."""
    raw = entry.value
    if entry.type == 2:
        return raw.split(b"\x00")[0].decode("utf-8", errors="replace").strip()
    if entry.type in (5, 10):
        parts: list[str] = []
        for numerator, denominator in _rational(entry, order):
            if denominator:
                parts.append(f"{numerator / denominator:.6g}")
            else:
                parts.append(f"{numerator}/0")
        return ", ".join(parts)
    if entry.type == 7:
        if entry.tag == 0x9286:  # UserComment: an 8-byte character-code prefix
            prefix, body = raw[:8], raw[8:]
            if prefix.startswith(b"UNICODE"):
                return body.decode("utf-16-be", errors="replace").strip("\x00").strip()
            if prefix.startswith(b"ASCII"):
                return body.decode("latin-1", errors="replace").strip("\x00").strip()
            return raw[:16].hex()
        return raw[:16].hex()
    if entry.type in (1, 6):
        return ", ".join(str(byte) for byte in raw[:12])
    if entry.type in (3, 8):
        code = "H" if entry.type == 3 else "h"
        values = [
            struct.unpack_from(f"{order}{code}", raw, index)[0]
            for index in range(0, min(len(raw), 24), 2)
        ]
        return ", ".join(str(value) for value in values)
    if entry.type in (4, 9, 13):
        code = "I" if entry.type in (4, 13) else "i"
        values = [
            struct.unpack_from(f"{order}{code}", raw, index)[0]
            for index in range(0, min(len(raw), 48), 4)
        ]
        return ", ".join(str(value) for value in values)
    return raw[:32].hex()


def _gps_decimal(entry: _TiffEntry, order: str, ref: str) -> str:
    """Degrees/minutes/seconds → the decimal degrees a map understands."""
    parts = _rational(entry, order)
    if len(parts) < 3:
        return ""
    degrees, minutes, seconds = (
        (numerator / denominator) if denominator else 0.0 for numerator, denominator in parts[:3]
    )
    value = degrees + minutes / 60 + seconds / 3600
    if ref.strip().upper() in {"S", "W"}:
        value = -value
    return f"{value:.6f}"


def _exif_fields(tiff: _Tiff) -> tuple[list[MetadataField], list[str]]:
    fields: list[MetadataField] = []
    warnings = list(tiff.warnings)
    order = ">" if tiff.byte_order == b"MM" else "<"
    for ifd_name, ifd in tiff.ifds.items():
        if ifd_name == "ifd1":
            continue  # the thumbnail IFD is structure, reported as one field below
        for entry in ifd.entries:
            if entry.tag in _IFD_POINTERS or entry.tag in _STRUCTURAL_TAGS:
                continue  # a pointer, not data
            if ifd_name == "gps":
                name, category = _GPS_TAGS.get(
                    entry.tag, (f"GPSTag0x{entry.tag:04x}", MetadataCategory.UNKNOWN)
                )
                prefix, container = "gps", "eXIf/GPS"
            else:
                name, category = _EXIF_TAGS.get(
                    entry.tag, (f"Tag0x{entry.tag:04x}", MetadataCategory.UNKNOWN)
                )
                prefix, container = "exif", f"eXIf/{ifd_name}"
            value = _render_value(entry, order)
            removable = category is MetadataCategory.PERSONAL or (
                prefix == "exif" and entry.tag in _ATTRIBUTION_TAGS
            )
            fields.append(
                MetadataField(
                    key=f"png.{prefix}.0x{entry.tag:04x}",
                    label=name,
                    value=value[:MAX_DISPLAY_VALUE],
                    category=category,
                    container=container,
                    removable=removable,
                    reason="" if removable else "kept by policy",
                    size=len(entry.value),
                    truncated=len(value) > MAX_DISPLAY_VALUE,
                )
            )
    if tiff.thumbnail:
        fields.append(
            MetadataField(
                key="png.exif.thumbnail",
                label="Embedded EXIF thumbnail",
                value=f"{len(tiff.thumbnail)} bytes",
                category=MetadataCategory.UNKNOWN,
                container="eXIf/ifd1",
                removable=False,
                reason="structural: the cleaner preserves the thumbnail",
                size=len(tiff.thumbnail),
            )
        )
    latitude = _gps_pair(tiff, 0x0002, 0x0001, order)
    longitude = _gps_pair(tiff, 0x0004, 0x0003, order)
    if latitude and longitude:
        fields.append(
            MetadataField(
                key="png.gps.coordinates",
                label="GPS coordinates",
                value=f"{latitude}, {longitude}",
                category=MetadataCategory.PERSONAL,
                container="eXIf/GPS",
                removable=True,
                reason="removing the GPS IFD removes this",
            )
        )
    return fields, warnings


def _gps_pair(tiff: _Tiff, value_tag: int, ref_tag: int, order: str) -> str:
    gps = tiff.ifds.get("gps")
    if gps is None:
        return ""
    value_entry = gps.get(value_tag)
    if value_entry is None:
        return ""
    ref_entry = gps.get(ref_tag)
    ref = _render_value(ref_entry, order) if ref_entry is not None else ""
    return _gps_decimal(value_entry, order, ref)


# ---------------------------------------------------------------------------
# EXIF (TIFF) writer
# ---------------------------------------------------------------------------
def _serialize_tiff(tiff: _Tiff) -> bytes:
    """Rebuild the TIFF from the parsed entries, keeping every surviving value.

    Offsets are recomputed from scratch, so entries can be dropped freely; the
    embedded thumbnail is re-attached and the two tags pointing at it are updated.
    Two passes: first decide which entries survive (their count fixes the IFD
    sizes, and therefore the offsets), then render them.
    """
    order = ">" if tiff.byte_order == b"MM" else "<"
    names = [name for name in ("0th", "exif", "gps", "interop", "ifd1") if name in tiff.ifds]
    if "0th" not in names:
        raise ValueError("no IFD0 to serialize")
    thumbnail = tiff.thumbnail
    survivors: dict[str, list[_TiffEntry]] = {}
    for name in names:
        kept: list[_TiffEntry] = []
        for entry in tiff.ifds[name].entries:
            if entry.tag in _IFD_POINTERS:
                if _IFD_POINTERS[entry.tag] in names:
                    kept.append(entry)
                continue
            if name == "ifd1" and entry.tag in (0x0201, 0x0202) and not thumbnail:
                # The thumbnail could not be located, so its pointers would dangle.
                continue
            kept.append(entry)
        survivors[name] = kept

    offsets: dict[str, int] = {}
    cursor = 8
    for name in names:
        offsets[name] = cursor
        cursor += 2 + 12 * len(survivors[name]) + 4
    data_start = cursor
    data_area = bytearray()
    rendered: dict[str, list[bytes]] = {}
    for name in names:
        rows: list[bytes] = []
        for entry in survivors[name]:
            if entry.tag in _IFD_POINTERS:
                rows.append(
                    struct.pack(f"{order}HHI", entry.tag, entry.type, entry.count)
                    + struct.pack(f"{order}I", offsets[_IFD_POINTERS[entry.tag]])
                )
                continue
            if name == "ifd1" and entry.tag == 0x0201:
                rows.append(
                    struct.pack(f"{order}HHI", entry.tag, entry.type, entry.count) + b"\x00" * 4
                )
                continue
            if len(entry.value) > 4:
                value_offset = data_start + len(data_area)
                data_area.extend(entry.value)
                if len(data_area) % 2:
                    data_area.append(0)
                rows.append(
                    struct.pack(f"{order}HHI", entry.tag, entry.type, entry.count)
                    + struct.pack(f"{order}I", value_offset)
                )
                continue
            rows.append(
                struct.pack(f"{order}HHI", entry.tag, entry.type, entry.count)
                + entry.value.ljust(4, b"\x00")
            )
        rendered[name] = rows
    if thumbnail and "ifd1" in names:
        thumbnail_offset = data_start + len(data_area)
        data_area.extend(thumbnail)
        rows = rendered["ifd1"]
        for index, row in enumerate(rows):
            tag = struct.unpack_from(f"{order}H", row)[0]
            if tag == 0x0201:
                rows[index] = struct.pack(f"{order}HHI", tag, 4, 1) + struct.pack(
                    f"{order}I", thumbnail_offset
                )
    out = bytearray(tiff.byte_order)
    out.extend(struct.pack(f"{order}H", 42))
    out.extend(struct.pack(f"{order}I", offsets["0th"]))
    for name in names:
        rows = rendered[name]
        out.extend(struct.pack(f"{order}H", len(rows)))
        for row in rows:
            out.extend(row)
        next_offset = offsets.get("ifd1", 0) if name == "0th" and "ifd1" in names else 0
        out.extend(struct.pack(f"{order}I", next_offset))
    out.extend(data_area)
    return bytes(out)


def _apply_exif_removal(tiff: _Tiff, removal: PngRemoval) -> _Tiff:
    """Drop the selected tags, and the GPS IFD when nothing personal is left."""
    ifds: dict[str, _TiffIfd] = {}
    for name, ifd in tiff.ifds.items():
        if name == "gps":
            kept = [entry for entry in ifd.entries if entry.tag not in removal.gps_tags]
            if kept:
                ifds[name] = _TiffIfd(kept)
            continue
        kept = [entry for entry in ifd.entries if entry.tag not in removal.exif_tags]
        ifds[name] = _TiffIfd(kept)
    return _Tiff(byte_order=tiff.byte_order, ifds=ifds, thumbnail=tiff.thumbnail)


# ---------------------------------------------------------------------------
# inspection
# ---------------------------------------------------------------------------
def _technical(chunks: list[PngChunk]) -> list[tuple[str, str]]:
    technical: list[tuple[str, str]] = []
    for chunk in chunks:
        if chunk.kind == "IHDR" and len(chunk.data) >= 13:
            width, height, depth, colour, _comp, _filter, interlace = struct.unpack(
                ">IIBBBBB", chunk.data[:13]
            )
            technical.extend(
                (
                    ("width", str(width)),
                    ("height", str(height)),
                    ("bit_depth", str(depth)),
                    ("colour_type", str(colour)),
                    ("interlaced", "yes" if interlace else "no"),
                )
            )
        elif chunk.kind == "pHYs" and len(chunk.data) >= 9:
            x, y, unit = struct.unpack(">IIB", chunk.data[:9])
            technical.append(("pixel_per_unit", f"{x}x{y} unit={unit}"))
        elif chunk.kind == "gAMA" and len(chunk.data) >= 4:
            (gamma,) = struct.unpack(">I", chunk.data[:4])
            technical.append(("gamma", f"{gamma / 100000:.5f}"))
        elif chunk.kind == "sRGB" and chunk.data:
            technical.append(("srgb_rendering_intent", str(chunk.data[0])))
        elif chunk.kind == "iCCP":
            name, _, _rest = chunk.data.partition(b"\x00")
            technical.append(("icc_profile", name.decode("latin-1") or "<unnamed>"))
    return technical


def _chunk_fields(chunks: list[PngChunk]) -> list[MetadataField]:
    """Fields for the chunks that carry data of their own, plus the unknown ones."""
    fields: list[MetadataField] = []
    for chunk in chunks:
        kind = chunk.kind
        if kind in TEXT_CHUNKS:
            continue  # handled by the caller, which decodes the text
        if kind == "eXIf":
            continue  # handled by the EXIF reader
        if kind == "tIME" and len(chunk.data) >= 7:
            year, month, day, hour, minute, second = struct.unpack(">HBBBBB", chunk.data[:7])
            fields.append(
                MetadataField(
                    key="png.chunk.tIME",
                    label="Last modification time (tIME)",
                    value=f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}",
                    category=MetadataCategory.PERSONAL,
                    container="tIME",
                    removable=True,
                    size=len(chunk.data),
                )
            )
            continue
        if kind == "iCCP":
            name, _, body = chunk.data.partition(b"\x00")
            fields.append(
                MetadataField(
                    key="png.chunk.iCCP",
                    label="ICC colour profile",
                    value=f"{name.decode('latin-1') or '<unnamed>'} ({len(body)} bytes)",
                    category=MetadataCategory.TECHNICAL,
                    container="iCCP",
                    removable=False,
                    reason="colour profile: removing it changes how the image is displayed",
                    size=len(chunk.data),
                )
            )
            continue
        if kind in SIGNATURE_CHUNKS:
            fields.append(
                MetadataField(
                    key=f"png.chunk.{kind}",
                    label=f"Digital signature block ({kind})",
                    value=f"{len(chunk.data)} bytes",
                    category=MetadataCategory.PROVENANCE,
                    container=kind,
                    removable=False,
                    reason="content credential: never rewritten by the cleaner",
                    size=len(chunk.data),
                )
            )
            continue
        if kind in STRUCTURAL_CHUNKS or kind in TECHNICAL_CHUNKS:
            # Structure and colour data are listed in the chunk table, not here:
            # a field list is for things a user can decide about.
            continue
        fields.append(
            MetadataField(
                key=f"png.chunk.{kind}",
                label=f"Unknown chunk {kind}",
                value=f"{len(chunk.data)} bytes",
                category=MetadataCategory.UNKNOWN,
                container=kind,
                removable=False,
                reason="unknown data: listed, never removed automatically",
                size=len(chunk.data),
            )
        )
    return fields


def _xmp_fields(text: str) -> list[MetadataField]:
    """Pull the few XMP properties that matter out of the packet, without a parser.

    XMP is RDF/XML: parsing it with a real parser buys nothing here (we display a
    handful of properties and never rewrite them), while a regex over a truncated
    value cannot be tricked into fetching anything.
    """
    fields: list[MetadataField] = []
    for name, label, category in _XMP_PROPERTIES:
        marker = f"{name}>"
        index = text.find(marker)
        if index < 0:
            continue
        tail = text[index + len(marker) :]
        end = tail.find("<")
        if end < 0:
            continue
        value = tail[:end].strip()
        if not value:
            continue
        fields.append(
            MetadataField(
                key=f"png.xmp.{name}",
                label=label,
                value=value[:MAX_DISPLAY_VALUE],
                category=category,
                container="iTXt/XMP",
                removable=False,
                reason="XMP is rewritten as a whole packet, not property by property",
                size=len(value),
            )
        )
    return fields


def inspect_png(data: bytes) -> Section:
    """Read one PNG: chunks, text, EXIF, XMP, ICC, signatures, technical facts."""
    chunks = iter_chunks(data)
    warnings: list[str] = []
    fields: list[MetadataField] = []
    manifests: list[tuple[str, bytes]] = []
    signatures: list[str] = []
    xmp_seen = False
    xmp_personal = False

    kinds = [chunk.kind for chunk in chunks]
    if not kinds or kinds[0] != "IHDR":
        warnings.append("the first chunk is not IHDR")
    if "IEND" not in kinds:
        warnings.append("the file has no IEND chunk (truncated?)")
    broken = [chunk for chunk in chunks if chunk.crc_ok is False]
    if broken:
        warnings.append(f"{len(broken)} chunk(s) fail their CRC check")

    for chunk in chunks:
        if chunk.kind in TEXT_CHUNKS:
            keyword, text, text_warnings = _decode_text(chunk)
            warnings.extend(text_warnings)
            if not keyword:
                continue
            if keyword == XMP_KEYWORD:
                xmp_seen = True
                properties = _xmp_fields(text)
                xmp_personal = any(item.sensitive for item in properties)
                fields.extend(properties)
                continue
            lowered = keyword.lower()
            category = _TEXT_KEYWORDS.get(lowered, MetadataCategory.UNKNOWN)
            removable = category is MetadataCategory.PERSONAL or (
                lowered in _ATTRIBUTION_KEYWORDS
            )
            fields.append(
                MetadataField(
                    key=f"png.text.{chunk.kind}.{keyword}",
                    label=keyword,
                    value=text[:MAX_DISPLAY_VALUE],
                    category=category,
                    container=chunk.kind,
                    removable=removable,
                    reason="" if removable else "kept by policy",
                    size=len(chunk.data),
                    truncated=len(text) > MAX_DISPLAY_VALUE,
                )
            )
        elif chunk.kind == "eXIf":
            tiff = _parse_tiff(chunk.data, warnings)
            if tiff is None:
                continue
            exif_fields, exif_warnings = _exif_fields(tiff)
            fields.extend(exif_fields)
            warnings.extend(exif_warnings)
        elif chunk.kind in SIGNATURE_CHUNKS:
            signatures.append(chunk.kind)
            manifests.append((chunk.kind, chunk.data))
    if xmp_seen:
        fields.append(
            MetadataField(
                key="png.xmp",
                label="XMP packet",
                value=(
                    "contains personal properties"
                    if xmp_personal
                    else "present, no personal properties recognised"
                ),
                category=(
                    MetadataCategory.PERSONAL if xmp_personal else MetadataCategory.PROVENANCE
                ),
                container="iTXt/XMP",
                removable=xmp_personal,
                reason="" if xmp_personal else "no personal property recognised",
                size=0,
            )
        )
    fields.extend(_chunk_fields(chunks))

    section_chunks = tuple(
        ContainerChunk(
            kind=chunk.kind,
            offset=chunk.offset,
            size=len(chunk.data),
            checksum_ok=chunk.crc_ok,
            note="" if chunk.crc_ok is not False else "CRC mismatch",
        )
        for chunk in chunks
    )
    checksum_ok: bool | None = None
    if any(chunk.crc_ok is not None for chunk in chunks):
        checksum_ok = not broken
    return Section(
        fields=tuple(fields),
        chunks=section_chunks,
        technical=tuple(_technical(chunks)),
        warnings=tuple(warnings),
        manifests=tuple(manifests),
        signature_blobs=tuple(signatures),
        checksum_ok=checksum_ok,
    )


# ---------------------------------------------------------------------------
# rewriting
# ---------------------------------------------------------------------------
def removal_from_keys(keys: frozenset[str]) -> PngRemoval:
    """Turn the selected field keys into the operations the rewriter understands."""
    keywords: set[str] = set()
    exif_tags: set[int] = set()
    gps_tags: set[int] = set()
    chunk_kinds: set[str] = set()
    drop_exif = False
    drop_xmp = False
    for key in keys:
        if not key.startswith("png."):
            continue
        parts = key.split(".")
        if len(parts) < 3:
            continue
        group = parts[1]
        if group == "text" and len(parts) >= 4:
            keywords.add(".".join(parts[3:]))
        elif group == "exif" and parts[2].startswith("0x"):
            exif_tags.add(int(parts[2], 16))
        elif group == "gps" and parts[2].startswith("0x"):
            gps_tags.add(int(parts[2], 16))
        elif group == "gps" and parts[2] == "coordinates":
            gps_tags.update(_GPS_TAGS)
        elif group == "chunk" and len(parts) >= 3:
            chunk_kinds.add(parts[2])
        elif group == "xmp":
            drop_xmp = True
    return PngRemoval(
        keywords=frozenset(keywords),
        exif_tags=frozenset(exif_tags),
        gps_tags=frozenset(gps_tags),
        drop_exif=drop_exif,
        drop_xmp=drop_xmp,
        chunk_kinds=frozenset(chunk_kinds),
    )


def rewrite_png(data: bytes, removal: PngRemoval) -> bytes:
    """Write a new PNG with the selected metadata removed and the pixels untouched."""
    if removal.empty:
        return data
    chunks = iter_chunks(data)
    warnings: list[str] = []
    out = bytearray(PNG_SIGNATURE)
    for chunk in chunks:
        kind = chunk.kind
        drop = kind in removal.chunk_kinds
        if not drop and kind in TEXT_CHUNKS:
            keyword = _keyword(chunk.data)
            drop = keyword in removal.keywords or (
                removal.drop_xmp and keyword == XMP_KEYWORD
            )
        payload = chunk.data
        if not drop and kind == "eXIf":
            drop = removal.drop_exif
            tiff = None if drop else _parse_tiff(payload, warnings)
            if tiff is not None:
                payload = _serialize_tiff(_apply_exif_removal(tiff, removal))
        if drop:
            continue
        out.extend(_CHUNK_HEADER.pack(len(payload), kind.encode("latin-1")))
        out.extend(payload)
        out.extend(_CHUNK_CRC.pack(zlib.crc32(kind.encode("latin-1") + payload) & 0xFFFFFFFF))
    if not out.endswith(b"IEND\xaeB`\x82"):
        # A file whose IEND was missing still gets one: the result must be a PNG.
        out.extend(_CHUNK_HEADER.pack(0, b"IEND"))
        out.extend(_CHUNK_CRC.pack(zlib.crc32(b"IEND") & 0xFFFFFFFF))
    return bytes(out)
