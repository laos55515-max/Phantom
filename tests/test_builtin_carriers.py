"""Tests for the built-in offline PNG carrier set and capacity auto-selection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from phantom import bridge
from phantom.core import api
from phantom.core import carriers as builtin_carriers

EXAMPLE_PDF = api.RESOURCES_DIR / "example_payload.pdf"
TEST_PASSWORD = "builtin-carrier-test-passphrase-9a4e"


def _call(req: dict) -> dict:
    return bridge.handle(json.dumps(req))


def test_builtin_carrier_set_has_8_to_12_distinct_pngs() -> None:
    items = builtin_carriers.list_builtin_carriers()
    assert 8 <= len(items) <= 12
    capacities = [item.capacity.payload_bytes for item in items]
    # Sorted ascending and covering distinct sizes
    assert capacities == sorted(capacities)
    assert len(set(capacities)) == len(capacities)
    assert capacities[0] >= 20_000
    assert capacities[-1] >= 2_000_000


def test_select_carrier_for_text_and_pdf_and_cycle_variant() -> None:
    text_res = _call({"cmd": "select_carrier", "text": "Hello PHANTOM offline carrier"})
    assert text_res["ok"] is True
    assert text_res["fits"] is True
    assert text_res["carrier"]["builtin"] is True
    assert text_res["carrier"]["capacity_bytes"] >= text_res["required_bytes"]

    # Cycling ("Другой вариант") picks a different suitable built-in PNG
    alt_res = _call(
        {
            "cmd": "select_carrier",
            "text": "Hello PHANTOM offline carrier",
            "exclude_path": text_res["carrier"]["path"],
        }
    )
    assert alt_res["ok"] is True
    assert alt_res["fits"] is True
    assert alt_res["carrier"]["path"] != text_res["carrier"]["path"]

    # Auto-selection for a real PDF
    pdf_res = _call({"cmd": "select_carrier", "secret_path": str(EXAMPLE_PDF)})
    assert pdf_res["ok"] is True
    assert pdf_res["fits"] is True
    assert pdf_res["carrier"]["builtin"] is True
    assert pdf_res["carrier"]["capacity_bytes"] >= pdf_res["required_bytes"]


def test_select_carrier_when_payload_exceeds_all_builtin_pngs() -> None:
    all_res = _call({"cmd": "builtin_carriers"})
    assert all_res["ok"] is True
    max_cap = all_res["max_capacity_bytes"]

    too_big = max_cap + 1024
    res = _call({"cmd": "select_carrier", "required_bytes": too_big})
    assert res["ok"] is True
    assert res["fits"] is False
    assert res["carrier"] is None
    assert res["required_bytes"] == too_big
    assert res["max_capacity_bytes"] == max_cap
    assert res["suitable_count"] == 0


def test_builtin_png_hide_and_extract_pdf_byte_identical(tmp_path: Path) -> None:
    sel = _call({"cmd": "select_carrier", "secret_path": str(EXAMPLE_PDF)})
    assert sel["ok"] is True and sel["fits"] is True
    carrier_path = sel["carrier"]["path"]

    container = tmp_path / "container.png"
    extracted = tmp_path / "extracted.pdf"

    hide_res = _call(
        {
            "cmd": "hide",
            "carrier_path": carrier_path,
            "output_path": str(container),
            "secret_path": str(EXAMPLE_PDF),
            "password": TEST_PASSWORD,
        }
    )
    assert hide_res["ok"] is True
    assert hide_res["verified"] is True
    assert container.is_file()

    save_res = _call(
        {
            "cmd": "extract",
            "carrier_path": str(container),
            "password": TEST_PASSWORD,
            "mode": "save_file",
            "output_path": str(extracted),
        }
    )
    assert save_res["ok"] is True
    assert save_res["integrity_ok"] is True
    assert hashlib.sha256(extracted.read_bytes()).hexdigest() == hashlib.sha256(
        EXAMPLE_PDF.read_bytes()
    ).hexdigest()
