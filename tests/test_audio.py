"""Audio carriers: WAV matrix, FLAC depths and the libsndfile 24-bit trap."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from phantom.audio import (
    NOTICE_AUDIO_SILENCE,
    NOTICE_AUDIO_TAGS,
    AudioKind,
    audio_capacity,
    detect_audio_kind,
    extract_from_audio,
    load_audio,
    probe_audio,
)
from phantom.audio import flac as flac_mod
from phantom.audio import wav as wav_mod
from phantom.core import api
from phantom.core import constants as C
from phantom.core.errors import (
    AudioFormatError,
    CapacityError,
    LossyAudioError,
    UnsupportedCarrier,
    WrongPasswordOrNoData,
)
from phantom.core.payload import Payload

BITS = (8, 16, 24, 32)
CHANNELS = ((1, "mono"), (2, "stereo"), (6, "5.1"))


def _make_extensible_wav(path: Path, *, bits: int, channels: int, frames: int) -> None:
    peak = (1 << (bits - 1)) - 1
    rng = np.random.default_rng(3)
    data = rng.integers(-peak // 2, peak // 2, (frames, channels), dtype=np.int64)
    layout = wav_mod.WavLayout(
        data_offset=0,
        data_size=0,
        channels=channels,
        sample_rate=44100,
        bits=bits,
        block_align=channels * bits // 8,
        format_tag=1,
        file_size=0,
    )
    encoded = wav_mod.encode_samples(data.reshape(-1), layout)
    subformat = struct.pack("<H", 1) + b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    fmt = (
        struct.pack("<HHIIHH", 0xFFFE, channels, 44100, 44100 * channels * bits // 8, channels * bits // 8, bits)
        + struct.pack("<H", 22)
        + struct.pack("<H", bits)
        + struct.pack("<I", 0)
        + subformat
    )
    chunks = (
        b"fmt " + struct.pack("<I", len(fmt)) + fmt
        + b"data" + struct.pack("<I", len(encoded)) + encoded
    )
    body = b"WAVE" + chunks
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


@pytest.mark.parametrize("bits", BITS)
@pytest.mark.parametrize("channels,label", CHANNELS)
@pytest.mark.parametrize("kind", ["short", "long", "silence", "fullscale"])
def test_wav_matrix_roundtrip(  # noqa: PLR0917 - fixtures plus four parameters
    tmp_path: Path,
    wav_factory,
    password: str,
    bits: int,
    channels: int,
    label: str,
    kind: str,
) -> None:
    frames = 4_000 if kind in ("short", "silence") else 12_000
    carrier = wav_factory(bits=bits, channels=channels, frames=frames, kind=kind)
    output = tmp_path / f"stego_{bits}_{channels}_{kind}.wav"
    payload = Payload.text(f"audio {bits}/{channels}/{kind}")
    outcome = api.embed_payload(str(carrier), str(output), payload, password=password)
    assert outcome.verified
    result = api.extract_from_path(outcome.output_path, password)
    assert result.payload.data == payload.data

    original = carrier.read_bytes()
    updated = Path(outcome.output_path).read_bytes()
    assert len(updated) == len(original), "WAV size must not change"
    layout = wav_mod.parse_wav(original)
    differing = [i for i, (a, b) in enumerate(zip(original, updated)) if a != b]
    assert differing, "nothing was embedded"
    assert min(differing) >= layout.data_offset
    assert max(differing) < layout.data_offset + layout.data_size


@pytest.mark.parametrize("bits", BITS)
def test_wav_changes_are_within_one_lsb(wav_factory, tmp_path: Path, password: str, bits: int) -> None:
    carrier = wav_factory(bits=bits, channels=2, frames=4_000, kind="noise")
    output = tmp_path / f"lsb_{bits}.wav"
    api.embed_payload(str(carrier), str(output), Payload.text("lsb"), password=password)
    before = load_audio(str(carrier))
    after = load_audio(str(output))
    delta = np.abs(after.samples.astype(np.int64) - before.samples.astype(np.int64))
    assert delta.max() <= 1
    low, high = before.range()
    assert after.samples.min() >= low
    assert after.samples.max() <= high


@pytest.mark.parametrize("bits", BITS)
def test_wav_decoded_by_the_standard_library(wav_factory, tmp_path: Path, password: str, bits: int) -> None:
    """Second, independent decoder: the stdlib ``wave`` module."""
    carrier = wav_factory(bits=bits, channels=2, frames=3_000, kind="noise")
    output = tmp_path / f"stdlib_{bits}.wav"
    api.embed_payload(str(carrier), str(output), Payload.text("stdlib"), password=password)
    with wave.open(str(output), "rb") as handle:
        frames = handle.getnframes()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        payload = handle.readframes(frames)
    assert frames == 3_000
    assert channels == 2
    assert width == bits // 8
    assert len(payload) == frames * channels * width
    with wave.open(str(carrier), "rb") as handle:
        assert handle.readframes(handle.getnframes()) != payload


def test_wav_extensible_format_is_supported(tmp_path: Path, password: str) -> None:
    carrier = tmp_path / "extensible.wav"
    _make_extensible_wav(carrier, bits=24, channels=2, frames=4_000)
    layout = wav_mod.parse_wav(carrier.read_bytes())
    assert layout.format_tag == 1
    assert layout.bits == 24
    output = tmp_path / "extensible_out.wav"
    api.embed_payload(str(carrier), str(output), Payload.text("extensible"), password=password)
    assert api.extract_from_path(str(output), password).payload.data == b"extensible"


def test_float_wav_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "float.wav"
    frames = 1_000
    samples = np.linspace(-0.5, 0.5, frames * 2, dtype=np.float32).tobytes()
    fmt = struct.pack("<HHIIHH", 3, 2, 48000, 48000 * 8, 8, 32)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(samples)) + samples
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    with pytest.raises(AudioFormatError):
        probe_audio(str(path))


def test_unsupported_bit_depth_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "twelve.wav"
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000 * 2, 2, 12)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    with pytest.raises(AudioFormatError):
        probe_audio(str(path))


@pytest.mark.parametrize("name,content", [("song.mp3", b"ID3\x04\x00\x00"), ("song.ogg", b"OggS\x00\x02")])
def test_lossy_formats_are_refused(tmp_path: Path, name: str, content: bytes) -> None:
    path = tmp_path / name
    path.write_bytes(content + b"\x00" * 128)
    with pytest.raises(LossyAudioError):
        detect_audio_kind(str(path))


def test_lossy_extension_refused_before_reading(tmp_path: Path) -> None:
    path = tmp_path / "song.opus"
    path.write_bytes(b"OggS" + b"\x00" * 32)
    with pytest.raises(LossyAudioError):
        detect_audio_kind(str(path))


def test_wav_with_odd_data_chunk_keeps_padding(tmp_path: Path, password: str) -> None:
    carrier = tmp_path / "odd.wav"
    samples = np.arange(0, 3001, dtype=np.int64) % 256
    encoded = (samples).astype(np.uint8).tobytes()  # 3001 bytes, odd
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000, 1, 8)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(encoded)) + encoded + b"\x00"
    body = b"WAVE" + chunks
    carrier.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    output = tmp_path / "odd_out.wav"
    api.embed_payload(str(carrier), str(output), Payload.text("odd"), password=password)
    assert carrier.stat().st_size == output.stat().st_size
    assert api.extract_from_path(str(output), password).payload.data == b"odd"


def test_silence_produces_a_warning(wav_factory) -> None:
    path = wav_factory(bits=16, channels=1, frames=20_000, kind="silence")
    info = probe_audio(str(path))
    assert info.silence_ratio > 0.9
    assert NOTICE_AUDIO_SILENCE in info.notices


def test_audio_capacity_counts_every_sample_channel(wav_factory) -> None:
    path = wav_factory(bits=16, channels=2, frames=5_000, kind="noise")
    capacity = audio_capacity(str(path))
    assert capacity.usable_bits == 10_000
    assert capacity.payload_bytes == (10_000 - C.HEADER_BITS) // 8 - C.TAG_SIZE


def test_audio_capacity_error( tmp_path: Path, password: str) -> None:
    path = tmp_path / "tiny.wav"
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 16000, 2, 16)
    data = b"\x00\x00" * 32
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    with pytest.raises(CapacityError):
        api.embed_payload(str(path), str(tmp_path / "o.wav"), Payload.text("x" * 500), password=password)


def test_audio_output_extension_matches_container(tmp_path: Path, wav_factory, password: str) -> None:
    carrier = wav_factory(bits=16, channels=1, frames=2_000, kind="noise")
    outcome = api.embed_payload(
        str(carrier), str(tmp_path / "out.flac"), Payload.text("ext"), password=password
    )
    assert outcome.output_path.endswith(".wav")


def test_wrong_password_on_audio(wav_factory, password: str) -> None:
    carrier = wav_factory(bits=16, channels=2, frames=4_000, kind="noise")
    with pytest.raises(WrongPasswordOrNoData):
        extract_from_audio(str(carrier), password)


# ---------------------------------------------------------------------------
# FLAC
# ---------------------------------------------------------------------------
def _make_flac(path: Path, *, bits: int, channels: int, frames: int = 8_000, tags: bool = False) -> np.ndarray:
    subtype = "PCM_16" if bits == 16 else "PCM_24"
    peak = (1 << (bits - 1)) - 1
    rng = np.random.default_rng(17)
    native = rng.integers(-peak // 2, peak // 2, (frames, channels), dtype=np.int64)
    shift = 32 - bits
    sf.write(
        str(path),
        (native << shift).astype(np.int32),
        44_100,
        subtype=subtype,
        format="FLAC",
    )
    if tags:
        from mutagen.flac import Picture

        audio = sf  # keep the import local and explicit
        del audio
        from mutagen.flac import FLAC

        handle = FLAC(str(path))
        handle["TITLE"] = "PHANTOM test"
        handle["ARTIST"] = "unit tests"
        picture = Picture()
        picture.type = 3
        picture.mime = "image/png"
        picture.data = _tiny_png()
        handle.add_picture(picture)
        handle.save()
    return native


def _tiny_png() -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.parametrize("bits", [16, 24])
@pytest.mark.parametrize("channels", [1, 2])
def test_flac_roundtrip(tmp_path: Path, password: str, bits: int, channels: int) -> None:
    carrier = tmp_path / f"carrier_{bits}_{channels}.flac"
    _make_flac(carrier, bits=bits, channels=channels)
    output = tmp_path / f"stego_{bits}_{channels}.flac"
    outcome = api.embed_payload(
        str(carrier), str(output), Payload.text(f"flac {bits}/{channels}"), password=password
    )
    assert outcome.verified
    result = api.extract_from_path(outcome.output_path, password)
    assert result.payload.data == f"flac {bits}/{channels}".encode()


@pytest.mark.parametrize("bits", [16, 24])
def test_flac_survives_re_encoding_and_second_decoder(tmp_path: Path, password: str, bits: int) -> None:
    carrier = tmp_path / f"carrier_{bits}.flac"
    original = _make_flac(carrier, bits=bits, channels=2)
    output = tmp_path / f"stego_{bits}.flac"
    api.embed_payload(str(carrier), str(output), Payload.text("reencode"), password=password)

    # Decoder 1: soundfile (libsndfile) into int32, left-justified.
    decoded, rate = sf.read(str(output), dtype="int32", always_2d=True)
    assert rate == 44_100
    shift = 32 - bits
    native = np.asarray(decoded, dtype=np.int64) >> shift
    delta = np.abs(native - original)
    assert delta.max() <= 1
    # Left-justified means the low 8 bits are zero — the trap we must avoid.
    assert (np.asarray(decoded) & 0xFF == 0).all()

    # Decoder 2: ffprobe/ffmpeg when available.
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", str(output)],
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0
        assert b"flac" in completed.stdout.lower()


def test_flac_metadata_is_preserved(tmp_path: Path, password: str) -> None:
    carrier = tmp_path / "tagged.flac"
    _make_flac(carrier, bits=16, channels=2, tags=True)
    output = tmp_path / "tagged_out.flac"
    outcome = api.embed_payload(
        str(carrier), str(output), Payload.text("tags"), password=password
    )
    assert NOTICE_AUDIO_TAGS in outcome.notices
    from mutagen.flac import FLAC

    source = FLAC(str(carrier))
    copied = FLAC(str(output))
    assert copied["TITLE"] == source["TITLE"]
    assert copied["ARTIST"] == source["ARTIST"]
    assert len(copied.pictures) == len(source.pictures)


def test_flac_24bit_native_lsb_survives(tmp_path: Path, password: str) -> None:
    """Explicit regression for the int32 left-justified trap in libsndfile."""
    carrier = tmp_path / "lsb24.flac"
    native = _make_flac(carrier, bits=24, channels=2, frames=6_000)
    output = tmp_path / "lsb24_out.flac"
    api.embed_payload(str(carrier), str(output), Payload.text("24bit"), password=password)
    info = sf.info(str(output))
    assert info.subtype == "PCM_24"
    decoded, _ = sf.read(str(output), dtype="int32", always_2d=True)
    shifted = np.asarray(decoded, dtype=np.int64) >> 8
    assert np.abs(shifted - native).max() <= 1
    # If the writer had used raw 24-bit values in an int32 buffer, the low byte
    # would be non-zero and the data would be lost.
    assert (np.asarray(decoded) & 0xFF == 0).all()


def test_flac_capacity_and_probe(tmp_path: Path) -> None:
    carrier = tmp_path / "cap.flac"
    _make_flac(carrier, bits=16, channels=2, frames=2_000)
    info = probe_audio(str(carrier))
    assert info.kind is AudioKind.FLAC
    assert info.channels == 2
    assert info.bits == 16
    assert info.capacity.usable_bits == 4_000


def test_flac_with_wrong_extension_is_detected_by_content(tmp_path: Path) -> None:
    path = tmp_path / "mislabelled.wav"
    _make_flac(path, bits=16, channels=1, frames=1_000)
    assert detect_audio_kind(str(path)) is AudioKind.FLAC


def test_mp3_renamed_to_wav_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "fake.wav"
    path.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + os.urandom(64))
    with pytest.raises((LossyAudioError, UnsupportedCarrier)):
        detect_audio_kind(str(path))


def test_flac_metadata_helper_reports_blocks(tmp_path: Path) -> None:
    plain = tmp_path / "plain.flac"
    _make_flac(plain, bits=16, channels=1, frames=1_000)
    assert flac_mod.flac_metadata_size(str(plain)) >= 1
