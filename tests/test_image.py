"""Image carriers: modes, capacity, spatial spread, atomic output, policy (D9)."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from phantom.core import api
from phantom.core import constants as C
from phantom.core.errors import (
    CapacityError,
    IntegrityError,
    OutputIsCarrierError,
    UnsupportedCarrier,
    WrongPasswordOrNoData,
)
from phantom.core.payload import Payload
from phantom.stego.engine import capacity_for, embed_payload, extract_payload
from phantom.stego.image import (
    NOTICE_GRAYSCALE,
    NOTICE_PALETTE,
    enforce_png_extension,
    probe_image,
)
from phantom.stego.samples import ImageSamples


def _texture(width: int, height: int, *, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    base = (np.sin(xx / 5.0) * 40 + np.cos(yy / 7.0) * 35 + 128).astype(np.int16)
    return np.stack(
        [
            np.clip(base + rng.integers(-8, 9, base.shape), 0, 255),
            np.clip(base + 20 + rng.integers(-8, 9, base.shape), 0, 255),
            np.clip(base - 10 + rng.integers(-8, 9, base.shape), 0, 255),
        ],
        axis=-1,
    ).astype(np.uint8)


@pytest.fixture
def plain_carrier(tmp_path: Path) -> Path:
    path = tmp_path / "plain.png"
    Image.fromarray(_texture(200, 150), "RGB").save(path, compress_level=1)
    return path


def test_rgb_roundtrip_api(tmp_path: Path, plain_carrier: Path, password: str) -> None:
    output = tmp_path / "out.png"
    outcome = api.embed_payload(
        str(plain_carrier), str(output), Payload.text("image roundtrip"), password=password
    )
    assert outcome.verified
    assert Path(outcome.output_path).exists()
    result = api.extract_from_path(outcome.output_path, password)
    assert result.payload.data == b"image roundtrip"
    assert result.payload.container_version == C.CONTAINER_VERSION


def test_output_extension_is_forced_to_png(tmp_path: Path, plain_carrier: Path, password: str) -> None:
    outcome = api.embed_payload(
        str(plain_carrier),
        str(tmp_path / "photo.jpg"),
        Payload.text("extension"),
        password=password,
    )
    assert outcome.output_path.endswith(".png")
    assert Path(outcome.output_path).read_bytes().startswith(b"\x89PNG")


def test_enforce_png_extension_helper() -> None:
    assert enforce_png_extension("a/b.png") == "a/b.png"
    assert enforce_png_extension("a/b.PNG") == "a/b.PNG"
    assert enforce_png_extension("a/b.jpg") == "a/b.png"
    assert enforce_png_extension("a/b") == "a/b.png"


def test_output_cannot_overwrite_carrier(tmp_path: Path, plain_carrier: Path, password: str) -> None:
    with pytest.raises(OutputIsCarrierError):
        api.embed_payload(
            str(plain_carrier), str(plain_carrier), Payload.text("x"), password=password
        )
    assert plain_carrier.read_bytes().startswith(b"\x89PNG")


def test_alpha_and_transparent_rgb_are_untouched(tmp_path: Path, rgba_carrier: Path, password: str) -> None:
    original = np.array(Image.open(rgba_carrier), dtype=np.uint8)
    output = tmp_path / "rgba_out.png"
    api.embed_payload(str(rgba_carrier), str(output), Payload.text("rgba"), password=password)
    updated = np.array(Image.open(output), dtype=np.uint8)

    np.testing.assert_array_equal(original[:, :, 3], updated[:, :, 3])
    transparent = original[:, :, 3] == 0
    assert transparent.any()
    np.testing.assert_array_equal(original[transparent][:, :3], updated[transparent][:, :3])
    assert np.abs(original[:, :, :3].astype(int) - updated[:, :, :3].astype(int)).max() <= 1
    assert api.extract_from_path(str(output), password).payload.data == b"rgba"


def test_delta_never_exceeds_one(plain_carrier: Path, password: str) -> None:
    original = np.array(Image.open(plain_carrier), dtype=np.uint8)
    space = ImageSamples(original.copy())
    payload = os.urandom(900)
    embed_payload(space, payload, password)
    delta = np.abs(space._flat.reshape(original.shape).astype(int) - original.astype(int))
    assert delta.max() <= 1


def test_payload_is_spread_over_the_whole_frame(tmp_path: Path, password: str) -> None:
    """D3 regression: sequential embedding touched only the top ~22% of a frame."""
    width, height = 600, 600
    path = tmp_path / "spread.png"
    Image.fromarray(_texture(width, height, seed=3), "RGB").save(path, compress_level=1)
    output = tmp_path / "spread_out.png"
    payload = os.urandom(30 * 1024)
    api.embed_payload(
        str(path), str(output), Payload.file("blob.bin", payload), password=password
    )
    original = np.array(Image.open(path), dtype=np.int64)
    updated = np.array(Image.open(output), dtype=np.int64)
    changed_rows = np.where((original != updated).any(axis=(1, 2)))[0]
    deciles = np.array_split(np.arange(height), 10)
    covered = sum(1 for block in deciles if np.intersect1d(changed_rows, block).size > 0)
    assert covered == 10, f"payload only reached {covered}/10 horizontal deciles"
    top_share = np.isin(changed_rows, deciles[0]).sum() / changed_rows.size
    assert top_share < 0.2, "changes cluster in the first decile (sequential embedding)"


def test_capacity_boundary_exact_and_plus_one(plain_carrier: Path, password: str) -> None:
    pixels = np.array(Image.open(plain_carrier), dtype=np.uint8)
    space = ImageSamples(pixels)
    capacity = capacity_for(space)
    exact = os.urandom(capacity.payload_bytes)
    embed_payload(space, exact, password)
    with pytest.raises(CapacityError):
        embed_payload(
            ImageSamples(np.array(Image.open(plain_carrier), dtype=np.uint8)),
            os.urandom(capacity.payload_bytes + 1),
            password,
        )


def test_capacity_random_payload_still_extracts(plain_carrier: Path, password: str) -> None:
    pixels = np.array(Image.open(plain_carrier), dtype=np.uint8)
    packed = Payload.file("data.bin", os.urandom(1_500))
    from phantom.core.payload import pack_payload

    blob = pack_payload(packed).blob
    space = ImageSamples(pixels)
    embed_payload(space, blob, password)
    result = extract_payload(space, password)
    assert result.data == packed.data
    assert result.name == "data.bin"


def test_gray_png_is_converted_with_a_notice(tmp_path: Path, password: str) -> None:
    gray = _texture(120, 90, seed=11)[:, :, 0]
    path = tmp_path / "gray.png"
    Image.fromarray(gray, "L").save(path, compress_level=1)
    info = probe_image(str(path))
    assert info.mode == "L"
    assert NOTICE_GRAYSCALE in info.notices
    output = tmp_path / "gray_out.png"
    outcome = api.embed_payload(str(path), str(output), Payload.text("gray"), password=password)
    assert NOTICE_GRAYSCALE in outcome.notices
    assert api.extract_from_path(str(output), password).payload.data == b"gray"


def test_palette_png_keeps_transparency(tmp_path: Path, password: str) -> None:
    rgba = np.dstack([_texture(64, 64, seed=5), np.full((64, 64), 255, dtype=np.uint8)])
    rgba[0:8, 0:8, 3] = 0
    image = Image.fromarray(rgba, "RGBA").convert("P", palette=Image.Palette.ADAPTIVE, colors=32)
    image.info["transparency"] = 0
    path = tmp_path / "palette.png"
    image.save(path, transparency=0)
    info = probe_image(str(path))
    assert info.mode == "P"
    assert NOTICE_PALETTE in info.notices
    output = tmp_path / "palette_out.png"
    api.embed_payload(str(path), str(output), Payload.text("palette"), password=password)
    with Image.open(output) as saved:
        assert saved.mode in ("RGB", "RGBA")
    assert api.extract_from_path(str(output), password).payload.data == b"palette"


def test_sixteen_bit_png_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "deep.png"
    image = Image.new("I;16", (64, 64))
    image.putdata([(index * 37) % 65536 for index in range(64 * 64)])
    image.save(path)
    with pytest.raises(UnsupportedCarrier):
        probe_image(str(path))


def test_multiframe_image_is_refused(tmp_path: Path) -> None:
    frames = [Image.fromarray(_texture(48, 48, seed=index), "RGB") for index in range(3)]
    path = tmp_path / "anim.png"
    frames[0].save(path, save_all=True, append_images=frames[1:], format="PNG")
    with pytest.raises(UnsupportedCarrier):
        probe_image(str(path))


def test_non_png_image_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "photo.jpg"
    Image.fromarray(_texture(64, 64), "RGB").save(path, format="JPEG")
    with pytest.raises((UnsupportedCarrier, api.UnsupportedCarrier if hasattr(api, "UnsupportedCarrier") else UnsupportedCarrier)):
        probe_image(str(path))


def test_icc_profile_is_preserved(tmp_path: Path, password: str) -> None:
    from PIL import ImageCms

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    path = tmp_path / "icc.png"
    Image.fromarray(_texture(80, 60), "RGB").save(path, icc_profile=profile, compress_level=1)
    output = tmp_path / "icc_out.png"
    api.embed_payload(str(path), str(output), Payload.text("icc"), password=password)
    with Image.open(output) as saved:
        assert saved.info.get("icc_profile") == profile
        assert "exif" not in saved.info


def test_damaged_stego_image_is_rejected(tmp_path: Path, plain_carrier: Path, password: str) -> None:
    output = tmp_path / "damage_src.png"
    api.embed_payload(
        str(plain_carrier), str(output), Payload.text("fragile"), password=password
    )
    pixels = np.array(Image.open(output), dtype=np.uint8)
    rng = np.random.default_rng(0)
    mask = rng.random(pixels.shape[:2]) < 0.4
    pixels[mask] = np.clip(
        pixels[mask].astype(int) + rng.integers(-3, 4, pixels[mask].shape), 0, 255
    ).astype(np.uint8)
    damaged = tmp_path / "damaged.png"
    Image.fromarray(pixels, "RGB").save(damaged, compress_level=1)
    with pytest.raises((WrongPasswordOrNoData, IntegrityError)):
        api.extract_from_path(str(damaged), password)


def test_probe_reports_capacity(plain_carrier: Path) -> None:
    info = probe_image(str(plain_carrier))
    assert info.width == 200 and info.height == 150
    assert info.capacity.usable_bits == 200 * 150 * 3
    assert info.capacity.payload_bytes == (info.capacity.usable_bits - C.HEADER_BITS) // 8 - C.TAG_SIZE


def test_tiny_image_has_no_capacity(tmp_path: Path) -> None:
    path = tmp_path / "tiny.png"
    Image.fromarray(np.zeros((4, 4, 3), dtype=np.uint8), "RGB").save(path)
    capacity = probe_image(str(path)).capacity
    assert capacity.payload_bytes == 0


def test_extract_from_plain_carrier_fails_cleanly(plain_carrier: Path, password: str) -> None:
    with pytest.raises(WrongPasswordOrNoData):
        api.extract_from_path(str(plain_carrier), password)


def test_missing_carrier_reports_io_error(tmp_path: Path, password: str) -> None:
    from phantom.core.errors import PhantomIOError

    with pytest.raises(PhantomIOError):
        api.embed_payload(
            str(tmp_path / "nope.png"), str(tmp_path / "out.png"), Payload.text("x"), password=password
        )


def test_carrier_file_is_not_modified(tmp_path: Path, plain_carrier: Path, password: str) -> None:
    before = plain_carrier.read_bytes()
    api.embed_payload(
        str(plain_carrier), str(tmp_path / "out.png"), Payload.text("keep"), password=password
    )
    assert plain_carrier.read_bytes() == before
