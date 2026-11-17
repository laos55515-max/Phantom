"""Rendering the inspection result: a text report and a JSON report.

Both are produced from the same model, so the two exports can never disagree.  The
text report is what a user reads; the JSON report is what a script reads.  Neither
one is written anywhere by this module — the interface decides when to save one,
because the report contains the personal data the cleaner was about to remove.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Final

from phantom.metadata.cleaner import CleaningOutcome, CleaningPlan
from phantom.metadata.models import MetadataReport

#: Bumped when the shape of the JSON report changes incompatibly.
REPORT_FORMAT_VERSION: Final = 1

#: Printed in every export: a report is a record of what was found, not a verdict.
REPORT_DISCLAIMER: Final = (
    "A report lists what PHANTOM could read. The absence of a known field is not "
    "proof that no digital trace exists: unknown blocks are listed as unknown, and "
    "data inside an embedded picture or an unrecognised vendor block is not read."
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def inspection_text(report: MetadataReport) -> str:
    """The readable report: header, technical facts, provenance, every field."""
    lines: list[str] = [
        "PHANTOM — metadata report",
        f"generated: {_now()}",
        f"file: {report.path}",
        f"format: {report.fmt.value.upper()}",
        f"size: {report.size_bytes} bytes",
        f"sha256 (whole file): {report.sha256}",
        (
            "fields: total={total} personal={personal} descriptive={descriptive} "
            "technical={technical} rights={rights} provenance={provenance} unknown={unknown}"
        ).format(**_counts(report)),
    ]
    if report.checksum_ok is not None:
        lines.append(f"container checksums: {'valid' if report.checksum_ok else 'BROKEN'}")
    technical = report.technical
    if technical:
        lines.append("technical:")
        lines.extend(f"  {key}: {technical[key]}" for key in sorted(technical))
    lines.append(f"provenance: {report.provenance.state.value}")
    for key, value in report.provenance.details:
        lines.append(f"  {key}: {value}")
    if report.warnings:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in report.warnings)
    lines.append("fields:")
    for item in report.sorted_fields():
        marker = "removable" if item.removable else f"protected: {item.reason or 'kept'}"
        lines.append(
            f"  [{item.category.value}] {item.container} · {item.label} = {item.value} ({marker})"
        )
    if report.chunks:
        lines.append("structure:")
        for chunk in report.chunks:
            checksum = ""
            if chunk.checksum_ok is not None:
                checksum = " crc=ok" if chunk.checksum_ok else " crc=BAD"
            lines.append(f"  {chunk.kind} @{chunk.offset} {chunk.size}B{checksum}")
    lines.append("")
    lines.append(REPORT_DISCLAIMER)
    return "\n".join(lines) + "\n"


def _counts(report: MetadataReport) -> dict[str, int]:
    counts = report.counts
    return {
        "total": counts.total,
        "personal": counts.personal,
        "descriptive": counts.descriptive,
        "technical": counts.technical,
        "rights": counts.rights,
        "provenance": counts.provenance,
        "unknown": counts.unknown,
    }


def cleaning_text(plan: CleaningPlan, outcome: CleaningOutcome | None = None) -> str:
    """The preview report: what will be removed, what stays, and why."""
    lines: list[str] = ["PHANTOM — cleaning plan", f"generated: {_now()}"]
    if not plan.allowed:
        lines.append(f"not allowed: {plan.reason}")
        return "\n".join(lines) + "\n"
    lines.append(f"will be removed ({len(plan.remove)}):")
    lines.extend(f"  - {item.container} · {item.label} = {item.value}" for item in plan.remove)
    if plan.blocked:
        lines.append(f"selected but protected ({len(plan.blocked)}):")
        lines.extend(
            f"  - {item.container} · {item.label}: {item.reason or 'protected'}"
            for item in plan.blocked
        )
    lines.append(f"kept ({len(plan.keep)}):")
    lines.extend(
        f"  - {item.container} · {item.label} ({item.category.value})" for item in plan.keep
    )
    if plan.warnings:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in plan.warnings)
    if outcome is not None:
        lines.append("result:")
        lines.append(f"  output: {outcome.output}")
        lines.append(f"  removed: {len(outcome.removed)}")
        lines.append(f"  kept: {len(outcome.kept)}")
        identical = "yes" if outcome.comparison.identical else "NO"
        lines.append(f"  decoded content identical: {identical}")
        for difference in outcome.comparison.differences:
            lines.append(f"  difference: {difference}")
        lines.append(f"  sha256 (whole file, source): {outcome.source_sha256}")
        lines.append(f"  sha256 (whole file, output): {outcome.output_sha256}")
        right = outcome.comparison.right
        decoded = right.content_sha256 if right is not None else "not checked"
        lines.append(f"  sha256 (decoded content): {decoded}")
        lines.append(
            "  the whole-file hash changes when metadata is removed; the decoded-content "
            "hash must not change at all"
        )
    return "\n".join(lines) + "\n"


def report_json(
    report: MetadataReport,
    plan: CleaningPlan | None = None,
    outcome: CleaningOutcome | None = None,
) -> str:
    """The machine-readable report; the same data as :func:`inspection_text`."""
    payload: dict[str, object] = {
        "report_format": REPORT_FORMAT_VERSION,
        "generated": _now(),
        "tool": "PHANTOM metadata inspector",
        "disclaimer": REPORT_DISCLAIMER,
        "report": report.to_dict(),
    }
    if plan is not None:
        payload["plan"] = plan.to_dict()
    if outcome is not None:
        outcome_dict = outcome.to_dict()
        outcome_dict["report_after"] = outcome.report_after.to_dict()
        payload["outcome"] = outcome_dict
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def summary_line(report: MetadataReport) -> str:
    """One line for the clipboard: what was found, without the values themselves."""
    counts = report.counts
    return (
        f"{report.path} · {report.fmt.value.upper()} · {report.size_bytes} bytes · "
        f"{counts.total} fields ({counts.personal} personal, {counts.removable} removable) · "
        f"provenance {report.provenance.state.value}"
    )
