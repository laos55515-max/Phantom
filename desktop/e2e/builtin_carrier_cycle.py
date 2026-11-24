#!/usr/bin/env python3
"""Offline packaged-build check for built-in PNG carriers and auto-selection.

Verifies against a frozen ``phantom-bridge`` binary (without any network access):
1. ``builtin_carriers`` lists 8-12 bundled PNG carriers of different sizes.
2. ``select_carrier`` auto-selects a carrier for text and for a PDF.
3. ``select_carrier`` with ``exclude_path`` switches to another built-in PNG.
4. ``select_carrier`` with an oversized payload returns ``fits=False``.
5. ``hide`` with the auto-selected built-in PNG saves and self-verifies a container.
6. ``extract`` (``save_file``) recovers the PDF byte-for-byte (SHA-256 match).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_PDF = REPO_ROOT / "src" / "phantom" / "resources" / "example_payload.pdf"
TEST_PASSWORD = "desktop-e2e-builtin-carrier-passphrase-4b9d"


def call_bridge(binary: Path, request: dict, timeout: float = 600.0) -> dict:
    result = subprocess.run(
        [str(binary)],
        input=(json.dumps(request) + "\n").encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    lines = [
        line
        for line in result.stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    if not lines:
        raise RuntimeError(
            f"bridge produced no output (exit {result.returncode}); "
            f"stderr: {result.stderr.decode(errors='replace')[-300:]}"
        )
    return json.loads(lines[-1])


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", required=True, help="path to the frozen phantom-bridge")
    args = parser.parse_args()

    binary = Path(args.bridge)
    if not binary.is_file():
        print(f"error: bridge not found: {binary}", file=sys.stderr)
        return 2

    checks: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}", flush=True)

    listed = call_bridge(binary, {"cmd": "builtin_carriers"})
    count = int(listed.get("count", 0))
    max_cap = int(listed.get("max_capacity_bytes", 0))
    record("bundled 8-12 offline PNG carriers", listed.get("ok") is True and 8 <= count <= 12, f"count={count} max_cap={max_cap}")

    text_sel = call_bridge(binary, {"cmd": "select_carrier", "text": "Offline test message"})
    text_carrier = text_sel.get("carrier") or {}
    record(
        "auto-select built-in PNG for text",
        text_sel.get("ok") is True and text_sel.get("fits") is True and bool(text_carrier.get("builtin")),
        f"carrier={text_carrier.get('name')}",
    )

    alt_sel = call_bridge(
        binary,
        {
            "cmd": "select_carrier",
            "text": "Offline test message",
            "exclude_path": text_carrier.get("path", ""),
        },
    )
    alt_carrier = alt_sel.get("carrier") or {}
    record(
        "switch to another built-in PNG",
        alt_sel.get("ok") is True
        and alt_sel.get("fits") is True
        and alt_carrier.get("path") != text_carrier.get("path"),
        f"alt={alt_carrier.get('name')}",
    )

    overflow = call_bridge(binary, {"cmd": "select_carrier", "required_bytes": max_cap + 4096})
    record(
        "report when no built-in PNG fits",
        overflow.get("ok") is True and overflow.get("fits") is False and overflow.get("carrier") is None,
        f"max_cap={overflow.get('max_capacity_bytes')}",
    )

    pdf_sel = call_bridge(binary, {"cmd": "select_carrier", "secret_path": str(EXAMPLE_PDF)})
    pdf_carrier = pdf_sel.get("carrier") or {}
    record(
        "auto-select built-in PNG for PDF",
        pdf_sel.get("ok") is True and pdf_sel.get("fits") is True and bool(pdf_carrier.get("builtin")),
        f"carrier={pdf_carrier.get('name')}",
    )

    with tempfile.TemporaryDirectory(prefix="phantom-builtin-e2e-") as tmp_raw:
        tmp = Path(tmp_raw)
        container = tmp / "builtin-container.png"
        extracted = tmp / "extracted.pdf"

        hide = call_bridge(
            binary,
            {
                "cmd": "hide",
                "carrier_path": pdf_carrier.get("path", ""),
                "output_path": str(container),
                "secret_path": str(EXAMPLE_PDF),
                "password": TEST_PASSWORD,
            },
        )
        record(
            "hide PDF into built-in PNG (offline, self-verified)",
            hide.get("ok") is True and hide.get("verified") is True and container.is_file(),
            f"verified={hide.get('verified')}",
        )

        save = call_bridge(
            binary,
            {
                "cmd": "extract",
                "carrier_path": str(container),
                "password": TEST_PASSWORD,
                "mode": "save_file",
                "output_path": str(extracted),
            },
        )
        same_sha = extracted.is_file() and sha256_hex(extracted) == sha256_hex(EXAMPLE_PDF)
        record(
            "extract PDF byte-identical to original (SHA-256)",
            save.get("ok") is True and same_sha,
            f"sha256={sha256_hex(extracted)[:16] if extracted.is_file() else 'missing'}",
        )

    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
