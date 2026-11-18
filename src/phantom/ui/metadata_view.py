"""Display-independent logic for the metadata tab (no tkinter here).

Same split as the rest of the interface: this object decides what a click means,
which job to run and what the numbers are; the panel only paints it.  That is what
makes the tab testable without a display, and it keeps the "what will be removed"
answer in one place, used by both the preview and the worker job.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from phantom.core.errors import MetadataError
from phantom.core.progress import ProgressFn, noop_progress
from phantom.metadata import cleaner as meta_cleaner
from phantom.metadata import inspector as meta_inspector
from phantom.metadata import report as meta_report
from phantom.metadata.models import (
    MetadataCategory,
    MetadataField,
    MetadataReport,
)

#: How many field rows the results card builds at once.  Beyond this the list is
#: truncated with a counter instead of freezing the main thread on a file with
#: thousands of text chunks.
MAX_VISIBLE_FIELDS = 150


class MetadataService(Protocol):
    """The slice of :mod:`phantom.metadata` the view model needs."""

    def inspect(
        self,
        path: str,
        *,
        cancel: threading.Event | None = None,
        progress: ProgressFn = ...,
    ) -> MetadataReport: ...

    def clean(
        self,
        source: str,
        output: str,
        selection: frozenset[str],
        *,
        overwrite: bool = False,
        cancel: threading.Event | None = None,
        progress: ProgressFn = ...,
    ) -> meta_cleaner.CleaningOutcome: ...


class CoreMetadataService:
    """Thin adapter over the metadata package, so tests can swap it out."""

    def inspect(
        self,
        path: str,
        *,
        cancel: threading.Event | None = None,
        progress: ProgressFn = noop_progress,
    ) -> MetadataReport:
        return meta_inspector.inspect_path(path, cancel=cancel, progress=progress)

    def clean(
        self,
        source: str,
        output: str,
        selection: frozenset[str],
        *,
        overwrite: bool = False,
        cancel: threading.Event | None = None,
        progress: ProgressFn = noop_progress,
    ) -> meta_cleaner.CleaningOutcome:
        return meta_cleaner.clean(
            source,
            output,
            selection,
            overwrite=overwrite,
            cancel=cancel,
            progress=progress,
        )


@dataclass(slots=True)
class MetadataViewModel:
    """State of the metadata tab: the file, its report, the selection, the result."""

    service: MetadataService = field(default_factory=CoreMetadataService)
    path: str = ""
    report: MetadataReport | None = None
    output_path: str = ""
    selection: set[str] = field(default_factory=set)
    outcome: meta_cleaner.CleaningOutcome | None = None
    category: MetadataCategory | None = None
    only_removable: bool = False
    search: str = ""

    # -- state ---------------------------------------------------------
    def set_path(self, path: str) -> None:
        """Point the tab at a new file; the previous report no longer applies."""
        self.path = path
        self.report = None
        self.selection.clear()
        self.outcome = None
        self.output_path = self.suggest_output(path)

    def apply_report(self, report: MetadataReport) -> None:
        """Install a fresh inspection and preselect the safe personal fields."""
        self.report = report
        self.selection = set(meta_cleaner.default_selection(report))
        self.outcome = None

    def clear(self) -> None:
        self.path = ""
        self.report = None
        self.selection.clear()
        self.outcome = None
        self.output_path = ""

    @property
    def ready(self) -> bool:
        return self.report is not None

    @staticmethod
    def suggest_output(path: str) -> str:
        """``photo.png`` → ``photo-clean.png`` in the same folder."""
        if not path:
            return ""
        directory, name = os.path.split(path)
        stem, extension = os.path.splitext(name)
        return os.path.join(directory, f"{stem}-clean{extension}")

    # -- selection -----------------------------------------------------
    def toggle(self, key: str) -> bool:
        """Flip one field; returns its new state.  Protected fields never toggle."""
        report = self.report
        if report is None:
            return False
        item = report.find(key)
        if item is None or not item.removable:
            return key in self.selection
        if key in self.selection:
            self.selection.discard(key)
            return False
        self.selection.add(key)
        return True

    def select_all_removable(self) -> int:
        report = self.report
        if report is None:
            return 0
        self.selection = set(report.removable_keys)
        return len(self.selection)

    def select_personal(self) -> int:
        report = self.report
        if report is None:
            return 0
        self.selection = set(meta_cleaner.default_selection(report))
        return len(self.selection)

    def clear_selection(self) -> None:
        self.selection.clear()

    def is_selected(self, key: str) -> bool:
        return key in self.selection

    # -- filtering -----------------------------------------------------
    def visible_fields(self) -> list[MetadataField]:
        report = self.report
        if report is None:
            return []
        needle = self.search.strip().lower()
        rows: list[MetadataField] = []
        for item in report.sorted_fields():
            if self.category is not None and item.category is not self.category:
                continue
            if self.only_removable and not item.removable:
                continue
            if needle and needle not in item.label.lower() and needle not in item.value.lower():
                continue
            rows.append(item)
        return rows

    @property
    def truncated_fields(self) -> int:
        return max(0, len(self.visible_fields()) - MAX_VISIBLE_FIELDS)

    # -- planning ------------------------------------------------------
    def plan(self) -> meta_cleaner.CleaningPlan | None:
        report = self.report
        if report is None:
            return None
        return meta_cleaner.plan(report, frozenset(self.selection))

    def plan_summary(self) -> dict[str, int]:
        report = self.report
        decision = self.plan()
        if report is None or decision is None:
            return {"remove": 0, "kept": 0, "blocked": 0, "personal": 0}
        return {
            "remove": len(decision.remove),
            "kept": len(decision.keep),
            "blocked": len(decision.blocked),
            "personal": report.counts.personal,
        }

    # -- jobs ----------------------------------------------------------
    def make_inspect_job(
        self, path: str
    ) -> Callable[[ProgressFn, threading.Event], MetadataReport]:
        service = self.service

        def job(progress: ProgressFn, cancel: threading.Event) -> MetadataReport:
            return service.inspect(path, cancel=cancel, progress=progress)

        return job

    def make_clean_job(
        self, *, overwrite: bool = False
    ) -> Callable[[ProgressFn, threading.Event], meta_cleaner.CleaningOutcome]:
        if not self.path:
            raise MetadataError("no file is selected")
        if not self.output_path:
            raise MetadataError("no output path is selected")
        if not self.selection:
            raise MetadataError("no field is selected")
        source = self.path
        output = self.output_path
        selection = frozenset(self.selection)
        service = self.service

        def job(
            progress: ProgressFn, cancel: threading.Event
        ) -> meta_cleaner.CleaningOutcome:
            return service.clean(
                source,
                output,
                selection,
                overwrite=overwrite,
                cancel=cancel,
                progress=progress,
            )

        return job

    # -- exports -------------------------------------------------------
    def inspection_text(self, texts: meta_report.Texts | None = None) -> str:
        """The user's text report in the current language (JSON stays English)."""
        if self.report is None:
            return ""
        return meta_report.inspection_text(self.report, texts)

    def json_text(self) -> str:
        if self.report is None:
            return ""
        decision = self.plan()
        return meta_report.report_json(self.report, decision, self.outcome)

    def summary_line(self, texts: meta_report.Texts | None = None) -> str:
        if self.report is None:
            return ""
        return meta_report.summary_line(self.report, texts)

    def cleaning_text(self, texts: meta_report.Texts | None = None) -> str:
        decision = self.plan()
        if decision is None:
            return ""
        return meta_report.cleaning_text(decision, self.outcome, texts)

    # -- counts the panel shows -----------------------------------------
    def personal_removable(self) -> int:
        """How many personal fields the cleaner would preselect (0 = nothing to do)."""
        report = self.report
        if report is None:
            return 0
        return len(meta_cleaner.default_selection(report))

    def protected_personal(self) -> int:
        """Personal fields the cleaner may not remove: they live in a kept block.

        A packet with a copyright line in it, a signed block, an unreadable block:
        the data is there, it just cannot be taken out whole.  The plan card says
        so instead of claiming that there is nothing to clean.
        """
        report = self.report
        if report is None:
            return 0
        return sum(
            1
            for item in report.fields
            if item.sensitive and not item.removable and not item.derived
        )

    def removable_total(self) -> int:
        report = self.report
        return 0 if report is None else len(report.removable_keys)

    def save_allowed(self) -> bool:
        """Whether the save button may do anything, and whether it would do it.

        A selection that survives from a previous report, or one that only names
        fields the policy keeps, used to leave the button enabled; pressing it then
        failed with "no removable field was selected".  The button now follows the
        plan it would execute.
        """
        if self.report is None or not self.selection:
            return False
        decision = self.plan()
        if decision is None or not decision.allowed:
            return False
        return bool(decision.remove)

    def hash_block(self) -> str:
        """The full SHA-256 values, for the clipboard (never truncated)."""
        outcome = self.outcome
        if outcome is None:
            return ""
        right = outcome.comparison.right
        lines = [
            f"sha256 (whole file, source): {outcome.source_sha256}",
            f"sha256 (whole file, output): {outcome.output_sha256}",
            "sha256 (decoded content): "
            + (right.content_sha256 if right is not None else "not checked"),
        ]
        return "\n".join(lines)

    def apply_outcome(self, outcome: meta_cleaner.CleaningOutcome) -> None:
        self.outcome = outcome
        self.report = outcome.report_after
        self.output_path = outcome.output
        # The selection belonged to the file that was read before the clean; the
        # report now describes the cleaned copy, so it starts from its own default
        # instead of naming fields that no longer exist.
        self.selection = set(meta_cleaner.default_selection(outcome.report_after))

    def with_filter(
        self, *, category: MetadataCategory | None = None, only_removable: bool | None = None
    ) -> MetadataViewModel:
        """A copy with a different filter, for tests and for the filter widgets."""
        return replace(
            self,
            category=self.category if category is None else category,
            only_removable=self.only_removable if only_removable is None else only_removable,
        )

    def as_dict(self) -> dict[str, Any]:
        """Snapshot for diagnostics: no values, only counts and keys."""
        counts = self.report.counts.to_dict() if self.report is not None else {}
        return {
            "path": self.path,
            "format": self.report.fmt.value if self.report is not None else "",
            "counts": counts,
            "selected": len(self.selection),
            "output": self.output_path,
            "outcome": self.outcome is not None,
        }
