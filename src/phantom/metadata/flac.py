"""FLAC container: metadata block walk, Vorbis comments, pictures, rewriting.

Every FLAC metadata block is length-prefixed, so removal is a matter of building a
new block list and copying the audio frames that follow it verbatim.  The
STREAMINFO block is rebuilt from its original bytes (only the last-block flag ever
changes), and the MD5 in it stays valid because not one sample is touched.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Final

from phantom.metadata.models import (
    MAX_DISPLAY_VALUE,
    ContainerChunk,
    MetadataCategory,
    MetadataField,
    Section,
)

FLAC_MAGIC: Final = b"fLaC"

BLOCK_STREAMINFO: Final = 0
BLOCK_PADDING: Final = 1
BLOCK_APPLICATION: Final = 2
BLOCK_SEEKTABLE: Final = 3
BLOCK_VORBIS_COMMENT: Final = 4
BLOCK_CUESHEET: Final = 5
BLOCK_PICTURE: Final = 6

BLOCK_NAMES: Final[dict[int, str]] = {
    BLOCK_STREAMINFO: "STREAMINFO",
    BLOCK_PADDING: "PADDING",
    BLOCK_APPLICATION: "APPLICATION",
    BLOCK_SEEKTABLE: "SEEKTABLE",
    BLOCK_VORBIS_COMMENT: "VORBIS_COMMENT",
    BLOCK_CUESHEET: "CUESHEET",
    BLOCK_PICTURE: "PICTURE",
    7: "UNKNOWN(7)",
    8: "UNKNOWN(8)",
    9: "UNKNOWN(9)",
    10: "UNKNOWN(10)",
}

MAX_BLOCK_BYTES: Final = 16 * 1024 * 1024
MAX_BLOCKS: Final = 512

#: Attribution fields: removable if the user asks, unticked by default.
_ATTRIBUTION_KEYS: Final[frozenset[str]] = frozenset({"ARTIST", "ALBUMARTIST", "PERFORMER"})

#: Vorbis comment fields and how each one is classified.
_VORBIS_TAGS: Final[dict[str, MetadataCategory]] = {
    "TITLE": MetadataCategory.DESCRIPTIVE,
    "ARTIST": MetadataCategory.RIGHTS,
    "ALBUM": MetadataCategory.DESCRIPTIVE,
    "ALBUMARTIST": MetadataCategory.RIGHTS,
    "DATE": MetadataCategory.PERSONAL,
    "YEAR": MetadataCategory.PERSONAL,
    "COMMENT": MetadataCategory.PERSONAL,
    "DESCRIPTION": MetadataCategory.DESCRIPTIVE,
    "GENRE": MetadataCategory.DESCRIPTIVE,
    "TRACKNUMBER": MetadataCategory.TECHNICAL,
    "DISCNUMBER": MetadataCategory.TECHNICAL,
    "ENCODER": MetadataCategory.TECHNICAL,
    "ENCODED-BY": MetadataCategory.TECHNICAL,
    "ENCODING": MetadataCategory.TECHNICAL,
    "COPYRIGHT": MetadataCategory.RIGHTS,
    "LICENSE": MetadataCategory.RIGHTS,
    "ORGANIZATION": MetadataCategory.RIGHTS,
    "LABEL": MetadataCategory.RIGHTS,
    "ISRC": MetadataCategory.TECHNICAL,
    "LOCATION": MetadataCategory.PERSONAL,
    "CONTACT": MetadataCategory.PERSONAL,
    "OWNER": MetadataCategory.PERSONAL,
    "SERIAL": MetadataCategory.PERSONAL,
    "SERIALNUMBER": MetadataCategory.PERSONAL,
    "GPS": MetadataCategory.PERSONAL,
    "GPSLATITUDE": MetadataCategory.PERSONAL,
    "GPSLONGITUDE": MetadataCategory.PERSONAL,
    "USERCOMMENT": MetadataCategory.PERSONAL,
}

#: Freeform keys whose *name* already says "this is about a person or a device".
_PERSONAL_HINTS: Final[tuple[str, ...]] = (
    "OWNER",
    "SERIAL",
    "GPS",
    "LOCATION",
    "EMAIL",
    "PHONE",
    "AUTHOR",
    "CONTACT",
)

_PICTURE_TYPES: Final[dict[int, str]] = {
    0: "Other",
    1: "32x32 file icon",
    2: "Other file icon",
    3: "Cover (front)",
    4: "Cover (back)",
    5: "Leaflet page",
    6: "Media",
    7: "Lead artist",
    8: "Artist",
    9: "Conductor",
    10: "Band",
    11: "Composer",
    12: "Lyricist",
    13: "Recording location",
    14: "During recording",
    15: "During performance",
    16: "Screen capture",
    17: "Bright coloured fish",
    18: "Illustration",
    19: "Band logo",
    20: "Publisher logo",
}

SIGNATURE_APPLICATION_IDS: Final[frozenset[bytes]] = frozenset({b"c2pa", b"jumb", b"JUMB"})


@dataclass(frozen=True, slots=True)
class FlacBlock:
    """One metadata block, with its raw payload and its position."""

    kind: int
    data: bytes = field(repr=False)
    offset: int = 0
    index: int = 0

    @property
    def name(self) -> str:
        return BLOCK_NAMES.get(self.kind, f"UNKNOWN({self.kind})")


@dataclass(frozen=True, slots=True)
class FlacRemoval:
    """What the cleaner was asked to take out of one FLAC file."""

    vorbis_keys: frozenset[str] = frozenset()
    pictures: frozenset[int] = frozenset()
    applications: frozenset[int] = frozenset()
    drop_blocks: frozenset[int] = frozenset()

    @property
    def empty(self) -> bool:
        return not (self.vorbis_keys or self.pictures or self.applications or self.drop_blocks)


def sniff(data: bytes) -> bool:
    """True when the bytes start with the FLAC stream marker."""
    return data.startswith(FLAC_MAGIC)


def iter_blocks(data: bytes) -> list[FlacBlock]:
    """Every metadata block, tolerating a truncated last one."""
    if not sniff(data):
        return []
    blocks: list[FlacBlock] = []
    offset = len(FLAC_MAGIC)
    total = len(data)
    index = 0
    while offset + 4 <= total and len(blocks) < MAX_BLOCKS:
        header = data[offset]
        length = int.from_bytes(data[offset + 1 : offset + 4], "big")
        start = offset + 4
        end = start + length
        if end > total:
            blocks.append(
                FlacBlock(
                    kind=header & 0x7F, data=data[start:total], offset=offset, index=index
                )
            )
            break
        blocks.append(
            FlacBlock(kind=header & 0x7F, data=data[start:end], offset=offset, index=index)
        )
        index += 1
        offset = end
        if header & 0x80:
            break
    return blocks


def _streaminfo_technical(data: bytes) -> list[tuple[str, str]]:
    if len(data) < 34:
        return []
    min_block, max_block = struct.unpack_from(">HH", data, 0)
    packed = int.from_bytes(data[10:18], "big")
    sample_rate = packed >> 44
    channels = ((packed >> 41) & 0x7) + 1
    bits = ((packed >> 36) & 0x1F) + 1
    total_samples = packed & 0xFFFFFFFFF
    technical = [
        ("sample_rate", str(sample_rate)),
        ("channels", str(channels)),
        ("bits_per_sample", str(bits)),
        ("total_samples", str(total_samples)),
        ("min_block_size", str(min_block)),
        ("max_block_size", str(max_block)),
    ]
    if sample_rate:
        seconds = total_samples / sample_rate
        technical.append(("duration_seconds", f"{seconds:.3f}"))
    technical.append(("stream_md5", data[18:34].hex()))
    return technical


def _vorbis_fields(block: FlacBlock) -> tuple[list[MetadataField], list[str]]:
    fields: list[MetadataField] = []
    warnings: list[str] = []
    data = block.data
    if len(data) < 4:
        warnings.append("VORBIS_COMMENT block is too short")
        return fields, warnings
    vendor_length = int.from_bytes(data[0:4], "little")
    if 4 + vendor_length + 4 > len(data):
        warnings.append("VORBIS_COMMENT vendor string runs past the block")
        return fields, warnings
    vendor = data[4 : 4 + vendor_length].decode("utf-8", errors="replace")
    fields.append(
        MetadataField(
            key="flac.vendor",
            label="Vorbis vendor",
            value=vendor[:MAX_DISPLAY_VALUE],
            category=MetadataCategory.TECHNICAL,
            container="VORBIS_COMMENT",
            removable=False,
            reason="written by the encoder; kept by policy",
            size=vendor_length,
        )
    )
    cursor = 4 + vendor_length
    count = int.from_bytes(data[cursor : cursor + 4], "little")
    cursor += 4
    seen: dict[str, int] = {}
    for _index in range(min(count, 4096)):
        if cursor + 4 > len(data):
            warnings.append("VORBIS_COMMENT entry count exceeds the block")
            break
        length = int.from_bytes(data[cursor : cursor + 4], "little")
        cursor += 4
        raw = data[cursor : cursor + length]
        cursor += length
        try:
            entry = raw.decode("utf-8")
        except UnicodeDecodeError:
            warnings.append("VORBIS_COMMENT entry is not valid UTF-8; replacing bad bytes")
            entry = raw.decode("utf-8", errors="replace")
        key, _, value = entry.partition("=")
        upper = key.upper()
        seen[upper] = seen.get(upper, 0) + 1
        suffix = "" if seen[upper] == 1 else f"#{seen[upper]}"
        category = _VORBIS_TAGS.get(upper)
        if category is None:
            category = (
                MetadataCategory.PERSONAL
                if any(hint in upper for hint in _PERSONAL_HINTS)
                else MetadataCategory.UNKNOWN
            )
        removable = category is MetadataCategory.PERSONAL or upper in _ATTRIBUTION_KEYS
        fields.append(
            MetadataField(
                key=f"flac.vorbis.{upper}{suffix}",
                label=key,
                value=value[:MAX_DISPLAY_VALUE],
                category=category,
                container="VORBIS_COMMENT",
                removable=removable,
                reason="" if removable else "kept by policy",
                size=length,
                truncated=len(value) > MAX_DISPLAY_VALUE,
            )
        )
    return fields, warnings


def _picture_field(block: FlacBlock) -> tuple[list[MetadataField], list[str]]:
    warnings: list[str] = []
    data = block.data
    if len(data) < 32:
        return [], ["PICTURE block is too short"]
    picture_type = int.from_bytes(data[0:4], "big")
    mime_length = int.from_bytes(data[4:8], "big")
    cursor = 8
    mime = data[cursor : cursor + mime_length].decode("latin-1")
    cursor += mime_length
    description_length = int.from_bytes(data[cursor : cursor + 4], "big")
    cursor += 4
    description = data[cursor : cursor + description_length].decode("utf-8", errors="replace")
    cursor += description_length
    width, height, _depth, _colours = struct.unpack_from(">IIII", data, cursor)
    image_bytes = int.from_bytes(data[cursor + 16 : cursor + 20], "big")
    label = _PICTURE_TYPES.get(picture_type, f"type {picture_type}")
    value = f"{label}, {mime or 'unknown type'}, {width}x{height} px, {image_bytes} bytes"
    if description:
        value = f"{value}; description {description!r}"
    warnings.append(
        "an embedded picture can carry metadata of its own (EXIF, GPS); "
        "PHANTOM does not inspect inside it"
    )
    return (
        [
            MetadataField(
                key=f"flac.picture.{block.index}",
                label="Embedded picture",
                value=value,
                # Cover art is content, not personal data: offered, never default.
                category=MetadataCategory.DESCRIPTIVE,
                container="PICTURE",
                removable=True,
                reason="removed as a whole picture",
                size=len(data),
            )
        ],
        warnings,
    )


def _application_field(block: FlacBlock) -> tuple[list[MetadataField], list[str], bool]:
    data = block.data
    app_id = data[:4]
    payload = data[4:]
    signed = app_id in SIGNATURE_APPLICATION_IDS or b"jumb" in payload[:16]
    if signed:
        return (
            [
                MetadataField(
                    key=f"flac.application.{block.index}",
                    label="Content credential block",
                    value=f"application {app_id.decode('latin-1')!r}, {len(payload)} bytes",
                    category=MetadataCategory.PROVENANCE,
                    container="APPLICATION",
                    removable=False,
                    reason="content credential: never rewritten by the cleaner",
                    size=len(data),
                )
            ],
            [],
            True,
        )
    return (
        [
            MetadataField(
                key=f"flac.application.{block.index}",
                label="Application block",
                value=f"application {app_id.decode('latin-1')!r}, {len(payload)} bytes",
                category=MetadataCategory.UNKNOWN,
                container="APPLICATION",
                removable=True,
                reason="removed as a whole block",
                size=len(data),
            )
        ],
        [],
        False,
    )


def _cuesheet_field(block: FlacBlock) -> tuple[list[MetadataField], list[str]]:
    data = block.data
    if len(data) < 396:
        return [], ["CUESHEET block is too short"]
    catalog = data[:128].split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
    tracks = data[395] if len(data) > 395 else 0
    value = f"{tracks} track(s)"
    if catalog:
        value = f"{value}, media catalog {catalog}"
    return (
        [
            MetadataField(
                key="flac.cuesheet",
                label="CUE sheet",
                value=value,
                category=MetadataCategory.TECHNICAL,
                container="CUESHEET",
                removable=False,
                reason="kept by policy",
                size=len(data),
            )
        ],
        [],
    )


def inspect_flac(data: bytes) -> Section:
    """Read one FLAC file: blocks, comments, pictures, applications, technical facts."""
    blocks = iter_blocks(data)
    warnings: list[str] = []
    fields: list[MetadataField] = []
    technical: list[tuple[str, str]] = []
    manifests: list[tuple[str, bytes]] = []
    signatures: list[str] = []
    if not blocks:
        warnings.append("no FLAC metadata blocks could be read")
    elif blocks[0].kind != BLOCK_STREAMINFO:
        warnings.append("the first block is not STREAMINFO")
    for block in blocks:
        if block.kind == BLOCK_STREAMINFO:
            technical.extend(_streaminfo_technical(block.data))
        elif block.kind == BLOCK_VORBIS_COMMENT:
            vorbis_fields, vorbis_warnings = _vorbis_fields(block)
            fields.extend(vorbis_fields)
            warnings.extend(vorbis_warnings)
        elif block.kind == BLOCK_PICTURE:
            picture_fields, picture_warnings = _picture_field(block)
            fields.extend(picture_fields)
            warnings.extend(picture_warnings)
        elif block.kind == BLOCK_APPLICATION:
            app_fields, app_warnings, signed = _application_field(block)
            fields.extend(app_fields)
            warnings.extend(app_warnings)
            if signed:
                signatures.append(f"APPLICATION[{block.index}]")
                manifests.append((f"APPLICATION[{block.index}]", block.data))
        elif block.kind == BLOCK_CUESHEET:
            cue_fields, cue_warnings = _cuesheet_field(block)
            fields.extend(cue_fields)
            warnings.extend(cue_warnings)
        elif block.kind == BLOCK_SEEKTABLE:
            points = len(block.data) // 18
            technical.append(("seek_points", str(points)))
        elif block.kind == BLOCK_PADDING:
            technical.append(("padding_bytes", str(len(block.data))))
        else:
            fields.append(
                MetadataField(
                    key=f"flac.block.{block.index}",
                    label=f"Unknown block {block.name}",
                    value=f"{len(block.data)} bytes",
                    category=MetadataCategory.UNKNOWN,
                    container=block.name,
                    removable=False,
                    reason="unknown data: listed, never removed automatically",
                    size=len(block.data),
                )
            )
    section_chunks = tuple(
        ContainerChunk(kind=block.name, offset=block.offset, size=len(block.data))
        for block in blocks
    )
    return Section(
        fields=tuple(fields),
        chunks=section_chunks,
        technical=tuple(technical),
        warnings=tuple(warnings),
        manifests=tuple(manifests),
        signature_blobs=tuple(signatures),
    )


def removal_from_keys(keys: frozenset[str]) -> FlacRemoval:
    """Turn the selected field keys into the operations the rewriter understands."""
    vorbis_keys: set[str] = set()
    pictures: set[int] = set()
    applications: set[int] = set()
    drop_blocks: set[int] = set()
    for key in keys:
        parts = key.split(".")
        if len(parts) < 3 or parts[0] != "flac":
            continue
        group = parts[1]
        if group == "vorbis":
            vorbis_keys.add(parts[2].split("#")[0])
        elif group == "picture":
            pictures.add(int(parts[2]))
        elif group == "application":
            applications.add(int(parts[2]))
        elif group == "block":
            drop_blocks.add(int(parts[2]))
        elif group == "cuesheet":
            drop_blocks.add(BLOCK_CUESHEET)
    return FlacRemoval(
        vorbis_keys=frozenset(vorbis_keys),
        pictures=frozenset(pictures),
        applications=frozenset(applications),
        drop_blocks=frozenset(drop_blocks),
    )


def _rebuild_vorbis(block: FlacBlock, removal: FlacRemoval) -> bytes:
    data = block.data
    vendor_length = int.from_bytes(data[0:4], "little")
    vendor = data[4 : 4 + vendor_length]
    entries: list[bytes] = []
    cursor = 4 + vendor_length
    count = int.from_bytes(data[cursor : cursor + 4], "little")
    cursor += 4
    for _index in range(min(count, 4096)):
        if cursor + 4 > len(data):
            break
        length = int.from_bytes(data[cursor : cursor + 4], "little")
        cursor += 4
        raw = data[cursor : cursor + length]
        cursor += length
        key = raw.split(b"=", 1)[0].decode("utf-8", errors="replace").upper()
        if key not in removal.vorbis_keys:
            entries.append(raw)
    out = bytearray(len(vendor).to_bytes(4, "little"))
    out.extend(vendor)
    out.extend(len(entries).to_bytes(4, "little"))
    for entry in entries:
        out.extend(len(entry).to_bytes(4, "little"))
        out.extend(entry)
    return bytes(out)


def rewrite_flac(data: bytes, removal: FlacRemoval) -> bytes:
    """Write a new FLAC file with the selected metadata removed and the audio untouched."""
    if removal.empty:
        return data
    blocks = iter_blocks(data)
    audio_offset = len(data)
    rebuilt: list[FlacBlock] = []
    for block in blocks:
        if block.kind == BLOCK_VORBIS_COMMENT:
            rebuilt.append(
                FlacBlock(
                    kind=block.kind,
                    data=_rebuild_vorbis(block, removal),
                    offset=block.offset,
                    index=block.index,
                )
            )
        elif (
            (block.kind == BLOCK_PICTURE and block.index in removal.pictures)
            or (block.kind == BLOCK_APPLICATION and block.index in removal.applications)
            or block.kind in removal.drop_blocks
        ):
            continue
        else:
            rebuilt.append(block)
        audio_offset = block.offset + 4 + len(block.data)
    if not rebuilt or rebuilt[0].kind != BLOCK_STREAMINFO:
        # Never write a block list that a decoder would reject.
        return data
    frames = data[audio_offset:]
    out = bytearray(FLAC_MAGIC)
    for position, block in enumerate(rebuilt):
        header = block.kind & 0x7F
        if position == len(rebuilt) - 1:
            header |= 0x80
        out.append(header)
        out.extend(len(block.data).to_bytes(3, "big"))
        out.extend(block.data)
    out.extend(frames)
    return bytes(out)
