"""Container-level audio edges: malformed WAV/FLAC, codecs, detection."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from phantom.audio import (
    AudioKind,
    LoadedAudio,
    audio_capacity,
    detect_audio_kind,
    enforce_audio_extension,
    load_audio,
    probe_audio,
)
from phantom.audio import flac as flac_mod
from phantom.audio import wav as wav_mod
from phantom.audio.common import AudioInfo, silence_ratio
from phantom.core.errors import (
    AudioFormatError,
    LossyAudioError,
    PhantomIOError,
    UnsupportedCarrier,
)
from phantom.stego.engine import Capacity


def _wav_bytes(
    *,
    bits: int = 16,
    channels: int = 1,
    frames: int = 8,
    tag: int = 1,
    data: bytes | None = None,
    extra_chunks: bytes = b"",
    declared_size: int | None = None,
) -> bytes:
    align = channels * bits // 8
    if data is None:
        data = (np.arange(frames * channels, dtype=np.int32) % 100).astype(
            {8: np.uint8, 16: "<i2", 24: np.uint8, 32: "<i4"}[bits]
        ).tobytes()
        if bits == 24:
            data = b"".join(
                bytes(((index * 3) & 0xFF, (index * 5) & 0xFF, (index * 7) & 0xFF))
                for index in range(frames * channels)
            )
    fmt = struct.pack("<HHIIHH", tag, channels, 8000, 8000 * align, align, bits)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt + extra_chunks
    chunks += b"data" + struct.pack("<I", len(data) if declared_size is None else declared_size) + data
    body = b"WAVE" + chunks
    return b"RIFF" + struct.pack("<I", len(body)) + body


# ---------------------------------------------------------------------------
# parse_wav
# ---------------------------------------------------------------------------
def test_parse_rejects_non_riff() -> None:
    with pytest.raises(UnsupportedCarrier):
        wav_mod.parse_wav(b"NOTRIFF" + b"\x00" * 32)


def test_parse_rejects_short_riff() -> None:
    with pytest.raises(UnsupportedCarrier):
        wav_mod.parse_wav(b"MThd" + b"\x00" * 8)


def test_parse_rejects_non_wave_riff() -> None:
    with pytest.raises(UnsupportedCarrier):
        wav_mod.parse_wav(b"RIFF" + struct.pack("<I", 4) + b"AVI ")


def test_parse_rejects_small_fmt_chunk() -> None:
    body = b"WAVE" + b"fmt " + struct.pack("<I", 10) + b"\x00" * 10
    body += b"data" + struct.pack("<I", 0)
    with pytest.raises(AudioFormatError):
        wav_mod.parse_wav(b"RIFF" + struct.pack("<I", len(body)) + body)


def test_parse_rejects_short_extensible_chunk(tmp_path: Path) -> None:
    fmt = struct.pack("<HHIIHH", 0xFFFE, 1, 8000, 8000, 1, 8) + struct.pack("<H", 22)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", 0)
    with pytest.raises(AudioFormatError):
        wav_mod.parse_wav(b"RIFF" + struct.pack("<I", len(body)) + body)


def test_parse_rejects_missing_fmt() -> None:
    body = b"WAVE" + b"data" + struct.pack("<I", 0)
    with pytest.raises(AudioFormatError):
        wav_mod.parse_wav(b"RIFF" + struct.pack("<I", len(body)) + body)


def test_parse_rejects_missing_data() -> None:
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000, 1, 8)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    with pytest.raises(AudioFormatError):
        wav_mod.parse_wav(b"RIFF" + struct.pack("<I", len(body)) + body)


def test_parse_rejects_multiple_data_chunks() -> None:
    data = b"\x00\x00" * 4
    body = b"WAVE" + b"fmt " + struct.pack("<I", 16) + struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    body += b"data" + struct.pack("<I", len(data)) + data
    body += b"data" + struct.pack("<I", len(data)) + data
    with pytest.raises(AudioFormatError):
        wav_mod.parse_wav(b"RIFF" + struct.pack("<I", len(body)) + body)


@pytest.mark.parametrize("tag", [3, 7, 0xFFFE])
def test_parse_rejects_unsupported_codecs(tag: int) -> None:
    raw = _wav_bytes(tag=tag, bits=16)
    if tag == 0xFFFE:
        # Extensible with a float sub-format must still be refused.
        fmt = (
            struct.pack("<HHIIHH", 0xFFFE, 1, 8000, 16000, 2, 16)
            + struct.pack("<H", 22)
            + struct.pack("<H", 16)
            + struct.pack("<I", 0)
            + struct.pack("<H", 3)
            + b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
        )
        data = b"\x00\x00" * 4
        body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
        body += b"data" + struct.pack("<I", len(data)) + data
        raw = b"RIFF" + struct.pack("<I", len(body)) + body
    with pytest.raises(AudioFormatError):
        wav_mod.parse_wav(raw)


@pytest.mark.parametrize("channels", [0, 65])
def test_parse_rejects_bad_channel_counts(channels: int) -> None:
    fmt = struct.pack("<HHIIHH", 1, channels, 8000, 8000, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", 0)
    with pytest.raises(AudioFormatError):
        wav_mod.parse_wav(b"RIFF" + struct.pack("<I", len(body)) + body)


def test_parse_rejects_unaligned_data() -> None:
    fmt = struct.pack("<HHIIHH", 1, 2, 8000, 32000, 4, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", 3) + b"\x00\x00\x00"
    with pytest.raises(AudioFormatError):
        wav_mod.parse_wav(b"RIFF" + struct.pack("<I", len(body)) + body)


def test_parse_clamps_a_stale_chunk_size() -> None:
    raw = _wav_bytes(frames=8, declared_size=1_000_000)
    layout = wav_mod.parse_wav(raw)
    assert layout.data_offset < len(raw)
    assert layout.data_size == len(raw) - layout.data_offset


def test_parse_recomputes_a_wrong_block_align() -> None:
    fmt = struct.pack("<HHIIHH", 1, 2, 8000, 32000, 3, 16)  # 3 is wrong
    data = b"\x00\x00" * 8
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    layout = wav_mod.parse_wav(b"RIFF" + struct.pack("<I", len(body)) + body)
    assert layout.block_align == 4
    assert layout.frames == 4


def test_layout_with_zero_block_align_has_no_frames() -> None:
    layout = wav_mod.WavLayout(
        data_offset=0,
        data_size=10,
        channels=0,
        sample_rate=8000,
        bits=16,
        block_align=0,
        format_tag=1,
        file_size=10,
    )
    assert layout.frames == 0


# ---------------------------------------------------------------------------
# sample codecs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bits", [8, 16, 24, 32])
def test_codec_roundtrip(bits: int) -> None:
    raw = _wav_bytes(bits=bits, channels=2, frames=16)
    layout = wav_mod.parse_wav(raw)
    payload = raw[layout.data_offset : layout.data_offset + layout.data_size]
    samples = wav_mod.decode_samples(payload, layout)
    assert samples.size == layout.frames * layout.channels
    assert wav_mod.encode_samples(samples, layout) == payload


@pytest.mark.parametrize("bits", [8, 16, 24, 32])
def test_encode_rejects_out_of_range(bits: int) -> None:
    raw = _wav_bytes(bits=bits, frames=2)
    layout = wav_mod.parse_wav(raw)
    limit = 1 << (bits - 2)
    with pytest.raises(AudioFormatError):
        wav_mod.encode_samples(np.array([limit * 4], dtype=np.int64), layout)


def test_decode_rejects_unknown_depth() -> None:
    layout = wav_mod.WavLayout(
        data_offset=44,
        data_size=4,
        channels=1,
        sample_rate=8000,
        bits=12,
        block_align=2,
        format_tag=1,
        file_size=48,
    )
    with pytest.raises(AudioFormatError):
        wav_mod.decode_samples(b"\x00\x00\x00\x00", layout)
    with pytest.raises(AudioFormatError):
        wav_mod.encode_samples(np.zeros(2, dtype=np.int64), layout)


def test_splice_requires_the_same_size() -> None:
    raw = _wav_bytes(frames=4)
    layout = wav_mod.parse_wav(raw)
    with pytest.raises(AudioFormatError):
        wav_mod.splice_data(raw, layout, b"\x00")
    same = raw[layout.data_offset : layout.data_offset + layout.data_size]
    assert wav_mod.splice_data(raw, layout, same) == raw


def test_write_wav_atomic_requires_an_existing_directory(tmp_path: Path) -> None:
    raw = _wav_bytes(frames=4)
    target = tmp_path / "nested" / "out.wav"
    with pytest.raises(PhantomIOError):
        wav_mod.write_wav_atomic(str(target), raw)
    target.parent.mkdir()
    wav_mod.write_wav_atomic(str(target), raw)
    assert target.read_bytes() == raw
    assert [entry.name for entry in target.parent.iterdir()] == ["out.wav"]


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------
def test_detect_riff_that_is_not_wave(tmp_path: Path) -> None:
    path = tmp_path / "notwave.wav"
    path.write_bytes(b"RIFF" + struct.pack("<I", 4) + b"AVI ")
    with pytest.raises(UnsupportedCarrier):
        detect_audio_kind(str(path))


def test_detect_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(PhantomIOError):
        detect_audio_kind(str(tmp_path / "missing.wav"))


@pytest.mark.parametrize("prefix", [b"OggS", b"FORM", b"ID3\x04\x00", b"\xff\xfb\x90\x00"])
def test_detect_lossy_containers(tmp_path: Path, prefix: bytes) -> None:
    path = tmp_path / "audio.bin"
    path.write_bytes(prefix + b"\x00" * 32)
    with pytest.raises(LossyAudioError):
        detect_audio_kind(str(path))


def test_three_byte_id3_tag_is_detected_by_content(tmp_path: Path) -> None:
    """Regression: the sniff table keys ``ID3`` with three bytes, not four."""
    path = tmp_path / "song.bin"
    path.write_bytes(b"ID3\x04\x00\x00" + b"\x00" * 32)
    with pytest.raises(LossyAudioError):
        detect_audio_kind(str(path))


def test_detect_wav_extension_with_garbage(tmp_path: Path) -> None:
    path = tmp_path / "fake.wav"
    path.write_bytes(b"garbage" + b"\x00" * 32)
    with pytest.raises(UnsupportedCarrier):
        detect_audio_kind(str(path))


def test_detect_flac_extension_with_garbage(tmp_path: Path) -> None:
    path = tmp_path / "fake.flac"
    path.write_bytes(b"garbage" + b"\x00" * 32)
    with pytest.raises(UnsupportedCarrier):
        detect_audio_kind(str(path))


def test_detect_unknown_extension(tmp_path: Path) -> None:
    path = tmp_path / "audio.xyz"
    path.write_bytes(b"garbage" + b"\x00" * 32)
    with pytest.raises(UnsupportedCarrier):
        detect_audio_kind(str(path))


def test_detect_wav_and_flac(wav_factory, tmp_path: Path) -> None:
    assert detect_audio_kind(str(wav_factory(bits=16, channels=1, frames=64))) is AudioKind.WAV
    carrier = tmp_path / "c.flac"
    import soundfile as sf

    sf.write(str(carrier), np.zeros((64, 1), dtype=np.int32), 44_100, subtype="PCM_16", format="FLAC")
    assert detect_audio_kind(str(carrier)) is AudioKind.FLAC


# ---------------------------------------------------------------------------
# extensions, capacity, silence
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,kind,expected",
    [
        ("a.wav", AudioKind.WAV, "a.wav"),
        ("a.WAV", AudioKind.WAV, "a.WAV"),
        ("a.flac", AudioKind.WAV, "a.wav"),
        ("a.flac", AudioKind.FLAC, "a.flac"),
        ("a.wav", AudioKind.FLAC, "a.flac"),
        ("a.mp3", AudioKind.WAV, "a.wav"),
    ],
)
def test_enforce_audio_extension(name: str, kind: AudioKind, expected: str) -> None:
    assert enforce_audio_extension(f"/tmp/{name}", kind) == f"/tmp/{expected}"


def test_capacity_matches_usable_samples(wav_factory) -> None:
    path = wav_factory(bits=16, channels=2, frames=500, kind="noise")
    info = probe_audio(str(path))
    assert info.capacity.usable_bits == 1_000
    assert audio_capacity(str(path)).payload_bytes == info.capacity.payload_bytes


def test_audio_info_duration_handles_zero_rate() -> None:
    info = AudioInfo(
        path="x.wav",
        kind=AudioKind.WAV,
        frames=100,
        channels=1,
        sample_rate=0,
        bits=16,
        capacity=Capacity(usable_bits=0, payload_bytes=0),
    )
    assert info.duration_seconds == 0.0


def test_audio_info_duration_in_seconds(wav_factory) -> None:
    path = wav_factory(bits=16, channels=1, frames=8_000, kind="noise")
    info = probe_audio(str(path))
    assert info.duration_seconds == pytest.approx(info.frames / info.sample_rate)


@pytest.mark.parametrize(
    "samples,expected",
    [
        ([], 1.0),
        ([5], 0.0),
        ([7, 7, 7], 1.0),
        ([1, 2, 3, 4], 0.0),
        ([1, 1, 2, 2], 2 / 3),
    ],
)
def test_silence_ratio(samples: list[int], expected: float) -> None:
    assert silence_ratio(np.asarray(samples)) == pytest.approx(expected)


def test_silence_notice_threshold(wav_factory) -> None:
    loud = wav_factory(bits=16, channels=1, frames=10_000, kind="noise")
    assert probe_audio(str(loud)).notices == ()
    quiet = wav_factory(bits=16, channels=1, frames=10_000, kind="silence")
    assert probe_audio(str(quiet)).silence_ratio > 0.9


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def test_load_audio_wav_keeps_the_raw_bytes(wav_factory) -> None:
    path = wav_factory(bits=24, channels=2, frames=100, kind="noise")
    loaded = load_audio(str(path))
    assert isinstance(loaded, LoadedAudio)
    assert loaded.kind is AudioKind.WAV
    assert loaded.raw == Path(path).read_bytes()
    assert loaded.frames == 100
    low, high = loaded.range()
    assert low == -(1 << 23) and high == (1 << 23) - 1


def test_loaded_audio_without_channels_has_no_frames() -> None:
    loaded = LoadedAudio(
        samples=np.zeros(0, dtype=np.int64),
        source_path="x.wav",
        channels=0,
        sample_rate=8000,
        bits=16,
        kind=AudioKind.WAV,
    )
    assert loaded.frames == 0


def test_loaded_audio_32bit_range() -> None:
    loaded = LoadedAudio(
        samples=np.zeros(3, dtype=np.int64),
        source_path="x.wav",
        channels=1,
        sample_rate=8000,
        bits=32,
        kind=AudioKind.WAV,
    )
    assert loaded.range() == (-(1 << 31), (1 << 31) - 1)


# ---------------------------------------------------------------------------
# flac error paths
# ---------------------------------------------------------------------------
def _flac(tmp_path: Path, *, bits: int = 16, frames: int = 128, channels: int = 1) -> Path:
    import soundfile as sf

    path = tmp_path / f"c{bits}{channels}.flac"
    peak = (1 << (bits - 1)) - 1
    rng = np.random.default_rng(4)
    native = rng.integers(-peak // 2, peak // 2, (frames, channels), dtype=np.int64)
    sf.write(
        str(path),
        (native << (32 - bits)).astype(np.int32),
        44_100,
        subtype="PCM_16" if bits == 16 else "PCM_24",
        format="FLAC",
    )
    return path


def test_probe_flac_rejects_other_formats(tmp_path: Path, wav_factory) -> None:
    path = wav_factory(bits=16, channels=1, frames=64)
    with pytest.raises(UnsupportedCarrier):
        flac_mod.probe_flac(str(path))


def test_probe_flac_rejects_garbage(tmp_path: Path) -> None:
    path = tmp_path / "bad.flac"
    path.write_bytes(b"fLaC" + b"\x00" * 64)
    with pytest.raises(UnsupportedCarrier):
        flac_mod.probe_flac(str(path))


def test_read_flac_detects_a_rate_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _flac(tmp_path)
    real = flac_mod.probe_flac(str(path))
    monkeypatch.setattr(
        flac_mod, "probe_flac", lambda _path: flac_mod.FlacLayout(**{**real.__dict__, "sample_rate": 1})
        if hasattr(real, "__dict__")
        else real
    )
    import soundfile as sf

    data = sf.read(str(path), dtype="int32", always_2d=True)[0]
    monkeypatch.setattr(sf, "read", lambda *_a, **_k: (data, 48000))
    with pytest.raises(AudioFormatError):
        flac_mod.read_flac(str(path))


def test_write_flac_rejects_out_of_range(tmp_path: Path) -> None:
    path = _flac(tmp_path)
    layout = flac_mod.probe_flac(str(path))
    too_big = np.array([1 << (layout.bits - 1)], dtype=np.int64)
    with pytest.raises(AudioFormatError):
        flac_mod.write_flac(str(tmp_path / "out.flac"), too_big, layout)


def test_copy_metadata_returns_false_for_a_broken_source(tmp_path: Path) -> None:
    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"fLaC" + b"\x00" * 32)
    target = _flac(tmp_path)
    assert flac_mod.copy_metadata(str(broken), str(target)) is False


def test_flac_metadata_size_of_a_broken_file(tmp_path: Path) -> None:
    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"fLaC" + b"\x00" * 32)
    assert flac_mod.flac_metadata_size(str(broken)) == 0


# ---------------------------------------------------------------------------
# FLAC failure paths that need a real encoder
# ---------------------------------------------------------------------------
def test_missing_libsndfile_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "soundfile", None)
    with pytest.raises(AudioFormatError):
        flac_mod._import_soundfile()


def test_read_flac_reports_decode_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import soundfile as real_soundfile

    path = _flac(tmp_path)
    layout = flac_mod.probe_flac(str(path))

    class BrokenSoundfile:
        info = staticmethod(real_soundfile.info)

        @staticmethod
        def read(*_args: object, **_kwargs: object) -> tuple[np.ndarray, int]:
            raise RuntimeError("decoder crashed")

    monkeypatch.setattr(flac_mod, "_import_soundfile", lambda: BrokenSoundfile)
    with pytest.raises(UnsupportedCarrier):
        flac_mod.read_flac(str(path))
    assert layout.bits in (16, 24)


def test_read_flac_rejects_a_rate_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _flac(tmp_path)
    layout = flac_mod.probe_flac(str(path))
    mismatched = flac_mod.FlacLayout(
        frames=layout.frames,
        channels=layout.channels,
        sample_rate=layout.sample_rate + 1,
        bits=layout.bits,
        subtype=layout.subtype,
    )
    monkeypatch.setattr(flac_mod, "probe_flac", lambda _path: mismatched)
    with pytest.raises(AudioFormatError):
        flac_mod.read_flac(str(path))


def test_read_flac_stays_inside_the_declared_depth(tmp_path: Path) -> None:
    """Left-justified int32 decoding cannot leave the declared range."""
    path = _flac(tmp_path, bits=24, frames=500)
    native, layout = flac_mod.read_flac(str(path))
    assert layout.bits == 24
    assert native.min() >= -(1 << 23)
    assert native.max() <= (1 << 23) - 1


def test_write_flac_reports_encoder_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import soundfile as real_soundfile

    path = _flac(tmp_path)
    layout = flac_mod.probe_flac(str(path))

    class BrokenSoundfile:
        info = staticmethod(real_soundfile.info)

        @staticmethod
        def write(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("encoder crashed")

    monkeypatch.setattr(flac_mod, "_import_soundfile", lambda: BrokenSoundfile)
    with pytest.raises(AudioFormatError):
        flac_mod.write_flac(str(tmp_path / "out.flac"), np.zeros(8, dtype=np.int64), layout)


def test_write_flac_atomic_creates_the_file(tmp_path: Path) -> None:
    source = _flac(tmp_path)
    layout = flac_mod.probe_flac(str(source))
    samples, _ = flac_mod.read_flac(str(source))
    target = tmp_path / "written.flac"
    flac_mod.write_flac_atomic(str(target), samples, layout)
    assert target.exists()
    assert flac_mod.flac_metadata_size(str(target)) >= 1


def test_copy_metadata_without_tags_returns_true(tmp_path: Path) -> None:
    source = _flac(tmp_path)
    target = _flac(tmp_path)
    assert flac_mod.copy_metadata(str(source), str(target)) is True


def test_flac_metadata_size_reports_zero_without_mutagen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    path = _flac(tmp_path)
    monkeypatch.setitem(sys.modules, "mutagen.flac", None)
    assert flac_mod.flac_metadata_size(str(path)) == 0


# ---------------------------------------------------------------------------
# file payloads through audio, and tag notices
# ---------------------------------------------------------------------------
def test_audio_file_payload_roundtrip(wav_factory, tmp_path: Path, password: str) -> None:
    from phantom.audio import embed_file_into_audio, extract_from_audio

    carrier = wav_factory(bits=16, channels=2, frames=8_000, kind="noise")
    secret = tmp_path / "payload.bin"
    secret.write_bytes(b"audio file payload" * 40)
    result = embed_file_into_audio(str(carrier), str(tmp_path / "out.wav"), str(secret), password)
    assert result.frames == 8_000
    extracted = extract_from_audio(result.path, password)
    assert extracted.data == secret.read_bytes()
    assert extracted.name == "payload.bin"


def test_audio_file_payload_capacity_error(wav_factory, tmp_path: Path, password: str) -> None:
    import os as _os

    from phantom.audio import embed_file_into_audio
    from phantom.core.errors import CapacityError

    carrier = wav_factory(bits=16, channels=1, frames=600, kind="noise")
    secret = tmp_path / "big.bin"
    secret.write_bytes(_os.urandom(4_000))
    with pytest.raises(CapacityError):
        embed_file_into_audio(str(carrier), str(tmp_path / "out.wav"), str(secret), password)


def test_flac_embed_reports_the_tags_notice(tmp_path: Path, password: str) -> None:
    from phantom.audio import NOTICE_AUDIO_TAGS, NOTICE_AUDIO_TAGS_LOST, embed_into_audio
    from phantom.core.payload import Payload

    carrier = _flac(tmp_path, frames=4_000)
    result = embed_into_audio(
        str(carrier), str(tmp_path / "out.flac"), Payload.text("tags"), password
    )
    assert result.notices == (NOTICE_AUDIO_TAGS,)
    assert NOTICE_AUDIO_TAGS_LOST not in result.notices


def test_flac_embed_reports_lost_tags(
    tmp_path: Path, password: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from phantom.audio import NOTICE_AUDIO_TAGS_LOST, embed_into_audio
    from phantom.core.payload import Payload

    carrier = _flac(tmp_path, frames=4_000)
    monkeypatch.setattr(flac_mod, "copy_metadata", lambda _source, _target: False)
    result = embed_into_audio(
        str(carrier), str(tmp_path / "out.flac"), Payload.text("tags"), password
    )
    assert NOTICE_AUDIO_TAGS_LOST in result.notices


def test_extract_from_audio_on_clean_carrier(wav_factory, password: str) -> None:
    from phantom.audio import extract_from_audio
    from phantom.core.errors import WrongPasswordOrNoData

    carrier = wav_factory(bits=16, channels=2, frames=4_000, kind="noise")
    with pytest.raises(WrongPasswordOrNoData):
        extract_from_audio(str(carrier), password)


def test_lossy_audio_cannot_be_embedded(tmp_path: Path, password: str) -> None:
    from phantom.audio import embed_into_audio
    from phantom.core.payload import Payload

    lossy = tmp_path / "song.mp3"
    lossy.write_bytes(b"ID3\x04" + b"\x00" * 64)
    with pytest.raises(LossyAudioError):
        embed_into_audio(str(lossy), str(tmp_path / "out.mp3"), Payload.text("x"), password)


def test_audio_output_is_refused_when_it_equals_the_carrier(
    wav_factory, password: str
) -> None:
    from phantom.audio import embed_into_audio
    from phantom.core.payload import Payload

    carrier = wav_factory(bits=16, channels=1, frames=4_000, kind="noise")
    with pytest.raises(Exception):
        embed_into_audio(str(carrier), str(carrier), Payload.text("x"), password)


def test_probe_flac_rejects_an_unsupported_subtype(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _flac(tmp_path)

    class FakeInfo:
        format = "FLAC"
        subtype = "PCM_U8"
        frames = 10
        channels = 1
        samplerate = 44_100

    class FakeSoundfile:
        @staticmethod
        def info(_path: str) -> FakeInfo:
            return FakeInfo()

    monkeypatch.setattr(flac_mod, "_import_soundfile", lambda: FakeSoundfile)
    with pytest.raises(AudioFormatError):
        flac_mod.probe_flac(str(path))


def test_copy_metadata_without_mutagen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    source = _flac(tmp_path)
    target = _flac(tmp_path)
    monkeypatch.setitem(sys.modules, "mutagen.flac", None)
    assert flac_mod.copy_metadata(str(source), str(target)) is False


def test_write_flac_atomic_rejects_an_empty_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _flac(tmp_path)
    layout = flac_mod.probe_flac(str(source))
    monkeypatch.setattr(flac_mod, "write_flac", lambda path, samples, layout: None)
    with pytest.raises(AudioFormatError):
        flac_mod.write_flac_atomic(str(tmp_path / "empty.flac"), np.zeros(4, dtype=np.int64), layout)
