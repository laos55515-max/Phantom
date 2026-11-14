import os
import struct
import zlib

import numpy as np
import pytest
import soundfile as sf
from PIL import Image
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from phantom import (AudioSteganographyEngine, ImageSteganographyEngine,
                     AuthenticationError, CapacityError, CarrierError)
from phantom.crypto import (LEGACY_KDF_ITERATIONS, MAGIC, TYPE_FILE, TYPE_TEXT,
                            derive_key, pack_payload)

PASSWORD = "a strong test password"


@pytest.fixture
def image_path(tmp_path):
    path = tmp_path / "source.png"
    pixels = np.random.default_rng(42).integers(0, 256, (180, 180, 4), dtype=np.uint8)
    Image.fromarray(pixels, "RGBA").save(path)
    return path


@pytest.mark.parametrize("payload,filename", [("Hello, Україно!\n", None), (b"\x00\x01hello\xff" * 40, "тест.bin")])
def test_png_roundtrip(image_path, tmp_path, payload, filename):
    output = tmp_path / "out.png"
    progress = []
    engine = ImageSteganographyEngine()
    engine.hide(image_path, output, payload, PASSWORD, filename=filename, progress=lambda *x: progress.append(x))
    result = engine.extract(output, PASSWORD)
    assert result.data == (payload.encode() if isinstance(payload, str) else payload)
    assert result.filename == (filename or "")
    assert result.payload_type == (TYPE_FILE if filename else TYPE_TEXT)
    assert result.integrity_ok
    with Image.open(image_path) as original, Image.open(output) as stego:
        a = np.asarray(original).astype(np.int16)
        b = np.asarray(stego).astype(np.int16)
        assert np.array_equal(a[:, :, 3], b[:, :, 3])
        assert np.abs(a[:, :, :3] - b[:, :, :3]).max() <= 1
    assert any(stage == "embed" and done == total for stage, done, total in progress)


@pytest.mark.parametrize("fmt,subtype", [("WAV", "PCM_16"), ("WAV", "PCM_24"),
                                         ("WAV", "PCM_32"), ("FLAC", "PCM_16"),
                                         ("FLAC", "PCM_24")])
def test_audio_roundtrip_and_sample_fidelity(tmp_path, fmt, subtype):
    ext = fmt.lower()
    source = tmp_path / f"source.{ext}"
    output = tmp_path / f"out.{ext}"
    shift = {"PCM_16": 16, "PCM_24": 8, "PCM_32": 0}[subtype]
    rng = np.random.default_rng(4)
    samples = rng.integers(-(2**30), 2**30, size=(50_000, 2), dtype=np.int32)
    # Make sample values representable in the chosen PCM subtype.
    samples = (samples >> shift) << shift
    sf.write(source, samples, 44_100, format=fmt, subtype=subtype)
    engine = AudioSteganographyEngine()
    engine.hide(source, output, b"important \x00 bytes" * 80, PASSWORD, filename="test.bin")
    result = engine.extract(output, PASSWORD)
    assert result.filename == "test.bin"
    assert result.data == b"important \x00 bytes" * 80
    before, _ = sf.read(source, dtype="int32", always_2d=True)
    after, _ = sf.read(output, dtype="int32", always_2d=True)
    assert sf.info(output).subtype == subtype
    assert np.max(np.abs((before.astype(np.int64) - after.astype(np.int64)))) <= (1 << shift)


def test_audio_text_roundtrip(tmp_path):
    source, out = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(source, np.zeros((20_000, 1), dtype=np.int16), 44100, subtype="PCM_16")
    AudioSteganographyEngine().hide(source, out, "audio test", PASSWORD)
    assert AudioSteganographyEngine().extract(out, PASSWORD).data == b"audio test"


def test_legacy_200k_png_is_readable(image_path, tmp_path):
    # Build an old-format carrier independently of the new writer.
    salt, nonce = b"s" * 16, b"n" * 12
    plaintext = pack_payload("старый файл".encode())
    key = derive_key(PASSWORD, salt, LEGACY_KDF_ITERATIONS)
    blob = salt + nonce + AESGCM(key).encrypt(nonce, plaintext, None)
    encoded = struct.pack(">I", len(blob)) + blob
    with Image.open(image_path) as image:
        pixels = np.array(image)
    bits = np.unpackbits(np.frombuffer(encoded, dtype=np.uint8))
    positions = np.arange(len(bits))
    flat = pixels.reshape(-1, 4)
    rows, channels = positions // 3, positions % 3
    flat[rows, channels] = (flat[rows, channels] & 254) | bits
    out = tmp_path / "legacy.png"
    Image.fromarray(pixels).save(out)
    assert ImageSteganographyEngine().extract(out, PASSWORD).data.decode() == "старый файл"


def test_bad_password_and_damaged_stego(image_path, tmp_path):
    out = tmp_path / "out.png"
    engine = ImageSteganographyEngine()
    engine.hide(image_path, out, "private", PASSWORD)
    with pytest.raises(AuthenticationError):
        engine.extract(out, "wrong")
    with Image.open(out) as im:
        pixels = np.array(im)
    # Flip a bit within ciphertext, leaving the header intact.
    flat = pixels.reshape(-1, 4)
    flat[100, 0] ^= 1
    Image.fromarray(pixels).save(out)
    with pytest.raises(AuthenticationError):
        engine.extract(out, PASSWORD)


def test_capacity_format_and_same_output(image_path, tmp_path):
    engine = ImageSteganographyEngine()
    tiny = tmp_path / "tiny.png"
    Image.new("RGB", (4, 4)).save(tiny)
    with pytest.raises(CapacityError):
        engine.hide(tiny, tmp_path / "small.png", "hello", PASSWORD)
    wrong = tmp_path / "not-png.png"
    Image.new("RGB", (50, 50)).save(wrong, format="JPEG")
    with pytest.raises(CarrierError):
        engine.check_capacity(wrong)
    old = image_path.read_bytes()
    with pytest.raises(CarrierError):
        engine.hide(image_path, image_path, "hello", PASSWORD)
    assert image_path.read_bytes() == old


def test_audio_rejects_lossy_and_floating_point(tmp_path):
    source = tmp_path / "float.wav"
    sf.write(source, np.zeros(3000, dtype=np.float32), 44100, subtype="FLOAT")
    with pytest.raises(CarrierError):
        AudioSteganographyEngine().check_capacity(source)


def test_uncompressed_payload_limit():
    from phantom.crypto import MAX_DATA_BYTES, unpack_payload
    raw = b"\0" * (MAX_DATA_BYTES + 1)
    compressed = zlib.compress(raw, 9)
    payload = b"\x01\x00\x00" + b"x" * 32 + struct.pack(">I", len(compressed)) + compressed
    with pytest.raises(AuthenticationError):
        unpack_payload(payload)


@pytest.mark.parametrize("fmt,subtype", [("WAV", "PCM_U8"), ("WAVEX", "PCM_24"), ("FLAC", "PCM_S8")])
def test_other_integer_pcm_encodings(tmp_path, fmt, subtype):
    ext = ".flac" if fmt == "FLAC" else ".wav"
    source, output = tmp_path / ("source" + ext), tmp_path / ("output" + ext)
    data = np.zeros((40_001, 3), dtype=np.int32)
    sf.write(source, data, 22050, format=fmt, subtype=subtype)
    engine = AudioSteganographyEngine()
    engine.hide(source, output, "odd channels", PASSWORD)
    assert engine.extract(output, PASSWORD).data == b"odd channels"
    assert sf.info(output).subtype == subtype


def test_audio_wrong_password_and_damage(tmp_path):
    source, output = tmp_path / "in.wav", tmp_path / "out.wav"
    sf.write(source, np.zeros((20000, 2), dtype=np.int16), 44100, subtype="PCM_16")
    engine = AudioSteganographyEngine()
    engine.hide(source, output, "audio payload", PASSWORD)
    with pytest.raises(AuthenticationError):
        engine.extract(output, "incorrect")
    samples, rate = sf.read(output, dtype="int32", always_2d=True)
    samples.reshape(-1)[200] ^= (1 << 16)
    sf.write(output, samples, rate, subtype="PCM_16")
    with pytest.raises(AuthenticationError):
        engine.extract(output, PASSWORD)


def test_png_palette_transparency_preserved(tmp_path):
    source, output = tmp_path / "palette.png", tmp_path / "out.png"
    img = Image.new("P", (120, 120))
    img.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
    img.info["transparency"] = 0
    img.save(source, transparency=0)
    ImageSteganographyEngine().hide(source, output, "hello", PASSWORD)
    with Image.open(source) as a, Image.open(output) as b:
        assert np.array_equal(np.array(a.convert("RGBA"))[:, :, 3], np.array(b.convert("RGBA"))[:, :, 3])
    assert ImageSteganographyEngine().extract(output, PASSWORD).data == b"hello"


def test_over_capacity_does_not_replace_existing_output(image_path, tmp_path):
    output = tmp_path / "existing.png"
    output.write_bytes(b"keep me")
    with pytest.raises(CapacityError):
        ImageSteganographyEngine().hide(image_path, output, os.urandom(50000), PASSWORD, filename="x.bin")
    assert output.read_bytes() == b"keep me"


def test_secret_size_limit_before_read(tmp_path):
    import core
    from phantom.crypto import MAX_DATA_BYTES
    path = tmp_path / "oversize.bin"
    with open(path, "wb") as file:
        file.truncate(MAX_DATA_BYTES + 1)
    with pytest.raises(CapacityError):
        core._read_file(path)


def test_filename_path_traversal_is_removed():
    from phantom.crypto import unpack_payload
    result = unpack_payload(pack_payload(b"data", r"C:\\secret\\..\\test.txt"))
    assert result.filename == "test.txt"


def test_locales_have_matching_keys_and_format_fields():
    import json
    import string
    from pathlib import Path
    base = Path(__file__).resolve().parents[1] / "locales"
    packs = [json.loads((base / f"{language}.json").read_text(encoding="utf-8"))
             for language in ("ru", "ua", "en")]
    assert all(p.keys() == packs[0].keys() for p in packs)
    formatter = string.Formatter()
    for key in packs[0]:
        fields = [{field for _, field, _, _ in formatter.parse(pack[key]) if field}
                  for pack in packs]
        assert fields[0] == fields[1] == fields[2], key


def test_secure_delete_refuses_links_without_modifying_target(tmp_path):
    import core
    original = tmp_path / "secret.bin"
    original.write_bytes(b"important source")
    symlink = tmp_path / "link.bin"
    try:
        symlink.symlink_to(original)
    except (NotImplementedError, OSError):
        pytest.skip("Symbolic links are unavailable")
    with pytest.raises((core.PhantomError, OSError)):
        core.secure_delete(symlink)
    assert original.read_bytes() == b"important source"
    assert symlink.is_symlink()
    hardlink = tmp_path / "hardlink.bin"
    try:
        os.link(original, hardlink)
    except OSError:
        pytest.skip("Hard links are unavailable")
    with pytest.raises(core.PhantomError):
        core.secure_delete(original)
    assert original.read_bytes() == b"important source"
    assert hardlink.read_bytes() == b"important source"


def test_key_derivation_matches_pbkdf2_600000():
    import hashlib
    salt = bytes(range(16))
    expected = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), salt, 600_000, dklen=32)
    assert derive_key(PASSWORD, salt) == expected


def test_payload_with_mismatching_digest_is_rejected():
    from phantom.crypto import unpack_payload
    payload = bytearray(pack_payload(b"authenticated content"))
    payload[3] ^= 1
    with pytest.raises(AuthenticationError):
        unpack_payload(bytes(payload))
