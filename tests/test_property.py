"""Property-based round trips: any payload that fits must survive exactly."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image

from phantom.core import api
from phantom.core import constants as C
from phantom.core.errors import PhantomError
from phantom.core.payload import Payload, estimate_size

FIXED_PASSWORD = "Property-passphrase-2026"

SETTINGS = settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)

PASSWORDS = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=0x4E2D),
    min_size=C.MIN_PASSWORD_LENGTH,
    max_size=40,
)
TEXTS = st.text(alphabet=st.characters(min_codepoint=1, max_codepoint=0x10FFFF), max_size=200)
SIZES = st.integers(min_value=64, max_value=320)
NAMES = st.text(
    alphabet=st.sampled_from("abcXYZ019._- "),
    min_size=1,
    max_size=24,
)


def _noise(width: int, height: int) -> Image.Image:
    rng = np.random.default_rng(width * 7 + height)
    data = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    return Image.fromarray(data, "RGB")


@SETTINGS
@given(width=SIZES, height=SIZES, text=TEXTS)
def test_text_roundtrip_is_exact(tmp_path_factory, width: int, height: int, text: str) -> None:
    password = FIXED_PASSWORD
    directory = Path(tmp_path_factory.mktemp("prop"))
    carrier = directory / f"{width}x{height}.png"
    _noise(width, height).save(carrier)
    payload = Payload.text(text)
    capacity = api.capacity_of(str(carrier))
    if not estimate_size(payload, capacity_bytes=capacity.user_bytes("")).fits(
        capacity.user_bytes("")
    ):
        return
    output = directory / "out.png"
    outcome = api.embed_payload(str(carrier), str(output), payload, password=password)
    assert outcome.verified
    result = api.extract_from_path(str(output), password)
    assert result.payload.data == payload.data
    assert result.payload.integrity_ok


@SETTINGS
@given(
    width=SIZES,
    height=SIZES,
    data=st.binary(min_size=1, max_size=200),
    name=NAMES,
)
def test_file_roundtrip_is_exact(
    tmp_path_factory,
    width: int,
    height: int,
    data: bytes,
    name: str,
) -> None:
    password = FIXED_PASSWORD
    directory = Path(tmp_path_factory.mktemp("prop"))
    carrier = directory / "carrier.png"
    _noise(width, height).save(carrier)
    secret = directory / "secret.bin"
    secret.write_bytes(data)
    payload = Payload.file(name, data)
    capacity = api.capacity_of(str(carrier)).user_bytes(name)
    if not estimate_size(payload, capacity_bytes=capacity).fits(capacity):
        return
    output = directory / "out.png"
    api.embed_payload(str(carrier), str(output), payload, password=password)
    result = api.extract_from_path(str(output), password)
    assert result.payload.data == data
    assert result.payload.name == name


@SETTINGS
@given(width=SIZES, height=SIZES, rng_seed=st.integers(0, 2**16))
def test_clean_carrier_never_reveals_data(
    tmp_path_factory, width: int, height: int, rng_seed: int, password: str
) -> None:
    directory = Path(tmp_path_factory.mktemp("prop"))
    carrier = directory / "clean.png"
    _noise(width, height).save(carrier)
    try:
        api.extract_from_path(str(carrier), password)
    except PhantomError:
        return
    raise AssertionError("a clean carrier must not yield a payload")


@SETTINGS
@given(data=st.binary(min_size=1, max_size=4000), password=PASSWORDS)
def test_garbage_input_only_raises_typed_errors(
    tmp_path_factory, data: bytes, password: str
) -> None:
    directory = Path(tmp_path_factory.mktemp("prop"))
    junk = directory / "junk.png"
    junk.write_bytes(b"\x89PNG\r\n\x1a\n" + data)
    try:
        api.extract_from_path(str(junk), password)
    except PhantomError:
        return
    raise AssertionError("garbage must not decode")


@SETTINGS
@given(
    frames=st.integers(min_value=2_000, max_value=3_000),
    channels=st.sampled_from([1, 2]),
    text=st.text(max_size=40),
)
def test_wav_roundtrip_is_exact(
    tmp_path_factory, wav_factory, frames: int, channels: int, text: str
) -> None:
    password = FIXED_PASSWORD
    directory = Path(tmp_path_factory.mktemp("prop"))
    carrier = wav_factory(bits=16, channels=channels, frames=frames, kind="noise")
    payload = Payload.text(text)
    capacity = api.capacity_of(str(carrier))
    if not estimate_size(payload, capacity_bytes=capacity.user_bytes("")).fits(
        capacity.user_bytes("")
    ):
        return
    output = directory / "out.wav"
    api.embed_payload(str(carrier), str(output), payload, password=password)
    assert api.extract_from_path(str(output), password).payload.data == payload.data


@SETTINGS
@given(wrong=PASSWORDS)
def test_wrong_password_is_always_rejected(tmp_path_factory, rgb_carrier, password: str, wrong: str):  # noqa: E501
    assume_different = wrong != password
    if not assume_different:
        return
    directory = Path(tmp_path_factory.mktemp("prop"))
    output = directory / "out.png"
    api.embed_payload(
        str(rgb_carrier), str(output), Payload.text("bound"), password=password
    )
    try:
        api.extract_from_path(str(output), wrong)
    except PhantomError:
        return
    raise AssertionError("a different password must not decrypt the payload")
