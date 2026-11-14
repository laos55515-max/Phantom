"""FLAC carrier via ``soundfile``/libsndfile, with metadata kept by ``mutagen``.

Critical libsndfile detail (covered by tests):

* ``PCM_24`` is exposed through int32 buffers **left-justified** by 8 bits.
  A raw 24-bit value written into the low bits of an int32 is silently
  truncated, so every read/write shifts by ``32 - bits``;
* reading PCM_24 through int16 buffers would destroy the low bit entirely.

FLAC itself is lossless, so a ±1 change at the native bit depth survives
encode → decode.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Final

import numpy as np

from phantom.core.errors import AudioFormatError, UnsupportedCarrier

_SUBTYPE_BITS: Final[dict[str, int]] = {
    "PCM_S8": 8,
    "PCM_16": 16,
    "PCM_24": 24,
}


@dataclass(frozen=True, slots=True)
class FlacLayout:
    frames: int
    channels: int
    sample_rate: int
    bits: int
    subtype: str

    @property
    def shift(self) -> int:
        return 32 - self.bits


def _import_soundfile() -> Any:
    try:
        import soundfile  # noqa: PLC0415
    except OSError as exc:  # libsndfile missing
        raise AudioFormatError("libsndfile is unavailable", detail=str(exc)) from exc
    return soundfile


def probe_flac(path: str) -> FlacLayout:
    soundfile = _import_soundfile()
    try:
        info = soundfile.info(path)
    except Exception as exc:
        raise UnsupportedCarrier("cannot read FLAC header", detail=str(exc)) from exc
    if info.format != "FLAC":
        raise UnsupportedCarrier("not a FLAC stream", detail=str(info.format))
    bits = _SUBTYPE_BITS.get(info.subtype)
    if bits is None:
        raise AudioFormatError("unsupported FLAC bit depth", detail=str(info.subtype))
    return FlacLayout(
        frames=int(info.frames),
        channels=int(info.channels),
        sample_rate=int(info.samplerate),
        bits=bits,
        subtype=str(info.subtype),
    )


def read_flac(path: str) -> tuple[np.ndarray, FlacLayout]:
    """Return (frames, channels) int64 array of *native* sample values."""
    soundfile = _import_soundfile()
    layout = probe_flac(path)
    try:
        data, rate = soundfile.read(path, dtype="int32", always_2d=True)
    except Exception as exc:
        raise UnsupportedCarrier("cannot decode FLAC", detail=str(exc)) from exc
    if int(rate) != layout.sample_rate:
        raise AudioFormatError("sample rate mismatch while decoding")
    raw = np.asarray(data, dtype=np.int32).astype(np.int64)
    native = raw >> layout.shift
    if native.size and (
        native.min() < -(1 << (layout.bits - 1)) or native.max() > (1 << (layout.bits - 1)) - 1
    ):
        raise AudioFormatError("decoded FLAC samples exceed the declared bit depth")
    return native, layout


def write_flac(path: str, samples: np.ndarray, layout: FlacLayout) -> None:
    """Write native sample values back as the same FLAC subtype."""
    soundfile = _import_soundfile()
    values = np.asarray(samples, dtype=np.int64)
    low = -(1 << (layout.bits - 1))
    high = (1 << (layout.bits - 1)) - 1
    if values.size and (values.min() < low or values.max() > high):
        raise AudioFormatError("sample value outside the FLAC bit depth")
    shifted = np.left_shift(values, layout.shift)
    buffer = np.asarray(shifted, dtype=np.int32)
    try:
        soundfile.write(
            path,
            buffer.reshape(-1, layout.channels),
            layout.sample_rate,
            subtype=layout.subtype,
            format="FLAC",
            endian="FILE",
        )
    except Exception as exc:
        raise AudioFormatError("cannot encode FLAC", detail=str(exc)) from exc


def copy_metadata(source: str, target: str) -> bool:
    """Copy Vorbis comments and cover art between FLAC files.

    Returns ``True`` when there was nothing to copy or the copy succeeded and
    ``False`` when metadata existed but could not be transferred (the caller
    must then warn the user instead of pretending nothing happened).
    """
    try:
        from mutagen.flac import FLAC  # noqa: PLC0415
    except ImportError:
        return False
    try:
        src = FLAC(source)
        dst = FLAC(target)
        had_metadata = bool(src.tags) or bool(src.pictures)
        source_tags: Any = src.tags
        if source_tags:
            dst.clear()
            for key, values in source_tags.items():
                if values is not None:
                    dst[key] = list(values)
        dst.clear_pictures()
        for picture in src.pictures:
            dst.add_picture(picture)
        if dst.tags or dst.pictures or not had_metadata:
            dst.save()
        return True
    except Exception:  # noqa: BLE001 - mutagen raises its own types; best effort
        return False


def flac_metadata_size(path: str) -> int:
    """Cheap probe used by tests: how many metadata blocks a FLAC file has."""
    try:
        from mutagen.flac import FLAC  # noqa: PLC0415
    except ImportError:
        return 0
    try:
        audio = FLAC(path)
    except Exception:  # noqa: BLE001 - a probe never raises on a broken file
        return 0
    return len(audio.metadata_blocks)


def write_flac_atomic(path: str, samples: np.ndarray, layout: FlacLayout) -> None:
    from phantom.core.files import atomic_path  # noqa: PLC0415

    with atomic_path(path) as tmp:
        write_flac(tmp, samples, layout)
        if os.path.getsize(tmp) == 0:
            raise AudioFormatError("encoder produced an empty file")
