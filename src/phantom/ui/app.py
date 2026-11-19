"""PHANTOM main window.

The Tk layer is deliberately thin: it renders :mod:`phantom.ui.viewmodel` state
and forwards user intent.  All work that can block runs in
:class:`phantom.ui.worker.WorkerService`; the main thread only touches widgets.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import os
import re
import time
from collections.abc import Callable
from tkinter import TclError, filedialog, messagebox
from typing import Any, Final

import customtkinter as ctk

from phantom.core import constants as C
from phantom.core import files as fsutil
from phantom.core import security
from phantom.core.api import CarrierProbe, EmbedOutcome, ExtractOutcome
from phantom.core.errors import (
    OperationCancelled,
    PhantomError,
    UnsupportedCarrier,
)
from phantom.core.payload import PayloadKind
from phantom.core.progress import INDETERMINATE_STAGES, Stage
from phantom.core.text import utf8_size
from phantom.i18n import I18n
from phantom.metadata.cleaner import CleaningOutcome
from phantom.metadata.models import MetadataReport
from phantom.ui import clipboard as clip
from phantom.ui import format as ui_format
from phantom.ui import theme as T
from phantom.ui.animator import Animator
from phantom.ui.bindings import I18nBinder
from phantom.ui.debounce import Debouncer
from phantom.ui.dnd import DndProvider, DropTarget
from phantom.ui.format import layout_text, size_text, written_layout_text
from phantom.ui.metadata_panel import MetadataPanel
from phantom.ui.responsive import (
    FlowRow,
    Responsive,
    fit_checkbox_on_change,
    fit_switch_on_change,
)
from phantom.ui.result_window import ResultWindow
from phantom.ui.scroll import DIAGNOSTICS_ENV, ScrollRouter
from phantom.ui.settings import AppSettings, SettingsStore
from phantom.ui.startup import StartupTrace
from phantom.ui.system_theme import SystemThemeWatcher
from phantom.ui.viewmodel import (
    CoreBackend,
    EmbedViewModel,
    ExtractViewModel,
    PayloadMode,
)
from phantom.ui.widgets import (
    CTkAdapter,
    card,
    card_title,
    display_font,
    hint_label,
    install_text_shortcuts,
    mono_font,
    paste_into,
    primary_button,
    quiet_idle_flush,
    quiet_redraws,
    sans_font,
    secondary_button,
    set_enabled,
    value_label,
)
from phantom.ui.worker import WorkerEvent, WorkerEventType, WorkerService

LOGGER: Final = logging.getLogger("phantom.app")
GEOMETRY_RE: Final = re.compile(r"^\d{3,4}x\d{3,4}([+-]\d{1,5}[+-]\d{1,5})?$")
WINDOW_SIZE: Final = (1140, 900)
MIN_SIZE: Final = (980, 720)
CARRIER_PATTERNS: Final = (("*.png", "*.wav", "*.flac"))
IMAGE_PATTERNS: Final = (".png",)
AUDIO_PATTERNS: Final = (".wav", ".flac")


class PhantomApp:
    """Owns the Tk root and wires view models to widgets."""

    def __init__(
        self,
        root: ctk.CTk,
        *,
        backend: Any = None,
        settings_store: SettingsStore | None = None,
        dnd: DndProvider | None = None,
        i18n: I18n | None = None,
        trace: StartupTrace | None = None,
    ) -> None:
        self.root = root
        #: Per-stage timings; silent unless ``PHANTOM_STARTUP_DEBUG=1``.
        self.trace = trace if trace is not None else StartupTrace()
        #: Measured button widths and wrap widths for the whole window.
        self.responsive = Responsive()
        self.trace.mark("init:begin")
        self.store = settings_store or SettingsStore()
        self.settings: AppSettings = self.store.load()
        self.trace.mark("init:settings")
        self.i18n = i18n or I18n(self.settings.language)
        self.settings.language = self.i18n.language
        self.binder = I18nBinder(self.i18n)
        self.backend = backend or CoreBackend()
        self.embed_vm = EmbedViewModel(backend=self.backend, settings=self.settings)
        self.extract_vm = ExtractViewModel(backend=self.backend, settings=self.settings)
        self.worker = WorkerService()
        self.animator = Animator(enabled=self.settings.animations)
        self.dnd = dnd or DndProvider()
        self.theme_watcher = SystemThemeWatcher()
        self.debouncer = Debouncer(self.root.after, self.root.after_cancel)

        self._gc_paused = False
        self._poll_token: str | None = None
        self._anim_token: str | None = None
        self._theme_token: str | None = None
        self._clipboard_token: str | None = None
        self._clipboard_value: str | None = None
        self._operation = ""
        self._status_key = "status_ready"
        self._status_params: dict[str, object] = {}
        self._busy = False
        self._progress_indeterminate = False
        #: What the footer is showing right now, as ``(locale key, params)`` — kept
        #: so the elapsed-time tick and a language switch can repaint it.
        self._stage_key: tuple[str, Stage | None, str | None] | None = None
        self._stage_tick_token: str | None = None
        self._operation_started = 0.0
        self._generated_password = ""
        self._suppress_events = False
        self.current_tab = self.settings.last_tab
        self._lang_by_label: dict[str, str] = {}
        self._label_by_lang: dict[str, str] = {}
        self._theme_by_label: dict[str, str] = {}
        self._mode_by_label: dict[str, PayloadMode] = {}
        self._tab_by_label: dict[str, str] = {}
        self._drop_zones: list[Any] = []
        self.metadata_panel: MetadataPanel | None = None
        # One application-wide router: the pointer decides which tab scrolls,
        # so nothing has to be rebound when a card or a language appears.
        self._scroll = ScrollRouter(root)
        self._scroll_frames: list[Any] = []
        self._text_shortcuts: list[Any] = []
        self._result_window: ResultWindow | None = None
        self._last_window_width = 0
        #: The one display pass the build deferred, and whether it is still owed.
        self._layout_token: str | None = None
        self._layout_pending = True
        #: How many display passes this window has really run (diagnostics).
        self._display_passes = 0
        #: The last exception the poll chain swallowed, for the tests and the log.
        self._last_poll_error = ""
        #: How many times the poll callback really ran (diagnostics).
        self._poll_runs = 0
        #: A pending retry of the collector resume, while a worker thread winds down.
        self._gc_resume_token: str | None = None

        self.trace.mark("init:services")
        self._configure_root()
        # The appearance mode goes on *before* the widgets exist.  CustomTkinter
        # notifies every registered widget when the mode actually changes, and each
        # notification ends with a full ``update_idletasks()`` of the window — one
        # nested event loop per widget, from inside the constructor, on the display
        # path that native crashes live in.  Applying the mode first makes the mode
        # already right when each widget is created, so the notification list is
        # empty; ``_apply_theme`` later finds the mode unchanged and does nothing.
        ctk.set_appearance_mode(T.appearance_for(self.settings.theme))
        # CustomTkinter's scrollbar flushes the whole window on every draw, and the
        # build plus the first render draw a hundred of them; each flush then pays
        # for everything laid out before it.  Nothing in this block needs a flush —
        # the fonts measure text, not layouts — so it runs with the flush off and
        # ends with exactly one, which answers every pending question at once.
        with self.trace.stage("build:chrome"), quiet_idle_flush():
            self._build()
            self._scroll.install()
            self._apply_theme(self.settings.theme, persist=False)
            self._wire_shortcuts()
            with self.trace.stage("render:labels"):
                self._render_all_labels()
            with self.trace.stage("render:state"):
                self._refresh_all(initial=True)
            # Widths and wrap widths are derived from what the cards really became:
            # this is the one pass that needs every widget to exist.
            with self.trace.stage("layout:adopt"):
                self.responsive.adopt(self.root)
        # The single display pass the build deferred is *scheduled*, never entered
        # from the constructor: see _flush_layout for why that distinction is the
        # difference between a slow first frame and a native crash.
        self._schedule_layout_flush()
        # Anything that is useful but not needed for the first frame waits for
        # the event loop, so the window is on screen before it happens.
        self.root.after_idle(self._after_first_frame)
        self.trace.mark("init:done")

    # ------------------------------------------------------------------
    # construction helpers
    # ------------------------------------------------------------------
    def _t(self, key: str, **params: object) -> str:
        return self.binder.text(key, **params)

    # -- scrolling ------------------------------------------------------
    def _release_scroll(self) -> None:
        """Drop the application-wide wheel routing this window installed."""
        self._scroll.uninstall()

    def _seat_scroll(self, scroll: Any) -> None:
        """Register one scrollable area; the wheel itself is bound once, globally.

        Binding every widget of a tab looked robust and was not: the router now
        resolves the area from the pointer at event time, so a card built later
        needs no binding and a hidden tab cannot be scrolled through.
        """
        canvas = CTkAdapter.scroll_canvas(scroll)
        if canvas is None:
            return
        self._scroll.register(scroll, canvas, name=f"tab{len(self._scroll_frames) + 1}")
        self._scroll_frames.append(scroll)

    def _paragraph_shortcuts(self, widget: Any) -> None:
        """Cmd/Ctrl + C/V/X/A and a right-click menu for one text widget."""
        self._text_shortcuts.append(
            install_text_shortcuts(self.root, widget, labels_provider=self._menu_labels)
        )

    def _menu_labels(self) -> dict[str, str]:
        return {
            "menu_paste": self._t("menu_paste"),
            "menu_copy": self._t("menu_copy"),
            "menu_cut": self._t("menu_cut"),
            "menu_select_all": self._t("menu_select_all"),
        }

    def file_manager_name(self) -> str:
        """What this system calls the program "reveal in folder" opens."""
        if T.IS_MACOS:
            return self._t("filemanager.finder")
        if os.name == "nt":
            return self._t("filemanager.explorer")
        return self._t("filemanager.generic")

    def _security_params(self) -> dict[str, object]:
        profile = C.default_kdf_profile()
        return {
            "cipher": "AES-256-GCM",
            "kdf": C.kdf_summary(profile),
            "version": C.APP_VERSION,
            "min_len": C.MIN_PASSWORD_LENGTH,
            "length": C.GENERATED_PASSWORD_LENGTH,
            "seconds": C.CLIPBOARD_CLEAR_SECONDS,
        }

    def _schedule_layout_flush(self) -> None:
        """Ask the event loop for the display pass the window owes.

        Owed by the build, by a theme change and by the first mapping of a window
        that was built hidden.  ``after_idle`` and not a direct call: the pass then
        runs as a loop task of its own, like every other repaint, instead of as a
        nested event loop inside a constructor or an event handler (see
        :meth:`_flush_layout`).
        """
        if self._layout_token is not None:
            return
        self._layout_token = self.root.after_idle(self._run_layout_flush)

    def _run_layout_flush(self) -> None:
        """The idle callback behind :meth:`_schedule_layout_flush`."""
        self._layout_token = None
        self._flush_layout()

    def _flush_layout(self) -> bool:
        """One display pass — from the loop, and only for a window that is on screen.

        ``update idletasks`` runs Tk's *display* idle handlers, and on macOS (Aqua)
        that is where native crashes live: a pass for a toplevel that was never
        mapped — ``withdraw`` leaves no NSWindow to draw into — took the whole
        interpreter down with SIGSEGV inside ``update_idletasks`` under Tk 9.1, and
        Tk's own tracker carries open Aqua tickets about idle-task and unretained
        window handling.  Two rules follow, and both are about never asking Tk for
        pixels it cannot produce:

        * the pass is never entered from ``__init__``.  A widget tree that is still
          being built has nothing to display yet, and a nested event loop in a
          constructor turns a display bug into a start-up crash;
        * a window that is not viewable is never asked for one: nothing can be
          drawn, so the request is remembered and the first ``<Map>`` runs it
          (:meth:`_on_root_mapped`).

        Returns whether the pass really ran.
        """
        try:
            if not self.root.winfo_viewable():
                self._layout_pending = True
                return False
            self.root.update_idletasks()
        except Exception as exc:  # noqa: BLE001 - a headless Tk must not stop the launch
            LOGGER.debug("idle flush skipped: %s", exc)
            return False
        self._layout_pending = False
        self._display_passes += 1
        return True

    def _on_root_mapped(self, event: Any) -> None:
        """A window that has just been mapped owes the build its display pass.

        Scheduled, not run here: the pass is never entered from inside a Tk event
        handler or a constructor, only from a task of its own.
        """
        if getattr(event, "widget", None) is not self.root:
            return
        if not self._layout_pending:
            return
        self._schedule_layout_flush()

    def _configure_root(self) -> None:
        self.root.title(self._t("window_title"))
        self.root.geometry(f"{WINDOW_SIZE[0]}x{WINDOW_SIZE[1]}")
        self.root.minsize(*MIN_SIZE)
        # Restore the saved geometry *before* the widgets exist: resizing a
        # fully built window forces a second layout pass over every card, which
        # is most of the "empty window for a second" the owner saw.
        self._restore_geometry()
        self.trace.mark("root:configured")
        self.root.configure(fg_color=T.CLR_BG)
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=1)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # A child's <Configure> reaches the toplevel's bind tags too, so the
        # handler filters on the event's widget before doing anything.
        self.root.bind("<Configure>", self._on_window_resize, add="+")
        # The display pass the build deferred waits for the first mapping; until
        # then the window has no native surface to draw on (see _flush_layout).
        self.root.bind("<Map>", self._on_root_mapped, add="+")

    def _build(self) -> None:
        self._build_header()
        self._build_tab_bar()
        self._content = ctk.CTkFrame(self.root, fg_color="transparent")
        self._content.grid(row=2, column=0, sticky="nsew", padx=18, pady=(4, 0))
        self._content.grid_columnconfigure(0, weight=1)
        self._content.grid_rowconfigure(0, weight=1)
        self.frames = ctk.CTkFrame(self._content, fg_color="transparent")
        self.frames.grid(row=0, column=0, sticky="nsew")
        self.frames.grid_columnconfigure(0, weight=1)
        self.frames.grid_rowconfigure(0, weight=1)
        self.tab_frames = {}
        for name, builder in (
            ("embed", self._build_embed_tab),
            ("extract", self._build_extract_tab),
            ("metadata", self._build_metadata_tab),
            ("settings", self._build_settings_tab),
        ):
            self.tab_frames[name] = builder()
            # One mark per tab: the report can then say which tab was the expensive
            # one instead of "the window took two seconds".
            self.trace.mark(f"build:tab:{name}")
        for frame in self.tab_frames.values():
            frame.grid(row=0, column=0, sticky="nsew")
        self._build_footer()
        self._bind_static_texts()
        self._show_tab(self.current_tab)

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 2))
        header.grid_columnconfigure(0, weight=1)
        title = ctk.CTkLabel(
            header,
            text="P H A N T O M",
            font=display_font(self.root, 26, weight="bold"),
            text_color=T.CLR_TEXT,
            anchor="w",
        )
        title.grid(row=0, column=0, sticky="w")
        # ``text=""``: a CTkLabel without text paints "CTkLabel" until something
        # sets it, and an empty label is a much better first frame than that.
        subtitle = ctk.CTkLabel(
            header,
            text="",
            font=sans_font(self.root, T.FONT_SIZE_SMALL),
            text_color=T.CLR_MUTED,
            anchor="w",
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self.binder.bind_map(subtitle, "header_subtitle", self._security_params())
        version = ctk.CTkLabel(
            header,
            text=f"v{C.APP_VERSION}",
            font=mono_font(self.root, T.FONT_SIZE_SMALL),
            text_color=T.CLR_MUTED,
            anchor="e",
        )
        version.grid(row=0, column=1, rowspan=2, sticky="e")

    def _build_tab_bar(self) -> None:
        self.tab_bar = ctk.CTkSegmentedButton(
            self.root,
            values=[],
            command=self._on_tab_click,
            font=sans_font(self.root, T.FONT_SIZE_LABEL, weight="bold"),
            height=34,
            corner_radius=10,
            selected_color=T.CLR_ACCENT,
            selected_hover_color=T.CLR_ACCENT_HOVER,
            unselected_color=T.CLR_CARD,
            unselected_hover_color=T.CLR_CARD_ALT,
            text_color=T.CLR_TEXT,
        )
        self.tab_bar.grid(row=1, column=0, padx=24, pady=(10, 6), sticky="ew")

    # ------------------------------------------------------------------
    # embed tab
    # ------------------------------------------------------------------
    def _build_embed_tab(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.frames, fg_color="transparent")
        scroll = ctk.CTkScrollableFrame(frame, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True, padx=6, pady=(4, 0))
        scroll.grid_columnconfigure(0, weight=1)
        self._seat_scroll(scroll)
        row = 0

        # payload card ----------------------------------------------------
        payload_card = card(scroll)
        payload_card.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        row += 1
        self.payload_card_title = card_title(payload_card, self.root, "")
        self.payload_card_title.pack(fill="x", padx=18, pady=(14, 6))
        self.binder.bind(self.payload_card_title, "card_payload")

        self.mode_bar = ctk.CTkSegmentedButton(
            payload_card,
            values=[],
            command=self._on_mode_click,
            font=sans_font(self.root, T.FONT_SIZE_LABEL, weight="bold"),
            height=32,
            selected_color=T.CLR_ACCENT,
            selected_hover_color=T.CLR_ACCENT_HOVER,
            unselected_color=T.CLR_FIELD,
            unselected_hover_color=T.CLR_CARD_ALT,
            text_color=T.CLR_TEXT,
        )
        self.mode_bar.pack(fill="x", padx=18, pady=(0, 10))

        self.text_frame = ctk.CTkFrame(payload_card, fg_color="transparent")
        self.text_frame.pack(fill="both", expand=True, padx=18, pady=(0, 4))
        self.embed_text = ctk.CTkTextbox(
            self.text_frame,
            height=168,
            font=sans_font(self.root, T.FONT_SIZE_BODY),
            wrap="word",
            fg_color=T.CLR_FIELD,
            border_color=T.CLR_BORDER,
            border_width=1,
            corner_radius=10,
        )
        self.embed_text.pack(fill="both", expand=True)
        self._paragraph_shortcuts(self.embed_text)
        self.embed_text.bind("<KeyRelease>", self._on_text_changed)
        self.embed_text.bind("<FocusOut>", self._on_text_changed)

        self.char_label = ctk.CTkLabel(
            payload_card,
            text="",
            font=mono_font(self.root, T.FONT_SIZE_FOOTER),
            text_color=T.CLR_MUTED,
            anchor="w",
        )
        self.char_label.pack(fill="x", padx=18, pady=(2, 12))

        self.file_frame = ctk.CTkFrame(payload_card, fg_color="transparent")
        self.secret_button = secondary_button(
            self.file_frame, self.root, "", self._choose_secret
        )
        self.secret_button.pack(side="left")
        self.secret_label = ctk.CTkLabel(
            self.file_frame,
            text="",
            font=sans_font(self.root, T.FONT_SIZE_HINT),
            text_color=T.CLR_TEXT_DIM,
            anchor="w",
        )
        self.secret_label.pack(side="left", padx=(12, 0), fill="x", expand=True)
        self.dnd_secret_hint = hint_label(payload_card, self.root, "", wrap=520)
        self.dnd_secret_hint.pack(fill="x", padx=18, pady=(0, 10))
        self._seat_drop_zone("secret", self.file_frame)

        # password card ---------------------------------------------------
        password_card = card(scroll)
        password_card.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        row += 1
        self.password_card_title = card_title(password_card, self.root, "")
        self.password_card_title.pack(fill="x", padx=18, pady=(14, 6))
        self.binder.bind(self.password_card_title, "card_password")

        pwd_row = ctk.CTkFrame(password_card, fg_color="transparent")
        pwd_row.pack(fill="x", padx=18)
        self.embed_pwd = ctk.CTkEntry(
            pwd_row,
            height=40,
            show="•",
            font=sans_font(self.root, T.FONT_SIZE_BODY),
            fg_color=T.CLR_FIELD,
            border_color=T.CLR_BORDER,
            corner_radius=10,
        )
        self.embed_pwd.pack(side="left", fill="x", expand=True)
        self._paragraph_shortcuts(self.embed_pwd)
        self.embed_pwd.bind("<KeyRelease>", self._on_password_changed)
        # The caption comes from the locale ("Show password"), so the box is
        # measured once it has one: a fixed 22 px width cut the label to "Sh…".
        self.show_embed_pwd: ctk.CTkCheckBox = ctk.CTkCheckBox(
            pwd_row,
            text="",
            command=lambda: self._toggle_password_visibility(self.embed_pwd, self.show_embed_pwd),
        )
        self.show_embed_pwd.pack(side="left", padx=(10, 0))
        fit_checkbox_on_change(self.show_embed_pwd)

        # A flow row, not three packed buttons: Russian and Ukrainian captions are
        # wider than English ones and the row has to re-wrap instead of running
        # off the card when the window is not maximised (D15).
        actions = self.embed_password_actions = FlowRow(password_card)
        actions.pack(fill="x", padx=18, pady=(10, 2))
        self.generate_button = actions.add(
            secondary_button(actions, self.root, "", self._generate_password)
        )
        self.paste_pwd_button = actions.add(
            secondary_button(actions, self.root, "", self._paste_password)
        )
        self.copy_pwd_button = actions.add(
            secondary_button(actions, self.root, "", self._copy_password)
        )
        self.binder.bind(self.paste_pwd_button, "btn_paste")
        self.binder.bind(self.copy_pwd_button, "btn_copy_pwd")
        self.clipboard_hint = hint_label(password_card, self.root, "", wrap=520)
        self.clipboard_hint.pack(fill="x", padx=18, pady=(4, 0))
        self.password_hint = hint_label(password_card, self.root, "", wrap=620)
        self.password_hint.pack(fill="x", padx=18, pady=(4, 12))

        # carrier card ------------------------------------------------------
        carrier_card = card(scroll)
        carrier_card.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        row += 1
        self.carrier_card_title = card_title(carrier_card, self.root, "")
        self.carrier_card_title.pack(fill="x", padx=18, pady=(14, 6))
        self.binder.bind(self.carrier_card_title, "card_carrier")

        carrier_row = ctk.CTkFrame(carrier_card, fg_color="transparent")
        carrier_row.pack(fill="x", padx=18)
        self.carrier_button = primary_button(
            carrier_row, self.root, "", self._choose_carrier, height=36
        )
        self.carrier_button.pack(side="left")
        self.carrier_label = value_label(carrier_row, self.root, "")
        self.carrier_label.pack(side="left", padx=(12, 0), fill="x", expand=True)
        self.dnd_carrier_hint = hint_label(carrier_card, self.root, "", wrap=620)
        self.dnd_carrier_hint.pack(fill="x", padx=18, pady=(6, 0))
        self._seat_drop_zone("carrier", carrier_row)

        self.capacity_label = value_label(carrier_card, self.root, "", mono=True)
        self.capacity_label.pack(fill="x", padx=18, pady=(8, 4))
        self.fill_bar = ctk.CTkProgressBar(
            carrier_card,
            height=8,
            corner_radius=4,
            progress_color=T.CLR_ACCENT,
            fg_color=T.CLR_PROGRESS_TROUGH,
        )
        self.fill_bar.set(0)
        self.fill_bar.pack(fill="x", padx=18, pady=(2, 4))
        self.usage_label = hint_label(carrier_card, self.root, "", wrap=620)
        self.usage_label.pack(fill="x", padx=18, pady=(0, 4))
        self.notice_label = hint_label(carrier_card, self.root, "", wrap=620)
        self.notice_label.pack(fill="x", padx=18, pady=(0, 4))
        self.format_label = hint_label(carrier_card, self.root, "", wrap=620)
        self.format_label.pack(fill="x", padx=18, pady=(0, 12))

        # output card -------------------------------------------------------
        output_card = card(scroll)
        output_card.grid(row=row, column=0, sticky="ew", pady=(0, 12))
        row += 1
        self.output_title = card_title(output_card, self.root, "")
        self.output_title.pack(fill="x", padx=18, pady=(14, 6))
        self.binder.bind(self.output_title, "lbl_output")
        output_row = ctk.CTkFrame(output_card, fg_color="transparent")
        output_row.pack(fill="x", padx=18)
        self.output_entry = ctk.CTkEntry(
            output_row,
            height=38,
            font=mono_font(self.root, T.FONT_SIZE_MONO),
            fg_color=T.CLR_FIELD,
            border_color=T.CLR_BORDER,
            corner_radius=10,
        )
        self.output_entry.pack(side="left", fill="x", expand=True)
        self._paragraph_shortcuts(self.output_entry)
        self.output_entry.bind("<KeyRelease>", self._on_output_changed)
        self.output_button = secondary_button(
            output_row, self.root, "", self._choose_output
        )
        self.output_button.pack(side="left", padx=(10, 0))

        self.wipe_checkbox = ctk.CTkCheckBox(
            output_card,
            text="",
            width=100,
            font=sans_font(self.root, T.FONT_SIZE_BODY),
            command=self._on_wipe_toggle,
            checkbox_width=20,
            checkbox_height=20,
            corner_radius=5,
            fg_color=T.CLR_ACCENT,
            hover_color=T.CLR_ACCENT_HOVER,
        )
        self.wipe_checkbox.pack(fill="x", padx=18, pady=(10, 4))
        self.verify_checkbox = ctk.CTkCheckBox(
            output_card,
            text="",
            width=100,
            font=sans_font(self.root, T.FONT_SIZE_BODY),
            command=self._on_verify_toggle,
            checkbox_width=20,
            checkbox_height=20,
            corner_radius=5,
            fg_color=T.CLR_ACCENT,
            hover_color=T.CLR_ACCENT_HOVER,
        )
        self.verify_checkbox.pack(fill="x", padx=18, pady=(0, 12))

        action_row = ctk.CTkFrame(frame, fg_color="transparent")
        action_row.pack(fill="x", padx=12, pady=(6, 10))
        self.embed_button = primary_button(action_row, self.root, "", self._start_embed)
        self.embed_button.pack(side="left", fill="x", expand=True)
        self.embed_clear_button = secondary_button(
            action_row, self.root, "", self._clear_embed, width=140
        )
        self.embed_clear_button.pack(side="left", padx=(10, 0))
        return frame

    # ------------------------------------------------------------------
    # extract tab
    # ------------------------------------------------------------------
    def _build_extract_tab(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.frames, fg_color="transparent")
        scroll = ctk.CTkScrollableFrame(frame, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True, padx=6, pady=(4, 0))
        scroll.grid_columnconfigure(0, weight=1)
        self._seat_scroll(scroll)

        stego_card = card(scroll)
        stego_card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.stego_card_title = card_title(stego_card, self.root, "")
        self.stego_card_title.pack(fill="x", padx=18, pady=(14, 6))
        self.binder.bind(self.stego_card_title, "card_stego")

        stego_row = ctk.CTkFrame(stego_card, fg_color="transparent")
        stego_row.pack(fill="x", padx=18)
        self.stego_button = primary_button(
            stego_row, self.root, "", self._choose_stego, height=36
        )
        self.stego_button.pack(side="left")
        self.stego_label = value_label(stego_row, self.root, "")
        self.stego_label.pack(side="left", padx=(12, 0), fill="x", expand=True)
        self.dnd_stego_hint = hint_label(stego_card, self.root, "", wrap=620)
        self.dnd_stego_hint.pack(fill="x", padx=18, pady=(6, 0))
        self._seat_drop_zone("stego", stego_row)

        pwd_row = ctk.CTkFrame(stego_card, fg_color="transparent")
        pwd_row.pack(fill="x", padx=18, pady=(10, 4))
        self.extract_pwd = ctk.CTkEntry(
            pwd_row,
            height=40,
            show="•",
            font=sans_font(self.root, T.FONT_SIZE_BODY),
            fg_color=T.CLR_FIELD,
            border_color=T.CLR_BORDER,
            corner_radius=10,
        )
        self.extract_pwd.pack(side="left", fill="x", expand=True)
        self._paragraph_shortcuts(self.extract_pwd)
        self.show_extract_pwd: ctk.CTkCheckBox = ctk.CTkCheckBox(
            pwd_row,
            text="",
            command=lambda: self._toggle_password_visibility(
                self.extract_pwd, self.show_extract_pwd
            ),
        )
        self.show_extract_pwd.pack(side="left", padx=(10, 0))
        fit_checkbox_on_change(self.show_extract_pwd)
        extract_actions = self.extract_password_actions = FlowRow(stego_card)
        extract_actions.pack(fill="x", padx=18, pady=(8, 4))
        self.paste_extract_button = extract_actions.add(
            secondary_button(extract_actions, self.root, "", self._paste_password)
        )
        self.copy_extract_button = extract_actions.add(
            secondary_button(extract_actions, self.root, "", self._copy_password)
        )
        self.binder.bind(self.paste_extract_button, "btn_paste")
        self.binder.bind(self.copy_extract_button, "btn_copy_pwd")
        self.stego_notice = hint_label(stego_card, self.root, "", wrap=620)
        self.stego_notice.pack(fill="x", padx=18, pady=(0, 12))

        result_card = card(scroll)
        result_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.result_card_title = card_title(result_card, self.root, "")
        self.result_card_title.pack(fill="x", padx=18, pady=(14, 6))
        self.binder.bind(self.result_card_title, "card_result")
        self.result_text = ctk.CTkTextbox(
            result_card,
            height=150,
            font=sans_font(self.root, T.FONT_SIZE_BODY),
            wrap="word",
            fg_color=T.CLR_FIELD,
            border_color=T.CLR_BORDER,
            border_width=1,
            corner_radius=10,
        )
        self.result_text.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        self.result_text.configure(state="disabled")
        self._paragraph_shortcuts(self.result_text)
        self.result_file_label = value_label(result_card, self.root, "")
        self.result_file_label.pack(fill="x", padx=18)
        # Seven buttons: packed side by side they needed ~1150 logical pixels and
        # the window is 980 wide at its smallest, so the last ones were cut off.
        # The flow row keeps them in reading order and moves the overflow down.
        buttons_row = self.extract_buttons_row = FlowRow(result_card)
        buttons_row.pack(fill="x", padx=18, pady=(8, 4))
        self.copy_button = buttons_row.add(
            secondary_button(buttons_row, self.root, "", self._copy_result)
        )
        self.save_button = buttons_row.add(
            secondary_button(buttons_row, self.root, "", self._save_result_file)
        )
        self.save_as_button = buttons_row.add(
            secondary_button(buttons_row, self.root, "", self._save_result_file_as)
        )
        self.expand_button = buttons_row.add(
            secondary_button(buttons_row, self.root, "", self._expand_result)
        )
        self.clear_result_button = buttons_row.add(
            secondary_button(buttons_row, self.root, "", self._clear_extract)
        )
        self.copy_path_button = buttons_row.add(
            secondary_button(buttons_row, self.root, "", self._copy_result_path)
        )
        self.reveal_button = buttons_row.add(
            secondary_button(buttons_row, self.root, "", self._reveal_result)
        )
        self.binder.bind(self.expand_button, "btn_expand")
        self.binder.bind(self.copy_path_button, "btn_copy_path")
        # The caption names the file manager this system actually opens.
        self.binder.bind_dynamic(
            self.reveal_button, "btn_reveal", lambda: {"file_manager": self.file_manager_name()}
        )
        self.binder.bind(self.copy_button, "btn_copy")
        self.binder.bind(self.save_button, "btn_save")
        self.binder.bind(self.save_as_button, "btn_save_as")
        self.binder.bind(self.clear_result_button, "btn_clear")

        audit_card = card(scroll)
        audit_card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.audit_title = card_title(audit_card, self.root, "")
        self.audit_title.pack(fill="x", padx=18, pady=(14, 6))
        self.binder.bind(self.audit_title, "card_audit")
        self.audit_rows: dict[str, ctk.CTkLabel] = {}
        for key in (
            "audit_layout",
            "audit_cipher",
            "audit_kdf",
            "audit_integrity",
            "audit_size",
        ):
            holder = ctk.CTkFrame(audit_card, fg_color="transparent")
            holder.pack(fill="x", padx=18, pady=1)
            caption = ctk.CTkLabel(
                holder,
                text="",
                width=110,
                anchor="w",
                font=sans_font(self.root, T.FONT_SIZE_SMALL, weight="bold"),
                text_color=T.CLR_MUTED,
            )
            caption.pack(side="left")
            self.binder.bind(caption, key)
            value = value_label(holder, self.root, "", mono=True)
            value.pack(side="left", fill="x", expand=True)
            self.audit_rows[key] = value
        self.audit_status = hint_label(audit_card, self.root, "", wrap=620)
        self.audit_status.pack(fill="x", padx=18, pady=(8, 12))

        action_row = ctk.CTkFrame(frame, fg_color="transparent")
        action_row.pack(fill="x", padx=12, pady=(6, 10))
        self.extract_button = primary_button(action_row, self.root, "", self._start_extract)
        self.extract_button.pack(side="left", fill="x", expand=True)
        return frame

    # ------------------------------------------------------------------
    # metadata tab
    # ------------------------------------------------------------------
    def _build_metadata_tab(self) -> ctk.CTkFrame:
        """The fourth tab: inspection, selective cleaning, integrity, exports.

        The panel owns its five cards and the state they render; the window only
        routes jobs to it (``metadata_inspect`` / ``metadata_clean``) and asks it
        to repaint when the language or the busy state changes.
        """
        panel = MetadataPanel(self)
        self.metadata_panel = panel
        return panel.build()

    # ------------------------------------------------------------------
    # settings tab
    # ------------------------------------------------------------------
    def _build_settings_tab(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.frames, fg_color="transparent")
        scroll = ctk.CTkScrollableFrame(frame, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True, padx=6, pady=(4, 0))
        scroll.grid_columnconfigure(0, weight=1)
        self._seat_scroll(scroll)

        lang_card = card(scroll)
        lang_card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.lang_title = card_title(lang_card, self.root, "")
        self.lang_title.pack(fill="x", padx=18, pady=(14, 6))
        self.binder.bind(self.lang_title, "settings_language")
        self.lang_bar = ctk.CTkSegmentedButton(
            lang_card,
            values=[],
            command=self._on_language_click,
            font=sans_font(self.root, T.FONT_SIZE_LABEL, weight="bold"),
            height=34,
            selected_color=T.CLR_ACCENT,
            selected_hover_color=T.CLR_ACCENT_HOVER,
            unselected_color=T.CLR_FIELD,
            unselected_hover_color=T.CLR_CARD_ALT,
            text_color=T.CLR_TEXT,
        )
        self.lang_bar.pack(fill="x", padx=18, pady=(0, 12))

        theme_card = card(scroll)
        theme_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.theme_title = card_title(theme_card, self.root, "")
        self.theme_title.pack(fill="x", padx=18, pady=(14, 6))
        self.binder.bind(self.theme_title, "settings_appearance")
        self.theme_bar = ctk.CTkSegmentedButton(
            theme_card,
            values=[],
            command=self._on_theme_click,
            font=sans_font(self.root, T.FONT_SIZE_LABEL, weight="bold"),
            height=34,
            selected_color=T.CLR_ACCENT,
            selected_hover_color=T.CLR_ACCENT_HOVER,
            unselected_color=T.CLR_FIELD,
            unselected_hover_color=T.CLR_CARD_ALT,
            text_color=T.CLR_TEXT,
        )
        self.theme_bar.pack(fill="x", padx=18, pady=(0, 12))

        options_card = card(scroll)
        options_card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.options_title = card_title(options_card, self.root, "")
        self.options_title.pack(fill="x", padx=18, pady=(14, 6))
        self.binder.bind(self.options_title, "settings_animations")
        self.animations_switch = ctk.CTkSwitch(
            options_card,
            text="",
            command=self._on_animations_toggle,
            font=sans_font(self.root, T.FONT_SIZE_BODY),
            progress_color=T.CLR_ACCENT,
            button_color=T.CLR_ON_ACCENT,
            button_hover_color=T.CLR_ACCENT_DIM,
        )
        self.animations_switch.pack(fill="x", padx=18, pady=(0, 6))
        self.binder.bind(self.animations_switch, "settings_reduce_motion")
        fit_switch_on_change(self.animations_switch)
        self.verify_switch = ctk.CTkSwitch(
            options_card,
            text="",
            command=self._on_verify_switch,
            font=sans_font(self.root, T.FONT_SIZE_BODY),
            progress_color=T.CLR_ACCENT,
            button_color=T.CLR_ON_ACCENT,
            button_hover_color=T.CLR_ACCENT_DIM,
        )
        self.verify_switch.pack(fill="x", padx=18, pady=(0, 6))
        self.binder.bind(self.verify_switch, "audit_verified", attribute="text")
        fit_switch_on_change(self.verify_switch)
        self.reset_button = secondary_button(
            options_card, self.root, "", self._on_reset_settings
        )
        self.reset_button.pack(anchor="w", padx=18, pady=(6, 14))
        self.binder.bind(self.reset_button, "settings_reset")

        about_card = card(scroll)
        about_card.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        self.about_title = card_title(about_card, self.root, "")
        self.about_title.pack(fill="x", padx=18, pady=(14, 6))
        self.binder.bind(self.about_title, "settings_about")
        self.about_label = value_label(about_card, self.root, "", mono=True)
        self.about_label.pack(fill="x", padx=18)
        self.binder.bind_map(self.about_label, "settings_about", self._security_params())
        self.limitations_label = hint_label(about_card, self.root, "", wrap=780)
        self.limitations_label.pack(fill="x", padx=18, pady=(10, 4))
        self.binder.bind(self.limitations_label, "about_limitations")
        self.threat_label = hint_label(about_card, self.root, "", wrap=780)
        self.threat_label.pack(fill="x", padx=18, pady=(4, 4))
        self.binder.bind(self.threat_label, "settings_threat_model")
        self.wipe_warning_label = hint_label(about_card, self.root, "", wrap=780)
        self.wipe_warning_label.pack(fill="x", padx=18, pady=(4, 14))
        self.binder.bind(self.wipe_warning_label, "settings_wipe_warning")
        return frame

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self.root, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=24, pady=(4, 16))
        footer.grid_columnconfigure(0, weight=1)
        self.progress_bar = ctk.CTkProgressBar(
            footer,
            height=6,
            corner_radius=3,
            progress_color=T.CLR_ACCENT,
            fg_color=T.CLR_PROGRESS_TROUGH,
        )
        self.progress_bar.set(0)
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        # Without ``text=""`` the footer shows the literal word "CTkLabel" from
        # start-up until the first progress event — the bug this line fixes.
        self.stage_label = ctk.CTkLabel(
            footer,
            text="",
            font=mono_font(self.root, T.FONT_SIZE_FOOTER),
            text_color=T.CLR_MUTED,
            anchor="w",
        )
        self.binder.bind(self.stage_label, "lbl_progress_idle")
        self.stage_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.cancel_button = secondary_button(footer, self.root, "", self._cancel_operation)
        self.cancel_button.grid(row=0, column=1, rowspan=2, padx=(12, 0))
        self.binder.bind(self.cancel_button, "btn_cancel")
        self.status_label = ctk.CTkLabel(
            footer,
            text="",
            font=sans_font(self.root, T.FONT_SIZE_LABEL, weight="bold"),
            text_color=T.CLR_TEXT_DIM,
            anchor="w",
        )
        self.status_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._set_status_key("status_ready")

    # ------------------------------------------------------------------
    # label rendering
    # ------------------------------------------------------------------
    def _bind_static_texts(self) -> None:
        """Register every static translation exactly once (called from build)."""
        params = self._security_params()
        self.binder.bind(self.embed_pwd, "ph_password_embed", attribute="placeholder_text")
        self.binder.bind(self.extract_pwd, "ph_password_extract", attribute="placeholder_text")
        self.binder.bind(self.generate_button, "btn_generate_pwd")
        self.binder.bind(self.embed_button, "btn_encrypt")
        self.binder.bind(self.embed_clear_button, "btn_clear")
        self.binder.bind(self.extract_button, "btn_extract")
        self.binder.bind(self.carrier_button, "btn_choose_carrier")
        self.binder.bind(self.stego_button, "btn_choose_stego")
        self.binder.bind(self.secret_button, "btn_choose_file")
        self.binder.bind(self.output_button, "btn_browse_output")
        self.binder.bind(self.wipe_checkbox, "chk_secure_delete")
        self.binder.bind(self.verify_checkbox, "chk_verify")
        # A checkbox draws its caption inside its own width: measure it, in the
        # current language, instead of leaving the default 100 px.
        fit_checkbox_on_change(self.wipe_checkbox)
        fit_checkbox_on_change(self.verify_checkbox)
        self.binder.bind(self.show_embed_pwd, "chk_show_password")
        self.binder.bind(self.show_extract_pwd, "chk_show_password")
        self.binder.bind_dynamic(
            self.password_hint,
            "lbl_pwd_hint",
            lambda: {"min_len": params["min_len"], "kdf": params["kdf"]},
        )
        self.binder.bind_dynamic(
            self.clipboard_hint,
            "clipboard_warning",
            lambda: {"seconds": params["seconds"]},
        )
        self.binder.bind(self.dnd_carrier_hint, "dnd_hint_carrier")
        self.binder.bind(self.dnd_stego_hint, "dnd_hint_stego")
        self.binder.bind(self.dnd_secret_hint, "dnd_hint_secret")
        if not self.dnd.available:
            for hint in (self.dnd_carrier_hint, self.dnd_stego_hint, self.dnd_secret_hint):
                hint.pack_forget()

    def _render_all_labels(self) -> None:
        self._render_language_labels()
        self._render_theme_labels()
        self._render_mode_labels()
        self._render_tab_labels()
        self._render_format_labels()
        self.binder.apply()
        # Captions changed, so their measured widths did too: re-wrap the rows
        # and the paragraphs before the next frame is drawn.
        self.responsive.refit_flows()
        self.responsive.rewrap()

    def _on_window_resize(self, event: Any) -> None:
        """One place that reacts to the window size, debounced to real changes."""
        if getattr(event, "widget", None) is not self.root:
            return
        width = int(getattr(event, "width", 0) or 0)
        if abs(width - self._last_window_width) < 4:
            return
        self._last_window_width = width
        self.responsive.refit_flows()

    def _render_format_labels(self) -> None:
        """Say which layout this build writes, for the carrier that is chosen."""
        carrier = self.embed_vm.carrier
        self.format_label.configure(
            text=self.i18n.t(
                "lbl_embed_format",
                layout=written_layout_text(
                    self.i18n, carrier.kind if carrier is not None else None
                ),
            )
        )

    def _render_language_labels(self) -> None:
        codes = list(self.i18n.languages)
        self._lang_by_label = {self.i18n.t(f"lang.{code}"): code for code in codes}
        self._label_by_lang = {code: label for label, code in self._lang_by_label.items()}
        values = [self._label_by_lang[code] for code in codes]
        self.lang_bar.configure(values=values)
        self.lang_bar.set(self._label_by_lang.get(self.i18n.language, values[0]))

    def _render_theme_labels(self) -> None:
        mapping = {
            "theme_dark": "Dark",
            "theme_light": "Light",
            "theme_system": "System",
        }
        self._theme_by_label = {self.i18n.t(key): mode for key, mode in mapping.items()}
        self.theme_bar.configure(values=list(self._theme_by_label))
        for label, mode in self._theme_by_label.items():
            if mode == self.settings.theme:
                self.theme_bar.set(label)
                break

    def _render_mode_labels(self) -> None:
        mapping = {"radio_text": PayloadMode.TEXT, "radio_file": PayloadMode.FILE}
        self._mode_by_label = {self.i18n.t(key): mode for key, mode in mapping.items()}
        self.mode_bar.configure(values=list(self._mode_by_label))
        for label, mode in self._mode_by_label.items():
            if mode is self.embed_vm.mode:
                self.mode_bar.set(label)
                break

    def _render_tab_labels(self) -> None:
        mapping = {
            "tab_embed": "embed",
            "tab_extract": "extract",
            "tab_metadata": "metadata",
            "tab_settings": "settings",
        }
        self._tab_by_label = {self.i18n.t(key): ident for key, ident in mapping.items()}
        self.tab_bar.configure(values=list(self._tab_by_label))
        for label, ident in self._tab_by_label.items():
            if ident == self.current_tab:
                self.tab_bar.set(label)
                break

    def _after_first_frame(self) -> None:
        """Work that must not delay the window: logging, theme polling, log lines."""
        self.trace.mark("first_frame")
        try:
            self._log_startup_state()
        except Exception as exc:  # noqa: BLE001 - never let housekeeping kill the UI
            LOGGER.debug("post-startup work skipped: %s", exc)
        self.trace.mark("after_idle")
        self.trace.log_summary()

    # ------------------------------------------------------------------
    # state refresh
    # ------------------------------------------------------------------
    def _refresh_all(self, *, initial: bool = False) -> None:
        self._refresh_embed_state()
        self._refresh_extract_state()
        self._refresh_settings_state()
        # The panel repaints itself at the end of its own build; doing it again
        # in the same tick was a second full render of five cards for nothing.
        if self.metadata_panel is not None and not initial:
            self.metadata_panel.refresh()
        self._refresh_busy_state()
        # A new carrier (or none) changes which layout the tab promises to write.
        self._render_format_labels()
        # The binder has just re-rendered the static labels, the footer included:
        # put the running stage back, in the new language and with its clock.
        self._render_stage_label()

    def _refresh_embed_state(self) -> None:
        vm = self.embed_vm
        text = self.embed_text.get("1.0", "end-1c")
        if text != vm.text and not self._suppress_events:
            vm.set_text(text)
        self.char_label.configure(
            text=self.i18n.t(
                "lbl_char_count",
                chars=len(vm.text),
                # Every keystroke passes through here: a lone surrogate in the text
                # must not raise from a counter and freeze the character label.
                **{"bytes": utf8_size(vm.text)},
            )
        )
        if vm.mode is PayloadMode.TEXT:
            if not self.text_frame.winfo_ismapped():
                self.file_frame.pack_forget()
                self.text_frame.pack(fill="both", expand=True, padx=18, pady=(0, 4))
                self.char_label.pack(fill="x", padx=18, pady=(2, 12))
            self.secret_button.configure(state="disabled")
        else:
            if not self.file_frame.winfo_ismapped():
                self.text_frame.pack_forget()
                self.char_label.pack_forget()
                self.file_frame.pack(fill="x", padx=18, pady=(0, 10))
            self.secret_button.configure(state="normal")
        if vm.secret_path:
            self.secret_label.configure(text=os.path.basename(vm.secret_path))
        else:
            self.secret_label.configure(text=self.i18n.t("lbl_no_secret_file"))
        self.carrier_label.configure(
            text=(
                self.i18n.t("lbl_carrier_info", info=vm.carrier.description)
                if vm.carrier
                else self.i18n.t("lbl_no_file")
            )
        )
        if vm.carrier is not None:
            size = vm.capacity
            self.capacity_label.configure(
                text=self.i18n.t(
                    "lbl_capacity_bits",
                    size=size,
                    kb=f"{size / 1024:.1f}",
                    bits=vm.carrier.capacity.usable_bits,
                )
            )
        else:
            self.capacity_label.configure(text="")
        fill = vm.fill_percent()
        percent = 0.0 if fill is None else fill
        self.fill_bar.set(min(1.0, percent / 100.0))
        if vm.exceeds_capacity():
            self.usage_label.configure(
                text=self.i18n.t("lbl_usage_exceeded", pct=f"{percent:.0f}"),
                text_color=T.CLR_RED,
            )
        elif fill is None:
            self.usage_label.configure(
                text=self.i18n.t("lbl_usage_empty"), text_color=T.CLR_MUTED
            )
        else:
            severity = (
                T.CLR_RED
                if percent >= C.CAPACITY_WARNING_PCT
                else T.CLR_YELLOW
                if percent >= C.CAPACITY_NOTICE_PCT
                else T.CLR_GREEN
            )
            self.usage_label.configure(
                text=self.i18n.t("lbl_usage", pct=f"{percent:.1f}"), text_color=severity
            )
        notices = [self.i18n.t(key) for key in vm.notices if self.i18n.has(key)]
        if vm.carrier is not None and vm.carrier.audio is not None:
            audio = vm.carrier.audio
            notices.append(self.i18n.t("lbl_silence", pct=f"{audio.silence_ratio * 100:.0f}"))
        self.notice_label.configure(text="\n".join(notices))
        if not self.output_entry.get():
            self._set_entry_text(self.output_entry, vm.output_path)
        if self.wipe_checkbox.get() != vm.wipe_source:
            self.wipe_checkbox.select() if vm.wipe_source else self.wipe_checkbox.deselect()
        if self.verify_checkbox.get() != vm.verify:
            self.verify_checkbox.select() if vm.verify else self.verify_checkbox.deselect()
        self.wipe_checkbox.configure(state="normal" if vm.verify else "disabled")

    def _refresh_extract_state(self) -> None:
        vm = self.extract_vm
        self.stego_label.configure(
            text=(
                self.i18n.t("lbl_carrier_info", info=vm.carrier.description)
                if vm.carrier
                else self.i18n.t("lbl_no_file")
            )
        )
        notices = [self.i18n.t(key) for key in vm.notices if self.i18n.has(key)]
        self.stego_notice.configure(text="\n".join(notices))
        payload = vm.result
        if payload is None:
            self.result_file_label.configure(text="")
            self.audit_status.configure(text="")
            for value in self.audit_rows.values():
                value.configure(text="—")
            self.expand_button.configure(state="disabled")
            self.copy_path_button.configure(state="disabled")
            self.reveal_button.configure(state="disabled")
            return
        self.audit_rows["audit_layout"].configure(
            text=layout_text(self.i18n, payload.container_version)
        )
        self.audit_rows["audit_cipher"].configure(text="AES-256-GCM")
        self.audit_rows["audit_kdf"].configure(text=payload.kdf_label or "—")
        self.audit_rows["audit_integrity"].configure(
            text=("SHA-256 MATCH" if payload.integrity_ok else "SHA-256 MISMATCH")
        )
        capacity = vm.carrier.capacity if vm.carrier is not None else None
        self.audit_rows["audit_size"].configure(
            text=size_text(payload, capacity, self.i18n)
        )
        if payload.integrity_ok:
            self.audit_status.configure(
                text=self.i18n.t("audit_verified"), text_color=T.CLR_GREEN
            )
        else:
            self.audit_status.configure(
                text=self.i18n.t("lbl_integrity_fail"), text_color=T.CLR_RED
            )
        if payload.kind is PayloadKind.TEXT:
            self._set_textbox(self.result_text, payload.data.decode("utf-8", errors="replace"))
            self.result_file_label.configure(text=self.i18n.t("lbl_extract_text_hint"))
        else:
            self._set_textbox(self.result_text, "")
            self.result_file_label.configure(
                text=f"{payload.name} · {payload.original_size} B"
            )
        has_text = payload.kind is PayloadKind.TEXT
        self.copy_button.configure(state="normal" if has_text else "disabled")
        state = "normal" if not has_text else "disabled"
        self.save_button.configure(state=state)
        self.save_as_button.configure(state=state)
        self.expand_button.configure(state="normal")
        path = self._result_path()
        self.copy_path_button.configure(state="normal" if path else "disabled")
        revealable = bool(path) and os.path.exists(path)
        self.reveal_button.configure(state="normal" if revealable else "disabled")

    def _refresh_settings_state(self) -> None:
        if self.settings.animations:
            self.animations_switch.select()
        else:
            self.animations_switch.deselect()
        if self.settings.verify_after_embed:
            self.verify_switch.select()
        else:
            self.verify_switch.deselect()

    def _refresh_busy_state(self) -> None:
        state = "disabled" if self._busy else "normal"
        for widget in (
            self.embed_button,
            self.extract_button,
            self.carrier_button,
            self.stego_button,
            self.secret_button if self.embed_vm.mode is PayloadMode.FILE else None,
            self.generate_button,
            self.embed_clear_button,
            self.output_button,
            self.clear_result_button,
        ):
            if widget is not None:
                set_enabled(widget, not self._busy)
        if self.metadata_panel is not None:
            # When the job ends, "save" is decided by the selection and the plan,
            # not by the busy flag: a disabled save button has to say why.
            buttons = (
                self.metadata_panel.action_buttons()
                if state == "disabled"
                else self.metadata_panel.editable_action_buttons()
            )
            for widget in buttons:
                set_enabled(widget, not self._busy)
            self.metadata_panel._render_save_state()
        self.cancel_button.configure(state="normal" if self._busy else "disabled")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _set_entry_text(entry: ctk.CTkEntry, value: str) -> None:
        entry.delete(0, "end")
        entry.insert(0, value)

    @staticmethod
    def _set_textbox(textbox: ctk.CTkTextbox, value: str) -> None:
        textbox.configure(state="normal")
        textbox.delete("1.0", "end")
        if value:
            textbox.insert("1.0", value)
        textbox.configure(state="disabled")

    def _set_status_key(self, key: str, *, severity: str = "info", **params: object) -> None:
        self._status_key = key
        self._status_params = dict(params)
        self.status_label.configure(
            text=self.i18n.t(key, **params), text_color=T.severity_color(severity)
        )

    def _error_message(self, exc: PhantomError) -> str:
        params = dict(exc.params)
        params["details"] = exc.detail or exc.message or exc.code.value
        key = exc.i18n_key
        if self.i18n.has(key):
            return self.i18n.t(key, **params)
        return self.i18n.t("err.internal", details=str(exc))

    def _show_error(self, exc: PhantomError) -> None:
        self._set_status_key("status_error", severity="error")
        if isinstance(exc, OperationCancelled):
            return
        messagebox.showerror(self.i18n.t("err.title"), self._error_message(exc), parent=self.root)

    def _show_warning(self, title_key: str, message_key: str, **params: object) -> None:
        messagebox.showwarning(
            self.i18n.t(title_key), self.i18n.t(message_key, **params), parent=self.root
        )

    def _log_startup_state(self) -> None:
        LOGGER.info(
            "PHANTOM %s started; language=%s theme=%s dnd=%s",
            C.APP_VERSION,
            self.i18n.language,
            self.settings.theme,
            "on" if self.dnd.available else f"off ({self.dnd.reason})",
        )
        if self.metadata_panel is not None:
            LOGGER.info("%s", self.metadata_panel.summary_for_log())
        LOGGER.info("scroll: %s", self._scroll.describe())
        if self._scroll.diagnostics.enabled:
            LOGGER.warning(
                "scroll event diagnostics are on (%s): every wheel event is logged",
                DIAGNOSTICS_ENV,
            )

    def _seat_drop_zone(self, zone: str, widget: Any) -> None:
        callbacks = {
            "carrier": self._drop_carrier,
            "stego": self._drop_stego,
            "secret": self._drop_secret,
            "metadata": self._drop_metadata,
        }
        extensions = (
            C.IMAGE_EXTENSIONS + C.AUDIO_WAV_EXTENSIONS + C.AUDIO_FLAC_EXTENSIONS
            if zone != "secret"
            else (".bin", ".txt", ".pdf", ".zip", ".png", ".jpg", ".json", ".csv", ".dat")
        )
        def _enter(_widget: Any = widget) -> None:
            self._highlight_zone(_widget, True)

        def _leave(_widget: Any = widget) -> None:
            self._highlight_zone(_widget, False)

        target = DropTarget(
            widget=widget,
            on_drop=callbacks[zone],
            extensions=extensions,
            on_enter=_enter,
            on_leave=_leave,
        )
        self._drop_zones.append(target)
        if self.dnd.available:
            self.dnd.register(target)

    def _highlight_zone(self, widget: Any, active: bool) -> None:
        color = T.CLR_BORDER_OVER if active else T.CLR_BORDER
        try:
            widget.configure(border_color=color, border_width=1)
        except Exception as exc:  # noqa: BLE001 - any widget may lack the option
            LOGGER.debug("cannot highlight drop zone: %s", exc)

    # ------------------------------------------------------------------
    # user actions
    # ------------------------------------------------------------------
    def _on_tab_click(self, label: str) -> None:
        ident = self._tab_by_label.get(label)
        if ident:
            self._show_tab(ident)

    def _show_tab(self, ident: str) -> None:
        if ident not in self.tab_frames:
            ident = "embed"
        self.current_tab = ident
        self.tab_frames[ident].tkraise()
        self.settings.last_tab = ident
        if ident == "extract":
            self.extract_pwd.focus_set()
        elif ident == "embed":
            target = self.embed_text if self.embed_vm.mode is PayloadMode.TEXT else self.embed_pwd
            target.focus_set()

    def _on_mode_click(self, label: str) -> None:
        mode = self._mode_by_label.get(label)
        if mode is None:
            return
        self.embed_vm.set_mode(mode)
        self._refresh_all()

    def _on_text_changed(self, _event: object | None = None) -> None:
        if self._suppress_events:
            return
        self.embed_vm.set_text(self.embed_text.get("1.0", "end-1c"))
        self._refresh_embed_state()
        self.debouncer.schedule(self._start_estimate)

    def _on_password_changed(self, _event: object | None = None) -> None:
        self.embed_vm.set_password(self.embed_pwd.get())

    def _on_output_changed(self, _event: object | None = None) -> None:
        self.embed_vm.set_output(self.output_entry.get())

    def _toggle_password_visibility(self, entry: ctk.CTkEntry, checkbox: ctk.CTkCheckBox) -> None:
        entry.configure(show="" if checkbox.get() else "•")

    def _generate_password(self) -> None:
        password = security.generate_password(C.GENERATED_PASSWORD_LENGTH)
        self._generated_password = password
        self.embed_vm.set_password(password)
        self._set_entry_text(self.embed_pwd, password)
        self.embed_pwd.configure(show="")
        self.show_embed_pwd.select()
        self._copy_to_clipboard(password)
        self._set_status_key(
            "status_pwd_generated",
            severity="ok",
            length=C.GENERATED_PASSWORD_LENGTH,
        )

    def _copy_to_clipboard(self, value: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self._clipboard_value = value
        if self._clipboard_token is not None:
            self.root.after_cancel(self._clipboard_token)
        self._clipboard_token = self.root.after(
            int(C.CLIPBOARD_CLEAR_SECONDS * 1000), lambda: self._clear_clipboard_if(value)
        )

    def _clipboard_holds(self, value: str) -> bool:
        """Is ``value`` still on the clipboard?  Tk only answers while it owns it."""
        current = clip.read_clipboard(self.root)
        if current is None:
            return False
        return current.rstrip("\n") == value.rstrip("\n")

    def _clear_clipboard_if(self, value: str) -> None:
        self._clipboard_token = None
        if not self._clipboard_holds(value):
            self._clipboard_value = None
            return
        try:
            self.root.clipboard_clear()
        except Exception as exc:  # noqa: BLE001 - pragma: no cover, Tk dependent
            LOGGER.warning("clipboard clear failed: %s", exc)
        # Tk stops being the clipboard owner as soon as another app writes, and
        # the text outlives the window on macOS: ask the OS to empty it as well.
        clip.clear_clipboard()
        self._clipboard_value = None
        self._generated_password = ""
        self._set_status_key("status_clipboard_cleared", severity="info")

    def _forget_clipboard(self) -> None:
        """Drop our copy when the window goes away, so it cannot outlive us."""
        value, self._clipboard_value = self._clipboard_value, None
        if value is None:
            return
        if not self._clipboard_holds(value):
            return
        try:
            self.root.clipboard_clear()
        except Exception as exc:  # noqa: BLE001 - pragma: no cover, Tk dependent
            LOGGER.debug("clipboard clear on close failed: %s", exc)
        clip.clear_clipboard()

    def _on_wipe_toggle(self) -> None:
        self.embed_vm.set_wipe_source(bool(self.wipe_checkbox.get()))

    def _on_verify_toggle(self) -> None:
        self.embed_vm.set_verify(bool(self.verify_checkbox.get()))
        if not self.embed_vm.verify and self.embed_vm.wipe_source:
            # Secure deletion is only ever offered on a verified output.
            self.embed_vm.set_wipe_source(False)
        self._refresh_embed_state()

    # -- file pickers ---------------------------------------------------
    def _file_types(self) -> list[tuple[str, str]]:
        return [
            (self.i18n.t("ft_media"), "*.png *.wav *.flac"),
            (self.i18n.t("ft_png"), "*.png"),
            (self.i18n.t("ft_audio"), "*.wav *.flac"),
            (self.i18n.t("ft_all"), "*.*"),
        ]

    def _choose_carrier(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title=self.i18n.t("dlg_open_carrier"),
            filetypes=self._file_types(),
        )
        if path:
            self._set_carrier(path)

    def _choose_stego(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title=self.i18n.t("dlg_open_stego"),
            filetypes=self._file_types(),
        )
        if path:
            self._set_stego(path)

    def _choose_secret(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title=self.i18n.t("dlg_open_secret"),
            filetypes=[(self.i18n.t("ft_all"), "*.*")],
        )
        if path:
            self._set_secret(path)

    def _choose_output(self) -> None:
        initial = self.embed_vm.output_path or ""
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title=self.i18n.t("dlg_save"),
            initialdir=os.path.dirname(initial) or None,
            initialfile=os.path.basename(initial) or None,
            defaultextension=".png",
            filetypes=self._file_types(),
        )
        if path:
            self.embed_vm.set_output(path)
            self._set_entry_text(self.output_entry, path)

    # -- drops ----------------------------------------------------------
    def _accepted_drop(self, paths: list[str], extensions: tuple[str, ...]) -> str | None:
        for path in paths:
            if os.path.splitext(path)[1].lower() in extensions and os.path.isfile(path):
                return path
        return None

    def _drop_carrier(self, paths: list[str]) -> None:
        path = self._accepted_drop(
            paths, C.IMAGE_EXTENSIONS + C.AUDIO_WAV_EXTENSIONS + C.AUDIO_FLAC_EXTENSIONS
        )
        if path:
            self._set_carrier(path)
        elif paths:
            self._show_error(UnsupportedCarrier("dropped file is not a supported carrier"))

    def _drop_stego(self, paths: list[str]) -> None:
        path = self._accepted_drop(
            paths, C.IMAGE_EXTENSIONS + C.AUDIO_WAV_EXTENSIONS + C.AUDIO_FLAC_EXTENSIONS
        )
        if path:
            self._set_stego(path)

    def _drop_metadata(self, paths: list[str]) -> None:
        path = self._accepted_drop(
            paths, C.IMAGE_EXTENSIONS + C.AUDIO_WAV_EXTENSIONS + C.AUDIO_FLAC_EXTENSIONS
        )
        if path and self.metadata_panel is not None:
            self.metadata_panel.load_file(path)

    def _drop_secret(self, paths: list[str]) -> None:
        for path in paths:
            if os.path.isfile(path):
                self._set_secret(path)
                return

    # -- probes ---------------------------------------------------------
    def _set_carrier(self, path: str) -> None:
        self.embed_vm.set_carrier(path)
        self._set_entry_text(self.output_entry, self.embed_vm.output_path)
        self._refresh_all()
        self._start_job("probe_embed", self.embed_vm.make_probe_job(path), "status_embedding")

    def _set_stego(self, path: str) -> None:
        self.extract_vm.set_stego(path)
        self._refresh_all()
        self._start_job("probe_extract", self.extract_vm.make_probe_job(path), "status_extracting")

    def _set_secret(self, path: str) -> None:
        self.embed_vm.set_secret_file(path)
        self.embed_vm.set_mode(PayloadMode.FILE)
        self._refresh_all()
        self.debouncer.schedule(self._start_estimate)

    # -- worker plumbing ------------------------------------------------
    def _start_estimate(self) -> None:
        if self.worker.busy:
            self.debouncer.schedule(self._start_estimate)
            return
        problem = self.embed_vm.text_problem()
        if problem is not None:
            # Text that has no UTF-8 form cannot be packed, so there is nothing to
            # estimate.  Say so in the status line instead of starting a job whose
            # only outcome is an error dialog over the text box the user is typing in.
            self._set_status_key(problem.i18n_key, severity="warn", **problem.params)
            return
        job = self.embed_vm.make_estimate_job()
        self._start_job("estimate", job, "status_ready", silent=True)

    def _start_job(
        self,
        name: str,
        job: Callable[[Any, Any], Any],
        status_key: str,
        *,
        silent: bool = False,
    ) -> bool:
        if self.worker.busy:
            if not silent:
                self._show_warning("warn_operation_t", "warn_operation_m")
            return False
        self._operation = name
        self._busy = True
        try:
            self._refresh_busy_state()
            self._reset_progress()
            if not silent:
                self._set_status_key(status_key, severity="info")
            self._pause_collection_for_job()
            started = self.worker.start(name, job)
        except BaseException:
            # Nothing is running, so nothing will ever report back: leaving _busy
            # true here wedged the footer for good (a job that never started, a poll
            # chain with nothing to poll).  Undo the start, then let the caller see
            # the failure.
            self._abandon_start()
            raise
        if not started:
            self._abandon_start()
            return False
        self._operation_started = time.monotonic()
        self._ensure_stage_tick()
        self._ensure_polling()
        return True

    #: Whether the collector is being held down for some job in this process, and
    #: whether it was on before the first pause.  Class level on purpose: an
    #: interpreter started with ``gc.disable()`` must still have it disabled
    #: afterwards, and a second window must not read the collector *while the first
    #: window holds it down* and conclude "it was never enabled" — the resume of that
    #: second window would then leave it off for the rest of the session.
    _collector_held: bool = False
    _collector_was_enabled: bool = True

    def _abandon_start(self) -> None:
        """Undo the bookkeeping of a job that never got to run."""
        self._resume_collection_after_job()
        self._busy = False
        self._operation = ""
        self._refresh_busy_state()

    def _pause_collection_for_job(self) -> None:
        """Keep the cyclic collector on this thread for the length of a job.

        A collection can run ``__del__`` on any object it reclaims, and
        ``tkinter.font.Font.__del__`` calls into Tcl.  Tcl is not thread-safe: a
        finalizer that lands on the worker thread makes Tk wait for a main loop
        that is itself waiting for the job, and the window freezes with the
        worker still "busy" — the intermittent embed stall that only ever showed
        up on a loaded CI runner, stuck inside ``tkinter/font.py`` on the worker.
        Collecting before the job and after it puts every finalizer back on the
        main thread; the job itself allocates only short-lived buffers, which
        refcounting frees without the collector.
        """
        if self._gc_paused:
            # A previous job already owns the pause.  Do not re-read the collector
            # here: it is off *because that job turned it off*, so reading it again
            # would record "it was never enabled" and the last resume would leave the
            # collector disabled for the rest of the session.  Chained jobs (a
            # carrier probe, the estimate it schedules, the embed that follows) hit
            # exactly this.
            return
        if not PhantomApp._collector_held:
            PhantomApp._collector_was_enabled = gc.isenabled()
            PhantomApp._collector_held = True
            if PhantomApp._collector_was_enabled:
                gc.collect()
                gc.disable()
            else:
                # Someone else (a profiler, a test, the embedding application) turned
                # the collector off.  Own the pause anyway, so the resume stays a
                # no-op instead of switching on something we did not switch off.
                LOGGER.debug("collector was already disabled; leaving it that way")
        self._gc_paused = True

    def _resume_collection_after_job(self) -> None:
        """Undo exactly one pause; never re-enable a collector we did not disable."""
        if not self._gc_paused:
            return
        if self.worker.busy:
            # A worker thread is still on its way out: enabling here would let a
            # finalizer run while it allocates, which is the freeze this exists to
            # prevent.  This used to simply give up — the job had already posted its
            # terminal event, so no further finish ever came and the collector stayed
            # off for the rest of the session (the 3.11 GUI suite saw exactly that).
            LOGGER.debug("collector stays paused: a job is still running")
            if self._gc_resume_token is None:
                self._gc_resume_token = self.root.after(
                    C.WORKER_POLL_MS, self._resume_collection_after_job
                )
            return
        self._cancel_gc_resume()
        self._gc_paused = False
        PhantomApp._collector_held = False
        if not PhantomApp._collector_was_enabled:
            return
        gc.enable()
        gc.collect()

    def _cancel_gc_resume(self) -> None:
        if self._gc_resume_token is not None:
            with contextlib.suppress(TclError):
                self.root.after_cancel(self._gc_resume_token)
            self._gc_resume_token = None

    def _ensure_polling(self) -> None:
        if self._poll_token is None:
            self._poll_token = self.root.after(C.WORKER_POLL_MS, self._poll)

    def _poll(self) -> None:
        self._poll_token = None
        self._poll_runs += 1
        try:
            self._poll_once()
        except Exception as exc:
            LOGGER.exception("worker poll failed")
            self._last_poll_error = f"{type(exc).__name__}: {exc}"
            self._finish_operation_ui_safely()
        # Keep the chain alive while the window still believes an operation is in
        # flight.  The old condition was ``worker.busy`` alone: a poll that failed
        # while the worker was already idle left ``_busy`` true with nothing left
        # to clear it, and the window stayed busy for ever.  Whatever the reason
        # for the failure, the next poll either finishes the operation or reports
        # the error again — a stuck footer with no polling is never the answer.
        if self.worker.busy or self._busy:
            self._ensure_polling()

    def _finish_operation_ui_safely(self) -> None:
        """Finish the UI bookkeeping, even if a widget has already been destroyed."""
        try:
            self._finish_operation_ui()
        except Exception:  # noqa: BLE001 - window closing mid-job
            LOGGER.debug("cannot repaint the footer; the window is going away")

    def _poll_once(self) -> None:
        self._drain_events()
        if self.worker.watchdog_expired():
            self._handle_watchdog()
        if self._worker_has_work():
            self._ensure_polling()
            return
        # The worker posts its terminal event and clears the job *before* its
        # thread stops, so ``busy`` going false promises that whatever it had
        # left to say is already in the queue.  Without this second drain a
        # thread that finished between the loop above and this check left its
        # DONE unhandled: the operation ended, nothing polled again, and the
        # result — the output path, the verification verdict — never reached the
        # interface.
        self._drain_events()
        if self._worker_has_work():
            # Handling those events may have started the next job (the probe a
            # finished embed schedules, for instance).
            self._ensure_polling()
            return
        if self._busy:
            self._finish_operation_ui()

    def _drain_events(self) -> None:
        """Handle every queued event; one bad handler must not stop the chain.

        A handler that raises used to take the poll callback with it: Tk prints the
        traceback, the ``after`` chain is never re-armed, and the window stays
        "busy" for an operation that has already finished with no way back.  The
        traceback is logged, the operation is finished and the footer says it
        failed — a visible error instead of a wedged window.
        """
        for event in self.worker.poll():
            try:
                self._handle_worker_event(event)
            except Exception:
                LOGGER.exception("worker event %s could not be handled", event.kind.value)
                self._finish_operation_ui()
                self._reset_progress()
                self._set_status_key("status_error", severity="error")

    def _worker_has_work(self) -> bool:
        """``worker.busy``, read through a call because it changes mid-poll."""
        return self.worker.busy

    def _handle_watchdog(self) -> None:
        LOGGER.warning("watchdog expired for %s", self._operation)
        answer = messagebox.askyesno(
            self.i18n.t("watchdog_t"),
            self.i18n.t("watchdog_m", seconds=int(self.worker.seconds_since_progress())),
            parent=self.root,
        )
        if answer:
            self.worker.cancel()
            self._set_status_key("status_cancelling", severity="warn")
        else:
            self.worker.reset_watchdog()

    def _handle_worker_event(self, event: WorkerEvent) -> None:
        if event.kind is WorkerEventType.PROGRESS:
            self._on_progress(event)
            return
        if event.kind is WorkerEventType.DONE:
            self._on_done(event)
            return
        if event.kind is WorkerEventType.CANCELLED:
            self._finish_operation_ui()
            # The bar used to keep whatever fraction the cancelled job reached,
            # which reads as "still running" when nothing is running any more.
            self._reset_progress()
            self._set_status_key("status_cancelled", severity="warn")
            return
        if event.kind is WorkerEventType.ERROR and event.error is not None:
            self._finish_operation_ui()
            self._reset_progress()
            self._show_error(event.error)

    def _on_progress(self, event: WorkerEvent) -> None:
        indeterminate = event.indeterminate or (
            event.stage is not None and event.stage in INDETERMINATE_STAGES
        )
        if indeterminate:
            if not self._progress_indeterminate:
                self._progress_indeterminate = True
                self.progress_bar.configure(mode="indeterminate")
                self.progress_bar.start()
            self._show_stage("lbl_progress_indeterminate", stage=event.stage)
            return
        if self._progress_indeterminate:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self._progress_indeterminate = False
        self._show_stage(
            "lbl_progress_stage", stage=event.stage, pct=f"{event.fraction * 100:.0f}"
        )
        self.animator.to(
            "progress",
            event.fraction,
            duration=0.15,
            on_update=self.progress_bar.set,
        )
        self._ensure_animation_loop()

    def _on_done(self, event: WorkerEvent) -> None:
        operation = self._operation
        value = event.value
        self._finish_operation_ui()
        if operation == "probe_embed" and isinstance(value, CarrierProbe):
            self.embed_vm.apply_probe(value)
            self._set_entry_text(self.output_entry, self.embed_vm.output_path)
            self._refresh_all()
            self._set_status_key("status_ready", severity="ok")
            self.debouncer.schedule(self._start_estimate)
        elif operation == "probe_extract" and isinstance(value, CarrierProbe):
            self.extract_vm.apply_probe(value)
            self._refresh_all()
            self._set_status_key("status_ready", severity="ok")
        elif operation == "estimate":
            if value is not None:
                self.embed_vm.apply_estimate(value)
            self._refresh_embed_state()
        elif operation == "embed" and isinstance(value, EmbedOutcome):
            self._on_embed_done(value)
        elif operation == "extract" and isinstance(value, ExtractOutcome):
            self._on_extract_done(value)
        elif (
            operation == "metadata_inspect"
            and isinstance(value, MetadataReport)
            and self.metadata_panel is not None
        ):
            self.metadata_panel.apply_report(value)
            LOGGER.info("%s", self.metadata_panel.summary_for_log())
        elif (
            operation == "metadata_clean"
            and isinstance(value, CleaningOutcome)
            and self.metadata_panel is not None
        ):
            self.metadata_panel.apply_outcome(value)
            LOGGER.info("%s", self.metadata_panel.summary_for_log())
        LOGGER.debug("operation %s finished", operation)

    def _on_embed_done(self, outcome: EmbedOutcome) -> None:
        # "Saved" is a claim about correctness, so it is only made when the
        # self-check really ran and really passed.
        if outcome.verified:
            self._set_status_key("status_done", severity="ok", path=outcome.output_path)
            self.stage_label.configure(text=self.i18n.t("audit_verified"))
        else:
            self._set_status_key(
                "status_unverified", severity="warn", path=outcome.output_path
            )
            self.stage_label.configure(text=self.i18n.t("audit_unverified"))
        self.animator.to("progress", 1.0, duration=0.2, on_update=self.progress_bar.set)
        self._ensure_animation_loop()
        self.settings.last_dir = os.path.dirname(outcome.output_path) or self.settings.last_dir
        self._save_settings()
        if outcome.wipe_error:
            self._set_status_key("status_error", severity="warn")
            self._show_warning(
                "warn_wipe_t", "warn_wipe_m", details=outcome.wipe_error
            )
        elif outcome.wiped_source:
            self._set_status_key("status_secure_del", severity="warn")
        if outcome.notices:
            LOGGER.info("embed notices: %s", ", ".join(outcome.notices))

    def _on_extract_done(self, outcome: ExtractOutcome) -> None:
        self.extract_vm.apply_result(outcome)
        self._refresh_extract_state()
        payload = outcome.payload
        if outcome.verified:
            self._set_status_key(
                "status_extracted_bytes", severity="ok", size=payload.original_size
            )
        else:
            self._set_status_key("status_error", severity="warn")
        self.animator.to("progress", 1.0, duration=0.2, on_update=self.progress_bar.set)
        self._ensure_animation_loop()

    def _finish_operation_ui(self) -> None:
        self._busy = False
        self._resume_collection_after_job()
        self._operation = ""
        self._stop_stage_tick()
        self._operation_started = 0.0
        self._stage_key = None
        # Leave no frozen "embedding · 74 % · 12 s" behind on cancel or failure;
        # a finished operation paints its own footer line right after this.
        self.stage_label.configure(text=self.i18n.t("lbl_progress_idle"))
        if self._progress_indeterminate:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self._progress_indeterminate = False
        self._refresh_busy_state()

    def _reset_progress(self) -> None:
        if self._progress_indeterminate:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            self._progress_indeterminate = False
        self.animator.set("progress", 0.0, notify=self.progress_bar.set)
        self._stage_key = None
        self.stage_label.configure(text=self.i18n.t("lbl_progress_idle"))

    # -- footer: stage line and elapsed time ------------------------------
    def _show_stage(
        self, key: str, *, stage: Stage | None = None, pct: str | None = None
    ) -> None:
        """Remember what the footer should say, then paint it.

        The stage is stored as the enum, never as finished text: the line is
        repainted for the elapsed-time tick and again when the language changes,
        and a name translated at event time would survive both unchanged.
        """
        self._stage_key = (key, stage, pct)
        self._render_stage_label()

    def _render_stage_label(self) -> None:
        """Repaint the stage line, appending the elapsed time while a job runs."""
        if self._stage_key is None:
            return
        key, stage, pct = self._stage_key
        self.stage_label.configure(
            text=ui_format.stage_line(
                self.i18n, key, stage=stage, pct=pct, seconds=self._elapsed_seconds()
            )
        )

    def _elapsed_seconds(self) -> int | None:
        """Whole seconds since the running operation started, or ``None`` if idle."""
        if not self._busy or self._operation_started <= 0.0:
            return None
        return int(time.monotonic() - self._operation_started)

    def _ensure_stage_tick(self) -> None:
        if self._stage_tick_token is None:
            self._stage_tick_token = self.root.after(C.STAGE_TICK_MS, self._stage_tick)

    def _stop_stage_tick(self) -> None:
        token, self._stage_tick_token = self._stage_tick_token, None
        if token is not None:
            # Tk forgets a callback id once it has fired, and cancelling it then
            # raises; nothing to do in that case.
            with contextlib.suppress(TclError):
                self.root.after_cancel(token)

    def _stage_tick(self) -> None:
        """Once a second: repaint the stage line so a long phase keeps counting."""
        self._stage_tick_token = None
        if not self._busy:
            return
        self._render_stage_label()
        self._ensure_stage_tick()

    def _ensure_animation_loop(self) -> None:
        if self._anim_token is None and self.animator.active:
            self._anim_token = self.root.after(self.animator.tick_ms, self._animation_tick)

    def _animation_tick(self) -> None:
        self._anim_token = None
        self.animator.step()
        self._ensure_animation_loop()

    # -- primary operations --------------------------------------------
    def _start_embed(self) -> None:
        if self.worker.busy:
            self._show_warning("warn_operation_t", "warn_operation_m")
            return
        self.embed_vm.set_password(self.embed_pwd.get())
        self.embed_vm.set_text(self.embed_text.get("1.0", "end-1c"))
        self.embed_vm.set_output(self.output_entry.get())
        try:
            request = self.embed_vm.snapshot()
        except PhantomError as exc:
            self._show_error(exc)
            return
        if request.wipe_source and request.secret_path:
            confirmed = messagebox.askyesno(
                self.i18n.t("confirm_wipe_t"),
                self.i18n.t("confirm_wipe_m", file=request.secret_path),
                parent=self.root,
            )
            if not confirmed:
                return
        job = self.embed_vm.make_embed_job()
        self._start_job("embed", job, "status_embedding")

    def _start_extract(self) -> None:
        if self.worker.busy:
            self._show_warning("warn_operation_t", "warn_operation_m")
            return
        self._clear_extract_result_only()
        self.extract_vm.password = self.extract_pwd.get()
        try:
            job = self.extract_vm.make_extract_job()
        except PhantomError as exc:
            self._show_error(exc)
            return
        self._start_job("extract", job, "status_extracting")

    def _cancel_operation(self) -> None:
        if not self.worker.busy:
            return
        self.worker.cancel()
        self._set_status_key("status_cancelling", severity="warn")
        self.cancel_button.configure(state="disabled")

    # -- clipboard / result helpers --------------------------------------
    def _password_entry(self) -> Any:
        """The password field of the visible tab (both tabs have one)."""
        if self.current_tab == "extract":
            return self.extract_pwd
        return self.embed_pwd

    def _paste_password(self) -> None:
        """Paste button: a fallback for systems where Cmd+V never arrives."""
        entry = self._password_entry()
        pasted = paste_into(entry)
        if pasted is None:
            self._set_status_key("status_clipboard_empty", severity="warn")
            return
        entry.focus_set()
        if entry is self.embed_pwd:
            self._on_password_changed()
        self._set_status_key("status_pasted", severity="ok")

    def _copy_password(self) -> None:
        value = self._password_entry().get()
        if not value:
            self._set_status_key("status_password_empty", severity="warn")
            return
        self._copy_to_clipboard(value)
        self._set_status_key("status_copied", severity="ok")

    def _result_path(self) -> str:
        """Where the extracted file is (or would be) saved; empty for text."""
        payload = self.extract_vm.result
        if payload is None or payload.kind is not PayloadKind.FILE:
            return ""
        return self.extract_vm.save_path or self.extract_vm.default_save_path()

    def _copy_result_path(self) -> None:
        path = self._result_path()
        if not path:
            self._set_status_key("status_path_missing", severity="warn")
            return
        self._copy_to_clipboard(path)
        self._set_status_key("status_copied", severity="ok")

    def _reveal_result(self) -> None:
        path = self._result_path()
        if not path:
            self._set_status_key("status_path_missing", severity="warn")
            return
        if fsutil.reveal_in_file_manager(path):
            self._set_status_key("status_revealed", severity="ok", path=path)
        else:
            self._set_status_key("status_reveal_failed", severity="warn")

    def _expand_result(self) -> None:
        """Open the result in a large, copyable window (80 % of the screen)."""
        if self._result_window is not None:
            self._result_window.close()
            self._result_window = None
        text = clip.all_text(CTkAdapter.inner_textbox(self.result_text) or self.result_text)
        if not text:
            payload = self.extract_vm.result
            if payload is not None and payload.kind is not PayloadKind.TEXT:
                text = f"{payload.name}\n{payload.original_size} B"
        labels = {
            "result_window_title": self._t("result_window_title"),
            "btn_copy_text": self._t("btn_copy_text"),
            "btn_copy_path": self._t("btn_copy_path"),
            "btn_reveal": self._t("btn_reveal", file_manager=self.file_manager_name()),
            "btn_close": self._t("btn_close"),
            "menu_paste": self._t("menu_paste"),
            "menu_copy": self._t("menu_copy"),
            "menu_cut": self._t("menu_cut"),
            "menu_select_all": self._t("menu_select_all"),
            "status_copied": self._t("status_copied"),
            "status_path_missing": self._t("status_path_missing"),
            "status_revealed": self._t("status_revealed"),
            "status_reveal_failed": self._t("status_reveal_failed"),
        }
        window = ResultWindow(
            self.root,
            title=self._t("result_window_title"),
            text=text,
            labels=labels,
            path=self._result_path() or None,
            copy_callback=self._copy_for_window,
            on_close=self._forget_result_window,
        )
        self._result_window = window

    def _forget_result_window(self) -> None:
        self._result_window = None

    def _copy_for_window(self, value: str) -> bool:
        """Copy from the result window, keeping the 30 s auto-clear promise."""
        self._copy_to_clipboard(value)
        return True

    def _copy_result(self) -> None:
        payload = self.extract_vm.result
        if payload is None or payload.kind is not PayloadKind.TEXT:
            return
        self._copy_to_clipboard(payload.data.decode("utf-8", errors="replace"))
        self._set_status_key("status_copied", severity="ok")

    def _save_result_file(self) -> None:
        payload = self.extract_vm.result
        if payload is None or payload.kind is not PayloadKind.FILE:
            return
        target = self.extract_vm.save_path or self.extract_vm.default_save_path()
        if not target:
            self._save_result_file_as()
            return
        try:
            # Owner-only (0600), verified before it replaces anything, and the
            # name is re-sanitised here even though the container was checked.
            target = fsutil.write_extracted_file(
                os.path.dirname(target) or ".",
                os.path.basename(target),
                payload.data,
                expected_sha256=payload.sha256_actual,
            )
        except PhantomError as exc:
            self._show_error(exc)
            return
        self._set_status_key("status_file_saved", severity="ok", path=target)
        self.settings.last_dir = os.path.dirname(target) or self.settings.last_dir
        self._save_settings()

    def _save_result_file_as(self) -> None:
        payload = self.extract_vm.result
        if payload is None or payload.kind is not PayloadKind.FILE:
            return
        target = filedialog.asksaveasfilename(
            parent=self.root,
            title=self.i18n.t("dlg_save_file"),
            initialdir=self.settings.last_dir or None,
            initialfile=payload.name or "extracted.bin",
        )
        if not target:
            return
        try:
            fsutil.write_bytes_verified(
                target, payload.data, expected_sha256=payload.sha256_actual
            )
        except PhantomError as exc:
            self._show_error(exc)
            return
        self._set_status_key("status_file_saved", severity="ok", path=target)
        self.settings.last_dir = os.path.dirname(target) or self.settings.last_dir
        self._save_settings()

    def _clear_extract_result_only(self) -> None:
        self.extract_vm.result = None
        self.extract_vm.outcome = None
        self._set_textbox(self.result_text, "")
        self._refresh_extract_state()

    def _clear_extract(self) -> None:
        self._clear_extract_result_only()
        self.extract_vm.clear()
        self.extract_pwd.delete(0, "end")
        self._refresh_all()
        self._set_status_key("status_ready", severity="info")

    def _clear_embed(self) -> None:
        self.embed_vm.clear()
        self._suppress_events = True
        try:
            self.embed_text.delete("1.0", "end")
            self.embed_pwd.delete(0, "end")
            self._set_entry_text(self.output_entry, "")
            self.secret_label.configure(text=self.i18n.t("lbl_no_secret_file"))
        finally:
            self._suppress_events = False
        self._refresh_all()

    # -- settings -------------------------------------------------------
    def _on_language_click(self, label: str) -> None:
        code = self._lang_by_label.get(label)
        if not code or code == self.i18n.language:
            return
        self.i18n.set_language(code)
        self.settings.language = code
        self._save_settings()
        self.root.title(self._t("window_title"))
        self._render_all_labels()
        self._refresh_all()
        self._set_status_key(self._status_key, severity="info", **self._status_params)

    def _on_theme_click(self, label: str) -> None:
        mode = self._theme_by_label.get(label)
        if not mode or mode == self.settings.theme:
            return
        self._apply_theme(mode)
        self._set_status_key("status_theme", severity="ok", mode=label)

    def _set_appearance_mode(self, mode: str) -> None:
        """Hand CustomTkinter a new appearance mode without flushing the window.

        ``set_appearance_mode`` notifies every registered widget synchronously and
        each notification ends with a full ``update_idletasks()``: one click is a
        few hundred nested display passes, all of them inside the button's own Tk
        callback.  On Aqua under Tk 9.1 that call segfaults (it was the last
        reproducible native crash in this window), so the passes are refused while
        the mode change runs — every widget recolours itself either way, Tk paints
        when this returns, and the window runs one pass of its own from an idle
        task (:meth:`_schedule_layout_flush`).
        """
        with quiet_redraws():
            ctk.set_appearance_mode(mode)
        self._schedule_layout_flush()

    def _apply_theme(self, theme: str, *, persist: bool = True) -> None:
        self.settings.theme = theme if theme in T.THEME_MODES else "System"
        self._set_appearance_mode(T.appearance_for(self.settings.theme))
        self.theme_watcher.set_enabled(self.settings.theme == "System")
        self._schedule_theme_poll()
        self._render_theme_labels()
        if persist:
            self._save_settings()

    def _schedule_theme_poll(self) -> None:
        if self.theme_watcher.enabled and self._theme_token is None:
            self._theme_token = self.root.after(
                self.theme_watcher.interval_ms, self._theme_tick
            )

    def _theme_tick(self) -> None:
        self._theme_token = None
        changed = self.theme_watcher.poll()
        if changed is not None:
            LOGGER.info("OS theme changed to %s", changed)
            self._set_appearance_mode("System")
        self._schedule_theme_poll()

    def _on_animations_toggle(self) -> None:
        enabled = bool(self.animations_switch.get())
        self.settings.animations = enabled
        self.animator.set_enabled(enabled)
        self._save_settings()
        self._set_status_key("status_settings_saved", severity="info")

    def _on_verify_switch(self) -> None:
        self.settings.verify_after_embed = bool(self.verify_switch.get())
        self.embed_vm.set_verify(self.settings.verify_after_embed)
        self._save_settings()
        self._refresh_extract_state()

    def _on_reset_settings(self) -> None:
        self.settings = self.store.reset()
        self.i18n.set_language(self.settings.language)
        self.embed_vm.settings = self.settings
        self.extract_vm.settings = self.settings
        self.embed_vm.set_verify(self.settings.verify_after_embed)
        self.animator.set_enabled(self.settings.animations)
        self._apply_theme(self.settings.theme, persist=False)
        self._render_all_labels()
        self._refresh_all()
        self._set_status_key("status_settings_saved", severity="ok")

    def _save_settings(self) -> None:
        try:
            self.store.save(self.settings)
        except OSError as exc:
            LOGGER.warning("cannot save settings: %s", exc)

    # -- window lifecycle ------------------------------------------------
    def _restore_geometry(self) -> None:
        geometry = self.settings.window_geometry
        if geometry and GEOMETRY_RE.match(geometry):
            self.root.geometry(geometry)

    def _wire_shortcuts(self) -> None:
        for sequence in ("<Control-Return>", "<Command-Return>"):
            self.root.bind(sequence, self._shortcut_primary)
        self.root.bind("<Escape>", lambda _event: self._cancel_operation())
        tabs = ("embed", "extract", "metadata", "settings")
        for index, ident in enumerate(tabs, start=1):
            for prefix in ("Control", "Command"):
                self.root.bind(
                    f"<{prefix}-Key-{index}>",
                    lambda _event, target=ident: self._show_tab(target),
                )

    def _shortcut_primary(self, _event: object | None = None) -> str:
        if self.current_tab == "embed":
            self._start_embed()
        elif self.current_tab == "extract":
            self._start_extract()
        return "break"

    def refresh_window(self) -> None:
        """Bring the window forward and give it its display pass, on every platform.

        macOS additionally needs ``lift()``: a window the window server has not
        brought forward yet paints late.  The pass itself goes through
        :meth:`_flush_layout`, so it is skipped for a window that is not on screen
        instead of forcing Tk to display one that has no surface.
        """
        if T.IS_MACOS:
            try:
                self.root.lift()
            except Exception as exc:  # noqa: BLE001 - pragma: no cover, macOS only
                LOGGER.debug("macOS lift skipped: %s", exc)
        self._flush_layout()

    def _on_close(self) -> None:
        if self.worker.busy:
            answer = messagebox.askyesno(
                self.i18n.t("confirm_close_t"),
                self.i18n.t("confirm_close_m"),
                parent=self.root,
            )
            if not answer:
                return
            self.worker.cancel()
            self.worker.shutdown(timeout=3.0)
        # The window is going away; leave the interpreter with a working collector
        # even if the last job never reported back.
        self._resume_collection_after_job()
        if self._poll_token is not None:
            self.root.after_cancel(self._poll_token)
            self._poll_token = None
        if self._clipboard_token is not None:
            self.root.after_cancel(self._clipboard_token)
            self._clipboard_token = None
        if self._layout_token is not None:
            self.root.after_cancel(self._layout_token)
            self._layout_token = None
        # The remaining timers die with the interpreter anyway, but a callback that
        # fires between destroy() and exit still reaches widgets that are gone.
        self._cancel_gc_resume()
        for name in ("_stage_tick_token", "_anim_token", "_theme_token"):
            token = getattr(self, name)
            if token is not None:
                self.root.after_cancel(token)
                setattr(self, name, None)
        self._forget_clipboard()
        if self._result_window is not None:
            self._result_window.close()
            self._result_window = None
        self._release_scroll()
        geometry = self.root.geometry()
        if GEOMETRY_RE.match(geometry):
            self.settings.window_geometry = geometry
        self._save_settings()
        self._clear_extract_result_only()
        self._generated_password = ""
        LOGGER.info("window closed")
        self.root.destroy()


def build_root() -> tuple[ctk.CTk, DndProvider]:
    provider = DndProvider()
    root = provider.make_ctk_root()
    return root, provider


def main(trace: StartupTrace | None = None) -> int:
    """Create the window and run it; ``trace`` is only passed by ``launch``."""
    logging.basicConfig(
        level=os.environ.get("PHANTOM_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    trace = trace if trace is not None else StartupTrace()
    ctk.set_default_color_theme("blue")
    with trace.stage("root"):
        root, provider = build_root()
    app = PhantomApp(root, dnd=provider, trace=trace)
    # "ready" is the moment the window is really on screen: everything before it
    # is what the user sees as a frozen, empty frame.
    mapped: dict[str, bool] = {"done": False}

    def on_map(_event: object | None = None) -> None:
        if mapped["done"]:
            return
        mapped["done"] = True
        trace.mark("ready")
        trace.log_summary()

    root.bind("<Map>", on_map, add="+")
    # Painting once before the loop is what makes the first frame the *first* thing
    # the user sees.  It goes through the same guard the window uses everywhere
    # else: a window that is not on screen yet keeps the request and runs it on the
    # <Map> event instead of asking Tk to display a surface it does not have.
    app.refresh_window()
    trace.mark("first_frame")
    trace.mark("mainloop")
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
