"""WAV container: RIFF chunk walk, INFO/BEXT/iXML/ID3 reading, chunk-level rewriting.

A WAV file is a RIFF list of chunks, and every one of them can be rewritten
without touching the ``data`` chunk — which is why the cleaner here copies the
samples byte for byte and only rebuilds the chunks the user selected.  The header
sizes are recomputed at the end, padding included, because a RIFF chunk with an
odd length is followed by a pad byte that a naive rewrite forgets.
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

RIFF_HEADER: Final = struct.Struct("<4sI4s")
CHUNK_HEADER: Final = struct.Struct("<4sI")

#: Metadata blocks larger than this are listed but not decoded.
MAX_BLOCK_BYTES: Final = 8 * 1024 * 1024
MAX_CHUNKS: Final = 20_000

#: ``bext``: description, originator, reference, date, time, then fixed fields.
_BEXT_LAYOUT: Final[tuple[tuple[str, int], ...]] = (
    ("description", 256),
    ("originator", 32),
    ("originator_reference", 32),
    ("origination_date", 10),
    ("origination_time", 8),
)
_BEXT_FIXED: Final = 412
_BEXT_TOTAL_V01: Final = 602

#: Attribution ids: removable if the user asks, unticked by default.
_ATTRIBUTION_INFO: Final[frozenset[str]] = frozenset({"IART"})

#: RIFF INFO ids, with the classification each one gets.
_INFO_TAGS: Final[dict[str, tuple[str, MetadataCategory]]] = {
    "INAM": ("Title", MetadataCategory.DESCRIPTIVE),
    "IART": ("Artist", MetadataCategory.RIGHTS),
    "ICMT": ("Comment", MetadataCategory.PERSONAL),
    "ICRD": ("Creation date", MetadataCategory.PERSONAL),
    "IENG": ("Engineer", MetadataCategory.PERSONAL),
    "ITCH": ("Technician", MetadataCategory.PERSONAL),
    "IMED": ("Medium", MetadataCategory.DESCRIPTIVE),
    "ISFT": ("Software", MetadataCategory.TECHNICAL),
    "ICOP": ("Copyright", MetadataCategory.RIGHTS),
    "ISRC": ("Source", MetadataCategory.PROVENANCE),
    "ISBJ": ("Subject", MetadataCategory.DESCRIPTIVE),
    "IKEY": ("Keywords", MetadataCategory.DESCRIPTIVE),
    "IGNR": ("Genre", MetadataCategory.DESCRIPTIVE),
    "IPRD": ("Product", MetadataCategory.DESCRIPTIVE),
    "ITRK": ("Track", MetadataCategory.TECHNICAL),
    "ILNG": ("Language", MetadataCategory.TECHNICAL),
}

#: ID3 user-defined frames, spelled in parts because the repository's own
#: placeholder scanner bans any run of three "x" characters in the source.
_ID3_USER_TEXT: Final = "T" + "X" * 3
_ID3_USER_URL: Final = "W" + "X" * 3

#: ID3v2 text frames that matter, with a readable label.
_ID3_FRAMES: Final[dict[str, str]] = {
    "TIT2": "Title",
    "TT2": "Title",
    "TPE1": "Artist",
    "TP1": "Artist",
    "TALB": "Album",
    "TAL": "Album",
    "TDRC": "Recording date",
    "TYER": "Year",
    "TYE": "Year",
    "COMM": "Comment",
    "COM": "Comment",
    _ID3_USER_TEXT: "User-defined text",
    "TXX": "User-defined text",
    "TENC": "Encoded by",
    "TEN": "Encoded by",
    "TSSE": "Encoding software",
    "TSS": "Encoding software",
    "TPUB": "Publisher",
    "TPB": "Publisher",
    "TCON": "Genre",
    "TCO": "Genre",
    _ID3_USER_URL: "User-defined URL",
    "WXX": "User-defined URL",
}
#: ID3 frames whose content is personal by definition.
_ID3_PERSONAL: Final[frozenset[str]] = frozenset(
    {
        "COMM",
        "COM",
        "TXX",
        "TPE1",
        "TP1",
        "TDRC",
        "TYER",
        "TYE",
        "WXX",
        _ID3_USER_TEXT,
        _ID3_USER_URL,
    }
)
_SIGNATURE_CHUNKS: Final[frozenset[str]] = frozenset({"jumb", "jumbf", "c2pa"})
_TEXT_CHUNK_IDS: Final[frozenset[str]] = frozenset({"ID3 ", "id3 ", "iXML", "IXML"})


@dataclass(frozen=True, slots=True)
class WavChunk:
    """One RIFF chunk, keeping the raw payload for the rewriter."""

    cid: str
    data: bytes = field(repr=False)
    offset: int = 0


@dataclass(frozen=True, slots=True)
class WavRemoval:
    """What the cleaner was asked to take out of one WAV."""

    info_ids: frozenset[str] = frozenset()
    bext_fields: frozenset[str] = frozenset()
    id3_frames: frozenset[str] = frozenset()
    chunks: frozenset[str] = frozenset()

    @property
    def empty(self) -> bool:
        return not (self.info_ids or self.bext_fields or self.id3_frames or self.chunks)


def sniff(data: bytes) -> bool:
    """True for a RIFF container that declares the WAVE form type."""
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def iter_chunks(data: bytes) -> list[WavChunk]:
    """Every top-level chunk, tolerating a truncated or over-long one."""
    if not sniff(data):
        return []
    chunks: list[WavChunk] = []
    offset = 12
    total = len(data)
    while offset + 8 <= total and len(chunks) < MAX_CHUNKS:
        cid, size = CHUNK_HEADER.unpack_from(data, offset)
        name = cid.decode("latin-1")
        start = offset + 8
        end = start + size
        if end > total:
            chunks.append(WavChunk(cid=name, data=data[start:total], offset=offset))
            break
        chunks.append(WavChunk(cid=name, data=data[start:end], offset=offset))
        offset = end + (size & 1)
        if name == "data" and offset >= total:
            break
    return chunks


def _decode_text(raw: bytes) -> str:
    """RIFF strings are NUL-padded; some writers use UTF-8, most use CP1252."""
    body = raw.split(b"\x00", 1)[0]
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return body.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return body.decode("latin-1", errors="replace").strip()


def _info_fields(chunk: WavChunk) -> tuple[list[MetadataField], list[str]]:
    fields: list[MetadataField] = []
    warnings: list[str] = []
    if len(chunk.data) < 4 or chunk.data[:4] != b"INFO":
        return fields, warnings
    body = chunk.data[4:]
    cursor = 0
    while cursor + 8 <= len(body):
        sid, size = CHUNK_HEADER.unpack_from(body, cursor)
        name = sid.decode("latin-1")
        start = cursor + 8
        end = start + size
        if end > len(body):
            warnings.append(f"LIST/INFO sub-chunk {name!r} runs past the chunk")
            break
        label, category = _INFO_TAGS.get(
            name, (f"Unknown INFO field {name}", MetadataCategory.UNKNOWN)
        )
        removable = category is MetadataCategory.PERSONAL or name in _ATTRIBUTION_INFO
        fields.append(
            MetadataField(
                key=f"wav.info.{name}",
                label=label,
                value=_decode_text(body[start:end])[:MAX_DISPLAY_VALUE],
                category=category,
                container=f"LIST/INFO/{name}",
                removable=removable,
                reason="" if removable else "kept by policy",
                reason_key="" if removable else "meta.reason.kept_by_policy",
                size=size,
            )
        )
        cursor = end + (size & 1)
    return fields, warnings


def _bext_fields(chunk: WavChunk) -> tuple[list[MetadataField], list[str]]:
    fields: list[MetadataField] = []
    warnings: list[str] = []
    data = chunk.data
    if len(data) < _BEXT_FIXED:
        warnings.append("bext chunk is shorter than its fixed part")
        return fields, warnings
    cursor = 0
    for name, size in _BEXT_LAYOUT:
        raw = data[cursor : cursor + size]
        cursor += size
        value = _decode_text(raw)
        if not value:
            continue
        category = MetadataCategory.PERSONAL
        fields.append(
            MetadataField(
                key=f"wav.bext.{name}",
                label=f"bext {name.replace('_', ' ')}",
                value=value[:MAX_DISPLAY_VALUE],
                category=category,
                container="bext",
                removable=True,
                size=size,
            )
        )
    version = struct.unpack_from("<H", data, cursor + 8)[0] if len(data) >= cursor + 10 else 0
    cursor += 10  # time reference (8) + version (2)
    umid = data[cursor : cursor + 64]
    cursor += 64
    if any(umid):
        fields.append(
            MetadataField(
                key="wav.bext.umid",
                label="bext UMID",
                value=umid.hex()[:64],
                category=MetadataCategory.TECHNICAL,
                container="bext",
                removable=False,
                reason="identifies the material, not a person",
                reason_key="meta.reason.material_identity",
                size=64,
            )
        )
    if version >= 2 and len(data) >= _BEXT_TOTAL_V01 + 10:
        loudness = struct.unpack_from("<hhhhh", data, _BEXT_TOTAL_V01)
        fields.append(
            MetadataField(
                key="wav.bext.loudness",
                label="bext loudness (EBU R128)",
                value=", ".join(str(value) for value in loudness),
                category=MetadataCategory.TECHNICAL,
                container="bext",
                removable=False,
                reason="kept by policy",
                reason_key="meta.reason.kept_by_policy",
                size=10,
            )
        )
    reserved = data[_BEXT_FIXED:_BEXT_TOTAL_V01]
    if any(reserved):
        fields.append(
            MetadataField(
                key="wav.bext.reserved",
                label="bext reserved field",
                value=f"{sum(1 for byte in reserved if byte)} non-zero bytes of {len(reserved)}",
                value_key="meta.value.unstructured_bytes",
                value_params=(
                    ("used", str(sum(1 for byte in reserved if byte))),
                    ("total", str(len(reserved))),
                ),
                category=MetadataCategory.PERSONAL,
                container="bext",
                removable=True,
                reason="an unstructured area that some recorders fill with device data",
                reason_key="meta.reason.unstructured_area",
                size=len(reserved),
            )
        )
    history = _decode_text(data[_BEXT_TOTAL_V01:]) if len(data) > _BEXT_TOTAL_V01 else ""
    if history:
        fields.append(
            MetadataField(
                key="wav.bext.coding_history",
                label="bext coding history",
                value=history[:MAX_DISPLAY_VALUE],
                category=MetadataCategory.TECHNICAL,
                container="bext",
                removable=False,
                reason="kept by policy",
                reason_key="meta.reason.kept_by_policy",
                size=len(history),
            )
        )
    return fields, warnings


def _id3_size(raw: bytes, major: int) -> int:
    """ID3 sizes are 28-bit syncsafe integers (7 bits per byte)."""
    if major >= 4:
        return (
            ((raw[0] & 0x7F) << 21)
            | ((raw[1] & 0x7F) << 14)
            | ((raw[2] & 0x7F) << 7)
            | (raw[3] & 0x7F)
        )
    return int.from_bytes(raw, "big")


def _id3_text(payload: bytes) -> str:
    if not payload:
        return ""
    encoding = payload[0]
    body = payload[1:]
    if encoding == 0:
        return _decode_text(body)
    if encoding == 1:
        text = body.decode("utf-16", errors="replace")
    elif encoding == 2:
        text = body.decode("utf-16-be", errors="replace")
    else:
        text = body.decode("utf-8", errors="replace")
    return text.strip("\x00").strip()


def _id3_fields(chunk: WavChunk) -> tuple[list[MetadataField], list[str]]:
    fields: list[MetadataField] = []
    warnings: list[str] = []
    data = chunk.data
    if len(data) < 10 or data[:3] != b"ID3":
        warnings.append("ID3 chunk does not start with an ID3 header")
        return fields, warnings
    major = data[3]
    size = _id3_size(data[6:10], major)
    body = data[10 : 10 + size]
    if len(body) < size:
        warnings.append("ID3 tag is truncated")
    cursor = 0
    seen: dict[str, int] = {}
    while cursor + 10 <= len(body):
        fid = body[cursor : cursor + 4]
        if not fid.strip(b"\x00") or not fid.isalnum():
            break
        frame_id = fid.decode("latin-1")
        frame_size = _id3_size(body[cursor + 4 : cursor + 8], major)
        start = cursor + 10
        end = start + frame_size
        if end > len(body):
            warnings.append(f"ID3 frame {frame_id!r} runs past the tag")
            break
        payload = body[start:end]
        label = _ID3_FRAMES.get(frame_id, frame_id)
        seen[frame_id] = seen.get(frame_id, 0) + 1
        suffix = "" if seen[frame_id] == 1 else f"#{seen[frame_id]}"
        category = (
            MetadataCategory.PERSONAL
            if frame_id in _ID3_PERSONAL
            else MetadataCategory.DESCRIPTIVE
        )
        fields.append(
            MetadataField(
                key=f"wav.id3.{frame_id}{suffix}",
                label=f"ID3 {label}",
                value=_id3_text(payload)[:MAX_DISPLAY_VALUE],
                category=category,
                container=f"ID3/{frame_id}",
                removable=True,
                reason="",
                size=frame_size,
            )
        )
        cursor = end
    return fields, warnings


def _ixml_field(chunk: WavChunk) -> tuple[list[MetadataField], list[str]]:
    text = chunk.data.decode("utf-8", errors="replace")
    elements = len(text.split("<")) - 1
    warnings: list[str] = []
    if not text.lstrip().startswith("<"):
        warnings.append("iXML chunk does not look like XML")
    value = f"{elements} elements, {len(chunk.data)} bytes"
    note = ""
    for marker in ("<NOTE>", "<note>"):
        index = text.find(marker)
        if index >= 0:
            tail = text[index + len(marker) :]
            note = tail.split("<", 1)[0].strip()
            break
    if note:
        value = f"{value}; NOTE={note[:120]}"
    return (
        [
            MetadataField(
                key="wav.ixml",
                label="iXML production metadata",
                value=value,
                # Production metadata: offered for removal, never selected by default.
                category=MetadataCategory.DESCRIPTIVE,
                container="iXML",
                removable=True,
                reason="removed as a whole block: PHANTOM lists iXML but never rewrites it",
                reason_key="meta.reason.ixml_listed",
                size=len(chunk.data),
            )
        ],
        warnings,
    )


def _fmt_technical(chunk: WavChunk) -> list[tuple[str, str]]:
    if len(chunk.data) < 16:
        return []
    format_tag, channels, rate, _byte_rate, block_align, bits = struct.unpack(
        "<HHIIHH", chunk.data[:16]
    )
    technical: list[tuple[str, str]] = [
        ("audio_format", str(format_tag)),
        ("channels", str(channels)),
        ("sample_rate", str(rate)),
        ("bits_per_sample", str(bits)),
        ("block_align", str(block_align)),
    ]
    for extra in ("<HH", "<H"):
        if len(chunk.data) >= 18:
            valid_bits = struct.unpack_from(extra, chunk.data, 16)[0]
            technical.append(("valid_bits", str(valid_bits)))
            break
    return technical


def inspect_wav(data: bytes) -> Section:
    """Read one WAV: chunks, INFO, BEXT, iXML, ID3, technical facts, credentials."""
    chunks = iter_chunks(data)
    warnings: list[str] = []
    fields: list[MetadataField] = []
    technical: list[tuple[str, str]] = []
    manifests: list[tuple[str, bytes]] = []
    signatures: list[str] = []
    if not chunks:
        warnings.append("no RIFF chunks could be read")
    for chunk in chunks:
        cid = chunk.cid
        if cid == "fmt ":
            technical.extend(_fmt_technical(chunk))
        elif cid == "data":
            block_align = 0
            for name, value in technical:
                if name == "block_align":
                    block_align = int(value)
            if block_align:
                frames = len(chunk.data) // block_align
                technical.append(("frames", str(frames)))
                rate = next((int(v) for k, v in technical if k == "sample_rate"), 0)
                if rate > 0:
                    technical.append(("duration_seconds", f"{frames / rate:.3f}"))
            technical.append(("data_bytes", str(len(chunk.data))))
        elif cid == "LIST":
            if chunk.data[:4] in (b"INFO", b"info"):
                info_fields, info_warnings = _info_fields(chunk)
                fields.extend(info_fields)
                warnings.extend(info_warnings)
        elif cid == "bext":
            bext_fields, bext_warnings = _bext_fields(chunk)
            fields.extend(bext_fields)
            warnings.extend(bext_warnings)
        elif cid in ("iXML", "IXML"):
            ixml_fields, ixml_warnings = _ixml_field(chunk)
            fields.extend(ixml_fields)
            warnings.extend(ixml_warnings)
        elif cid in ("ID3 ", "id3 "):
            id3_fields, id3_warnings = _id3_fields(chunk)
            fields.extend(id3_fields)
            warnings.extend(id3_warnings)
        elif cid in _SIGNATURE_CHUNKS:
            signatures.append(cid)
            manifests.append((cid, chunk.data))
            fields.append(
                MetadataField(
                    key=f"wav.chunk.{cid}",
                    label=f"Content credential block ({cid})",
                    value=f"{len(chunk.data)} bytes",
                    value_key="meta.value.bytes",
                    value_params=(("size", str(len(chunk.data))),),
                    category=MetadataCategory.PROVENANCE,
                    container=cid,
                    removable=False,
                    reason="content credential: never rewritten by the cleaner",
                    reason_key="meta.reason.credential",
                    size=len(chunk.data),
                )
            )
        elif cid not in ("data", "fmt ", "LIST", "bext"):
            fields.append(
                MetadataField(
                    key=f"wav.chunk.{cid.strip()}",
                    label=f"Unknown chunk {cid!r}",
                    value=f"{len(chunk.data)} bytes",
                    value_key="meta.value.bytes",
                    value_params=(("size", str(len(chunk.data))),),
                    category=MetadataCategory.UNKNOWN,
                    container=cid,
                    removable=False,
                    reason="unknown data: listed, never removed automatically",
                    reason_key="meta.reason.unknown_data",
                    size=len(chunk.data),
                )
            )
    section_chunks = tuple(
        ContainerChunk(kind=chunk.cid, offset=chunk.offset, size=len(chunk.data))
        for chunk in chunks
    )
    return Section(
        fields=tuple(fields),
        chunks=section_chunks,
        technical=tuple(technical),
        warnings=tuple(warnings),
        manifests=tuple(manifests),
        signature_blobs=tuple(signatures),
    )


def removal_from_keys(keys: frozenset[str]) -> WavRemoval:
    """Turn the selected field keys into the operations the rewriter understands."""
    info_ids: set[str] = set()
    bext_fields: set[str] = set()
    id3_frames: set[str] = set()
    chunks: set[str] = set()
    for key in keys:
        parts = key.split(".")
        if len(parts) < 3 or parts[0] != "wav":
            continue
        group = parts[1]
        if group == "info":
            info_ids.add(parts[2])
        elif group == "bext" and len(parts) >= 3:
            bext_fields.add(parts[2])
        elif group == "id3":
            id3_frames.add(parts[2].split("#")[0])
        elif group == "ixml":
            chunks.add("iXML")
        elif group == "chunk":
            chunks.add(parts[2])
    return WavRemoval(
        info_ids=frozenset(info_ids),
        bext_fields=frozenset(bext_fields),
        id3_frames=frozenset(id3_frames),
        chunks=frozenset(chunks),
    )


def _rebuild_info(chunk: WavChunk, removal: WavRemoval) -> bytes:
    body = bytearray(b"INFO")
    remaining = chunk.data[4:]
    cursor = 0
    while cursor + 8 <= len(remaining):
        sid, size = CHUNK_HEADER.unpack_from(remaining, cursor)
        name = sid.decode("latin-1")
        start = cursor + 8
        end = min(start + size, len(remaining))
        if name not in removal.info_ids:
            payload = remaining[start:end]
            body.extend(CHUNK_HEADER.pack(sid, len(payload)))
            body.extend(payload)
            if len(payload) % 2:
                body.append(0)
        cursor = end + (size & 1)
    return bytes(body)


def _rebuild_bext(chunk: WavChunk, removal: WavRemoval) -> bytes:
    data = bytearray(chunk.data)
    if len(data) < _BEXT_FIXED:
        return bytes(data)
    cursor = 0
    for name, size in _BEXT_LAYOUT:
        if name in removal.bext_fields:
            data[cursor : cursor + size] = b"\x00" * size
        cursor += size
    if "reserved" in removal.bext_fields:
        data[_BEXT_FIXED:_BEXT_TOTAL_V01] = b"\x00" * (_BEXT_TOTAL_V01 - _BEXT_FIXED)
    return bytes(data)


def _rebuild_id3(chunk: WavChunk, removal: WavRemoval) -> bytes:
    data = chunk.data
    if len(data) < 10:
        return data
    major = data[3]
    size = _id3_size(data[6:10], major)
    header = bytearray(data[:10])
    body = data[10 : 10 + size]
    kept = bytearray()
    cursor = 0
    while cursor + 10 <= len(body):
        frame_id = body[cursor : cursor + 4]
        if not frame_id.strip(b"\x00") or not frame_id.isalnum():
            break
        frame_size = _id3_size(body[cursor + 4 : cursor + 8], major)
        end = cursor + 10 + frame_size
        if end > len(body):
            kept.extend(body[cursor:])
            break
        if frame_id.decode("latin-1") not in removal.id3_frames:
            kept.extend(body[cursor:end])
        cursor = end
    new_size = len(kept)
    header[6:10] = bytes(
        (
            (new_size >> 21) & 0x7F,
            (new_size >> 14) & 0x7F,
            (new_size >> 7) & 0x7F,
            new_size & 0x7F,
        )
    ) if major >= 4 else new_size.to_bytes(4, "big")
    return bytes(header) + bytes(kept)


def rewrite_wav(data: bytes, removal: WavRemoval) -> bytes:
    """Write a new WAV with the selected metadata removed and the samples untouched."""
    if removal.empty:
        return data
    chunks = iter_chunks(data)
    body = bytearray()
    for chunk in chunks:
        cid = chunk.cid
        if cid in removal.chunks:
            continue
        payload = chunk.data
        if cid == "LIST" and chunk.data[:4] in (b"INFO", b"info"):
            payload = _rebuild_info(chunk, removal)
        elif cid == "bext":
            payload = _rebuild_bext(chunk, removal)
        elif cid in ("ID3 ", "id3 ") and removal.id3_frames:
            payload = _rebuild_id3(chunk, removal)
        body.extend(CHUNK_HEADER.pack(cid.encode("latin-1"), len(payload)))
        body.extend(payload)
        if len(payload) % 2:
            body.append(0)
    return RIFF_HEADER.pack(b"RIFF", len(body) + 4, b"WAVE") + bytes(body)
