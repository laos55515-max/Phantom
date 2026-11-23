"""Contract tests for ``phantom.bridge`` (the Tauri sidecar's stdio protocol).

The bridge exists so the desktop shell can reach the core without passing
secrets on the command line: one JSON request per line in, one JSON response
per line out.  These tests pin the protocol, the byte-for-byte round trip, and
the typed error codes the frontend translates.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
EXAMPLE_CARRIER = SRC / "phantom" / "resources" / "example_carrier.png"
EXAMPLE_PDF = SRC / "phantom" / "resources" / "example_payload.pdf"
PASSWORD = "bridge contract test password 9d2f41"  # noqa: S105 - test container


def _run_session(raw_lines: list[str], timeout: float = 120.0) -> list[dict]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "phantom.bridge"],
        input="\n".join(raw_lines) + "\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _call(request: dict, timeout: float = 120.0) -> dict:
    return _run_session([json.dumps(request)], timeout=timeout)[0]


def test_version_answers_with_protocol() -> None:
    response = _call({"cmd": "version"})
    assert response["ok"] is True
    assert response["protocol"] == 1
    assert response["version"]


def test_probe_reports_capacity() -> None:
    response = _call({"cmd": "probe", "path": str(EXAMPLE_CARRIER)})
    assert response["ok"] is True
    assert response["kind"] == "image"
    assert response["capacity_bytes"] > 1000
    assert response["name"] == "example_carrier.png"


def test_hide_and_extract_roundtrip_text(tmp_path: Path) -> None:
    hidden = tmp_path / "hidden.png"
    hide = _call(
        {
            "cmd": "hide",
            "carrier_path": str(EXAMPLE_CARRIER),
            "output_path": str(hidden),
            "password": PASSWORD,
            "text": "Файний текст з юнікодом ✓ і emoji 👻",
        }
    )
    assert hide["ok"] is True and hide["verified"] is True
    assert hide["payload_kind"] == "text"

    info = _call(
        {
            "cmd": "extract",
            "carrier_path": str(hidden),
            "password": PASSWORD,
            "mode": "info",
        }
    )
    assert info["ok"] is True
    assert info["kind"] == "text"
    assert info["text"] == "Файний текст з юнікодом ✓ і emoji 👻"
    assert info["integrity_ok"] is True
    assert info["container_version"] == 5  # v5 syndrome trellis is the default


def test_hide_and_extract_roundtrip_example_pdf_bytes(tmp_path: Path) -> None:
    hidden = tmp_path / "hidden.png"
    target = tmp_path / "extracted.pdf"
    hide = _call(
        {
            "cmd": "example_hide",
            "output_path": str(hidden),
            "password": PASSWORD,
        }
    )
    assert hide["ok"] is True and hide["verified"] is True

    info = _call(
        {
            "cmd": "extract",
            "carrier_path": str(hidden),
            "password": PASSWORD,
            "mode": "info",
        }
    )
    assert info["ok"] is True
    assert info["kind"] == "file"
    assert info["suggested_name"] == "example_payload.pdf"

    save = _call(
        {
            "cmd": "extract",
            "carrier_path": str(hidden),
            "password": PASSWORD,
            "mode": "save_file",
            "output_path": str(target),
        }
    )
    assert save["ok"] is True
    import hashlib

    assert hashlib.sha256(target.read_bytes()).hexdigest() == hashlib.sha256(
        EXAMPLE_PDF.read_bytes()
    ).hexdigest()


def test_wrong_password_is_a_typed_error(tmp_path: Path) -> None:
    hidden = tmp_path / "hidden.png"
    hide = _call(
        {
            "cmd": "hide",
            "carrier_path": str(EXAMPLE_CARRIER),
            "output_path": str(hidden),
            "password": PASSWORD,
            "text": "secret",
        }
    )
    assert hide["ok"] is True
    wrong = _call(
        {
            "cmd": "extract",
            "carrier_path": str(hidden),
            "password": PASSWORD + "-wrong",
            "mode": "info",
        }
    )
    assert wrong["ok"] is False
    assert wrong["error"]["code"] == "wrong_password_or_no_data"
    assert PASSWORD + "-wrong" not in json.dumps(wrong)


def test_password_is_refused_before_the_core(tmp_path: Path) -> None:
    response = _call(
        {
            "cmd": "hide",
            "carrier_path": str(EXAMPLE_CARRIER),
            "output_path": str(tmp_path / "x.png"),
            "password": "short",
            "text": "secret",
        }
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_password"


def test_unknown_command_is_invalid_input() -> None:
    response = _call({"cmd": "rm_rf"})
    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_input"


def test_argv_is_ignored_and_stdin_is_the_channel(tmp_path: Path) -> None:
    """A password smuggled into argv must never influence the bridge."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "phantom.bridge",
            PASSWORD,  # argv: must be ignored, never logged
        ],
        input=json.dumps({"cmd": "version"}) + "\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=30.0,
        check=False,
    )
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert lines[0]["ok"] is True
    assert PASSWORD not in proc.stderr
    assert PASSWORD not in proc.stdout


def test_extract_save_verifies_before_replace_and_creates_private_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins
    import hashlib
    import stat

    from phantom import bridge
    from phantom.core import api
    from phantom.core.errors import IntegrityError
    from phantom.core.payload import ExtractedPayload, PayloadKind

    data = b"sensitive recovered pdf"
    digest = hashlib.sha256(data).hexdigest()
    payload = ExtractedPayload(
        kind=PayloadKind.FILE, name="report.pdf", data=data,
        sha256_expected=digest, sha256_actual=digest, integrity_ok=True,
        original_size=len(data), compressed_size=len(data),
    )
    outcome = api.ExtractOutcome(payload=payload, carrier_kind=api.CarrierKind.IMAGE, duration_seconds=0)
    monkeypatch.setattr(bridge, "_extract_verified", lambda *_: outcome)
    target = tmp_path / "report.pdf"
    target.write_bytes(b"existing file")
    request = {"carrier_path": "carrier.png", "password": PASSWORD, "mode": "save_file", "output_path": str(target)}

    # Observe the temp at the instant it is opened for writing, before any bytes.
    original_open = builtins.open
    observed: list[int] = []

    def checked_open(path, mode="r", *args, **kwargs):
        if mode == "wb" and str(path) != str(target):
            observed.append(stat.S_IMODE(os.stat(path).st_mode))
            assert os.stat(path).st_size == 0
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", checked_open)
    assert bridge.cmd_extract(request)["sha256"] == digest
    assert target.read_bytes() == data
    if os.name == "posix":
        assert observed == [0o600]
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    # A wrong extracted digest must fail in the actual verified writer, leaving
    # the pre-existing destination intact and removing the staged file.
    from dataclasses import replace

    target.write_bytes(b"existing file")
    bad_outcome = api.ExtractOutcome(
        payload=replace(payload, sha256_actual="0" * 64),
        carrier_kind=api.CarrierKind.IMAGE, duration_seconds=0,
    )
    monkeypatch.setattr(bridge, "_extract_verified", lambda *_: bad_outcome)
    with pytest.raises(IntegrityError):
        bridge.cmd_extract(request)
    assert target.read_bytes() == b"existing file"
    assert [p.name for p in tmp_path.iterdir()] == ["report.pdf"]
