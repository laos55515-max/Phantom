"""The exports: a text report, a JSON report, and the honest wording both carry."""

from __future__ import annotations

import json
from pathlib import Path

import metadata_samples as samples
from phantom.metadata import cleaner, inspector
from phantom.metadata import report as meta_report


def _report(tmp_path: Path):
    return inspector.inspect_path(str(samples.personal_png(tmp_path / "doc.png")))


def test_the_text_report_lists_every_field_with_its_category(tmp_path: Path) -> None:
    found = _report(tmp_path)

    text = meta_report.inspection_text(found)

    for item in found.fields:
        assert item.label in text
        assert item.container in text
    assert "[personal]" in text
    assert "[rights]" in text
    assert "sha256 (whole file):" in text


def test_the_text_report_ends_with_the_disclaimer(tmp_path: Path) -> None:
    text = meta_report.inspection_text(_report(tmp_path))

    assert meta_report.REPORT_DISCLAIMER in text
    assert "absence of a known field is not proof" in text


def test_the_json_report_is_valid_versioned_and_complete(tmp_path: Path) -> None:
    found = _report(tmp_path)

    payload = json.loads(meta_report.report_json(found))

    assert payload["report_format"] == meta_report.REPORT_FORMAT_VERSION
    assert payload["tool"] == "PHANTOM metadata inspector"
    assert payload["disclaimer"] == meta_report.REPORT_DISCLAIMER
    assert payload["report"]["format"] == "png"
    counted = [item for item in found.fields if not item.derived]
    assert payload["report"]["counts"]["total"] == len(counted)
    assert len(payload["report"]["fields"]) == len(found.fields), "views are exported too"
    assert payload["report"]["fields"] != []
    assert any(item["derived"] for item in payload["report"]["fields"]), (
        "a derived view says so in the JSON"
    )
    assert payload["report"]["provenance"]["state"] == "none"
    assert "plan" not in payload


def test_the_json_report_carries_the_plan_when_asked(tmp_path: Path) -> None:
    found = _report(tmp_path)
    selection = frozenset({"png.text.tEXt.Comment"})
    decision = cleaner.plan(found, selection)

    payload = json.loads(meta_report.report_json(found, decision))

    assert payload["plan"]["allowed"] is True
    assert [item["key"] for item in payload["plan"]["remove"]] == ["png.text.tEXt.Comment"]
    assert "outcome" not in payload


def test_the_plan_preview_names_removed_kept_and_protected(tmp_path: Path) -> None:
    found = inspector.inspect_path(str(samples.personal_wav(tmp_path / "plan.wav")))
    decision = cleaner.plan(found, frozenset({"wav.info.ICMT", "wav.info.ICOP"}))

    text = meta_report.cleaning_text(decision)

    assert "will be removed (1):" in text
    assert "recorded in my kitchen" in text
    assert "selected but protected (1):" in text
    assert "kept (" in text


def test_a_blocked_plan_says_why(tmp_path: Path) -> None:
    found = inspector.inspect_path(str(samples.signed_png(tmp_path / "signed.png")))
    decision = cleaner.plan(found, found.removable_keys)

    text = meta_report.cleaning_text(decision)

    assert "not allowed: signed_media" in text


def test_the_cleaning_report_explains_both_hashes(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "hashes.png")
    output = tmp_path / "hashes-clean.png"
    found = inspector.inspect_path(str(source))
    selection = frozenset({"png.text.tEXt.Comment"})
    outcome = cleaner.clean(str(source), str(output), selection)

    text = meta_report.cleaning_text(cleaner.plan(found, selection), outcome)

    assert "sha256 (whole file, source):" in text
    assert "sha256 (whole file, output):" in text
    assert "sha256 (decoded content):" in text
    assert "decoded content identical: yes" in text
    assert "must not change at all" in text
    assert outcome.source_sha256 in text
    assert outcome.output_sha256 in text


def test_the_summary_line_carries_counts_but_no_values(tmp_path: Path) -> None:
    found = _report(tmp_path)

    line = meta_report.summary_line(found)

    assert "PNG" in line
    assert f"{found.counts.total} fields" in line
    assert "Ada Lovelace" not in line
    assert "50.45" not in line


def test_the_text_report_says_which_rows_are_derived(tmp_path: Path) -> None:
    """18 rows and "17 fields" is confusing unless the report explains itself."""
    found = _report(tmp_path)
    derived = [item for item in found.fields if item.derived]

    text = meta_report.inspection_text(found)

    assert derived, "the fixture has a derived view"
    assert f"derived views: {len(derived)}" in text
    assert "not counted" in text
    for item in derived:
        assert item.key not in {field.key for field in found.fields if not field.derived}


def test_the_exports_never_promise_anonymity(tmp_path: Path) -> None:
    found = _report(tmp_path)
    decision = cleaner.plan(found, cleaner.default_selection(found))

    for text in (
        meta_report.inspection_text(found),
        meta_report.cleaning_text(decision),
        meta_report.report_json(found, decision),
        meta_report.REPORT_DISCLAIMER,
    ):
        lowered = text.lower()
        for promise in ("anonymous", "untraceable", "guaranteed", "cannot be detected"):
            assert promise not in lowered
