"""Synthetic PNG, WAV and FLAC files with known metadata, built byte by byte.

The tests need containers whose metadata is *known*: an inspector test that
asserts "a comment was found" proves nothing if the comment came from whatever
image editor happened to make the fixture.  Everything here is generated on the
fly in ``tmp_path``, so no binary fixture is committed and the small-fixtures
rule in ``test_repo.py`` stays satisfied.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Final

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
SAMPLE_RATE: Final = 8000
FRAMES: Final = 400


def texture(width: int = 48, height: int = 32, *, seed: int = 7) -> np.ndarray:
    """A deterministic RGB image: structure to look at, noise to compress badly."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    base = (np.sin(xx / 5.0) * 45 + np.cos(yy / 7.0) * 30 + 128).astype(np.int16)
    channels = [
        np.clip(base + rng.integers(-8, 9, base.shape), 0, 255),
        np.clip(base + 20 + rng.integers(-8, 9, base.shape), 0, 255),
        np.clip(base - 12 + rng.integers(-8, 9, base.shape), 0, 255),
    ]
    return np.stack(channels, axis=-1).astype(np.uint8)


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    """One PNG chunk: length, type, payload, CRC — the whole wire format."""
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def png_bytes(
    *,
    text: dict[str, str] | None = None,
    exif: Image.Exif | None = None,
    extra: tuple[tuple[bytes, bytes], ...] = (),
    pixels: np.ndarray | None = None,
) -> bytes:
    """A PNG with the given tEXt entries, EXIF payload and trailing chunks."""
    image = Image.fromarray(texture() if pixels is None else pixels, "RGB")
    info = PngInfo()
    for key, value in (text or {}).items():
        info.add_text(key, value)
    buffer = bytearray()
    import io

    with io.BytesIO() as handle:
        image.save(handle, format="PNG", pnginfo=info, exif=exif, compress_level=1)
        buffer.extend(handle.getvalue())
    # Everything after IHDR is ours to extend; IEND must stay last.
    assert buffer.endswith(b"IEND\xaeB`\x82"), "unexpected PNG tail"
    tail = png_chunk(b"IEND", b"")
    body = buffer[: -len(tail)]
    for kind, payload in extra:
        body += png_chunk(kind, payload)
    return bytes(body + tail)


def gps_exif() -> Image.Exif:
    """EXIF with a camera model, a serial number and a GPS position."""
    exif = Image.Exif()
    exif[0x010F] = "PhantomCam"  # Make
    exif[0x0110] = "PhantomCam One"  # Model
    exif[0x0131] = "phantom-software 1.0"  # Software
    exif[0x013B] = "Ada Lovelace"  # Artist
    exif[0x0132] = "2024:05:06 07:08:09"  # DateTime
    exif[0x8298] = "(c) 2024 Ada"  # Copyright
    exif[0xA431] = "SN-0001-42"  # BodySerialNumber
    from fractions import Fraction

    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (Fraction(50), Fraction(27), Fraction(0))  # 50°27'0"
    gps[3] = "E"
    gps[4] = (Fraction(30), Fraction(31), Fraction(0))  # 30°31'0"
    return exif


def png_file(
    path: Path,
    *,
    text: dict[str, str] | None = None,
    exif: Image.Exif | None = None,
    extra: tuple[tuple[bytes, bytes], ...] = (),
) -> Path:
    path.write_bytes(png_bytes(text=text, exif=exif, extra=extra))
    return path


def personal_png(
    path: Path,
    *,
    text: dict[str, str] | None = None,
    extra: tuple[tuple[bytes, bytes], ...] = (),
) -> Path:
    """A PNG with personal text, personal EXIF and a copyright notice."""
    values = {
        "Title": "Holiday",
        "Author": "Ada Lovelace",
        "Comment": "shot at home, wifi hunter2",
        "Copyright": "(c) 2024 Ada Lovelace",
        "Software": "PhantomCam 1.0",
    }
    values.update(text or {})
    return png_file(
        path,
        text=values,
        exif=gps_exif(),
        extra=((b"prVt", b"\x00\x01private payload"), *extra),
    )


def rich_exif_png(path: Path, *, comment: str = "shot at home, wifi hunter2") -> Path:
    """A PNG carrying the whole camera dossier a phone photo comes with.

    The tags are the ones a real failure was reported with: GPS position and its
    two reference tags, the GPS version tag, both dates, make, model, owner, body
    serial, orientation, software, artist and copyright — plus one tag no table
    knows, because "unknown" must survive a clean as well as "protected".
    """
    exif = Image.Exif()
    exif[0x010F] = "PhantomCam"  # Make
    exif[0x0110] = "PhantomCam One"  # Model
    exif[0x0112] = 6  # Orientation (rotated: technical)
    exif[0x0131] = "phantom-software 1.0"  # Software (technical)
    exif[0x013B] = "Ada Lovelace"  # Artist (attribution: offered, unticked)
    exif[0x0132] = "2024:05:06 07:08:09"  # DateTime
    exif[0x8298] = "(c) 2024 Ada Lovelace"  # Copyright (rights: protected)
    exif[0x9003] = "2024:05:06 07:08:09"  # DateTimeOriginal (Exif IFD)
    exif[0xA430] = "Ada Lovelace"  # CameraOwnerName (Exif IFD)
    exif[0xA431] = "SN-0001-42"  # BodySerialNumber (Exif IFD)
    exif[0xC7A1] = "undocumented vendor blob"  # no name in any table
    from fractions import Fraction

    gps = exif.get_ifd(0x8825)
    gps[0x0000] = b"\x02\x03\x00\x00"  # GPSVersionID (technical: protected)
    gps[0x0001] = "N"  # GPSLatitudeRef
    gps[0x0002] = (Fraction(50), Fraction(27), Fraction(0))  # 50°27'0"
    gps[0x0003] = "E"  # GPSLongitudeRef
    gps[0x0004] = (Fraction(30), Fraction(31), Fraction(0))  # 30°31'0"
    return png_file(path, text={"Comment": comment}, exif=exif)


def signed_png(path: Path) -> Path:
    """A PNG that carries a JUMBF box with a C2PA signature claim."""
    blob = (
        b"jumb"
        + struct.pack(">I", 64)
        + b"c2pa"
        + b"c2pa.signature"
        + b"urn:uuid:12345678-1234-1234-1234-123456789abc"
    )
    return png_file(path, text={"Comment": "signed original"}, extra=((b"caBX", blob),))


#: The XMP namespaces the tests write properties in.
XMP_NAMESPACES: Final = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
}


def xmp_packet(properties: dict[str, str]) -> str:
    """A plain RDF/XML XMP packet, one element per property.

    ``{"dc:title": "Holiday"}`` becomes ``<dc:title>Holiday</dc:title>`` inside an
    ``rdf:Description`` that declares the namespaces the prefixes need.
    """
    used = sorted({name.split(":")[0] for name in properties} & set(XMP_NAMESPACES))
    declarations = " ".join(
        f'xmlns:{prefix}="{XMP_NAMESPACES[prefix]}"' for prefix in used
    )
    body = "".join(f"<{name}>{value}</{name}>" for name, value in properties.items())
    return (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:Description rdf:about="" {declarations}>{body}</rdf:Description>'
        "</rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    )


def xmp_png(path: Path, properties: dict[str, str]) -> Path:
    """A PNG whose only metadata is an XMP packet."""
    return png_file(path, text={"XML:com.adobe.xmp": xmp_packet(properties)})


# ---------------------------------------------------------------------------
# WAV
# ---------------------------------------------------------------------------
def _riff_chunk(cid: bytes, payload: bytes) -> bytes:
    padded = payload + (b"\x00" if len(payload) % 2 else b"")
    return cid + struct.pack("<I", len(payload)) + padded


def _list_info(entries: dict[str, str]) -> bytes:
    payload = b"INFO"
    for key, value in entries.items():
        raw = value.encode("latin-1", errors="replace") + b"\x00"
        payload += _riff_chunk(key.encode("ascii"), raw)
    return _riff_chunk(b"LIST", payload)


def _bext(description: str, originator: str, reference: str, date: str, time: str) -> bytes:
    def field(text: str, size: int) -> bytes:
        return text.encode("ascii", errors="replace")[: size - 1].ljust(size, b"\x00")

    return (
        field(description, 256)
        + field(originator, 32)
        + field(reference, 32)
        + field(date, 10)
        + field(time, 8)
        + struct.pack("<H", 1)  # version
        + b"SN" + b"0" * 62  # UMID
        + struct.pack("<5h", -1, -1, -1, -1, -1)  # loudness: unknown
        + b"\x00" * 180
    )


def wav_bytes(
    *,
    info: dict[str, str] | None = None,
    bext: bool = False,
    extra: tuple[tuple[bytes, bytes], ...] = (),
    frames: np.ndarray | None = None,
) -> bytes:
    """16-bit mono PCM with optional LIST/INFO, bext and unknown chunks."""
    samples = frames if frames is not None else tone(FRAMES)
    data = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    fmt = struct.pack("<HHIIHH", 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
    body = _riff_chunk(b"fmt ", fmt)
    body += _riff_chunk(b"data", data)
    if info:
        body += _list_info(info)
    if bext:
        body += _riff_chunk(
            b"bext",
            _bext(
                "field recording",
                "PhantomCam",
                "ref-42",
                "2024-05-06",
                "07:08:09",
            ),
        )
    for cid, payload in extra:
        body += _riff_chunk(cid, payload)
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body


def tone(count: int = FRAMES) -> np.ndarray:
    """A quiet sine sweep: decodes to the same samples everywhere."""
    t = np.arange(count) / SAMPLE_RATE
    return (0.3 * np.sin(2 * np.pi * (220 + 40 * t) * t)).astype(np.float32)


def personal_wav(path: Path) -> Path:
    """A WAV with INFO comments, an author, bext fields and an unknown chunk."""
    path.write_bytes(
        wav_bytes(
            info={
                "INAM": "Session 7",
                "IART": "Ada Lovelace",
                "ICMT": "recorded in my kitchen",
                "ICOP": "(c) 2024 Ada Lovelace",
                "IKEY": "field recording",
            },
            bext=True,
            extra=((b"junk", b"undocumented payload"),),
        )
    )
    return path


def signed_wav(path: Path, chunk_id: bytes = b"c2pa") -> Path:
    """A WAV with ICMT and a credential chunk, which the cleaner must refuse to touch."""
    path.write_bytes(wav_bytes(info={"ICMT": "signed"}, extra=((chunk_id, b"c2pa.signature"),)))
    return path


# ---------------------------------------------------------------------------
# FLAC
# ---------------------------------------------------------------------------
def flac_file(path: Path, tags: dict[str, str], *, picture: bool = False) -> Path:
    """A real FLAC stream written by libsndfile, then tagged with Mutagen."""
    import soundfile

    soundfile.write(str(path), tone(), SAMPLE_RATE, subtype="PCM_16", format="FLAC")
    from mutagen.flac import FLAC, Picture

    audio = FLAC(str(path))
    for key, value in tags.items():
        audio[key] = value
    if picture:
        image = Image.fromarray(texture(8, 8), "RGB")
        import io

        with io.BytesIO() as handle:
            image.save(handle, format="PNG")
            payload = handle.getvalue()
        art = Picture()
        art.type = 3
        art.mime = "image/png"
        art.data = payload
        audio.add_picture(art)
    audio.save()
    return path


def personal_flac(path: Path) -> Path:
    """A FLAC with a comment, an author, a date and a copyright line."""
    return flac_file(
        path,
        {
            "TITLE": "Session 7",
            "ARTIST": "Ada Lovelace",
            "COMMENT": "recorded in my kitchen",
            "DATE": "2024-05-06",
            "COPYRIGHT": "(c) 2024 Ada Lovelace",
        },
    )
