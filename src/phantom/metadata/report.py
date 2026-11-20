"""Rendering the inspection result: a text report and a JSON report.

Both are produced from the same model, so the two exports can never disagree.  The
text report is what a user reads; the JSON report is what a script reads.  Neither
one is written anywhere by this module — the interface decides when to save one,
because the report contains the personal data the cleaner was about to remove.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Final, Protocol

from phantom.metadata.cleaner import CleaningOutcome, CleaningPlan
from phantom.metadata.models import MetadataField, MetadataReport

LOGGER: Final = logging.getLogger("phantom.metadata.report")

#: Bumped when the shape of the JSON report changes incompatibly.
REPORT_FORMAT_VERSION: Final = 1

#: Printed in every export: a report is a record of what was found, not a verdict.
REPORT_DISCLAIMER: Final = (
    "A report lists what PHANTOM could read. The absence of a known field is not "
    "proof that no digital trace exists: unknown blocks are listed as unknown, and "
    "data inside an embedded picture or an unrecognised vendor block is not read."
)


class Texts(Protocol):
    """The slice of :class:`phantom.i18n.I18n` the reports need."""

    def has(self, key: str) -> bool: ...

    def t(self, key: str, **params: object) -> str: ...


#: English is both the fallback and the source of truth for the layout: a locale
#: that has not translated a line yet shows this one instead of a raw key.  The
#: JSON report never uses these — it stays machine-readable and versioned.
TEXTS: Final[dict[str, str]] = {
    "report.title": "PHANTOM \u2014 metadata report",
    "report.plan_title": "PHANTOM \u2014 cleaning plan",
    "report.generated": "generated: {timestamp}",
    "report.file": "file: {path}",
    "report.format": "format: {format}",
    "report.size": "size: {size} bytes",
    "report.sha_whole": "sha256 (whole file): {sha}",
    "report.counts": (
        "fields: total={total} personal={personal} descriptive={descriptive} "
        "technical={technical} rights={rights} provenance={provenance} unknown={unknown}"
    ),
    "report.derived": (
        "derived views: {count} (shown below, not counted: they are computed from the "
        "fields they are named after)"
    ),
    "report.checksums_ok": "container checksums: valid",
    "report.checksums_broken": "container checksums: BROKEN",
    "report.technical": "technical:",
    "report.provenance": "provenance: {state}",
    "report.warnings": "warnings:",
    "report.warning_line": "  - {warning}",
    "report.fields": "fields:",
    "report.marker_removable": "removable",
    "report.marker_protected": "protected: {reason}",
    "report.field_line": "  [{category}] {container} \u00b7 {label} = {value} ({marker})",
    "report.structure": "structure:",
    "report.disclaimer": REPORT_DISCLAIMER,
    "report.plan_blocked": "not allowed: {reason}",
    "report.plan_remove": "will be removed ({count}):",
    "report.plan_remove_line": "  - {container} \u00b7 {label} = {value}",
    "report.plan_protected": "selected but protected ({count}):",
    "report.plan_protected_line": "  - {container} \u00b7 {label}: {reason}",
    "report.plan_keep": "kept ({count}):",
    "report.plan_keep_line": "  - {container} \u00b7 {label} ({category})",
    "report.result": "result:",
    "report.result_output": "  output: {path}",
    "report.result_removed": "  removed: {count}",
    "report.result_kept": "  kept: {count}",
    "report.result_identical": "  decoded content identical: {state}",
    "report.result_difference": "  difference: {difference}",
    "report.sha_source": "  sha256 (whole file, source): {sha}",
    "report.sha_output": "  sha256 (whole file, output): {sha}",
    "report.sha_decoded": "  sha256 (decoded content): {sha}",
    "report.sha_not_checked": "not checked",
    "report.hash_note": (
        "  the whole-file hash changes when metadata is removed; the decoded-content "
        "hash must not change at all"
    ),
    "report.summary": (
        "{path} \u00b7 {format} \u00b7 {size} bytes \u00b7 {total} fields "
        "({personal} personal, {removable} removable) \u00b7 provenance {provenance}"
    ),
    "report.state_yes": "yes",
    "report.state_no": "no",
}


def text_for(texts: Texts | None, key: str, **params: object) -> str:
    """One localised line, falling back to the English template."""
    if texts is not None:
        try:
            if texts.has(key):
                return str(texts.t(key, **params))
        except Exception as exc:  # noqa: BLE001 - a bad locale must not break an export
            LOGGER.debug("locale lookup for %s failed: %s", key, exc)
    template = TEXTS[key]
    return template.format(**params) if params else template


def field_reason(item: MetadataField, texts: Texts | None = None) -> str:
    """The reason a field is kept, in the report's language when there is one.

    Readers that produce a structured reason leave it as a locale key, so a
    Ukrainian card never shows an English sentence in the middle of it; the
    English text stays the fallback for the JSON export and the log.
    """
    if not item.reason_key or texts is None:
        return item.reason
    params = dict(item.reason_params)
    try:
        if texts.has(item.reason_key):
            return str(texts.t(item.reason_key, **params))
    except Exception as exc:  # noqa: BLE001 - a bad locale must not hide the row
        LOGGER.debug("reason lookup for %s failed: %s", item.reason_key, exc)
    return item.reason


def field_value(item: MetadataField, texts: Texts | None = None) -> str:
    """The value a user sees: data verbatim, a sentence in their language.

    Field *values* are data — a city, a date, a SHA-256 — and are never translated.
    A handful of them are sentences this program wrote ("contains personal
    properties", "12 bytes"), and those carry a locale key; showing the English one
    inside a Ukrainian card was the bug this exists to prevent.
    """
    if not item.value_key or texts is None:
        return item.value
    params = dict(item.value_params)
    try:
        if texts.has(item.value_key):
            return str(texts.t(item.value_key, **params))
    except Exception as exc:  # noqa: BLE001 - a bad locale must not hide the row
        LOGGER.debug("value lookup for %s failed: %s", item.value_key, exc)
    return item.value


def yes_no(texts: Texts | None, value: bool) -> str:
    """``yes``/``no`` in the report's language (``{state}`` is filled with this)."""
    return text_for(texts, "report.state_yes" if value else "report.state_no")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def inspection_text(report: MetadataReport, texts: Texts | None = None) -> str:
    """The readable report: header, technical facts, provenance, every field.

    ``texts`` is an :class:`phantom.i18n.I18n` (or anything with ``has``/``t``);
    without it the report is English, which is what the tests and the JSON schema
    speak.  Field labels and values are data and are never translated.
    """
    counts = _counts(report)
    lines: list[str] = [
        text_for(texts, "report.title"),
        text_for(texts, "report.generated", timestamp=_now()),
        text_for(texts, "report.file", path=report.path),
        text_for(texts, "report.format", format=report.fmt.value.upper()),
        text_for(texts, "report.size", size=report.size_bytes),
        text_for(texts, "report.sha_whole", sha=report.sha256),
        text_for(texts, "report.counts", **counts),
    ]
    derived = [item for item in report.fields if item.derived]
    if derived:
        lines.append(text_for(texts, "report.derived", count=len(derived)))
    if report.checksum_ok is not None:
        lines.append(
            text_for(
                texts,
                "report.checksums_ok" if report.checksum_ok else "report.checksums_broken",
            )
        )
    technical = report.technical
    if technical:
        lines.append(text_for(texts, "report.technical"))
        lines.extend(f"  {key}: {technical[key]}" for key in sorted(technical))
    lines.append(text_for(texts, "report.provenance", state=report.provenance.state.value))
    for key, value in report.provenance.details:
        lines.append(f"  {key}: {value}")
    if report.warnings:
        lines.append(text_for(texts, "report.warnings"))
        lines.extend(
            text_for(texts, "report.warning_line", warning=warning)
            for warning in report.warnings
        )
    lines.append(text_for(texts, "report.fields"))
    for item in report.sorted_fields():
        marker = (
            text_for(texts, "report.marker_removable")
            if item.removable
            else text_for(
                texts,
                "report.marker_protected",
                reason=field_reason(item, texts) or "kept",
            )
        )
        lines.append(
            text_for(
                texts,
                "report.field_line",
                category=item.category.value,
                container=item.container,
                label=item.label,
                value=field_value(item, texts),
                marker=marker,
            )
        )
    if report.chunks:
        lines.append(text_for(texts, "report.structure"))
        for chunk in report.chunks:
            checksum = ""
            if chunk.checksum_ok is not None:
                checksum = " crc=ok" if chunk.checksum_ok else " crc=BAD"
            lines.append(f"  {chunk.kind} @{chunk.offset} {chunk.size}B{checksum}")
    lines.append("")
    lines.append(text_for(texts, "report.disclaimer"))
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


def cleaning_text(
    plan: CleaningPlan,
    outcome: CleaningOutcome | None = None,
    texts: Texts | None = None,
) -> str:
    """The preview report: what will be removed, what stays, and why."""
    lines: list[str] = [
        text_for(texts, "report.plan_title"),
        text_for(texts, "report.generated", timestamp=_now()),
    ]
    if not plan.allowed:
        lines.append(text_for(texts, "report.plan_blocked", reason=plan.reason))
        return "\n".join(lines) + "\n"
    lines.append(text_for(texts, "report.plan_remove", count=len(plan.remove)))
    lines.extend(
        text_for(
            texts,
            "report.plan_remove_line",
            container=item.container,
            label=item.label,
            value=item.value,
        )
        for item in plan.remove
    )
    if plan.blocked:
        lines.append(text_for(texts, "report.plan_protected", count=len(plan.blocked)))
        lines.extend(
            text_for(
                texts,
                "report.plan_protected_line",
                container=item.container,
                label=item.label,
                reason=field_reason(item, texts) or "protected",
            )
            for item in plan.blocked
        )
    lines.append(text_for(texts, "report.plan_keep", count=len(plan.keep)))
    lines.extend(
        text_for(
            texts,
            "report.plan_keep_line",
            container=item.container,
            label=item.label,
            category=item.category.value,
        )
        for item in plan.keep
    )
    if plan.warnings:
        lines.append(text_for(texts, "report.warnings"))
        lines.extend(
            text_for(texts, "report.warning_line", warning=warning)
            for warning in plan.warnings
        )
    if outcome is not None:
        right = outcome.comparison.right
        lines.append(text_for(texts, "report.result"))
        lines.append(text_for(texts, "report.result_output", path=outcome.output))
        lines.append(text_for(texts, "report.result_removed", count=len(outcome.removed)))
        lines.append(text_for(texts, "report.result_kept", count=len(outcome.kept)))
        lines.append(
            text_for(
                texts,
                "report.result_identical",
                state=yes_no(texts, outcome.comparison.identical),
            )
        )
        lines.extend(
            text_for(texts, "report.result_difference", difference=difference)
            for difference in outcome.comparison.differences
        )
        lines.append(text_for(texts, "report.sha_source", sha=outcome.source_sha256))
        lines.append(text_for(texts, "report.sha_output", sha=outcome.output_sha256))
        decoded = (
            right.content_sha256
            if right is not None
            else text_for(texts, "report.sha_not_checked")
        )
        lines.append(text_for(texts, "report.sha_decoded", sha=decoded))
        lines.append(text_for(texts, "report.hash_note"))
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


def summary_line(report: MetadataReport, texts: Texts | None = None) -> str:
    """One line for the clipboard: what was found, without the values themselves."""
    counts = report.counts
    state = report.provenance.state.value
    if texts is not None:
        try:
            if texts.has(report.provenance.state.i18n_key):
                state = str(texts.t(report.provenance.state.i18n_key))
        except Exception as exc:  # noqa: BLE001 - a bad locale must not break a copy
            LOGGER.debug("provenance label failed: %s", exc)
    return text_for(
        texts,
        "report.summary",
        path=report.path,
        format=report.fmt.value.upper(),
        size=report.size_bytes,
        total=counts.total,
        personal=counts.personal,
        removable=counts.removable,
        provenance=state,
    )
