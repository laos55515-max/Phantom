"""Audio carriers: WAV (8/16/24/32-bit PCM) and FLAC (8/16/24-bit).

The stego engine is shared with images: same v2 container, same LSB Matching,
same keyed permutation — one bit per sample-channel.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import numpy as np

from phantom.audio import flac as flac_mod
from phantom.audio import wav as wav_mod
from phantom.audio.common import (
    NOTICE_AUDIO_SILENCE,
    NOTICE_AUDIO_TAGS,
    NOTICE_AUDIO_TAGS_LOST,
    SILENCE_WARN_RATIO,
    AudioInfo,
    AudioKind,
    detect_audio_kind,
    silence_ratio,
)
from phantom.core import constants as C
from phantom.core import files as fsutil
from phantom.core.errors import CapacityError
from phantom.core.kdf import KdfProfile
from phantom.core.payload import (
    ExtractedPayload,
    PackedPayload,
    Payload,
    pack_file,
    pack_payload,
)
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress
from phantom.stego.engine import (
    Capacity,
    EmbedReport,
    capacity_for,
    embed_payload,
    extract_payload,
)
from phantom.stego.samples import ArraySamples

__all__ = [
    "NOTICE_AUDIO_SILENCE",
    "NOTICE_AUDIO_TAGS",
    "NOTICE_AUDIO_TAGS_LOST",
    "AudioInfo",
    "AudioKind",
    "CapacityError",
    "audio_capacity",
    "detect_audio_kind",
    "embed_file_into_audio",
    "embed_into_audio",
    "enforce_audio_extension",
    "extract_from_audio",
    "probe_audio",
]


@dataclass(frozen=True, slots=True)
class AudioEmbedResult:
    path: str
    report: EmbedReport
    notices: tuple[str, ...]
    packed: PackedPayload
    frames: int
    channels: int
    bits: int


@dataclass(frozen=True, slots=True)
class LoadedAudio:
    samples: np.ndarray  # native integer values, flat
    source_path: str
    channels: int
    sample_rate: int
    bits: int
    kind: AudioKind
    wav_layout: wav_mod.WavLayout | None = None
    flac_layout: flac_mod.FlacLayout | None = None
    raw: bytes | None = None

    @property
    def frames(self) -> int:
        if self.channels <= 0:
            return 0
        return int(self.samples.size // self.channels)

    def range(self) -> tuple[int, int]:
        if self.bits >= 32:
            return -(1 << 31), (1 << 31) - 1
        half = 1 << (self.bits - 1)
        return -half, half - 1

    def space(self) -> ArraySamples:
        low, high = self.range()
        return ArraySamples(self.samples, min_value=low, max_value=high)


def _probe_silence_wav(raw: bytes, layout: wav_mod.WavLayout, limit_frames: int = 262_144) -> float:
    usable = min(layout.data_size, limit_frames * layout.block_align)
    payload = raw[layout.data_offset : layout.data_offset + usable]
    samples = wav_mod.decode_samples(payload, layout)
    return silence_ratio(samples)


def probe_audio(path: str) -> AudioInfo:
    """Inspect an audio carrier: capacity, silence share, notices."""
    kind = detect_audio_kind(path)
    notices: list[str] = []
    silence = 0.0
    if kind is AudioKind.WAV:
        with open(path, "rb") as handle:
            raw = handle.read()
        layout = wav_mod.parse_wav(raw)
        frames = layout.frames
        channels = layout.channels
        bits = layout.bits
        rate = layout.sample_rate
        silence = _probe_silence_wav(raw, layout)
    else:
        layout_flac = flac_mod.probe_flac(path)
        frames = layout_flac.frames
        channels = layout_flac.channels
        bits = layout_flac.bits
        rate = layout_flac.sample_rate
        native, _ = flac_mod.read_flac(path)
        silence = silence_ratio(native)
    usable = int(frames) * int(channels)
    plaintext_bytes = max(0, (usable - C.HEADER_BITS) // 8 - C.TAG_SIZE)
    if silence >= SILENCE_WARN_RATIO:
        notices.append(NOTICE_AUDIO_SILENCE)
    return AudioInfo(
        path=path,
        kind=kind,
        frames=int(frames),
        channels=int(channels),
        sample_rate=int(rate),
        bits=int(bits),
        capacity=Capacity(usable_bits=usable, payload_bytes=plaintext_bytes),
        silence_ratio=silence,
        notices=tuple(notices),
    )


def audio_capacity(path: str) -> Capacity:
    return probe_audio(path).capacity


def enforce_audio_extension(path: str, kind: AudioKind) -> str:
    """Force the extension matching the real container (never mislabel output)."""
    wanted = ".wav" if kind is AudioKind.WAV else ".flac"
    root, ext = os.path.splitext(path)
    if ext.lower() == wanted:
        return path
    return f"{root}{wanted}"


def load_audio(path: str) -> LoadedAudio:
    kind = detect_audio_kind(path)
    if kind is AudioKind.WAV:
        with open(path, "rb") as handle:
            raw = handle.read()
        layout = wav_mod.parse_wav(raw)
        payload = raw[layout.data_offset : layout.data_offset + layout.data_size]
        samples = wav_mod.decode_samples(payload, layout)
        return LoadedAudio(
            samples=samples,
            source_path=path,
            channels=layout.channels,
            sample_rate=layout.sample_rate,
            bits=layout.bits,
            kind=kind,
            wav_layout=layout,
            raw=raw,
        )
    native, layout_flac = flac_mod.read_flac(path)
    flat = native.reshape(-1)
    return LoadedAudio(
        samples=flat,
        source_path=path,
        channels=layout_flac.channels,
        sample_rate=layout_flac.sample_rate,
        bits=layout_flac.bits,
        kind=kind,
        flac_layout=layout_flac,
    )


def embed_into_audio(
    carrier_path: str,
    output_path: str,
    payload: Payload,
    password: str,
    *,
    profile: KdfProfile | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> AudioEmbedResult:
    fsutil.ensure_distinct(carrier_path, output_path)
    loaded = load_audio(carrier_path)
    final_path = enforce_audio_extension(output_path, loaded.kind)
    fsutil.ensure_distinct(carrier_path, final_path)
    space = loaded.space()
    capacity = capacity_for(space)
    packed = pack_payload(
        payload, max_payload_bytes=capacity.payload_bytes, cancel=cancel, progress=progress
    )
    report = embed_payload(
        space, packed.blob, password, profile=profile, cancel=cancel, progress=progress
    )
    notices = _write_back(loaded, final_path, cancel=cancel, progress=progress)
    return AudioEmbedResult(
        path=final_path,
        report=report,
        notices=notices,
        packed=packed,
        frames=loaded.frames,
        channels=loaded.channels,
        bits=loaded.bits,
    )


def embed_file_into_audio(
    carrier_path: str,
    output_path: str,
    secret_path: str,
    password: str,
    *,
    profile: KdfProfile | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> AudioEmbedResult:
    fsutil.ensure_distinct(carrier_path, output_path)
    loaded = load_audio(carrier_path)
    final_path = enforce_audio_extension(output_path, loaded.kind)
    fsutil.ensure_distinct(carrier_path, final_path)
    space = loaded.space()
    capacity = capacity_for(space)
    packed = pack_file(
        secret_path, max_payload_bytes=capacity.payload_bytes, cancel=cancel, progress=progress
    )
    report = embed_payload(
        space, packed.blob, password, profile=profile, cancel=cancel, progress=progress
    )
    notices = _write_back(loaded, final_path, cancel=cancel, progress=progress)
    return AudioEmbedResult(
        path=final_path,
        report=report,
        notices=notices,
        packed=packed,
        frames=loaded.frames,
        channels=loaded.channels,
        bits=loaded.bits,
    )


def _write_back(
    loaded: LoadedAudio,
    final_path: str,
    *,
    cancel: threading.Event | None,
    progress: ProgressFn,
) -> tuple[str, ...]:
    check_cancel(cancel)
    progress(Stage.WRITING, 0, 1)
    notices: list[str] = []
    if loaded.kind is AudioKind.WAV:
        assert loaded.wav_layout is not None
        assert loaded.raw is not None
        payload_bytes = wav_mod.encode_samples(loaded.samples, loaded.wav_layout)
        new_raw = wav_mod.splice_data(loaded.raw, loaded.wav_layout, payload_bytes)
        wav_mod.write_wav_atomic(final_path, new_raw)
    else:
        assert loaded.flac_layout is not None
        flac_mod.write_flac_atomic(final_path, loaded.samples, loaded.flac_layout)
        if not flac_mod.copy_metadata(loaded.source_path, final_path):
            notices.append(NOTICE_AUDIO_TAGS_LOST)
        else:
            notices.append(NOTICE_AUDIO_TAGS)
    progress(Stage.WRITING, 1, 1)
    return tuple(notices)


def extract_from_audio(
    stego_path: str,
    password: str,
    *,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
) -> ExtractedPayload:
    loaded = load_audio(stego_path)
    return extract_payload(loaded.space(), password, cancel=cancel, progress=progress)
