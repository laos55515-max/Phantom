#!/usr/bin/env python3
"""Packaged bridge: same JPEG in PNG and WAV, PNG payload, original bytes on save."""

import argparse
import base64
import io
import math
import struct
import tempfile
import wave
from pathlib import Path

from full_cycle import DEMO_PASSWORD, Bridge
from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument("--bridge", required=True)
args = parser.parse_args()
bridge = Bridge(Path(args.bridge))

with tempfile.TemporaryDirectory(prefix="phantom-image-e2e-") as directory:
    root = Path(directory)
    png_carrier = root / "carrier.png"
    wav_carrier = root / "carrier.wav"
    Image.new("RGB", (300, 300), (80, 120, 180)).save(png_carrier)
    with wave.open(str(wav_carrier), "wb") as audio:
        audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        audio.writeframes(
            b"".join(struct.pack("<h", int(16000 * math.sin(i * 0.05))) for i in range(240000))
        )
    jpg = root / "original.jpeg"
    png = root / "original.png"
    Image.new("RGB", (90, 40), (180, 30, 50)).save(jpg, "JPEG", quality=90)
    Image.new("RGBA", (36, 60), (10, 170, 20, 120)).save(png, "PNG")

    for carrier, payload, kind in (
        (png_carrier, jpg, "JPEG"),
        (wav_carrier, jpg, "JPEG"),
        (png_carrier, png, "PNG"),
    ):
        hidden = root / ("hidden" + carrier.suffix)
        recovered = root / ("saved-" + payload.name)
        hide = bridge.call(
            {
                "cmd": "hide",
                "carrier_path": str(carrier),
                "output_path": str(hidden),
                "secret_path": str(payload),
                "password": DEMO_PASSWORD,
            }
        )
        assert hide.get("ok"), hide
        request = {"cmd": "extract", "carrier_path": str(hidden), "password": DEMO_PASSWORD}
        info = bridge.call({**request, "mode": "info"})
        assert info.get("ok"), info
        preview = info["image_preview"]
        assert preview["type"] == kind
        assert preview["status"] == "available"
        thumbnail = base64.b64decode(preview["data"], validate=True)
        assert len(thumbnail) <= 1024 * 1024
        with Image.open(io.BytesIO(thumbnail)) as image:
            assert image.format == "JPEG"
            assert max(image.size) <= 1024
        save = bridge.call({**request, "mode": "save_file", "output_path": str(recovered)})
        assert save.get("ok"), save
        assert recovered.read_bytes() == payload.read_bytes()
        print(f"PASS: {kind} from {carrier.suffix} preview + original-byte save", flush=True)
