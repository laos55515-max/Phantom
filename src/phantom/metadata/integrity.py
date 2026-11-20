"""Integrity: prove that cleaning metadata changed nothing but metadata.

Two different hashes are involved and the interface must never conflate them.

*The file hash* (SHA-256 of the whole container) changes whenever a byte changes,
so it is only useful to identify the exact file the user had and the exact file
that was produced.  A metadata cleaner is *supposed* to change it.

*The decoded content hash* is computed over the pixels or the PCM samples after
decoding.  It must not change at all — that is the property that makes "I removed
the GPS tag" different from "I re-encoded your photo".  Everything in this module
serves the second hash; the first one is carried along only for the record.

A difference in decoded content is never reported as a warning: the cleaner
refuses to publish such a file.
"""

from __future__ import annotations

import hashlib
import io
import threading
from dataclasses import dataclass, field
from typing import Any, Final

from phantom.core.progress import check_cancel
from phantom.metadata.models import MediaFormat

#: Reading a whole decoded image or audio file needs room; above this the check
#: reports that it could not run instead of swapping the machine to death.
MAX_DECODE_BYTES: Final = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class DecodedFingerprint:
    """A hash of the decoded media plus the parameters that make it meaningful."""

    content_sha256: str
    technical: tuple[tuple[str, str], ...] = ()
    detail: str = ""
    checked: bool = True

    @property
    def technical_map(self) -> dict[str, str]:
        return dict(self.technical)


@dataclass(frozen=True, slots=True)
class ContentComparison:
    """Whether two decoded fingerprints describe the same media."""

    identical: bool
    differences: tuple[str, ...] = ()
    left: DecodedFingerprint | None = None
    right: DecodedFingerprint | None = None

    @property
    def checked(self) -> bool:
        return self.left is not None and self.right is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "identical": self.identical,
            "checked": self.checked,
            "differences": list(self.differences),
            "source": self.left.content_sha256 if self.left is not None else "",
            "output": self.right.content_sha256 if self.right is not None else "",
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def png_fingerprint(data: bytes) -> DecodedFingerprint:
    """Hash the decoded pixels, the alpha channel and the geometry separately."""
    try:
        from PIL import Image  # noqa: PLC0415 - Pillow is optional for the headless tests
    except ImportError as exc:
        return DecodedFingerprint(
            content_sha256="", detail=f"Pillow unavailable: {exc}", checked=False
        )
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            pixels = image.tobytes()
            digest = hashlib.sha256(pixels)
            technical: list[tuple[str, str]] = [
                ("mode", image.mode),
                ("width", str(image.width)),
                ("height", str(image.height)),
                ("pixel_sha256", hashlib.sha256(pixels).hexdigest()),
            ]
            if "A" in image.getbands():
                alpha = image.getchannel("A").tobytes()
                technical.append(("alpha_sha256", hashlib.sha256(alpha).hexdigest()))
            if image.mode == "P":
                palette = image.getpalette() or []
                technical.append(("palette_sha256", sha256_bytes(bytes(palette))))
            return DecodedFingerprint(
                content_sha256=digest.hexdigest(),
                technical=tuple(technical),
                detail=f"{image.mode} {image.width}x{image.height}",
            )
    except Exception as exc:  # noqa: BLE001 - a broken carrier must not crash the check
        return DecodedFingerprint(content_sha256="", detail=f"cannot decode: {exc}", checked=False)


def audio_fingerprint(data: bytes) -> DecodedFingerprint:
    """Hash the PCM samples as int32, plus the format parameters around them."""
    try:
        import numpy as np  # noqa: PLC0415
        import soundfile  # noqa: PLC0415
    except ImportError as exc:
        return DecodedFingerprint(
            content_sha256="", detail=f"soundfile unavailable: {exc}", checked=False
        )
    if len(data) > MAX_DECODE_BYTES:
        return DecodedFingerprint(
            content_sha256="", detail="file is too large to decode in memory", checked=False
        )
    try:
        info = soundfile.info(io.BytesIO(data))
        samples, rate = soundfile.read(io.BytesIO(data), dtype="int32", always_2d=True)
        buffer = np.ascontiguousarray(samples, dtype=np.int32)
        digest = hashlib.sha256(buffer.tobytes())
        technical = (
            ("sample_rate", str(rate)),
            ("channels", str(info.channels)),
            ("frames", str(info.frames)),
            ("subtype", info.subtype or ""),
            ("sample_sha256", digest.hexdigest()),
        )
        return DecodedFingerprint(
            content_sha256=digest.hexdigest(),
            technical=technical,
            detail=f"{info.subtype} {rate} Hz, {info.channels} ch, {info.frames} frames",
        )
    except Exception as exc:  # noqa: BLE001 - libsndfile raises its own types
        return DecodedFingerprint(content_sha256="", detail=f"cannot decode: {exc}", checked=False)


def fingerprint(data: bytes, fmt: MediaFormat) -> DecodedFingerprint:
    """Decode ``data`` and hash what a listener or a viewer would actually get."""
    if fmt is MediaFormat.PNG:
        return png_fingerprint(data)
    return audio_fingerprint(data)


def compare(left: DecodedFingerprint, right: DecodedFingerprint) -> ContentComparison:
    """Compare two fingerprints; anything but equality counts as a difference."""
    if not (left.checked and right.checked):
        return ContentComparison(
            identical=False,
            differences=(
                left.detail or "the source could not be decoded",
                right.detail or "the output could not be decoded",
            ),
            left=left,
            right=right,
        )
    differences: list[str] = []
    if left.content_sha256 != right.content_sha256:
        differences.append("decoded content hash differs")
    left_tech = left.technical_map
    right_tech = right.technical_map
    compared = {key for key in left_tech if key.endswith("_sha256")}
    compared.update(key for key in ("sample_rate", "channels", "frames") if key in left_tech)
    compared.update(key for key in ("mode", "width", "height", "subtype") if key in left_tech)
    compared.update(key for key in right_tech if key in {"mode", "width", "height", "subtype"})
    for key in sorted(compared):
        if left_tech.get(key) != right_tech.get(key):
            differences.append(
                f"{key}: {left_tech.get(key, 'missing')} → {right_tech.get(key, 'missing')}"
            )
    return ContentComparison(
        identical=not differences, differences=tuple(differences), left=left, right=right
    )


def png_checksums_ok(data: bytes) -> bool | None:
    """Verify every PNG chunk CRC; ``None`` when the bytes are not a PNG at all."""
    from phantom.metadata import png  # noqa: PLC0415 - avoid a circular import at module load

    chunks = png.iter_chunks(data)
    if not chunks:
        return None
    return all(chunk.crc_ok is not False for chunk in chunks)


def describe_hashes(file_sha256: str, content_sha256: str) -> dict[str, str]:
    """The two hashes with the sentence that keeps them apart, for the reports."""
    return {
        "file_sha256": file_sha256,
        "content_sha256": content_sha256,
        "note": (
            "file_sha256 covers the whole container and changes when metadata is "
            "removed; content_sha256 covers the decoded pixels or PCM samples and "
            "must not change"
        ),
    }


@dataclass(slots=True)
class HashProgress:
    """Streaming SHA-256 with a cancel check between blocks."""

    digest: Any = field(default_factory=hashlib.sha256)
    read: int = 0

    def update(self, block: bytes, cancel: threading.Event | None = None) -> None:
        check_cancel(cancel)
        self.digest.update(block)
        self.read += len(block)

    def hexdigest(self) -> str:
        return str(self.digest.hexdigest())
