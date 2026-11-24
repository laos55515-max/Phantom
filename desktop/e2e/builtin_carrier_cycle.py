#!/usr/bin/env python3
"""Offline packaged-build check for built-in PNG carriers and auto-selection.

Every ``call_bridge`` spawns a fresh ``phantom-bridge`` process (matching Tauri's
one-process-per-action lifecycle) so PyInstaller's temporary extraction directory
changes on every call.  Verifies without any network access:

1. ``builtin_carriers`` lists 8-12 bundled PNG carriers with stable ``builtin_id``s.
2. ``select_carrier`` auto-selects a carrier for text and for a PDF.
3. ``select_carrier`` with ``exclude_id`` switches to a different ``builtin_id``.
4. ``select_carrier`` with an oversized payload returns ``fits=False``.
5. ``hide`` with ``builtin_carrier_id`` (separate bridge process) saves and
   self-verifies a container.
6. ``extract`` (``save_file``, separate bridge process) recovers the PDF
   byte-for-byte (SHA-256 match).
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
    carriers = listed.get("carriers") or []
    ids = [c.get("builtin_id") for c in carriers if isinstance(c, dict)]
    record(
        "bundled 8-12 offline PNG carriers with stable builtin_ids",
        listed.get("ok") is True and 8 <= count <= 12 and len(set(ids)) == count and all(ids),
        f"count={count} max_cap={max_cap}",
    )

    text_sel = call_bridge(binary, {"cmd": "select_carrier", "text": "Offline test message"})
    text_carrier = text_sel.get("carrier") or {}
    text_id = text_carrier.get("builtin_id")
    record(
        "auto-select built-in PNG for text",
        text_sel.get("ok") is True and text_sel.get("fits") is True and bool(text_id),
        f"builtin_id={text_id}",
    )

    alt_sel = call_bridge(
        binary,
        {
            "cmd": "select_carrier",
            "text": "Offline test message",
            "exclude_id": text_id or "",
        },
    )
    alt_carrier = alt_sel.get("carrier") or {}
    alt_id = alt_carrier.get("builtin_id")
    record(
        "switch to another built-in PNG (different builtin_id)",
        alt_sel.get("ok") is True
        and alt_sel.get("fits") is True
        and bool(alt_id)
        and alt_id != text_id,
        f"{text_id} -> {alt_id}",
    )

    overflow = call_bridge(binary, {"cmd": "select_carrier", "required_bytes": max_cap + 4096})
    record(
        "report when no built-in PNG fits",
        overflow.get("ok") is True and overflow.get("fits") is False and overflow.get("carrier") is None,
        f"max_cap={overflow.get('max_capacity_bytes')}",
    )

    pdf_sel = call_bridge(binary, {"cmd": "select_carrier", "secret_path": str(EXAMPLE_PDF)})
    pdf_carrier = pdf_sel.get("carrier") or {}
    pdf_builtin_id = pdf_carrier.get("builtin_id")
    record(
        "auto-select built-in PNG for PDF",
        pdf_sel.get("ok") is True and pdf_sel.get("fits") is True and bool(pdf_builtin_id),
        f"builtin_id={pdf_builtin_id}",
    )

    with tempfile.TemporaryDirectory(prefix="phantom-builtin-e2e-") as tmp_raw:
        tmp = Path(tmp_raw)
        container = tmp / "builtin-container.png"
        extracted = tmp / "extracted.pdf"

        # Separate bridge process resolves builtin_carrier_id internally
        hide = call_bridge(
            binary,
            {
                "cmd": "hide",
                "builtin_carrier_id": pdf_builtin_id,
                "output_path": str(container),
                "secret_path": str(EXAMPLE_PDF),
                "password": TEST_PASSWORD,
            },
        )
        record(
            "hide PDF using builtin_carrier_id (separate process, self-verified)",
            hide.get("ok") is True and hide.get("verified") is True and container.is_file(),
            f"verified={hide.get('verified')}",
        )

        # Separate bridge process extracts and verifies the PDF
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
