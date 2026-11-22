"""PNG carrier policy, loading, saving and the public image API (D3, D8, D9).

Supported carriers
------------------
* 8-bit ``RGB`` and ``RGBA`` PNG — used as-is (alpha is never modified);
* 8-bit ``L``, ``LA``, ``P``, ``1`` — converted to RGB/RGBA, with a notice the
  UI shows to the user (lossless for L/LA/1; palette transparency preserved);
* 16-bit PNG (any colour type) — refused explicitly, never clipped;
* ``CMYK`` — refused (the conversion is lossy);
* APNG / any multi-frame image — refused.

Output is always a real PNG (extension forced), written atomically, with the
ICC profile preserved and no EXIF added.
"""

from __future__ import annotations

import contextlib
import io
import os
import struct
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from phantom.core import constants as C
from phantom.core import files as fsutil
from phantom.core.errors import (
    PhantomIOError,
    UnsupportedCarrier,
    WrongPasswordOrNoData,
)
from phantom.core.kdf import KdfProfile
from phantom.core.payload import (
    ExtractedPayload,
    PackedPayload,
    Payload,
    pack_file,
    pack_payload,
)
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress
from phantom.stego.engine import Capacity, EmbedReport, capacity_for, embed_payload, extract_payload
from phantom.stego.legacy import extract_legacy_from_pixels
from phantom.stego.samples import ImageSamples

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_SUPPORTED_BIT_DEPTHS: Final = frozenset({1, 2, 4, 8})
_SUPPORTED_DIRECT: Final = frozenset({"RGB", "RGBA"})
_CONVERTIBLE: Final = frozenset({"L", "LA", "P", "1"})
_REFUSED_MODES: Final = frozenset(
    {"CMYK", "YCbCr", "HSV", "LAB", "F", "I", "I;16", "I;16B", "I;16L"}
)

NOTICE_PALETTE: Final = "notice.image.palette_converted"
NOTICE_GRAYSCALE: Final = "notice.image.grayscale_converted"
NOTICE_BITMAP: Final = "notice.image.bitmap_converted"
NOTICE_TRANSPARENCY_KEPT: Final = "notice.image.transparency_kept"


@dataclass(frozen=True, slots=True)
class ImageInfo:
    path: str
    width: int
    height: int
    mode: str
    capacity: Capacity
    notices: tuple[str, ...] = ()

    @property
    def usable_bits(self) -> int:
        return self.capacity.usable_bits


@dataclass(slots=True)
class PreparedImage:
    pixels: np.ndarray = field(repr=False)
    mode: str
    width: int
    height: int
    icc_profile: bytes | None = None
    dpi: tuple[float, float] | None = None
    notices: tuple[str, ...] = ()
    source_format: str = "PNG"

    def sample_space(self) -> ImageSamples:
        return ImageSamples(self.pixels)


def _read_png_header(path: str) -> tuple[int, int, int, bool]:
    """Return (bit_depth, colour_type, interlace, is_apng) from IHDR/actl."""
    with open(path, "rb") as handle:
        head = handle.read(33)
        if not head.startswith(_PNG_SIGNATURE) or len(head) < 33:
            raise UnsupportedCarrier("not a PNG file", detail=os.path.basename(path))
        width, height, bit_depth, colour_type, _comp, _filt, interlace = struct.unpack(
            ">IIBBBBB", head[16:29]
        )
        if width == 0 or height == 0:
            raise UnsupportedCarrier("empty image")
        is_apng = False
        while True:
            chunk_head = handle.read(8)
            if len(chunk_head) < 8:
                break
            length, chunk_type = struct.unpack(">I4s", chunk_head)
            if chunk_type == b"acTL":
                is_apng = True
                break
            if chunk_type in (b"IDAT", b"IEND"):
                break
            handle.seek(length + 4, io.SEEK_CUR)
        return bit_depth, colour_type, interlace, is_apng


def probe_image(path: str) -> ImageInfo:
    """Inspect a carrier without embedding anything (safe for a worker thread)."""
    if not os.path.isfile(path):
        raise PhantomIOError("file not found", detail=path)
    notices: list[str] = []
    extension = os.path.splitext(path)[1].lower()
    if extension not in C.IMAGE_EXTENSIONS:
        # Content sniffing wins over the extension: a PNG named .jpg is fine.
        with open(path, "rb") as handle:
            if handle.read(8) != _PNG_SIGNATURE:
                raise UnsupportedCarrier("not a PNG file", detail=extension)
    bit_depth, colour_type, _interlace, is_apng = _read_png_header(path)
    if is_apng:
        raise UnsupportedCarrier("animated PNG is not supported")
    if bit_depth == 16:
        raise UnsupportedCarrier("16-bit PNG is not supported (no silent clipping)")
    if bit_depth not in _SUPPORTED_BIT_DEPTHS:
        # 1/2/4-bit images expand to 8-bit without loss and are accepted; every
        # other depth is either refused above or unknown to this parser.
        raise UnsupportedCarrier("unsupported PNG bit depth", detail=str(bit_depth))
    if colour_type not in (0, 2, 3, 4, 6):
        raise UnsupportedCarrier("unsupported PNG colour type", detail=str(colour_type))
    try:
        with Image.open(path) as img:
            width, height = img.size
            mode = img.mode
            if getattr(img, "n_frames", 1) > 1:
                raise UnsupportedCarrier("multi-frame images are not supported")
            if mode in _REFUSED_MODES:
                raise UnsupportedCarrier("unsupported colour mode", detail=mode)
            if mode not in _SUPPORTED_DIRECT and mode not in _CONVERTIBLE:
                raise UnsupportedCarrier("unsupported colour mode", detail=mode)
            transparent = 0
            if mode in {"RGBA", "LA"}:
                alpha = np.asarray(img.getchannel("A"))
                transparent = int(np.count_nonzero(alpha == 0))
            elif mode == "P" and "transparency" in img.info:
                notices.append(NOTICE_TRANSPARENCY_KEPT)
            if mode in ("L", "LA", "1"):
                notices.append(NOTICE_GRAYSCALE if mode != "1" else NOTICE_BITMAP)
            elif mode == "P":
                notices.append(NOTICE_PALETTE)
    except UnidentifiedImageError as exc:
        raise UnsupportedCarrier("unknown image format") from exc
    usable = 3 * (width * height - transparent)
    plaintext_bytes = max(0, (usable - C.HEADER_BITS) // 8 - C.TAG_SIZE)
    return ImageInfo(
        path=path,
        width=width,
        height=height,
        mode=mode,
        capacity=Capacity(usable_bits=usable, payload_bytes=plaintext_bytes),
        notices=tuple(notices),
    )


def prepare_image(
    path: str,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> PreparedImage:
    info = probe_image(path)
    check_cancel(cancel)
    progress(Stage.READING, 0, 1)
    target_mode = "RGBA" if info.mode in ("RGBA", "LA", "P") else "RGB"
    try:
        with Image.open(path) as img:
            if target_mode == "RGBA" and info.mode == "P" and "transparency" not in img.info:
                target_mode = "RGB"
            converted = img.convert(target_mode)
            icc = img.info.get("icc_profile")
            dpi = img.info.get("dpi")
            pixels = np.array(converted, dtype=np.uint8)
            source_format = img.format or "PNG"
    except UnidentifiedImageError as exc:
        raise UnsupportedCarrier("unknown image format") from exc
    if pixels.ndim != 3 or pixels.shape[2] not in (3, 4):
        raise UnsupportedCarrier("unexpected pixel layout", detail=str(pixels.shape))
    if not pixels.flags.writeable:
        pixels = pixels.copy()
    progress(Stage.READING, 1, 1)
    return PreparedImage(
        pixels=pixels,
        mode=target_mode,
        width=int(pixels.shape[1]),
        height=int(pixels.shape[0]),
        icc_profile=icc if isinstance(icc, bytes) else None,
        dpi=dpi if isinstance(dpi, tuple) and len(dpi) == 2 else None,
        notices=info.notices,
        source_format=source_format,
    )


def prepare_carrier_copy(
    path: str,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> tuple[str, str | None]:
    """Resolve the carrier path PHANTOM embeds from.

    PNG carriers are used as-is.  A JPG/JPEG photo is converted to a temporary
    PNG copy: the original file is never touched, EXIF orientation is applied so
    the photo lands the right way up, and the embedded ICC colour profile is
    carried into the copy.  Returns ``(carrier path, original path)`` where the
    original path is ``None`` unless a copy was made.  The caller owns the
    temporary copy and removes it when the carrier changes.
    """
    extension = os.path.splitext(path)[1].lower()
    if extension not in (".jpg", ".jpeg"):
        return path, None
    return _jpg_to_png(path, cancel=cancel, progress=progress), path


def _jpg_to_png(
    path: str,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> str:
    """Convert a JPEG photo to a lossless PNG sibling, preserving orientation and colour profile.

    Returns the temporary PNG path; the caller owns it.
    """
    check_cancel(cancel)
    progress(Stage.READING, 0, 1)
    with Image.open(path) as img:
        transposed = ImageOps.exif_transpose(img)
        icc = transposed.info.get("icc_profile")
        target_mode = "RGBA" if transposed.mode in ("RGBA", "LA", "P") else "RGB"
        converted = transposed.convert(target_mode)
        fd, tmp_path = tempfile.mkstemp(prefix="phantom-carrier-", suffix=".png")
        os.close(fd)
        save_kwargs: dict[str, Any] = {
            "format": "PNG",
            "compress_level": C.PNG_COMPRESS_LEVEL,
        }
        if isinstance(icc, bytes):
            save_kwargs["icc_profile"] = icc
        try:
            converted.save(tmp_path, **save_kwargs)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
    check_cancel(cancel)
    progress(Stage.READING, 1, 1)
    return tmp_path


def enforce_png_extension(path: str) -> str:
    """Force a ``.png`` extension: the output format is never negotiated (D8)."""
    root, ext = os.path.splitext(path)
    if ext.lower() == ".png":
        return path
    return f"{root}.png"


def save_png(prepared: PreparedImage, target: str) -> str:
    """Atomically write the stego PNG, returning the final path."""
    final_path = enforce_png_extension(target)
    fsutil.ensure_output_dir(final_path)
    image = Image.fromarray(prepared.pixels, mode=prepared.mode)
    save_kwargs: dict[str, Any] = {
        "format": "PNG",
        "compress_level": C.PNG_COMPRESS_LEVEL,
        "optimize": False,
    }
    if prepared.icc_profile:
        save_kwargs["icc_profile"] = prepared.icc_profile
    if prepared.dpi:
        save_kwargs["dpi"] = prepared.dpi
    with fsutil.atomic_path(final_path) as tmp:
        image.save(tmp, **save_kwargs)
    return final_path


def image_capacity(path: str) -> Capacity:
    return probe_image(path).capacity


@dataclass(frozen=True, slots=True)
class ImageEmbedResult:
    path: str
    report: EmbedReport
    notices: tuple[str, ...]
    packed: PackedPayload
    width: int
    height: int
    mode: str


def embed_into_image(
    carrier_path: str,
    output_path: str,
    payload: Payload,
    password: str,
    *,
    profile: KdfProfile | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> ImageEmbedResult:
    """Embed a payload into a PNG carrier. Returns (final path, report, prepared)."""
    fsutil.ensure_distinct(carrier_path, output_path)
    fsutil.ensure_distinct(carrier_path, enforce_png_extension(output_path))
    prepared = prepare_image(carrier_path, cancel=cancel, progress=progress)
    space = prepared.sample_space()
    capacity = capacity_for(space)
    packed = pack_payload(
        payload,
        max_payload_bytes=capacity.payload_bytes,
        cancel=cancel,
        progress=progress,
    )
    report = embed_payload(
        space,
        packed.blob,
        password,
        profile=profile,
        cancel=cancel,
        progress=progress,
    )
    check_cancel(cancel)
    progress(Stage.WRITING, 0, 1)
    final_path = save_png(prepared, output_path)
    progress(Stage.WRITING, 1, 1)
    return ImageEmbedResult(
        path=final_path,
        report=report,
        notices=prepared.notices,
        packed=packed,
        width=prepared.width,
        height=prepared.height,
        mode=prepared.mode,
    )


def embed_file_into_image(
    carrier_path: str,
    output_path: str,
    secret_path: str,
    password: str,
    *,
    profile: KdfProfile | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> ImageEmbedResult:
    fsutil.ensure_distinct(carrier_path, output_path)
    fsutil.ensure_distinct(carrier_path, enforce_png_extension(output_path))
    prepared = prepare_image(carrier_path, cancel=cancel, progress=progress)
    space = prepared.sample_space()
    capacity = capacity_for(space)
    packed = pack_file(
        secret_path,
        max_payload_bytes=capacity.payload_bytes,
        cancel=cancel,
        progress=progress,
    )
    report = embed_payload(
        space,
        packed.blob,
        password,
        profile=profile,
        cancel=cancel,
        progress=progress,
    )
    check_cancel(cancel)
    progress(Stage.WRITING, 0, 1)
    final_path = save_png(prepared, output_path)
    progress(Stage.WRITING, 1, 1)
    return ImageEmbedResult(
        path=final_path,
        report=report,
        notices=prepared.notices,
        packed=packed,
        width=prepared.width,
        height=prepared.height,
        mode=prepared.mode,
    )


def extract_from_image(
    stego_path: str,
    password: str,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
    allow_legacy: bool = True,
) -> ExtractedPayload:
    """Extract a v2 payload; transparently fall back to the v1 legacy layout."""
    prepared = prepare_image(stego_path, cancel=cancel, progress=progress)
    space = prepared.sample_space()
    try:
        return extract_payload(space, password, cancel=cancel, progress=progress)
    except WrongPasswordOrNoData:
        if not allow_legacy:
            raise
    rgb = _legacy_pixels(prepared)
    return extract_legacy_from_pixels(rgb, password, cancel=cancel, progress=progress)


def _legacy_pixels(prepared: PreparedImage) -> np.ndarray:
    """v1 layout used a plain RGB(A) pixel sequence; alpha-aware v2 does not."""
    if prepared.mode == "RGBA":
        return np.ascontiguousarray(prepared.pixels[:, :, :3])
    return prepared.pixels


def extract_payload_from_pixels(
    pixels: np.ndarray, password: str, *, allow_legacy: bool = True
) -> ExtractedPayload:
    """Test/diagnostic helper: extract directly from an in-memory pixel array."""
    space = ImageSamples(pixels)
    try:
        return extract_payload(space, password)
    except WrongPasswordOrNoData:
        if not allow_legacy:
            raise
    rgb = np.ascontiguousarray(pixels[:, :, :3]) if pixels.shape[2] == 4 else pixels
    return extract_legacy_from_pixels(rgb, password)
