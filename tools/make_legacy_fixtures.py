"""Generate golden fixtures for the legacy v1 format.

MUST be run against the ORIGINAL (pre-refactor) core.py at the repository
root, because fixtures encode the historical behaviour byte-for-byte.

Usage:  python tools/make_legacy_fixtures.py tests/fixtures/legacy_v1
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import core  # noqa: E402  (original v1 implementation)

CARRIER_W, CARRIER_H = 160, 120
PASSWORD = "correct horse battery staple"
SECRET_TEXT = (
    "PHANTOM legacy v1 fixture → текст українською, русский текст, "
    "emoji 🔐🖼️ and ASCII. Second line."
)


def make_carrier(path: str) -> None:
    rng = np.random.default_rng(20260101)
    # Smooth-ish photographic structure plus noise: deterministic, no assets.
    yy, xx = np.mgrid[0:CARRIER_H, 0:CARRIER_W]
    base = (
        (np.sin(xx / 7.0) * 40 + np.cos(yy / 11.0) * 35 + 128)
        .round()
        .astype(np.int16)
    )
    rgb = np.stack(
        [
            np.clip(base + rng.integers(-12, 13, (CARRIER_H, CARRIER_W)), 0, 255),
            np.clip(base + 20 + rng.integers(-12, 13, (CARRIER_H, CARRIER_W)), 0, 255),
            np.clip(base - 15 + rng.integers(-12, 13, (CARRIER_H, CARRIER_W)), 0, 255),
        ],
        axis=-1,
    ).astype(np.uint8)
    Image.fromarray(rgb, "RGB").save(path, format="PNG", optimize=False, compress_level=6)


def main(outdir: str) -> int:
    os.makedirs(outdir, exist_ok=True)
    carrier = os.path.join(outdir, "carrier.png")
    make_carrier(carrier)

    text_stego = os.path.join(outdir, "stego_text.png")
    payload = core.encrypt_message(SECRET_TEXT, PASSWORD)
    core._embed_blob(carrier, text_stego, payload)

    secret_file = os.path.join(outdir, "secret.bin")
    blob = bytes(range(256)) * 5 + b"PHANTOM-FILE-PAYLOAD"
    with open(secret_file, "wb") as fh:
        fh.write(blob)

    file_stego = os.path.join(outdir, "stego_file.png")
    payload_file = core.encrypt_file(secret_file, PASSWORD)
    core._embed_blob(carrier, file_stego, payload_file)

    expected_text = core.extract_data_from_image(text_stego, PASSWORD)
    expected_file = core.extract_raw_from_image(file_stego, PASSWORD)
    assert expected_text == SECRET_TEXT
    assert expected_file.data == blob
    assert expected_file.filename == "secret.bin"
    assert expected_file.integrity_ok

    meta = {
        "format": "legacy-v1",
        "generator": "tools/make_legacy_fixtures.py @ commit 4920c65",
        "password": PASSWORD,
        "password_variants_expected": ["raw-utf8"],
        "carrier": "carrier.png",
        "carrier_size": [CARRIER_W, CARRIER_H],
        "kdf": "pbkdf2-hmac-sha256",
        "kdf_iterations": 200000,
        "salt_size": 16,
        "nonce_size": 12,
        "cipher": "AES-256-GCM",
        "header_bits_plaintext_length": 32,
        "embedding": "sequential LSB matching (+-1), RGB channels, row-major",
        "text": {
            "stego": "stego_text.png",
            "secret_utf8": SECRET_TEXT,
            "secret_sha256": hashlib.sha256(SECRET_TEXT.encode()).hexdigest(),
            "blob_size": len(payload),
            "stego_sha256": hashlib.sha256(open(text_stego, "rb").read()).hexdigest(),
        },
        "file": {
            "stego": "stego_file.png",
            "name": "secret.bin",
            "secret_sha256": hashlib.sha256(blob).hexdigest(),
            "blob_size": len(payload_file),
            "stego_sha256": hashlib.sha256(open(file_stego, "rb").read()).hexdigest(),
        },
        "carrier_sha256": hashlib.sha256(open(carrier, "rb").read()).hexdigest(),
    }
    with open(os.path.join(outdir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    print(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/legacy_v1"))
