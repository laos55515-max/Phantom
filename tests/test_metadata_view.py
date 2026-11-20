"""The metadata tab's logic, without a display: selection, planning, jobs, exports.

The panel itself needs Tk and is exercised in ``test_gui.py``; everything a click
*means* lives in :class:`phantom.ui.metadata_view.MetadataViewModel` and is tested
here, which is what keeps the interesting half of the tab testable on any machine.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

import metadata_samples as samples
from phantom.core.errors import MetadataError
from phantom.core.progress import ProgressFn
from phantom.i18n import load_locale
from phantom.metadata import cleaner, inspector
from phantom.metadata.models import MetadataCategory, MetadataReport
from phantom.ui.metadata_view import MAX_VISIBLE_FIELDS, CoreMetadataService, MetadataViewModel

LOCALES = Path(__file__).resolve().parent.parent / "src" / "phantom" / "locales"
PANEL = Path(__file__).resolve().parent.parent / "src" / "phantom" / "ui" / "metadata_panel.py"


def _report(tmp_path: Path, name: str = "view.png") -> MetadataReport:
    return inspector.inspect_path(str(samples.personal_png(tmp_path / name)))


def _loaded(tmp_path: Path, name: str = "view.png") -> tuple[MetadataViewModel, Path]:
    path = samples.personal_png(tmp_path / name)
    view = MetadataViewModel()
    view.set_path(str(path))
    view.apply_report(inspector.inspect_path(str(path)))
    return view, path


class RecordingService:
    """A service that records the arguments the jobs were built with."""

    def __init__(self) -> None:
        self.inspected: list[tuple[str, threading.Event | None]] = []
        self.cleaned: list[tuple[str, str, frozenset[str], bool]] = []

    def inspect(
        self,
        path: str,
        *,
        cancel: threading.Event | None = None,
        progress: ProgressFn | None = None,
    ) -> MetadataReport:
        self.inspected.append((path, cancel))
        return inspector.inspect_path(path)

    def clean(
        self,
        source: str,
        output: str,
        selection: frozenset[str],
        *,
        overwrite: bool = False,
        cancel: threading.Event | None = None,
        progress: ProgressFn | None = None,
    ) -> cleaner.CleaningOutcome:
        self.cleaned.append((source, output, selection, overwrite))
        return cleaner.clean(source, output, selection, overwrite=overwrite, cancel=cancel)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def test_choosing_a_file_proposes_a_clean_copy_beside_it(tmp_path: Path) -> None:
    view = MetadataViewModel()
    path = samples.personal_png(tmp_path / "holiday.png")

    view.set_path(str(path))

    assert view.path == str(path)
    assert view.output_path == str(tmp_path / "holiday-clean.png")
    assert view.ready is False
    assert view.report is None


def test_a_new_file_drops_the_previous_report_and_selection(tmp_path: Path) -> None:
    view, _path = _loaded(tmp_path)
    assert view.selection

    view.set_path(str(samples.personal_wav(tmp_path / "other.wav")))

    assert view.report is None
    assert view.selection == set()
    assert view.outcome is None


def test_installing_a_report_preselects_the_safe_personal_fields(tmp_path: Path) -> None:
    path = samples.personal_png(tmp_path / "select.png")
    view = MetadataViewModel()
    view.set_path(str(path))

    view.apply_report(inspector.inspect_path(str(path)))

    assert view.ready is True
    assert "png.gps.0x0002" in view.selection
    assert "png.text.tEXt.Copyright" not in view.selection
    assert "png.text.tEXt.Author" not in view.selection, "attribution is offered unticked"


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------
def test_toggling_a_protected_field_does_nothing(tmp_path: Path) -> None:
    view, _path = _loaded(tmp_path, "protected.png")

    assert view.toggle("png.text.tEXt.Copyright") is False
    assert "png.text.tEXt.Copyright" not in view.selection


def test_toggling_an_editable_field_flips_it(tmp_path: Path) -> None:
    view, _path = _loaded(tmp_path, "toggle.png")
    view.clear_selection()

    assert view.toggle("png.text.tEXt.Comment") is True
    assert view.is_selected("png.text.tEXt.Comment") is True
    assert view.toggle("png.text.tEXt.Comment") is False
    assert view.is_selected("png.text.tEXt.Comment") is False


def test_select_all_personal_and_clear(tmp_path: Path) -> None:
    view, path = _loaded(tmp_path, "buttons.png")
    report = inspector.inspect_path(str(path))

    assert view.select_all_removable() == len(report.removable_keys)
    assert view.selection == set(report.removable_keys)
    assert "png.text.tEXt.Author" in view.selection

    assert view.select_personal() == len(cleaner.default_selection(report))
    assert "png.text.tEXt.Author" not in view.selection

    view.clear_selection()
    assert view.selection == set()


# ---------------------------------------------------------------------------
# filtering
# ---------------------------------------------------------------------------
def test_filtering_by_category_and_removability(tmp_path: Path) -> None:
    view, _path = _loaded(tmp_path, "filter.png")

    view.category = MetadataCategory.PERSONAL
    assert all(item.category is MetadataCategory.PERSONAL for item in view.visible_fields())

    view.category = None
    view.only_removable = True
    assert all(item.removable for item in view.visible_fields())

    view.only_removable = False
    assert len(view.visible_fields()) == len(view.report.fields if view.report else ())


def test_search_matches_labels_and_values(tmp_path: Path) -> None:
    view, _path = _loaded(tmp_path, "search.png")

    view.search = "serial"
    assert [item.key for item in view.visible_fields()] == ["png.exif.0xa431"]

    view.search = "hunter2"
    assert [item.key for item in view.visible_fields()] == ["png.text.tEXt.Comment"]

    view.search = "nothing-matches-this"
    assert view.visible_fields() == []


def test_the_visible_list_is_capped_and_the_rest_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view, _path = _loaded(tmp_path, "cap.png")
    monkeypatch.setattr("phantom.ui.metadata_view.MAX_VISIBLE_FIELDS", 3)

    assert len(view.visible_fields()) == len(view.report.fields if view.report else ())
    assert view.truncated_fields == len(view.report.fields if view.report else ()) - 3
    assert MAX_VISIBLE_FIELDS >= 100


def test_with_filter_returns_a_copy(tmp_path: Path) -> None:
    view, _path = _loaded(tmp_path, "copy.png")

    filtered = view.with_filter(category=MetadataCategory.RIGHTS, only_removable=False)

    assert filtered is not view
    assert filtered.category is MetadataCategory.RIGHTS
    assert view.category is None
    assert filtered.selection == view.selection


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------
def test_the_plan_summary_counts_removed_kept_and_blocked(tmp_path: Path) -> None:
    view, path = _loaded(tmp_path, "summary.png")
    report = inspector.inspect_path(str(path))
    view.clear_selection()
    view.toggle("png.text.tEXt.Comment")
    view.toggle("png.text.tEXt.Copyright")  # protected: refused, stays out

    summary = view.plan_summary()

    entries = [item for item in report.fields if not item.derived]
    assert summary["remove"] == 1
    assert summary["kept"] == len(entries) - 1, "a derived view is neither removed nor kept"
    assert summary["blocked"] == 0
    assert summary["personal"] == report.counts.personal


def test_the_plan_is_blocked_for_a_signed_file(tmp_path: Path) -> None:
    path = samples.signed_png(tmp_path / "signed.png")
    view = MetadataViewModel()
    view.set_path(str(path))
    view.apply_report(inspector.inspect_path(str(path)))

    decision = view.plan()

    assert decision is not None
    assert decision.allowed is False
    assert decision.reason == "signed_media"


def test_a_plan_preview_is_produced_from_the_selection(tmp_path: Path) -> None:
    view, _path = _loaded(tmp_path, "preview.png")

    text = view.cleaning_text()

    assert "PHANTOM — cleaning plan" in text
    assert "will be removed" in text


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------
def test_the_inspect_job_calls_the_service_with_the_cancel_event(tmp_path: Path) -> None:
    service = RecordingService()
    view = MetadataViewModel(service=service)
    path = samples.personal_png(tmp_path / "job.png")
    cancel = threading.Event()

    report = view.make_inspect_job(str(path))(lambda *_args: None, cancel)

    assert report.fmt.value == "png"
    assert service.inspected == [(str(path), cancel)]


def test_a_clean_job_needs_a_file_a_path_and_a_selection(tmp_path: Path) -> None:
    view = MetadataViewModel()

    with pytest.raises(MetadataError):
        view.make_clean_job()
    view.set_path(str(samples.personal_png(tmp_path / "job.png")))
    with pytest.raises(MetadataError):
        view.make_clean_job()
    view.output_path = str(tmp_path / "out.png")
    with pytest.raises(MetadataError):
        view.make_clean_job()
    view.selection = {"png.text.tEXt.Comment"}
    assert view.make_clean_job() is not None


def test_the_clean_job_passes_the_selection_and_the_overwrite_flag(tmp_path: Path) -> None:
    service = RecordingService()
    view = MetadataViewModel(service=service)
    path = samples.personal_png(tmp_path / "clean.png")
    view.set_path(str(path))
    view.apply_report(inspector.inspect_path(str(path)))
    view.clear_selection()
    view.toggle("png.text.tEXt.Comment")
    view.output_path = str(tmp_path / "clean-out.png")

    outcome = view.make_clean_job(overwrite=True)(lambda *_args: None, threading.Event())

    assert outcome.verified is True
    source, output, selection, overwrite = service.cleaned[0]
    assert (source, output, overwrite) == (str(path), str(tmp_path / "clean-out.png"), True)
    assert selection == frozenset({"png.text.tEXt.Comment"})


def test_an_outcome_updates_the_view(tmp_path: Path) -> None:
    view, _path = _loaded(tmp_path, "outcome.png")
    view.clear_selection()
    view.toggle("png.text.tEXt.Comment")
    view.output_path = str(tmp_path / "outcome-out.png")
    outcome = view.make_clean_job()(lambda *_args: None, threading.Event())

    view.apply_outcome(outcome)

    assert view.outcome is outcome
    assert view.report is not None and view.report.find("png.text.tEXt.Comment") is None
    assert view.output_path == outcome.output


def test_an_outcome_reseeds_the_selection_from_the_new_report(tmp_path: Path) -> None:
    """The selection named the file that was read; the result is a different one."""
    view, _path = _loaded(tmp_path, "reseed.png")
    view.clear_selection()
    view.toggle("png.text.tEXt.Comment")
    view.output_path = str(tmp_path / "reseed-out.png")
    outcome = view.make_clean_job()(lambda *_args: None, threading.Event())

    view.apply_outcome(outcome)

    assert view.selection == set(cleaner.default_selection(outcome.report_after))
    assert "png.text.tEXt.Comment" not in view.selection, "the removed field is gone"
    # Whatever is enabled is exactly what the plan would execute on the new report.
    decision = view.plan()
    assert view.save_allowed() is (decision is not None and bool(decision.remove))


def test_saving_is_refused_for_a_selection_that_would_remove_nothing(tmp_path: Path) -> None:
    """A packet that also carries rights is not removable: the button follows the plan."""
    path = samples.xmp_png(
        tmp_path / "rights.png",
        {"dc:rights": "(c) 2024 Ada", "photoshop:City": "Lubny"},
    )
    view = MetadataViewModel()
    view.set_path(str(path))
    view.apply_report(inspector.inspect_path(str(path)))
    assert view.selection == set()

    # A selection can be set by hand, by a stale report or by a future caller;
    # the button must not offer an operation the cleaner would then refuse.
    view.selection = {"png.xmp"}

    assert view.plan() is not None and view.plan().blocked
    assert view.save_allowed() is False


def test_personal_data_the_cleaner_may_not_touch_is_counted(tmp_path: Path) -> None:
    path = samples.xmp_png(
        tmp_path / "locked.png",
        {"dc:rights": "(c) 2024 Ada", "photoshop:City": "Lubny"},
    )
    view = MetadataViewModel()
    view.set_path(str(path))
    view.apply_report(inspector.inspect_path(str(path)))

    assert view.personal_removable() == 0
    assert view.protected_personal() == 1, "the city is personal, and it is locked"
    assert view.save_allowed() is False


# ---------------------------------------------------------------------------
# exports
# ---------------------------------------------------------------------------
def test_exports_are_empty_without_a_report() -> None:
    view = MetadataViewModel()

    assert view.inspection_text() == ""
    assert view.json_text() == ""
    assert view.cleaning_text() == ""
    assert view.summary_line() == ""


def test_the_exports_describe_the_same_report(tmp_path: Path) -> None:
    view, _path = _loaded(tmp_path, "export.png")

    text = view.inspection_text()
    payload = json.loads(view.json_text())

    assert payload["report"]["path"] == view.path
    assert payload["report"]["format"] == "png"
    assert "PHANTOM — metadata report" in text
    assert payload["report"]["sha256"] in text


def test_the_diagnostics_snapshot_holds_no_personal_values(tmp_path: Path) -> None:
    view, _path = _loaded(tmp_path, "diag.png")

    snapshot = view.as_dict()
    rendered = json.dumps(snapshot)

    assert snapshot["format"] == "png"
    assert snapshot["selected"] > 0
    for value in ("Ada Lovelace", "hunter2", "50.45"):
        assert value not in rendered


# ---------------------------------------------------------------------------
# the core service
# ---------------------------------------------------------------------------
def test_the_core_service_inspects_and_cleans(tmp_path: Path) -> None:
    service = CoreMetadataService()
    source = samples.personal_flac(tmp_path / "service.flac")
    output = tmp_path / "service-clean.flac"

    found = service.inspect(str(source))
    outcome = service.clean(
        str(source), str(output), cleaner.default_selection(found), overwrite=False
    )

    assert found.fmt.value == "flac"
    assert outcome.verified is True
    assert output.exists()


# ---------------------------------------------------------------------------
# the tab's own wiring
# ---------------------------------------------------------------------------
def test_every_metadata_string_the_panel_uses_exists_in_all_locales() -> None:
    """A missing key would show a raw ``meta_…`` identifier to the user."""
    source = PANEL.read_text(encoding="utf-8")
    used = set()
    for pattern in (r'"([a-z][a-z0-9_.]*)"', r"'([a-z][a-z0-9_.]*?)'"):
        used.update(re.findall(pattern, source))
    keys = {
        key
        for key in used
        if key.startswith(("meta_", "meta.", "tab_", "stage.", "err."))
    }
    assert len(keys) > 40, "the scan found too few keys to be meaningful"

    for code in ("en", "ru", "uk"):
        locale = load_locale(code, LOCALES)
        missing = sorted(key for key in keys if key not in locale)
        assert not missing, f"{code} is missing {missing}"


def test_the_metadata_tab_can_be_the_saved_tab() -> None:
    from phantom.ui.settings import AppSettings

    assert AppSettings(last_tab="metadata").normalized().last_tab == "metadata"
    assert AppSettings(last_tab="nonsense").normalized().last_tab == "embed"
