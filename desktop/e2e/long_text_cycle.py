#!/usr/bin/env python3
"""Round-trip 10,000 Cyrillic words through the packaged bridge, preserving UTF-8."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

parser = argparse.ArgumentParser()
parser.add_argument("--bridge", required=True)
args = parser.parse_args()


def call(request):
    proc = subprocess.run(
        [args.bridge],
        input=(json.dumps(request, ensure_ascii=False) + "\n").encode(),
        capture_output=True,
        check=True,
        timeout=600,
    )
    result = json.loads(proc.stdout.decode().splitlines()[-1])
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error")))
    return result


text = "".join(f"слово{i} " if i % 25 != 24 else f"слово{i}\n\n" for i in range(10000))
password = "long text test password 9d2f41"  # noqa: S105
with tempfile.TemporaryDirectory(prefix="phantom-longtext-") as directory:
    base = Path(directory)
    carrier = base / "carrier.png"
    hidden = base / "hidden.png"
    output = base / "extracted.txt"
    Image.new("RGB", (1024, 1024), (125, 160, 200)).save(carrier)
    call(
        {
            "cmd": "hide",
            "carrier_path": str(carrier),
            "output_path": str(hidden),
            "password": password,
            "text": text,
        }
    )
    info = call(
        {"cmd": "extract", "carrier_path": str(hidden), "password": password, "mode": "info"}
    )
    assert info["kind"] == "text"
    assert info.get("text") == text
    call(
        {
            "cmd": "extract",
            "carrier_path": str(hidden),
            "password": password,
            "mode": "save_file",
            "output_path": str(output),
        }
    )
    assert output.read_bytes() == text.encode("utf-8")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    print(f"PASS: 10,000 Cyrillic words, paragraphs, preview and TXT bytes; SHA-256 {digest}")
