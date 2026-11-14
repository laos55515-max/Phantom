"""PNG carrier edges: header policy, notices, ICC/DPI and legacy pixels."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from phantom.core import api
from phantom.core import constants as C
from phantom.core.errors import PhantomIOError, UnsupportedCarrier, WrongPasswordOrNoData
from phantom.stego import image as image_mod
from phantom.stego.image import (
    extract_payload_from_pixels,
    image_capacity,
    prepare_image,
    probe_image,
    save_png,
)

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def _header_only(
    *, width: int = 8, height: int = 8, bit_depth: int = 8, colour_type: int = 2, extra: bytes = b""
) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, colour_type, 0, 0, 0)
    return PNG_SIG + _chunk(b"IHDR", ihdr) + extra + _chunk(b"IEND", b"")


def _write(tmp_path: Path, raw: bytes, name: str = "carrier.png") -> Path:
    path = tmp_path / name
    path.write_bytes(raw)
    return path


# ---------------------------------------------------------------------------
# header policy
# ---------------------------------------------------------------------------
def test_probe_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PhantomIOError):
        probe_image(str(tmp_path / "missing.png"))


def test_probe_rejects_a_truncated_png(tmp_path: Path) -> None:
    path = _write(tmp_path, PNG_SIG + b"\x00" * 10)
    with pytest.raises(UnsupportedCarrier):
        probe_image(str(path))


def test_probe_rejects_an_empty_image(tmp_path: Path) -> None:
    path = _write(tmp_path, _header_only(width=0))
    with pytest.raises(UnsupportedCarrier):
        probe_image(str(path))


@pytest.mark.parametrize("bit_depth", [3, 5, 12])
def test_probe_rejects_unexpected_bit_depths(tmp_path: Path, bit_depth: int) -> None:
    path = _write(tmp_path, _header_only(bit_depth=bit_depth))
    with pytest.raises(UnsupportedCarrier):
        probe_image(str(path))


@pytest.mark.parametrize("bit_depth", [1, 2, 4])
def test_low_bit_depths_are_accepted(tmp_path: Path, bit_depth: int) -> None:
    """1/2/4-bit PNGs expand to 8-bit without loss, so they are valid carriers."""
    raw = _header_only(bit_depth=bit_depth, colour_type=0)
    path = _write(tmp_path, raw)
    info = probe_image(str(path))
    assert info.width == 8 and info.height == 8


def test_probe_rejects_sixteen_bit(tmp_path: Path) -> None:
    path = _write(tmp_path, _header_only(bit_depth=16))
    with pytest.raises(UnsupportedCarrier):
        probe_image(str(path))


def test_probe_rejects_unknown_colour_type(tmp_path: Path) -> None:
    path = _write(tmp_path, _header_only(colour_type=5))
    with pytest.raises(UnsupportedCarrier):
        probe_image(str(path))


def test_probe_rejects_apng(tmp_path: Path) -> None:
    actl = _chunk(b"acTL", struct.pack(">II", 2, 0))
    idat = _chunk(b"IDAT", zlib.compress(b"\x00" * 32))
    path = _write(tmp_path, _header_only(extra=actl) + idat)
    with pytest.raises(UnsupportedCarrier):
        probe_image(str(path))


def test_probe_rejects_garbage_after_the_signature(tmp_path: Path) -> None:
    path = _write(tmp_path, PNG_SIG + b"garbage" * 8)
    with pytest.raises(UnsupportedCarrier):
        probe_image(str(path))


def test_probe_rejects_a_non_png_with_a_png_extension(tmp_path: Path) -> None:
    path = _write(tmp_path, b"not a png at all", "carrier.jpg")
    with pytest.raises(UnsupportedCarrier):
        probe_image(str(path))


def test_probe_accepts_a_png_renamed_to_jpg(tmp_path: Path) -> None:
    Image.new("RGB", (32, 32), (10, 20, 30)).save(tmp_path / "renamed.jpg", format="PNG")
    info = probe_image(str(tmp_path / "renamed.jpg"))
    assert info.mode == "RGB"
    assert info.capacity.usable_bits == 32 * 32 * 3


def test_probe_reports_multi_frame_apng(tmp_path: Path) -> None:
    frames = [Image.new("RGB", (16, 16), (i, i, i)) for i in (10, 20)]
    path = tmp_path / "animated.png"
    frames[0].save(path, save_all=True, append_images=frames[1:], format="PNG")
    with pytest.raises(UnsupportedCarrier):
        probe_image(str(path))


# ---------------------------------------------------------------------------
# notices and modes
# ---------------------------------------------------------------------------
def test_gray_notice(tmp_path: Path) -> None:
    path = tmp_path / "gray.png"
    Image.new("L", (40, 40), 120).save(path)
    info = probe_image(str(path))
    assert image_mod.NOTICE_GRAYSCALE in info.notices


def test_bitmap_notice(tmp_path: Path) -> None:
    path = tmp_path / "bitmap.png"
    Image.new("1", (40, 40), 1).save(path)
    info = probe_image(str(path))
    assert image_mod.NOTICE_BITMAP in info.notices


def test_palette_notice(tmp_path: Path) -> None:
    path = tmp_path / "palette.png"
    palette = Image.new("P", (40, 40), 0)
    palette.putpalette([0, 0, 0, 255, 255, 255, 9, 9, 9] + [0] * 759)
    palette.save(path)
    info = probe_image(str(path))
    assert image_mod.NOTICE_PALETTE in info.notices


def test_palette_transparency_notice(tmp_path: Path) -> None:
    path = tmp_path / "transparent.png"
    palette = Image.new("P", (40, 40), 0)
    palette.putpalette([0, 0, 0, 255, 255, 255] + [0] * 762)
    palette.save(path, transparency=0)
    info = probe_image(str(path))
    assert image_mod.NOTICE_TRANSPARENCY_KEPT in info.notices


def test_transparent_pixels_do_not_count_towards_capacity(rgba_carrier: Path) -> None:
    info = probe_image(str(rgba_carrier))
    assert info.capacity.usable_bits == 3 * (160 * 120 - 20 * 40)


def test_gray_with_alpha_counts_transparent_pixels(tmp_path: Path) -> None:
    path = tmp_path / "gray_alpha.png"
    data = np.dstack(
        [np.full((20, 20), 128, dtype=np.uint8), np.full((20, 20), 255, dtype=np.uint8)]
    )
    data[0:5, 0:5, 1] = 0
    Image.fromarray(data, "LA").save(path)
    info = probe_image(str(path))
    assert info.capacity.usable_bits == 3 * (400 - 25)


def test_prepare_converts_palette_without_transparency_to_rgb(tmp_path: Path) -> None:
    path = tmp_path / "opaque_palette.png"
    palette = Image.new("P", (30, 30), 0)
    palette.putpalette([0, 0, 0, 255, 255, 255, 9, 9, 9] + [0] * 759)
    palette.save(path)
    prepared = prepare_image(str(path))
    assert prepared.mode == "RGB"
    assert prepared.pixels.shape == (30, 30, 3)


def test_prepare_converts_palette_with_transparency_to_rgba(tmp_path: Path) -> None:
    path = tmp_path / "alpha_palette.png"
    palette = Image.new("P", (30, 30), 0)
    palette.putpalette([0, 0, 0, 9, 9, 9] + [0] * 762)
    palette.save(path, transparency=0)
    prepared = prepare_image(str(path))
    assert prepared.mode == "RGBA"
    assert prepared.pixels.shape == (30, 30, 4)


def test_prepare_keeps_icc_and_dpi(tmp_path: Path) -> None:
    source = tmp_path / "profile.png"
    rgb = np.zeros((40, 40, 3), dtype=np.uint8)
    rgb[:, :, 0] = 200
    Image.fromarray(rgb, "RGB").save(source, icc_profile=b"fake-icc", dpi=(144, 144))
    prepared = prepare_image(str(source))
    assert prepared.icc_profile == b"fake-icc"
    assert prepared.dpi is not None

    target = tmp_path / "out.png"
    save_png(prepared, str(target))
    with Image.open(target) as saved:
        assert saved.info.get("icc_profile") == b"fake-icc"
        dpi = saved.info.get("dpi")
    assert dpi is not None and round(dpi[0]) in (143, 144)


def test_prepare_rejects_garbage(tmp_path: Path) -> None:
    path = _write(tmp_path, PNG_SIG + b"garbage" * 8)
    with pytest.raises(UnsupportedCarrier):
        prepare_image(str(path))


def test_image_capacity_matches_probe(tmp_path: Path) -> None:
    path = tmp_path / "flat.png"
    Image.new("RGB", (50, 50), (1, 2, 3)).save(path)
    assert image_capacity(str(path)) == probe_image(str(path)).capacity


def test_save_png_forces_the_extension(tmp_path: Path) -> None:
    path = tmp_path / "flat.png"
    Image.new("RGB", (50, 50), (1, 2, 3)).save(path)
    prepared = prepare_image(str(path))
    saved = save_png(prepared, str(tmp_path / "out.bmp"))
    assert saved.endswith(".png")
    with Image.open(saved) as reopened:
        assert reopened.format == "PNG"


# ---------------------------------------------------------------------------
# pixel helpers and legacy fallbacks
# ---------------------------------------------------------------------------
def test_extract_payload_from_pixels_helper(rgba_carrier: Path, password: str) -> None:
    from phantom.core.payload import Payload

    prepared = prepare_image(str(rgba_carrier))
    from phantom.stego.engine import embed_payload

    space = prepared.sample_space()
    embed_payload(space, __import__("phantom.core.payload", fromlist=["pack_payload"]).pack_payload(
        Payload.text("pixels")
    ).blob, password)
    extracted = extract_payload_from_pixels(prepared.pixels, password)
    assert extracted.data == b"pixels"


def test_extract_from_clean_pixels_falls_back_to_legacy(rgb_carrier: Path, password: str) -> None:
    prepared = prepare_image(str(rgb_carrier))
    with pytest.raises(WrongPasswordOrNoData):
        extract_payload_from_pixels(prepared.pixels, password, allow_legacy=False)
    with pytest.raises(WrongPasswordOrNoData):
        extract_payload_from_pixels(prepared.pixels, password, allow_legacy=True)


def test_legacy_fixture_extracts_through_pixels(legacy_dir: Path) -> None:
    import json

    manifest = json.loads((legacy_dir / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["text"]
    secret_password = manifest["password"]
    prepared = prepare_image(str(legacy_dir / entry["stego"]))
    extracted = extract_payload_from_pixels(prepared.pixels, secret_password)
    assert extracted.data.decode("utf-8") == entry["secret_utf8"]
    with pytest.raises(WrongPasswordOrNoData):
        extract_payload_from_pixels(prepared.pixels, secret_password, allow_legacy=False)


def test_legacy_rgba_pixels_use_rgb_channels(legacy_dir: Path) -> None:
    import json

    manifest = json.loads((legacy_dir / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["text"]
    prepared = prepare_image(str(legacy_dir / entry["stego"]))
    rgb = np.ascontiguousarray(prepared.pixels[:, :, :3])
    extracted = extract_payload_from_pixels(rgb, manifest["password"])
    assert extracted.data.decode("utf-8") == entry["secret_utf8"]


def test_api_extract_disables_legacy(legacy_dir: Path) -> None:
    import json

    manifest = json.loads((legacy_dir / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["text"]
    stego = str(legacy_dir / entry["stego"])
    assert api.extract_from_path(stego, manifest["password"], allow_legacy=True) \
        .payload.data.decode("utf-8") == entry["secret_utf8"]
    with pytest.raises(WrongPasswordOrNoData):
        api.extract_from_path(stego, manifest["password"], allow_legacy=False)


def test_capacity_formula_reserves_the_tag(tmp_path: Path) -> None:
    path = tmp_path / "exact.png"
    Image.new("RGB", (20, 20), (0, 0, 0)).save(path)
    info = probe_image(str(path))
    expected = (info.capacity.usable_bits - C.HEADER_BITS) // 8 - C.TAG_SIZE
    assert info.capacity.payload_bytes == expected
    assert info.capacity.max_ciphertext_bytes == expected + C.TAG_SIZE


def test_image_info_usable_bits(rgb_carrier: Path) -> None:
    info = probe_image(str(rgb_carrier))
    assert info.usable_bits == info.capacity.usable_bits


def test_prepared_image_pixels_are_writeable(rgb_carrier: Path) -> None:
    prepared = prepare_image(str(rgb_carrier))
    assert prepared.pixels.flags.writeable is True
    assert prepared.source_format == "PNG"
    assert prepared.sample_space().size == prepared.width * prepared.height * 3


def test_save_png_without_dpi_or_icc(tmp_path: Path) -> None:
    path = tmp_path / "plain.png"
    Image.new("RGB", (20, 20), (5, 5, 5)).save(path)
    prepared = prepare_image(str(path))
    assert prepared.icc_profile is None
    saved = save_png(prepared, str(tmp_path / "plain_out.png"))
    assert Path(saved).exists()


def test_extract_payload_from_four_channel_pixels(rgba_carrier: Path, password: str) -> None:
    prepared = prepare_image(str(rgba_carrier))
    with pytest.raises(WrongPasswordOrNoData):
        extract_payload_from_pixels(prepared.pixels, password, allow_legacy=True)


def test_legacy_helpers_validate_input() -> None:
    from phantom.stego.legacy import decode_legacy_blob, legacy_bit_stream

    with pytest.raises(WrongPasswordOrNoData):
        decode_legacy_blob(b"short", "password-123")
    with pytest.raises(WrongPasswordOrNoData):
        legacy_bit_stream(np.zeros((4, 4), dtype=np.uint8))
    with pytest.raises(WrongPasswordOrNoData):
        from phantom.stego.legacy import extract_legacy_from_pixels

        extract_legacy_from_pixels(np.zeros((2, 2, 3), dtype=np.uint8), "password-123")
