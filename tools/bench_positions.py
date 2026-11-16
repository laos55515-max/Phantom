"""Measure what the v3 bootstrap key costs on realistic carriers.

Run with ``.venv/bin/python tools/bench_positions.py``.  Numbers are wall-clock
on the machine that runs it and are meant to answer one question: how much does
one extra PBKDF2-HMAC-SHA256 (600000 iterations) derivation add to embedding
and extracting, next to the Argon2id run the container already pays for.
"""

from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from phantom.core import api
from phantom.core import constants as C
from phantom.core.errors import WrongPasswordOrNoData
from phantom.core.kdf import derive_master_key, derive_position_key
from phantom.core.payload import Payload, pack_payload
from phantom.stego import engine
from phantom.stego.image import prepare_image
from phantom.stego.samples import (
    ArraySamples,
    SampleSpace,
)

PASSWORD = "correct horse battery staple"
#: ~12 KiB of text: the payload size the app actually deals with most of the time.
SECRET_TEXT = "benchmark payload line\n" * 560


def _median_seconds(fn: Callable[[], None], repeats: int = 3) -> float:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def bench_keys() -> None:
    """The two derivations every embed and extract pays for."""
    position_ms = _median_seconds(lambda: derive_position_key(PASSWORD), repeats=5) * 1000
    profile = C.default_kdf_profile()
    salt = b"\x01" * C.SALT_SIZE
    argon_ms = (
        _median_seconds(
            lambda: derive_master_key(PASSWORD, salt, profile, normalize=False), repeats=3
        )
        * 1000
    )
    print(f"derive_position_key (PBKDF2-600k, v3 only) {position_ms:7.1f} ms")
    print(f"derive_master_key ({profile.label})            {argon_ms:7.1f} ms")
    print(f"ratio: the new derivation costs {position_ms / argon_ms:.0%} of the existing one")


def _carrier(path: Path, width: int, height: int) -> None:
    rng = np.random.default_rng(width + height)
    pixels = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
    Image.fromarray(pixels, "RGB").save(path, format="PNG", compress_level=1)


def _extract(space: SampleSpace, password: str) -> None:
    engine.extract_payload(space, password)


def bench_engine(width: int, height: int, tmp: Path) -> None:
    path = tmp / f"carrier_{width}x{height}.png"
    _carrier(path, width, height)
    prepared = prepare_image(str(path))
    bits = int(prepared.sample_space().size)
    blob = pack_payload(Payload.text(SECRET_TEXT), max_payload_bytes=1 << 20).blob
    print(f"\n{width}x{height} = {width * height / 1e6:.1f} MP, {bits / 1e6:.1f} M usable bits")

    results: dict[str, float] = {}
    layouts = (
        (C.KEYED_CONTAINER_VERSION, "v3"),
        (C.STC_CONTAINER_VERSION, "v5"),
        (C.V2_CONTAINER_VERSION, "v2"),
    )
    for version, label in layouts:
        space = prepared.sample_space()
        results[f"embed {label}"] = _median_seconds(
            lambda space=space, version=version: engine.embed_payload(
                space, blob, PASSWORD, container_version=version
            )
        )
        extracted = engine.extract_payload(space, PASSWORD)
        assert extracted.data == SECRET_TEXT.encode(), "round trip failed"
        results[f"extract {label}"] = _median_seconds(lambda space=space: _extract(space, PASSWORD))
        wrong = PASSWORD + "!"
        results[f"wrong {label}"] = _median_seconds(
            lambda space=space, wrong=wrong: _wrong_password(space, wrong)
        )

    print(
        f"  embed   v3 {results['embed v3']:5.2f} s   v2 {results['embed v2']:5.2f} s"
        f"   delta +{(results['embed v3'] - results['embed v2']) * 1000:4.0f} ms"
    )
    print(
        f"  extract v3 {results['extract v3']:5.2f} s   v2 {results['extract v2']:5.2f} s"
        f"   (a v2 file pays the failed v3 attempt)"
    )
    print(
        f"  wrong password v3 {results['wrong v3']:5.2f} s   v2 {results['wrong v2']:5.2f} s"
        f"   (one extra KDF pass)"
    )

    space = ArraySamples(np.zeros(bits, dtype=np.int16))
    bootstrap_ms = _median_seconds(lambda: engine.bootstrap_for(space, PASSWORD)) * 1000
    legacy_ms = (
        _median_seconds(
            lambda: engine.legacy_permutation(space).apply(np.arange(C.HEADER_BITS))
        )
        * 1000
    )
    print(
        f"  layout only: bootstrap {bootstrap_ms:6.1f} ms   legacy {legacy_ms:5.1f} ms"
        f"   (the permutation itself is {legacy_ms:.1f} ms of that)"
    )


def _wrong_password(space: SampleSpace, password: str) -> None:
    try:
        engine.extract_payload(space, password)
    except WrongPasswordOrNoData:
        return
    raise AssertionError("a wrong password must not open the payload")


def bench_api(width: int, height: int, tmp: Path) -> None:
    carrier = tmp / f"api_{width}x{height}.png"
    _carrier(carrier, width, height)
    output = tmp / f"api_{width}x{height}_stego.png"
    started = time.perf_counter()
    api.embed_payload(
        str(carrier),
        str(output),
        api.Payload.file("secret.bin", SECRET_TEXT.encode()),
        password=PASSWORD,
    )
    embed_seconds = time.perf_counter() - started
    started = time.perf_counter()
    outcome = api.extract_from_path(str(output), PASSWORD)
    extract_seconds = time.perf_counter() - started
    assert outcome.payload.data == SECRET_TEXT.encode()
    print(
        f"  end to end (PNG read/write included): embed {embed_seconds:5.2f} s"
        f"   extract {extract_seconds:5.2f} s"
    )


def main() -> None:
    tmp = Path("/tmp/bench_positions")
    tmp.mkdir(parents=True, exist_ok=True)
    print(
        f"python {sys.version.split()[0]}  "
        f"default v{C.CONTAINER_VERSION}, keyed v{C.KEYED_CONTAINER_VERSION}"
    )
    bench_keys()
    bench_engine(3840, 2160, tmp)  # 4K UHD, 8.3 MP
    bench_engine(4000, 3000, tmp)  # 12 MP
    print("\n12 MP through the public API")
    bench_api(4000, 3000, tmp)


if __name__ == "__main__":
    main()
