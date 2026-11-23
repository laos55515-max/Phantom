#!/usr/bin/env python3
"""End-to-end check of the stage scenario: a real PDF, in and out of a PNG.

Drives the frozen ``phantom-bridge`` sidecar over its stdio JSON protocol —
never through command-line arguments — performs:

1. ``probe`` of the carrier image;
2. ``hide`` of ``src/phantom/resources/example_payload.pdf`` into a copy of
   ``src/phantom/resources/example_carrier.png``;
3. ``extract`` (info) and ``extract`` (save_file);
4. a byte-for-byte SHA-256 comparison of the original and the extracted PDF;
5. a negative control: the wrong password must fail with
   ``wrong_password_or_no_data``.

Usage::

    python desktop/e2e/full_cycle.py --bridge dist/phantom-bridge --out report.json

Exit code 0 iff every check passes.  The report JSON is printed and written to
``--out`` when given.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CARRIER = REPO_ROOT / "src" / "phantom" / "resources" / "example_carrier.png"
EXAMPLE_PDF = REPO_ROOT / "src" / "phantom" / "resources" / "example_payload.pdf"
DEMO_PASSWORD = "desktop-e2e-demo-passphrase-7f3a9c1e"  # test container, not a real secret


class Bridge:
    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def call(self, request: dict, timeout: float = 600.0) -> dict:
        result = subprocess.run(
            [str(self.binary)],
            input=(json.dumps(request) + "\n").encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        lines = [
            line for line in result.stdout.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        if not lines:
            raise RuntimeError(
                f"bridge produced no output (exit {result.returncode}); "
                f"stderr tail: {result.stderr.decode(errors='replace')[-300:]}"
            )
        return json.loads(lines[-1])


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", required=True, help="path to the frozen phantom-bridge")
    parser.add_argument("--out", default=None, help="where to write the JSON report")
    args = parser.parse_args()

    binary = Path(args.bridge)
    if not binary.is_file():
        print(f"error: bridge not found: {binary}", file=sys.stderr)
        return 2

    report: dict = {
        "bridge": str(binary),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "steps": [],
        "ok": False,
    }
    checks: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> bool:
        checks.append((name, ok, detail))
        report["steps"].append({"step": name, "ok": ok, "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}", flush=True)
        return ok

    bridge = Bridge(binary)
    started = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="phantom-e2e-") as tmp_raw:
        tmp = Path(tmp_raw)
        carrier = tmp / "carrier.png"
        shutil.copy(EXAMPLE_CARRIER, carrier)
        hidden = tmp / "hidden.png"
        extracted = tmp / "extracted.pdf"

        version = bridge.call({"cmd": "version"})
        record("bridge answers version", version.get("ok") is True, f"version={version.get('version')}")

        probe = bridge.call({"cmd": "probe", "path": str(carrier)})
        record(
            "probe carrier",
            probe.get("ok") is True and probe.get("kind") == "image",
            f"capacity_bytes={probe.get('capacity_bytes')}",
        )

        hide = bridge.call(
            {
                "cmd": "hide",
                "carrier_path": str(carrier),
                "output_path": str(hidden),
                "secret_path": str(EXAMPLE_PDF),
                "password": DEMO_PASSWORD,
            }
        )
        hide_ok = hide.get("ok") is True and hide.get("verified") is True
        record("hide PDF into PNG (self-verified)", hide_ok, f"verified={hide.get('verified')} kdf={hide.get('profile_label')}")

        info = bridge.call(
            {"cmd": "extract", "carrier_path": str(hidden), "password": DEMO_PASSWORD, "mode": "info"}
        )
        info_ok = (
            info.get("ok") is True
            and info.get("kind") == "file"
            and info.get("suggested_name") == "example_payload.pdf"
            and info.get("integrity_ok") is True
        )
        record(
            "extract info names the PDF",
            info_ok,
            f"suggested_name={info.get('suggested_name')} version={info.get('container_version')}",
        )

        save = bridge.call(
            {
                "cmd": "extract",
                "carrier_path": str(hidden),
                "password": DEMO_PASSWORD,
                "mode": "save_file",
                "output_path": str(extracted),
            }
        )
        record("extract writes the file", save.get("ok") is True, f"output={Path(extracted).name}")

        if extracted.exists() and hidden.exists():
            original_sha = sha256_hex(EXAMPLE_PDF)
            extracted_sha = sha256_hex(extracted)
            record(
                "extracted PDF is byte-identical (SHA-256)",
                original_sha == extracted_sha,
                f"{extracted_sha[:16]}…",
            )
            report["sha256"] = {"original": original_sha, "extracted": extracted_sha}
        else:
            record("extracted PDF is byte-identical (SHA-256)", False, "missing output file")

        wrong = bridge.call(
            {"cmd": "extract", "carrier_path": str(hidden), "password": DEMO_PASSWORD + "-wrong", "mode": "info"}
        )
        wrong_code = (wrong.get("error") or {}).get("code") if isinstance(wrong.get("error"), dict) else None
        record(
            "wrong password is refused without an oracle",
            wrong.get("ok") is False and wrong_code == "wrong_password_or_no_data",
            f"code={wrong_code}",
        )

    report["duration_seconds"] = round(time.perf_counter() - started, 2)
    report["ok"] = all(ok for _, ok, _ in checks)

    text = json.dumps(report, indent=2, ensure_ascii=False)
    print()
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
