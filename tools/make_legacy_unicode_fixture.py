"""Add a unicode-password legacy v1 fixture using the ORIGINAL core.py.

The original implementation is read straight out of git (commit 4920c65), so the
fixture cannot drift with the refactored code.

Usage: python tools/make_legacy_unicode_fixture.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unicodedata

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "legacy_v1")
ORIGINAL_COMMIT = "4920c65b416453fdf5381f49a58224d7131f922f"
PASSWORD_NFC = unicodedata.normalize("NFC", "Пароль-ї-Ω-тест-2026")
SECRET = "Legacy v1 with a non-ASCII password — кирилиця + Ω"


def load_original_core():
    source = subprocess.run(
        ["git", "show", f"{ORIGINAL_COMMIT}:core.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "core_v1.py")
        with open(path, "wb") as handle:
            handle.write(source)
        spec = importlib.util.spec_from_file_location("core_v1", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot import the original core.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def main() -> int:
    core = load_original_core()
    carrier = os.path.join(FIXTURES, "carrier.png")
    stego = os.path.join(FIXTURES, "stego_text_unicode.png")
    payload = core.encrypt_message(SECRET, PASSWORD_NFC)
    core._embed_blob(carrier, stego, payload)
    assert core.extract_data_from_image(stego, PASSWORD_NFC) == SECRET

    manifest_path = os.path.join(FIXTURES, "manifest.json")
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    manifest["unicode_text"] = {
        "stego": "stego_text_unicode.png",
        "password_nfc": PASSWORD_NFC,
        "password_nfd": unicodedata.normalize("NFD", PASSWORD_NFC),
        "secret_utf8": SECRET,
        "secret_sha256": hashlib.sha256(SECRET.encode()).hexdigest(),
        "blob_size": len(payload),
        "stego_sha256": hashlib.sha256(open(stego, "rb").read()).hexdigest(),
        "note": "password stored as NFC bytes by the original implementation",
    }
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(manifest["unicode_text"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
