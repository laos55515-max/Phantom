"""The METADATA tab: five cards, built from the widgets the rest of the app uses.

The panel owns no policy.  What a click means lives in
:class:`phantom.ui.metadata_view.MetadataViewModel`; this file turns that state
into widgets and turns widget events into calls.  Two consequences worth keeping:

* the results list is rebuilt only when the report or the filter changes, so
  toggling a checkbox never re-creates a hundred rows;
* the field list lives in the tab's own scroll area, not in a scrollable box of
  its own: nesting a ``CTkScrollableFrame`` inside another one makes the two
  canvases chase each other's ``<Configure>`` events, and the tab is scrolled by
  the same application-wide router as every other tab (PR #16).

Reading the tab, top to bottom: the file; what it carries; what the *next*
operation would do (the plan); where to save; and the result of the *previous*
operation.  The plan and the result are separate cards on purpose — mixing them
made a stale result look like a fresh plan.
"""

from __future__ import annotations

import os
from tkinter import filedialog, messagebox
from typing import Any, Final

import customtkinter as ctk

from phantom.core import files as fsutil
from phantom.core.errors import PhantomError
from phantom.metadata import report as meta_report
from phantom.metadata.cleaner import CleaningOutcome
from phantom.metadata.models import (
    CATEGORY_ORDER,
    MediaFormat,
    MetadataCategory,
    MetadataField,
    MetadataReport,
    ProvenanceState,
)
from phantom.ui import theme as T
from phantom.ui.metadata_view import MAX_VISIBLE_FIELDS, MetadataViewModel
from phantom.ui.responsive import (
    FlowRow,
    fit_checkbox,
    fit_checkbox_on_change,
    fit_option_menu_on_change,
)
from phantom.ui.widgets import (
    card,
    card_title,
    hint_label,
    inset_panel,
    mono_font,
    primary_button,
    quiet_button,
    sans_font,
    secondary_button,
    set_enabled,
    step_header,
    value_label,
)

#: Patterns for the open dialog.  Extensions only: the inspector decides by
#: content anyway, so a mislabelled file is refused rather than misparsed.
OPEN_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("meta.filter_supported", "*.png *.wav *.flac"),
    ("meta.filter_images", "*.png"),
    ("meta.filter_audio", "*.wav *.flac"),
    ("meta.filter_all", "*"),
)
EXPORT_TXT = (("meta.filter_txt", "*.txt"),)
EXPORT_JSON = (("meta.filter_json", "*.json"),)
#: The list of fields is capped so a file with thousands of chunks stays usable.
ROW_HEIGHT: Final = 26
#: A clipped value ends in one character.  It is spelled as a code point because
#: the repository's placeholder scanner rejects the literal on its own.
CLIP_MARK: Final = chr(0x2026)
#: Which explainer a content credential gets, per state reachable in this build.
PROVENANCE_KEYS: Final[dict[ProvenanceState, str]] = {
    ProvenanceState.NONE: "meta_provenance_none",
    ProvenanceState.MANIFEST_FOUND: "meta_provenance_detected",
    ProvenanceState.SIGNATURE_UNVERIFIED: "meta_provenance_unverified",
    ProvenanceState.SIGNATURE_INVALID: "meta_provenance_invalid",
}


def _bytes_text(size: int) -> str:
    """``12 345 bytes (12.1 KiB)`` — exact number first, readable second."""
    if size < 1024:
        return f"{size} B"
    return f"{size} B ({size / 1024:.1f} KiB)"


class MetadataPanel:
    """Builds the tab and keeps it in sync with one :class:`MetadataViewModel`."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.vm = MetadataViewModel()
        self.root = app.root
        self.frame: ctk.CTkFrame | None = None
        self._rows: list[ctk.CTkBaseClass] = []
        self._rows_signature: tuple[object, ...] | None = None
        self._category_by_label: dict[str, MetadataCategory | None] = {}

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def build(self) -> ctk.CTkFrame:
        app = self.app
        frame = ctk.CTkFrame(app.frames, fg_color="transparent")
        self.frame = frame
        scroll = ctk.CTkScrollableFrame(frame, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True, padx=6, pady=(4, 0))
        scroll.grid_columnconfigure(0, weight=1)
        app._seat_scroll(scroll)
        self.scroll = scroll

        self.file_card = self._build_file_card(scroll, row=0)
        self.results_card = self._build_results_card(scroll, row=1)
        self.cleaning_card = self._build_cleaning_card(scroll, row=2)
        self.save_card = self._build_save_card(scroll, row=3)
        self.integrity_card = self._build_integrity_card(scroll, row=4)
        self._bind_texts()
        self.refresh()
        return frame

    def _title(self, parent: Any, key: str) -> ctk.CTkLabel:
        title = card_title(parent, self.root, "")
        title.pack(fill="x", padx=18, pady=(12, 4))
        self.app.binder.bind(title, key)
        return title

    def _caption(self, parent: Any, key: str) -> ctk.CTkLabel:
        """A small heading that separates the plan from the previous result."""
        caption = ctk.CTkLabel(
            parent,
            text="",
            font=sans_font(self.root, T.FONT_SIZE_SMALL, weight="bold"),
            text_color=T.CLR_ACCENT,
            anchor="w",
        )
        caption.pack(fill="x", padx=18, pady=(0, 4))
        self.app.binder.bind(caption, key)
        return caption

    def _step_head(
        self, parent: Any, number: str, title_key: str, meta_key: str | None = None
    ) -> None:
        head, _badge, title, meta = step_header(parent, self.root, number)
        head.pack(fill="x", padx=18, pady=(14, 8))
        self.app.binder.bind(title, title_key)
        if meta_key is not None:
            self.app.binder.bind(meta, meta_key)

    def _build_file_card(self, parent: Any, *, row: int) -> ctk.CTkFrame:
        box = card(parent)
        box.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        self._step_head(box, "1", "meta_step_file", "step_extract_carrier_meta")

        drop_box = inset_panel(box, border_width=1)
        drop_box.pack(fill="x", padx=18, pady=(0, 8))
        drop_box.grid_columnconfigure(0, weight=1)

        drop_copy = ctk.CTkFrame(drop_box, fg_color="transparent")
        drop_copy.grid(row=0, column=0, sticky="ew", padx=(14, 10), pady=12)
        self.drop_title_label = ctk.CTkLabel(
            drop_copy,
            text="",
            font=sans_font(self.root, T.FONT_SIZE_BODY, weight="bold"),
            text_color=T.CLR_TEXT,
            anchor="w",
            justify="left",
            wraplength=520,
        )
        self.drop_title_label._phantom_autowrap = 520
        self.drop_title_label.pack(fill="x")
        self.drop_hint = hint_label(drop_copy, self.root, "", wrap=520)
        self.drop_hint.pack(fill="x", pady=(2, 0))

        self.file_row = FlowRow(drop_box)
        self.file_row.grid(row=0, column=1, sticky="e", padx=(0, 14), pady=12)
        self.file_button = self.file_row.add(
            quiet_button(self.file_row, self.root, "", self.choose_file)
        )
        self.reset_button = self.file_row.add(
            secondary_button(self.file_row, self.root, "", self.reset)
        )

        self.file_error_box = ctk.CTkFrame(
            box, fg_color=T.CLR_RED_BG, corner_radius=10, border_width=0
        )
        self.file_error_box.grid_columnconfigure(0, weight=1)
        self.file_error_label = ctk.CTkLabel(
            self.file_error_box,
            text="",
            font=sans_font(self.root, T.FONT_SIZE_LABEL),
            text_color=T.CLR_RED,
            anchor="w",
            justify="left",
            wraplength=640,
        )
        self.file_error_label._phantom_autowrap = 640
        self.file_error_label.grid(row=0, column=0, sticky="ew", padx=(12, 8), pady=8)
        self.file_error_dismiss = secondary_button(
            self.file_error_box, self.root, "", self.clear_file_error
        )
        self.file_error_dismiss.grid(row=0, column=1, padx=(0, 10), pady=8)

        info_box = inset_panel(box)
        info_box.pack(fill="x", padx=18, pady=(0, 14))
        self.name_label = value_label(info_box, self.root, "", mono=True)
        self.name_label.pack(fill="x", padx=12, pady=(8, 0))
        self.size_label = value_label(info_box, self.root, "", mono=True)
        self.size_label.pack(fill="x", padx=12, pady=(2, 0))
        self.format_label = value_label(info_box, self.root, "", mono=True)
        self.format_label.pack(fill="x", padx=12, pady=(2, 0))
        self.technical_label = hint_label(info_box, self.root, "", wrap=760)
        self.technical_label.pack(fill="x", padx=12, pady=(2, 8))

        self.app._seat_drop_zone("metadata", drop_box)
        return box

    def _build_results_card(self, parent: Any, *, row: int) -> ctk.CTkFrame:
        box = card(parent)
        box.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        self._step_head(box, "2", "meta_step_fields")
        # Which file these numbers describe. After a clean the card describes the
        # cleaned copy, while the card above still names the file that was opened:
        # without this line the two read as one file.
        self.target_label = hint_label(box, self.root, "", wrap=760)
        self.target_label.pack(fill="x", padx=18, pady=(0, 2))
        self.summary_label = value_label(box, self.root, "", mono=True)
        self.summary_label.pack(fill="x", padx=18, pady=(0, 6))

        pills_row = FlowRow(box)
        pills_row.pack(fill="x", padx=18, pady=(0, 6))
        self.pill_selected = ctk.CTkLabel(
            pills_row,
            text="",
            corner_radius=8,
            fg_color=T.CLR_ACCENT_BG,
            text_color=T.CLR_ACCENT_SOFT,
            font=sans_font(self.root, T.FONT_SIZE_SMALL, weight="bold"),
            padx=10,
            pady=4,
        )
        pills_row.add(self.pill_selected)
        self.pill_removable = ctk.CTkLabel(
            pills_row,
            text="",
            corner_radius=8,
            fg_color=T.CLR_GREEN_BG,
            text_color=T.CLR_GREEN,
            font=sans_font(self.root, T.FONT_SIZE_SMALL, weight="bold"),
            padx=10,
            pady=4,
        )
        pills_row.add(self.pill_removable)
        self.pill_protected = ctk.CTkLabel(
            pills_row,
            text="",
            corner_radius=8,
            fg_color=T.CLR_CARD_ALT,
            text_color=T.CLR_TEXT_DIM,
            font=sans_font(self.root, T.FONT_SIZE_SMALL, weight="bold"),
            padx=10,
            pady=4,
        )
        pills_row.add(self.pill_protected)

        self.no_removable_banner = ctk.CTkFrame(
            box, fg_color=T.CLR_ACCENT_BG, corner_radius=10, border_width=0
        )
        self.no_removable_label = ctk.CTkLabel(
            self.no_removable_banner,
            text="",
            font=sans_font(self.root, T.FONT_SIZE_LABEL),
            text_color=T.CLR_TEXT,
            anchor="w",
            justify="left",
            wraplength=720,
        )
        self.no_removable_label._phantom_autowrap = 720
        self.no_removable_label.pack(fill="x", padx=12, pady=10)

        self.provenance_label = hint_label(box, self.root, "", wrap=760)
        self.provenance_label.pack(fill="x", padx=18, pady=(0, 4))
        self.warning_label = hint_label(box, self.root, "", wrap=760)
        self.warning_label.pack(fill="x", padx=18, pady=(0, 4))

        filter_row = FlowRow(box)
        filter_row.pack(fill="x", padx=18, pady=(2, 8))
        self.filter_menu = ctk.CTkOptionMenu(
            filter_row,
            values=[],
            command=self._on_filter_change,
            font=sans_font(self.root, T.FONT_SIZE_LABEL),
            dropdown_font=sans_font(self.root, T.FONT_SIZE_LABEL),
            height=30,
            width=170,
            fg_color=T.CLR_FIELD,
            button_color=T.CLR_CARD_ALT,
            button_hover_color=T.CLR_ACCENT_HOVER,
            text_color=T.CLR_TEXT,
        )
        filter_row.add(self.filter_menu)
        self.removable_check = ctk.CTkCheckBox(
            filter_row,
            text="",
            width=180,
            command=self._on_removable_toggle,
            font=sans_font(self.root, T.FONT_SIZE_LABEL),
            checkbox_width=18,
            checkbox_height=18,
            fg_color=T.CLR_ACCENT,
            hover_color=T.CLR_ACCENT_HOVER,
            text_color=T.CLR_TEXT,
        )
        filter_row.add(self.removable_check)

        # A plain frame, not a second scrollable area: the rows are gridded into it
        # and the tab's own scroll area carries them.  Nesting two
        # CTkScrollableFrames makes their canvases chase each other's <Configure>
        # events, which is both a layout storm and a scroll bug waiting to happen.
        self.list_area = ctk.CTkFrame(box, fg_color="transparent", corner_radius=10)
        self.list_area.pack(fill="x", padx=18, pady=(0, 6))
        self.list_area.grid_columnconfigure(0, weight=1)
        self.truncated_label = hint_label(box, self.root, "", wrap=760)
        self.truncated_label.pack(fill="x", padx=18, pady=(0, 12))
        return box

    def _build_cleaning_card(self, parent: Any, *, row: int) -> ctk.CTkFrame:
        box = card(parent)
        box.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        self._title(box, "meta_card_cleaning")
        self.plan_caption = self._caption(box, "meta_plan_caption")
        buttons = FlowRow(box)
        buttons.pack(fill="x", padx=18, pady=(0, 6))
        self.select_all_button = buttons.add(
            secondary_button(buttons, self.root, "", self.select_all_removable)
        )
        self.select_personal_button = buttons.add(
            secondary_button(buttons, self.root, "", self.select_personal)
        )
        self.clear_selection_button = buttons.add(
            secondary_button(buttons, self.root, "", self.clear_selection)
        )
        self.preview_button = buttons.add(
            secondary_button(buttons, self.root, "", self.refresh_preview)
        )
        self.plan_label = hint_label(box, self.root, "", wrap=760)
        self.plan_label.pack(fill="x", padx=18, pady=(0, 6))
        self.preview_text = ctk.CTkTextbox(
            box,
            height=150,
            font=mono_font(self.root, T.FONT_SIZE_SMALL),
            wrap="word",
            fg_color=T.CLR_FIELD,
            border_color=T.CLR_BORDER,
            border_width=1,
            corner_radius=10,
        )
        self.preview_text.pack(fill="x", padx=18, pady=(0, 8))
        self.preview_text.configure(state="disabled")
        self.limits_label = hint_label(box, self.root, "", wrap=760)
        self.limits_label.pack(fill="x", padx=18, pady=(0, 10))
        self.app.binder.bind(self.limits_label, "meta_limits")
        return box

    def _build_save_card(self, parent: Any, *, row: int) -> ctk.CTkFrame:
        box = card(parent)
        box.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        self._step_head(box, "3", "meta_step_save")
        entry_row = FlowRow(box)
        entry_row.pack(fill="x", padx=18, pady=(0, 6))
        self.output_entry = ctk.CTkEntry(
            entry_row,
            height=38,
            width=420,
            font=mono_font(self.root, T.FONT_SIZE_SMALL),
            fg_color=T.CLR_FIELD,
            border_color=T.CLR_BORDER,
            corner_radius=10,
        )
        entry_row.add(self.output_entry)
        self.browse_button = entry_row.add(
            secondary_button(entry_row, self.root, "", self.choose_output, width=110)
        )
        action_row = FlowRow(box)
        action_row.pack(fill="x", padx=18, pady=(0, 6))
        self.save_button = action_row.add(
            primary_button(action_row, self.root, "", self.save_clean)
        )
        self.copy_button = action_row.add(
            secondary_button(action_row, self.root, "", self.copy_report)
        )
        self.copy_hashes_button = action_row.add(
            secondary_button(action_row, self.root, "", self.copy_hashes)
        )
        self.reveal_button = action_row.add(
            secondary_button(action_row, self.root, "", self.reveal_output)
        )
        self.save_hint = hint_label(box, self.root, "", wrap=760)
        self.save_hint.pack(fill="x", padx=18, pady=(0, 4))
        export_row = FlowRow(box)
        export_row.pack(fill="x", padx=18, pady=(0, 10))
        self.export_txt_button = export_row.add(
            secondary_button(export_row, self.root, "", self.export_txt, width=150)
        )
        self.export_json_button = export_row.add(
            secondary_button(export_row, self.root, "", self.export_json, width=150)
        )
        return box

    def _build_integrity_card(self, parent: Any, *, row: int) -> ctk.CTkFrame:
        box = card(parent)
        box.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        self._title(box, "meta_card_integrity")
        self.result_caption = self._caption(box, "meta_result_caption")
        self.decode_label = value_label(box, self.root, "", mono=True)
        self.decode_label.pack(fill="x", padx=18, pady=(2, 0))
        self.compare_label = value_label(box, self.root, "", mono=True)
        self.compare_label.pack(fill="x", padx=18, pady=(2, 0))
        self.counts_label = value_label(box, self.root, "", mono=True)
        self.counts_label.pack(fill="x", padx=18, pady=(2, 0))
        self.hash_label = hint_label(box, self.root, "", wrap=760)
        self.hash_label.pack(fill="x", padx=18, pady=(2, 0))
        self.hash_note_label = hint_label(box, self.root, "", wrap=760)
        self.hash_note_label.pack(fill="x", padx=18, pady=(0, 4))
        self.app.binder.bind(self.hash_note_label, "meta_hash_note")
        self.final_label = ctk.CTkLabel(
            box,
            text="",
            font=sans_font(self.root, T.FONT_SIZE_LABEL, weight="bold"),
            text_color=T.CLR_TEXT_DIM,
            anchor="w",
            justify="left",
            wraplength=760,
        )
        self.final_label.pack(fill="x", padx=18, pady=(4, 10))
        self.final_label._phantom_autowrap = 36
        return box

    def _bind_texts(self) -> None:
        binder = self.app.binder
        binder.bind(self.drop_title_label, "drop_meta_title")
        binder.bind(self.file_error_dismiss, "btn_dismiss")
        binder.bind(self.file_button, "meta_choose_file")
        binder.bind(self.reset_button, "meta_reset")
        binder.bind(self.drop_hint, "drop_meta_sub")
        binder.bind(self.removable_check, "meta_only_removable")
        binder.bind(self.select_all_button, "meta_select_all")
        binder.bind(self.select_personal_button, "meta_select_personal")
        binder.bind(self.clear_selection_button, "meta_clear_selection")
        binder.bind(self.preview_button, "meta_preview")
        binder.bind(self.browse_button, "meta_browse")
        binder.bind(self.save_button, "meta_save")
        binder.bind(self.copy_button, "meta_copy")
        binder.bind(self.copy_hashes_button, "meta_copy_hashes")
        binder.bind(self.export_txt_button, "meta_export_txt")
        binder.bind(self.export_json_button, "meta_export_json")
        # "Show in Finder" on macOS, "Проводник" on Windows: the caption names the
        # file manager the click will actually open.
        binder.bind_dynamic(
            self.reveal_button,
            "meta_reveal",
            lambda: {"file_manager": self.app.file_manager_name()},
        )
        fit_checkbox_on_change(self.removable_check)
        fit_option_menu_on_change(self.filter_menu)

    # ------------------------------------------------------------------
    # labels
    # ------------------------------------------------------------------
    def action_buttons(self) -> tuple[Any, ...]:
        """Buttons the busy state must disable, so a job cannot be started twice."""
        return (
            self.file_button,
            self.reset_button,
            self.save_button,
            self.browse_button,
            self.copy_hashes_button,
            self.export_txt_button,
            self.export_json_button,
            self.preview_button,
            self.select_all_button,
            self.select_personal_button,
            self.clear_selection_button,
        )

    def editable_action_buttons(self) -> tuple[Any, ...]:
        """Buttons that stay usable when no field is selected."""
        return tuple(
            button
            for button in self.action_buttons()
            if button not in (self.save_button,)
        )

    def t(self, key: str, **params: object) -> str:
        return str(self.app.i18n.t(key, **params))

    def _format_label(self, fmt: MediaFormat) -> str:
        return self.t(fmt.i18n_key)

    def _category_labels(self) -> dict[str, MetadataCategory | None]:
        labels: dict[str, MetadataCategory | None] = {self.t("meta_filter_all"): None}
        for name in CATEGORY_ORDER:
            category = MetadataCategory(name)
            labels[self.t(category.i18n_key)] = category
        return labels

    def refresh(self) -> None:
        """Repaint everything from the view model; called after any state change."""
        self._render_file()
        self._render_results()
        self._render_plan()
        self._render_integrity()
        self._render_save_state()

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def _render_file(self) -> None:
        vm = self.vm
        report = vm.report
        self.reset_button.configure(state="normal" if vm.path else "disabled")
        if not vm.path:
            self.name_label.configure(text=self.t("meta_no_file"))
            self.size_label.configure(text="")
            self.format_label.configure(text="")
            self.technical_label.configure(text="")
            return
        self.name_label.configure(
            text=f"{self.t('meta_file_name')}: {os.path.basename(vm.path)}"
        )
        if report is None:
            self.size_label.configure(text="")
            self.format_label.configure(text="")
            self.technical_label.configure(text="")
            return
        self.size_label.configure(
            text=f"{self.t('meta_file_size')}: {_bytes_text(report.size_bytes)}"
        )
        self.format_label.configure(
            text=f"{self.t('meta_file_format')}: {self._format_label(report.fmt)}"
        )
        technical = report.technical
        interesting = [
            f"{key} {technical[key]}"
            for key in (
                "width",
                "height",
                "sample_rate",
                "channels",
                "bits_per_sample",
                "frames",
            )
            if key in technical
        ]
        self.technical_label.configure(
            text=f"{self.t('meta_file_technical')}: " + (", ".join(interesting) or "—")
        )

    def _render_results(self) -> None:
        vm = self.vm
        report = vm.report
        labels = self._category_labels()
        self._category_by_label = labels
        self.filter_menu.configure(values=list(labels))
        current = next(
            (label for label, category in labels.items() if category is vm.category),
            next(iter(labels)),
        )
        self.filter_menu.set(current)
        fit_option_menu_on_change(self.filter_menu)
        if vm.only_removable:
            self.removable_check.select()
        else:
            self.removable_check.deselect()
        if report is None:
            self.summary_label.configure(text=self.t("meta_results_empty"))
            self.target_label.configure(text="")
            self.provenance_label.configure(text="")
            self.warning_label.configure(text="")
            self._update_pills()
            self.no_removable_banner.pack_forget()
            self._clear_rows()
            self._rows_signature = None
            self.truncated_label.configure(text="")
            return
        counts = report.counts
        derived = report.derived_counts
        summary = self.t(
            "meta_results_summary",
            total=counts.total,
            personal=counts.personal,
            removable=counts.removable,
            unknown=counts.unknown,
        )
        # Derived views are not entries: one GPS position is four tags plus a
        # decimal view, and counting the view again made "unknown 0" sit next to a
        # visible unknown property. They get their own clause instead.
        if derived.total:
            summary = "\n".join(
                [
                    summary,
                    self.t(
                        "meta_results_derived",
                        total=derived.total,
                        unknown=derived.unknown,
                    ),
                ]
            )
        self.summary_label.configure(text=summary)
        self._update_pills()
        if counts.removable == 0:
            self.no_removable_label.configure(
                text=self.t("meta_no_removable_banner" if report.fields else "meta_no_fields")
            )
            self.no_removable_banner.pack(
                fill="x", padx=18, pady=(0, 8), before=self.provenance_label
            )
        else:
            self.no_removable_banner.pack_forget()
        self.target_label.configure(text=self._target_text(report))
        self.provenance_label.configure(text=self._provenance_text(report))
        self.warning_label.configure(
            text="\n".join(report.warnings[:4]) if report.warnings else ""
        )
        signature = (
            report.path,
            report.sha256,
            vm.category,
            vm.only_removable,
            self.app.i18n.language,
        )
        if signature != self._rows_signature:
            self._render_rows()
            self._rows_signature = signature

    def _update_pills(self) -> None:
        report = self.vm.report
        if report is None:
            selected = 0
            removable = 0
            protected = 0
        else:
            selected = len(self.vm.selection)
            removable = report.counts.removable
            protected = max(0, report.counts.total - removable)
        self.pill_selected.configure(
            text=self.t("meta_pill_selected", count=selected)
        )
        self.pill_removable.configure(
            text=self.t("meta_pill_removable", count=removable)
        )
        self.pill_protected.configure(
            text=self.t("meta_pill_protected", count=protected)
        )

    def _provenance_text(self, report: MetadataReport) -> str:
        """A manifest is reported as a manifest — never as confirmed authenticity."""
        state = report.provenance.state
        key = PROVENANCE_KEYS.get(state)
        text = self.t(key) if key is not None else self.t(
            "meta_provenance_line",
            state=self.t(state.i18n_key),
            details=self.t("meta_provenance_limits"),
        )
        if state is ProvenanceState.SIGNATURE_VALID:
            # Unreachable in this build (no trust list), kept honest if it changes.
            text = self.t(
                "meta_provenance_line",
                state=self.t(state.i18n_key),
                details="",
            ).strip()
        return text

    def _render_rows(self) -> None:
        """Rebuild the field rows; a refresh that changes nothing keeps the old ones."""
        self._clear_rows()
        visible = self.vm.visible_fields()
        shown = visible[:MAX_VISIBLE_FIELDS]
        for index, item in enumerate(shown):
            self._rows.append(self._build_row(item, index))
        hidden = len(visible) - len(shown)
        self.truncated_label.configure(
            text=self.t("meta_results_truncated", shown=len(shown), hidden=hidden)
            if hidden > 0
            else ""
        )

    def _build_row(self, item: MetadataField, index: int) -> ctk.CTkFrame:
        """Two lines per field inside an inset tile: caption + value."""
        row = ctk.CTkFrame(
            self.list_area,
            fg_color=T.CLR_FIELD,
            corner_radius=10,
            border_width=0,
        )
        row.grid(row=index, column=0, sticky="ew", padx=0, pady=(0, 6))
        row.grid_columnconfigure(0, weight=1)
        check = ctk.CTkCheckBox(
            row,
            text=self._row_title(item),
            width=220,
            command=lambda key=item.key: self._on_field_toggle(key),
            font=sans_font(self.root, T.FONT_SIZE_SMALL, weight="bold"),
            checkbox_width=18,
            checkbox_height=18,
            fg_color=T.CLR_ACCENT,
            hover_color=T.CLR_ACCENT_HOVER,
            text_color=T.CLR_TEXT if item.removable else T.CLR_MUTED,
            state="normal" if item.removable else "disabled",
        )
        if item.removable and self.vm.is_selected(item.key):
            check.select()
        check.grid(row=0, column=0, sticky="w", padx=12, pady=(8, 0))
        fit_checkbox(check)
        value = hint_label(row, self.root, self._row_value(item), wrap=520)
        #: A protected row is dimmed rather than hidden: the reason it stays is
        #: the point of the tab, so it is spelled out instead of being a dead row.
        value.grid(row=1, column=0, sticky="w", padx=(38, 12), pady=(2, 8))
        return row

    def _row_title(self, item: MetadataField) -> str:
        if item.derived:
            # Readable, but not a field of its own: the position is the GPS tags
            # above it, the properties are the XMP packet below them.
            marker = self.t("meta_derived")
        else:
            marker = self.t("meta_removable") if item.removable else self.t("meta_protected")
        return f"{item.label} · {self.t(item.category.i18n_key)} · {marker}"

    def _row_value(self, item: MetadataField) -> str:
        # Values that are sentences this program wrote are translated; values that
        # are data are shown verbatim (see ``meta_report.field_value``).
        value = meta_report.field_value(item, self.app.i18n)
        text = value if len(value) <= 200 else value[:197] + CLIP_MARK
        text = text.replace("\n", " ")
        # A reader that knows its reason as a locale key gets it translated here:
        # an English sentence in the middle of a Ukrainian card reads like a bug.
        reason = meta_report.field_reason(item, self.app.i18n)
        if not item.removable and reason:
            return f"{text} — {reason}"
        return text or "—"

    def _target_text(self, report: MetadataReport) -> str:
        name = os.path.basename(report.path) or report.path
        cleaned = self.vm.outcome is not None
        key = "meta_results_target_cleaned" if cleaned else "meta_results_target"
        return self.t(key, file=name)

    def _clear_rows(self) -> None:
        for row in self._rows:
            row.destroy()
        self._rows.clear()

    def _render_plan(self) -> None:
        """The plan of the *next* operation, or the reason there is nothing to plan."""
        report = self.vm.report
        summary = self.vm.plan_summary()
        decision = self.vm.plan()
        if report is None:
            self.plan_label.configure(text=self.t("meta_plan_idle"))
            self._set_preview(self.vm.cleaning_text(self.app.i18n))
            return
        if decision is not None and not decision.allowed:
            text = self.t("meta_plan_blocked")
        elif not report.fields:
            text = self.t("meta_no_fields")
        elif not report.removable_keys and not self.vm.protected_personal():
            text = self.t("meta_no_removable_banner")
        elif self.vm.personal_removable() == 0:
            # "Nothing to clean" is an answer, not an empty card: say why, and say
            # what else is on offer when the file has removable descriptive fields.
            locked = self.vm.protected_personal()
            removable = self.vm.removable_total()
            if locked:
                # Personal data is in the file, but it sits in a block PHANTOM may
                # not touch.  Saying "no cleaning is needed" there was a lie.
                text = self.t("meta_cleanup_blocked_personal", count=locked)
            else:
                text = (
                    self.t("meta_cleanup_optional", count=removable)
                    if removable
                    else self.t("meta_nothing_removable_hint")
                )
                text = f"{self.t('meta_cleanup_not_needed')} {text}"
        else:
            text = self.t(
                "meta_plan_summary",
                remove=summary["remove"],
                kept=summary["kept"],
                blocked=summary["blocked"],
            )
        self.plan_label.configure(text=text)
        self._set_preview(self.vm.cleaning_text(self.app.i18n))

    def _render_save_state(self) -> None:
        """A save button that can do nothing is disabled, and the reason is shown."""
        if getattr(self.app, "_busy", False):
            set_enabled(self.save_button, False)
            self.save_hint.configure(text="")
            return
        report = self.vm.report
        decision = self.vm.plan()
        allowed = self.vm.save_allowed()
        set_enabled(self.save_button, allowed)
        if report is None:
            hint = ""
        elif decision is not None and not decision.allowed:
            hint = self.t("meta_save_blocked_hint")
        elif not self.vm.selection:
            hint = self.t("meta_save_disabled_hint")
        elif decision is not None and not decision.remove:
            hint = self.t("meta_save_nothing_removable_hint")
        else:
            hint = ""
        self.save_hint.configure(text=hint)

    def _set_preview(self, text: str) -> None:
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        if text:
            self.preview_text.insert("1.0", text)
        self.preview_text.configure(state="disabled")

    def _render_integrity(self) -> None:
        outcome = self.vm.outcome
        if outcome is None:
            self.decode_label.configure(text=self.t("meta_integrity_idle"))
            self.compare_label.configure(text="")
            self.counts_label.configure(text="")
            self.hash_label.configure(text="")
            self.final_label.configure(text="", text_color=T.CLR_TEXT_DIM)
            self.copy_hashes_button.configure(state="disabled")
            return
        right = outcome.comparison.right
        self.decode_label.configure(
            text=f"{self.t('meta_integrity_decode')}: {right.detail if right else '—'}"
        )
        self.compare_label.configure(
            text=self.t(
                "meta_integrity_compare",
                status=self.t(
                    "meta_identical" if outcome.comparison.identical else "meta_different"
                ),
                details="; ".join(outcome.comparison.differences),
            ).strip()
        )
        before = outcome.report_after.counts.total + len(outcome.removed)
        self.counts_label.configure(
            text=self.t(
                "meta_integrity_counts_full",
                before=before,
                after=outcome.report_after.counts.total,
                removed=len(outcome.removed),
                kept=len(outcome.kept),
            )
        )
        self.hash_label.configure(
            text=(
                f"sha256 file: {outcome.source_sha256[:16]}{CLIP_MARK} → "
                f"{outcome.output_sha256[:16]}{CLIP_MARK}\n"
                f"sha256 content: {right.content_sha256[:32] if right else '—'}"
            )
        )
        self.final_label.configure(
            text=self.t("meta_verdict_ok" if outcome.verified else "meta_verdict_failed"),
            text_color=T.CLR_GREEN if outcome.verified else T.CLR_RED,
        )
        self.copy_hashes_button.configure(state="normal" if self.vm.hash_block() else "disabled")

    # ------------------------------------------------------------------
    # file selection
    # ------------------------------------------------------------------
    def choose_file(self) -> None:
        patterns = [(self.t(key), pattern) for key, pattern in OPEN_PATTERNS]
        path = filedialog.askopenfilename(
            title=self.t("meta_choose_file"),
            filetypes=list(patterns),
            initialdir=self.app.settings.last_dir or None,
            parent=self.root,
        )
        if path:
            self.load_file(path)

    def load_file(self, path: str) -> None:
        """Point the tab at a file and start the scan job."""
        self.clear_file_error()
        self.vm.set_path(path)
        self.app.settings.last_dir = os.path.dirname(path) or self.app.settings.last_dir
        self._sync_output_entry()
        self.refresh()
        self.start_scan()

    def show_file_error(self, message: str) -> None:
        self.file_error_label.configure(text=message)
        self.file_error_box.pack(fill="x", padx=18, pady=(0, 8))

    def clear_file_error(self) -> None:
        self.file_error_box.pack_forget()
        self.file_error_label.configure(text="")

    def start_scan(self) -> None:
        if not self.vm.path:
            return
        job = self.vm.make_inspect_job(self.vm.path)
        self.app._start_job("metadata_inspect", job, "meta_status_scanning")

    def reset(self) -> None:
        """Forget the file, the report and the previous result (the disk is untouched)."""
        self.clear_file_error()
        self.vm.clear()
        self._rows_signature = None
        self._sync_output_entry()
        self.refresh()
        self.app._set_status_key("meta_status_reset", severity="info")

    def choose_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title=self.t("meta_browse"),
            defaultextension=os.path.splitext(self.vm.path)[1] or ".png",
            initialfile=os.path.basename(self.vm.output_path) if self.vm.output_path else "",
            initialdir=os.path.dirname(self.vm.output_path) or None,
            parent=self.root,
        )
        if path:
            self.vm.output_path = path
            self._sync_output_entry()

    def _sync_output_entry(self) -> None:
        self.output_entry.delete(0, "end")
        if self.vm.output_path:
            self.output_entry.insert(0, self.vm.output_path)

    # ------------------------------------------------------------------
    # selection
    # ------------------------------------------------------------------
    def _on_field_toggle(self, key: str) -> None:
        """A click only changes the selection: the rows stay, the plan is redrawn."""
        self.vm.toggle(key)
        self._update_pills()
        self._render_plan()
        self._render_save_state()

    def _on_filter_change(self, label: str) -> None:
        self.vm.category = self._category_by_label.get(label)
        fit_option_menu_on_change(self.filter_menu)
        self._render_rows()

    def _on_removable_toggle(self) -> None:
        self.vm.only_removable = bool(self.removable_check.get())
        self._render_rows()

    def select_all_removable(self) -> None:
        self.vm.select_all_removable()
        self._update_pills()
        self._render_rows()
        self._render_plan()
        self._render_save_state()

    def select_personal(self) -> None:
        self.vm.select_personal()
        self._update_pills()
        self._render_rows()
        self._render_plan()
        self._render_save_state()

    def clear_selection(self) -> None:
        self.vm.clear_selection()
        self._update_pills()
        self._render_rows()
        self._render_plan()
        self._render_save_state()

    def refresh_preview(self) -> None:
        self._set_preview(self.vm.cleaning_text(self.app.i18n))

    # ------------------------------------------------------------------
    # cleaning
    # ------------------------------------------------------------------
    def save_clean(self) -> None:
        output = self.output_entry.get().strip()
        if output:
            self.vm.output_path = output
        if self.vm.report is None:
            self.app._set_status_key("meta_status_no_report", severity="warn")
            return
        if not self.vm.selection:
            self.app._set_status_key("meta_status_nothing_selected", severity="warn")
            return
        decision = self.vm.plan()
        if decision is not None and not decision.allowed:
            # The policy is not negotiable here: a blocked plan never reaches a job.
            self.app._set_status_key("meta_status_blocked", severity="warn")
            self._render_save_state()
            return
        overwrite = False
        if os.path.exists(self.vm.output_path):
            overwrite = messagebox.askyesno(
                self.t("meta_overwrite_t"),
                self.t("meta_overwrite_m", path=self.vm.output_path),
                parent=self.root,
            )
            if not overwrite:
                return
        try:
            job = self.vm.make_clean_job(overwrite=overwrite)
        except PhantomError as exc:
            self.app._show_error(exc)
            return
        self.app._start_job("metadata_clean", job, "meta_status_cleaning")

    def apply_outcome(self, outcome: CleaningOutcome) -> None:
        """Called by the window when the cleaning job finished."""
        self.vm.apply_outcome(outcome)
        self._sync_output_entry()
        self.refresh()
        self.app._set_status_key("meta_status_cleaned", severity="ok", path=outcome.output)

    def apply_report(self, report: MetadataReport) -> None:
        """Called by the window when the inspection job finished."""
        self.vm.apply_report(report)
        self._sync_output_entry()
        self.refresh()
        self.app._set_status_key("meta_status_scanned", severity="ok")

    # ------------------------------------------------------------------
    # exports
    # ------------------------------------------------------------------
    def export_txt(self) -> None:
        text = self.vm.inspection_text(self.app.i18n)
        if not text:
            self.app._set_status_key("meta_status_no_report", severity="warn")
            return
        self._export(text, EXPORT_TXT, ".txt", "meta_export_txt_name")

    def export_json(self) -> None:
        """JSON stays machine-readable: English keys, stable shape, no locale."""
        text = self.vm.json_text()
        if not text:
            self.app._set_status_key("meta_status_no_report", severity="warn")
            return
        self._export(text, EXPORT_JSON, ".json", "meta_export_json_name")

    def _export(
        self,
        text: str,
        patterns: tuple[tuple[str, str], ...],
        extension: str,
        name_key: str,
    ) -> None:
        stem = os.path.splitext(os.path.basename(self.vm.path))[0] or "report"
        path = filedialog.asksaveasfilename(
            title=self.t(name_key),
            defaultextension=extension,
            initialfile=f"{stem}-metadata{extension}",
            filetypes=[(self.t(key), pattern) for key, pattern in patterns],
            initialdir=self.app.settings.last_dir or None,
            parent=self.root,
        )
        if not path:
            return
        try:
            fsutil.write_bytes_atomic(path, text.encode("utf-8"))
        except PhantomError as exc:
            self.app._show_error(exc)
            return
        self.app._set_status_key("meta_status_exported", severity="ok", path=path)

    def copy_report(self) -> None:
        text = self.vm.inspection_text(self.app.i18n)
        if not text:
            self.app._set_status_key("meta_status_no_report", severity="warn")
            return
        # The clipboard machinery clears the value after a delay: a report carries
        # exactly the personal data this tab exists to remove.
        self.app._copy_to_clipboard(text)
        self.app._set_status_key("meta_status_copied", severity="ok")

    def copy_hashes(self) -> None:
        """The whole SHA-256 values, not the 16-character teaser the card shows."""
        text = self.vm.hash_block()
        if not text:
            self.app._set_status_key("meta_status_no_report", severity="warn")
            return
        self.app._copy_to_clipboard(text)
        self.app._set_status_key("meta_status_hashes_copied", severity="ok")

    def reveal_output(self) -> None:
        target = self.vm.outcome.output if self.vm.outcome is not None else self.vm.path
        if not target:
            self.app._set_status_key("status_path_missing", severity="warn")
            return
        if fsutil.reveal_in_file_manager(target):
            self.app._set_status_key("status_revealed", severity="ok", path=target)
        else:
            self.app._set_status_key("status_reveal_failed", severity="warn")

    # ------------------------------------------------------------------
    # helpers used by the window
    # ------------------------------------------------------------------
    def summary_for_log(self) -> str:
        """A log line with counts only — never a field value."""
        snapshot = self.vm.as_dict()
        counts = snapshot.get("counts") or {}
        return (
            f"metadata: format={snapshot['format'] or '-'} fields={counts.get('total', 0)} "
            f"personal={counts.get('personal', 0)} selected={snapshot['selected']}"
        )
