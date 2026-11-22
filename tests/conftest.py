"""Shared fixtures: deterministic carriers, audio builders, temp settings."""

from __future__ import annotations

import struct
import sys
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PASSWORD = "correct horse battery staple"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _texture(width: int, height: int, *, seed: int = 1234) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:height, 0:width]
    base = (np.sin(xx / 6.0) * 40 + np.cos(yy / 9.0) * 35 + 128).astype(np.int16)
    channels = [
        np.clip(base + rng.integers(-10, 11, base.shape), 0, 255),
        np.clip(base + 18 + rng.integers(-10, 11, base.shape), 0, 255),
        np.clip(base - 14 + rng.integers(-10, 11, base.shape), 0, 255),
    ]
    return np.stack(channels, axis=-1).astype(np.uint8)


@pytest.fixture
def password() -> str:
    return PASSWORD


@pytest.fixture
def rgb_carrier(tmp_path: Path) -> Path:
    path = tmp_path / "carrier.png"
    Image.fromarray(_texture(160, 120), "RGB").save(path, format="PNG", compress_level=1)
    return path


@pytest.fixture
def rgba_carrier(tmp_path: Path) -> Path:
    pixels = _texture(160, 120, seed=99)
    alpha = np.full((120, 160), 255, dtype=np.uint8)
    alpha[0:20, 0:40] = 0  # fully transparent block: must never be used
    rgba = np.dstack([pixels, alpha])
    path = tmp_path / "carrier_rgba.png"
    Image.fromarray(rgba, "RGBA").save(path, format="PNG", compress_level=1)
    return path


@pytest.fixture
def jpg_carrier(tmp_path: Path) -> Path:
    """A baseline-JPEG photo (not progressive) that the embed path converts to PNG."""
    path = tmp_path / "photo.jpg"
    Image.fromarray(_texture(160, 120), "RGB").save(path, format="JPEG", quality=90)
    return path


@pytest.fixture(scope="session")
def legacy_dir() -> Path:
    return FIXTURES / "legacy_v1"


def write_wav(
    path: Path,
    *,
    bits: int,
    channels: int,
    frames: int,
    kind: str = "noise",
    sample_rate: int = 48_000,
    seed: int = 5,
) -> np.ndarray:
    """Write a PCM WAV with an extra LIST chunk; returns the native samples."""
    if kind in ("noise", "long", "short"):
        peak = (1 << (bits - 1)) - 1
        rng = np.random.default_rng(seed)
        data = rng.integers(-peak // 2, peak // 2, (frames, channels), dtype=np.int64)
    elif kind == "silence":
        data = np.zeros((frames, channels), dtype=np.int64)
    elif kind == "fullscale":
        peak = (1 << (bits - 1)) - 1
        pattern = np.array([peak, -(peak + 1), 0, peak - 1], dtype=np.int64)
        data = np.resize(pattern, (frames, channels))
    else:
        raise ValueError(kind)
    if bits == 8:
        encoded = (data + 128).astype(np.uint8).tobytes()
    elif bits == 16:
        encoded = data.astype("<i2").tobytes()
    elif bits == 24:
        unsigned = (data & 0xFFFFFF).astype(np.uint32).reshape(-1)
        out = np.empty((unsigned.size, 3), dtype=np.uint8)
        out[:, 0] = (unsigned & 0xFF).astype(np.uint8)
        out[:, 1] = ((unsigned >> 8) & 0xFF).astype(np.uint8)
        out[:, 2] = ((unsigned >> 16) & 0xFF).astype(np.uint8)
        encoded = out.tobytes()
    elif bits == 32:
        encoded = data.astype("<i4").tobytes()
    else:
        raise ValueError(bits)
    block_align = channels * bits // 8
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, sample_rate * block_align, block_align, bits)
    list_payload = b"INFO" + b"ISFT" + struct.pack("<I", 4) + b"test"
    list_chunk = b"LIST" + struct.pack("<I", len(list_payload)) + list_payload
    data_chunk = b"data" + struct.pack("<I", len(encoded)) + encoded
    if len(encoded) % 2:
        data_chunk += b"\x00"
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + list_chunk + data_chunk
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    return data


@pytest.fixture
def wav_factory(tmp_path: Path) -> Callable[..., Path]:
    counter = {"n": 0}

    def factory(**kwargs: object) -> Path:
        counter["n"] += 1
        bits = int(kwargs.pop("bits", 16))
        path = tmp_path / f"audio_{counter['n']}_{bits}.wav"
        write_wav(path, bits=bits, **kwargs)  # type: ignore[arg-type]
        return path

    return factory


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    monkeypatch.setenv("PHANTOM_CONFIG_DIR", str(config))
    return config


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Name the non-daemon threads that outlived the suite, with their stacks.

    A wedged GUI job keeps its worker thread alive; the interpreter then waits for
    that thread at shutdown and CI burns the whole step limit with nothing to show
    for it.  The stack printed here (and left in ``shutdown_stacks.txt``) says which
    frame the thread is stuck in, which the process cannot report once it is
    waiting to die.
    """
    import threading
    import traceback

    main = threading.main_thread()
    stuck = [t for t in threading.enumerate() if t is not main and not t.daemon]
    if not stuck:
        return
    stacks = sys._current_frames()
    lines = [f"=== {len(stuck)} non-daemon thread(s) survived the run (exit {exitstatus}) ==="]
    for thread in stuck:
        lines.append(f"--- thread {thread.name!r} ident={thread.ident}")
        frame = stacks.get(thread.ident or -1)
        if frame is not None:
            lines.extend("".join(traceback.format_stack(frame)).splitlines()[-12:])
    text = "\n".join(lines)
    print(text, flush=True)
    Path("shutdown_stacks.txt").write_text(text, encoding="utf-8")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: performance tests")
    config.addinivalue_line("markers", "gui: requires a Tk display")
    # A native crash (Tk's Aqua layer on macOS, for one) kills the interpreter
    # before pytest can say anything; faulthandler turns the SIGSEGV into the
    # Python stack that led to it, without every run having to remember
    # ``-X faulthandler``.  It is a reporter, not a handler: nothing is caught.
    import faulthandler

    faulthandler.enable(all_threads=True)
