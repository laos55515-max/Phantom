#!/usr/bin/env python3
"""Generate the built-in offline set of 10 PNG carriers in different sizes.

All images are procedurally generated from deterministic mathematical formulas
(no external assets, 100% original, MIT-licensed with the repository).
Run from the repository root::

    python tools/make_builtin_carriers.py
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "src" / "phantom" / "resources" / "carriers"

Rgb = tuple[int, int, int]
CarrierSpec = tuple[str, int, int, Rgb, Rgb, Rgb, float]

# 10 carriers spanning ~28 KB to ~2.4 MB of stego capacity.
SPECS: list[CarrierSpec] = [
    ("01_horizon_320x240.png", 320, 240, (24, 38, 66), (72, 118, 168), (224, 178, 132), 1.0),
    ("02_ember_400x300.png", 400, 300, (48, 22, 34), (168, 72, 54), (242, 186, 112), 1.4),
    ("03_fjord_640x480.png", 640, 480, (18, 42, 52), (48, 112, 128), (182, 214, 218), 1.8),
    ("04_aurora_800x600.png", 800, 600, (14, 24, 44), (36, 138, 122), (146, 226, 188), 2.2),
    ("05_dunes_1024x768.png", 1024, 768, (64, 44, 32), (176, 128, 84), (236, 212, 168), 2.6),
    ("06_canopy_1280x720.png", 1280, 720, (18, 44, 28), (52, 114, 68), (164, 206, 148), 3.0),
    ("07_glacier_1600x900.png", 1600, 900, (28, 48, 72), (92, 148, 192), (218, 236, 246), 3.4),
    ("08_nebula_1920x1080.png", 1920, 1080, (22, 16, 46), (98, 52, 134), (208, 148, 214), 3.8),
    ("09_canyon_2400x1600.png", 2400, 1600, (56, 28, 24), (156, 82, 56), (228, 174, 128), 4.2),
    ("10_alpine_3200x2000.png", 3200, 2000, (20, 32, 48), (74, 104, 136), (210, 224, 236), 4.6),
]


def render_carrier(
    width: int,
    height: int,
    *,
    c0: Rgb,
    c1: Rgb,
    c2: Rgb,
    phase: float,
) -> Image.Image:
    y = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    x = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]

    # Smooth layered wave field — compresses very cleanly in PNG while looking natural.
    wave = (
        0.08 * np.sin(2.0 * np.pi * (x * 1.5 + phase * 0.3))
        + 0.05 * np.cos(2.0 * np.pi * (x * 3.0 - y * 1.2 + phase))
    )
    t = np.clip(y + wave, 0.0, 1.0)

    c0_arr = np.array(c0, dtype=np.float32)
    c1_arr = np.array(c1, dtype=np.float32)
    c2_arr = np.array(c2, dtype=np.float32)

    low = np.clip(t * 2.0, 0.0, 1.0)[..., None]
    high = np.clip((t - 0.5) * 2.0, 0.0, 1.0)[..., None]
    rgb = np.where(
        t[..., None] < 0.5,
        c0_arr * (1.0 - low) + c1_arr * low,
        c1_arr * (1.0 - high) + c2_arr * high,
    )

    # Subtle radial glow for visual depth.
    cx = 0.25 + 0.5 * ((phase * 0.37) % 1.0)
    cy = 0.28 + 0.3 * ((phase * 0.61) % 1.0)
    dist2 = (x - cx) ** 2 + (y - cy) ** 2
    glow = np.exp(-dist2 * 6.0)[..., None] * 18.0
    rgb = np.clip(rgb + glow, 0.0, 255.0).astype(np.uint8)
    return Image.fromarray(rgb, mode="RGB")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    for name, width, height, c0, c1, c2, phase in SPECS:
        path = OUT_DIR / name
        img = render_carrier(width, height, c0=c0, c1=c1, c2=c2, phase=phase)
        img.save(path, format="PNG", optimize=True)
        size = path.stat().st_size
        total_bytes += size
        print(f"{name}: {width}x{height} ({size:,} bytes)")
    print(f"Total: {len(SPECS)} files, {total_bytes:,} bytes ({total_bytes / 1024:.1f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
