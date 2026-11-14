"""Audio carrier detection and shared metadata types."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final

import numpy as np

from phantom.core import constants as C
from phantom.core.errors import LossyAudioError, PhantomIOError, UnsupportedCarrier
from phantom.stego.engine import Capacity

_SNIFF: Final[dict[bytes, str]] = {
    b"RIFF": "wav",
    b"fLaC": "flac",
    b"OggS": "ogg",
    b"ID3": "mp3",
    b"FORM": "aiff",
}
#: Containers that always re-encode the samples, so a payload cannot survive.
_LOSSY_SIGNATURES: Final[frozenset[bytes]] = frozenset({b"OggS", b"FORM"})


@unique
class AudioKind(Enum):
    WAV = "wav"
    FLAC = "flac"


@dataclass(frozen=True, slots=True)
class AudioInfo:
    path: str
    kind: AudioKind
    frames: int
    channels: int
    sample_rate: int
    bits: int
    capacity: Capacity
    silence_ratio: float = 0.0
    notices: tuple[str, ...] = ()

    @property
    def duration_seconds(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return self.frames / float(self.sample_rate)


NOTICE_AUDIO_SILENCE: Final = "notice.audio.silence"
NOTICE_AUDIO_TAGS: Final = "notice.audio.tags_copied"
NOTICE_AUDIO_TAGS_LOST: Final = "notice.audio.tags_lost"
#: Above this fraction of digitally silent / constant samples we warn the user:
#: LSB changes inside "silence" are statistically detectable (documented
#: limitation — we do not claim a silent-mask implementation).
SILENCE_WARN_RATIO: Final = 0.15


def detect_audio_kind(path: str) -> AudioKind:
    """Detect by content first, then by extension; lossy formats are refused."""
    extension = os.path.splitext(path)[1].lower()
    if extension in C.LOSSY_AUDIO_EXTENSIONS:
        raise LossyAudioError("lossy audio destroys embedded data", detail=extension)
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
    except OSError as exc:
        raise PhantomIOError("cannot read file", detail=str(exc)) from exc

    signature = head[:4]
    # ``ID3`` is a three-byte tag, so the four-byte comparison alone is not enough.
    if signature in _LOSSY_SIGNATURES or head[:3] == b"ID3":
        raise LossyAudioError(
            "lossy audio destroys embedded data", detail=_SNIFF.get(signature, "mp3")
        )
    if signature == b"RIFF":
        if head[8:12] != b"WAVE":
            raise UnsupportedCarrier("RIFF file is not a WAVE file")
        return AudioKind.WAV
    if signature == b"fLaC":
        return AudioKind.FLAC
    if head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        raise LossyAudioError("lossy audio destroys embedded data", detail="mp3")
    if extension in C.AUDIO_WAV_EXTENSIONS:
        raise UnsupportedCarrier("file is not a valid WAV")
    if extension in C.AUDIO_FLAC_EXTENSIONS:
        raise UnsupportedCarrier("file is not a valid FLAC")
    raise UnsupportedCarrier("unsupported audio format", detail=extension or "unknown")


def silence_ratio(samples: object) -> float:
    """Fraction of samples that sit in a run of identical neighbours."""
    arr = np.asarray(samples)
    if arr.size == 0:
        return 1.0
    flat = arr.reshape(-1)
    if flat.size < 2:
        return 0.0
    identical = flat[1:] == flat[:-1]
    return float(np.count_nonzero(identical)) / float(flat.size - 1)
