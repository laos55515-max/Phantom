"""
app.py — PHANTOM · steganography suite (CustomTkinter · macOS-first).
All cryptographic / steganographic logic lives in core.py.
"""

from __future__ import annotations

import os
import sys
import json
import queue
import string
import secrets
import threading
import tempfile
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from PIL import Image as PILImage
from tkinterdnd2 import DND_FILES, TkinterDnD

import core
from phantom import AudioSteganographyEngine

IS_MACOS = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

WIN_W, WIN_H = 1060, 920
PAD_OUT = 26
PAD_CARD = 24
GAP = 16
CORNER_CARD = 18
CORNER_CTRL = 12
PREVIEW_SIZE = 142
TEXTBOX_HEIGHT = 196

MIN_PASSWORD_LENGTH = 12
GENERATED_PASSWORD_LENGTH = 16
GEN_SYMBOL_ALPHABET = "!@#$%^&*()-_=+[]{};:,.<>?/~|"

TRANSPARENT      = "transparent"

CLR_BG           = ("#F3F4F6", "#0B0F19")
CLR_LAYER        = ("#EAECF1", "#0F1523")
CLR_CARD         = ("#FFFFFF", "#151B2B")
CLR_CARD_ALT     = ("#F3F4F6", "#1C2438")
CLR_FIELD        = ("#FFFFFF", "#0D121E")
CLR_BORDER       = ("#E5E7EB", "#1F2937")

CLR_ACCENT       = ("#2563EB", "#2563EB")
CLR_ACCENT_HOVER = ("#1D4ED8", "#3B82F6")
CLR_ACCENT_SOFT  = ("#2563EB", "#93B4FC")
CLR_ACCENT_DIM   = ("#DBEAFE", "#1B2E4D")
# Keep the dark label readable on selected segments in light appearance.
CLR_SEGMENT_SELECTED = ("#BFDBFE", "#2563EB")
CLR_SEGMENT_HOVER = ("#93C5FD", "#3B82F6")

CLR_TEXT         = ("#111827", "#F9FAFB")
CLR_TEXT_DIM     = ("#4B5563", "#9CA3AF")
CLR_MUTED        = ("#6B7280", "#6B7280")

CLR_GREEN        = ("#047857", "#34D399")
CLR_RED          = ("#B91C1C", "#F87171")
CLR_YELLOW       = ("#B45309", "#FBBF24")

CLR_SCROLL       = ("#D1D5DB", "#243044")
CLR_SCROLL_HOVER = ("#2563EB", "#3B82F6")
CLR_ON_ACCENT    = ("#FFFFFF", "#FFFFFF")

LANGUAGES = ["Українська", "Русский", "English"]
DEFAULT_LANG = "Українська"
THEME_MODES = ("Dark", "Light", "System")

MODE_TEXT = "text"
MODE_FILE = "file"


def resolve(color):
    if isinstance(color, (tuple, list)):
        return color[0] if ctk.get_appearance_mode() == "Light" else color[1]
    return color


def _display_pref() -> str:
    if IS_MACOS:
        return "SF Pro Display"
    if IS_WINDOWS:
        return "Segoe UI Variable Display"
    return "Inter"


def _sans_pref() -> str:
    if IS_MACOS:
        return "SF Pro Text"
    if IS_WINDOWS:
        return "Segoe UI"
    return "Inter"


def _mono_pref() -> str:
    if IS_MACOS:
        return "SF Mono"
    if IS_WINDOWS:
        return "Cascadia Mono"
    return "JetBrains Mono"


def _display_fallback() -> str:
    if IS_MACOS:
        return "Helvetica Neue"
    if IS_WINDOWS:
        return "Segoe UI"
    return "DejaVu Sans"


def _mono_fallback() -> str:
    if IS_MACOS:
        return "Menlo"
    if IS_WINDOWS:
        return "Consolas"
    return "DejaVu Sans Mono"


def pick_font(root, preferred: str, fallback: str) -> str:
    try:
        import tkinter.font as tkfont
        if preferred in set(tkfont.families(root)):
            return preferred
    except Exception:
        pass
    return fallback


def generate_secure_password(length: int = GENERATED_PASSWORD_LENGTH) -> str:
    if length < 4:
        raise ValueError("Password length must be at least 4.")
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    symbols = GEN_SYMBOL_ALPHABET
    alphabet = upper + lower + digits + symbols
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c in upper for c in pwd) and
                any(c in lower for c in pwd) and
                any(c in digits for c in pwd) and
                any(c in symbols for c in pwd)):
            return pwd


def _make_thumbnail(path: str, size: int = PREVIEW_SIZE):
    try:
        with PILImage.open(path) as image:
            image.thumbnail((size, size), PILImage.LANCZOS)
            return image.copy()
    except Exception:
        return None


def _placeholder_preview(size: int = PREVIEW_SIZE):
    light_img = PILImage.new("RGB", (size, size), color="#F3F4F6")
    dark_img = PILImage.new("RGB", (size, size), color="#151B2B")
    return ctk.CTkImage(light_image=light_img, dark_image=dark_img,
                        size=(size, size))


# Localized strings are loaded once, and widgets are updated on language changes.
_LOCAL_DIR = Path(__file__).resolve().parent / "locales"
TRANSLATIONS = {
    name: json.loads((_LOCAL_DIR / f"{code}.json").read_text(encoding="utf-8"))
    for name, code in (("Українська", "ua"), ("Русский", "ru"), ("English", "en"))
}


class App(ctk.CTk):

    def __init__(self):
        super().__init__(fg_color=CLR_BG)

        self.language = DEFAULT_LANG
        self._theme_mode = "Dark"
        self._status_key = "status_ready"
        self._status_args: dict = {}
        self._last_appearance = ctk.get_appearance_mode()

        self._embed_image_path = None
        self._embed_secret_file = None
        self._extract_image_path = None
        self._embed_capacity = None
        self._embed_capacity_error = None
        self._embed_mode = MODE_TEXT
        self._embed_estimated_size = None
        self._last_audit = None

        self._ctx_widgets: list = []
        self._ctx_menus: list = []
        self._textboxes: list = []

        self.embed_scroll_frame = None
        self.extract_scroll_frame = None
        self.settings_scroll_frame = None
        self.audio_scroll_frame = None
        self._audio_path = None
        self._audio_secret_file = None
        self._events = queue.Queue()
        self._busy = False
        self._audio_i18n = []
        self._dnd_enabled = False
        try:
            TkinterDnD.require(self)
            self._dnd_enabled = True
        except (RuntimeError, tk.TclError):
            pass  # File pickers remain usable where the native extension is unavailable.

        self._build_fonts()

        self.title(self._t("window_title"))
        self.geometry(f"{WIN_W}x{WIN_H}")
        self.minsize(760, 540)
        self.configure(fg_color=CLR_BG)

        self._build_header()

        self._tab_names = {key: self._t(f"tab_{key}") for key in ("embed", "extract")}
        self._main_tab_names = {key: self._t(f"tab_{key}") for key in ("image", "audio", "settings")}

        self.main_tabs = ctk.CTkTabview(
            self, bg_color=TRANSPARENT, fg_color=CLR_LAYER,
            corner_radius=CORNER_CARD, segmented_button_selected_color=CLR_SEGMENT_SELECTED,
            segmented_button_selected_hover_color=CLR_SEGMENT_HOVER,
            segmented_button_unselected_color=CLR_CARD_ALT, text_color=CLR_TEXT)
        self.main_tabs.pack(fill="both", expand=True, padx=24, pady=(8, 10))
        for name in self._main_tab_names.values():
            self.main_tabs.add(name)

        self.tabview = ctk.CTkTabview(
            self.main_tabs.tab(self._main_tab_names["image"]),
            bg_color=TRANSPARENT,
            fg_color=CLR_LAYER,
            corner_radius=CORNER_CARD,
            border_width=1,
            border_color=CLR_BORDER,
            segmented_button_fg_color=CLR_CARD_ALT,
            segmented_button_selected_color=CLR_SEGMENT_SELECTED,
            segmented_button_selected_hover_color=CLR_SEGMENT_HOVER,
            segmented_button_unselected_color=CLR_CARD_ALT,
            segmented_button_unselected_hover_color=CLR_ACCENT_DIM,
            text_color=CLR_TEXT,
            anchor="center")
        self.tabview.pack(fill="both", expand=True)
        try:
            self.tabview._segmented_button.configure(font=self.f_tab)
        except Exception:
            pass

        for key in ("embed", "extract"):
            self.tabview.add(self._tab_names[key])

        self._build_statusbar()

        self._build_embed_tab(self.tabview.tab(self._tab_names["embed"]))
        self._build_extract_tab(self.tabview.tab(self._tab_names["extract"]))
        self._build_audio_tab(self.main_tabs.tab(self._main_tab_names["audio"]))
        self._build_settings_tab(self.main_tabs.tab(self._main_tab_names["settings"]))
        self.tabview.set(self._tab_names["embed"])
        self.main_tabs.set(self._main_tab_names["image"])
        self._register_drop_targets()
        self.after(50, self._poll_queue)

        self.bind_all("<MouseWheel>", self._mac_global_scroll)
        if IS_LINUX:
            self.bind_all("<Button-4>", self._mac_global_scroll)
            self.bind_all("<Button-5>", self._mac_global_scroll)

        self.after(60, self._sync_surfaces)
        self.after(500, self._watch_appearance)

    def _build_fonts(self):
        disp = pick_font(self, _display_pref(), _display_fallback())
        sans = pick_font(self, _sans_pref(), _display_fallback())
        mono = pick_font(self, _mono_pref(), _mono_fallback())

        self.f_sigil = ctk.CTkFont(family=disp, size=30, weight="bold")
        self.f_brand = ctk.CTkFont(family=disp, size=24, weight="bold")
        self.f_brand_sub = ctk.CTkFont(family=mono, size=10)
        self.f_tab = ctk.CTkFont(family=sans, size=13, weight="bold")
        self.f_section = ctk.CTkFont(family=disp, size=21, weight="bold")
        self.f_card_title = ctk.CTkFont(family=sans, size=11, weight="bold")
        self.f_label = ctk.CTkFont(family=sans, size=13, weight="bold")
        self.f_body = ctk.CTkFont(family=sans, size=12)
        self.f_hint = ctk.CTkFont(family=sans, size=11)
        self.f_btn_lg = ctk.CTkFont(family=sans, size=14, weight="bold")
        self.f_btn_sm = ctk.CTkFont(family=sans, size=12, weight="bold")

        self.f_mono = ctk.CTkFont(family=mono, size=13)
        self.f_mono_sm = ctk.CTkFont(family=mono, size=11)
        self.f_mono_xs = ctk.CTkFont(family=mono, size=10)
        self.f_status = ctk.CTkFont(family=mono, size=11)

    def _mac_global_scroll(self, event):
        try:
            widget = self.winfo_containing(event.x_root, event.y_root)
            if not widget:
                return
            widget_name = str(widget).lower()

            if "text" in widget_name or "entry" in widget_name:
                return

            main_tab = self.main_tabs.get()
            active_tab = self.tabview.get()
            target_canvas = None

            if main_tab == self._main_tab_names["audio"]:
                target_canvas = self.audio_scroll_frame._parent_canvas
            elif main_tab == self._main_tab_names["settings"]:
                target_canvas = self.settings_scroll_frame._parent_canvas
            elif active_tab == self._tab_names["embed"] and getattr(self, "embed_scroll_frame", None):
                target_canvas = self.embed_scroll_frame._parent_canvas
            elif active_tab == self._tab_names["extract"] and getattr(self, "extract_scroll_frame", None):
                target_canvas = self.extract_scroll_frame._parent_canvas

            if target_canvas:
                if IS_LINUX and getattr(event, "num", None) in (4, 5):
                    direction = -1 if event.num == 4 else 1
                else:
                    delta = getattr(event, "delta", 0)
                    if delta == 0:
                        return
                    direction = -1 if delta > 0 else 1
                target_canvas.yview_scroll(direction, "units")
        except Exception:
            pass

    def _bind_shortcuts(self, widget):
        inner = (getattr(widget, "_textbox", None) or
                 getattr(widget, "_entry", None) or widget)
        is_text = isinstance(inner, tk.Text)

        def do_copy(event=None):
            try:
                inner.event_generate("<<Copy>>")
                self._set_status("status_copied")
            except Exception:
                pass
            return "break"

        def do_paste(event=None):
            try:
                inner.event_generate("<<Paste>>")
                self._set_status("status_pasted")
            except Exception:
                pass
            self.after(30, self._safe_refresh_embed_ui)
            return "break"

        def do_cut(event=None):
            try:
                inner.event_generate("<<Cut>>")
            except Exception:
                pass
            self.after(30, self._safe_refresh_embed_ui)
            return "break"

        def do_select_all(event=None):
            try:
                if is_text:
                    inner.tag_add("sel", "1.0", "end-1c")
                    inner.mark_set("insert", "1.0")
                    inner.see("insert")
                else:
                    inner.select_range(0, "end")
                    inner.icursor("end")
            except Exception:
                pass
            return "break"

        def do_clear(event=None):
            try:
                if is_text:
                    widget.delete("1.0", "end")
                else:
                    widget.delete(0, "end")
            except Exception:
                pass
            self.after(30, self._safe_refresh_embed_ui)
            return "break"

        for seq, fn in (
            ("<Command-a>", do_select_all), ("<Command-A>", do_select_all),
            ("<Command-c>", do_copy),       ("<Command-C>", do_copy),
            ("<Command-v>", do_paste),      ("<Command-V>", do_paste),
            ("<Command-x>", do_cut),        ("<Command-X>", do_cut),
            ("<Control-a>", do_select_all), ("<Control-A>", do_select_all),
            ("<Control-c>", do_copy),       ("<Control-C>", do_copy),
            ("<Control-v>", do_paste),      ("<Control-V>", do_paste),
            ("<Control-x>", do_cut),        ("<Control-X>", do_cut),
            ("<Meta-a>", do_select_all),    ("<Meta-A>", do_select_all),
            ("<Meta-c>", do_copy),          ("<Meta-C>", do_copy),
            ("<Meta-v>", do_paste),         ("<Meta-V>", do_paste),
            ("<Meta-x>", do_cut),           ("<Meta-X>", do_cut),
        ):
            try:
                inner.bind(seq, fn, add="+")
            except Exception:
                pass
            try:
                widget.bind(seq, fn, add="+")
            except Exception:
                pass

        def focus_inner(_e=None):
            try:
                inner.focus_set()
            except Exception:
                pass

        widget.bind("<FocusIn>", focus_inner, add="+")
        widget.bind("<Button-1>", focus_inner, add="+")

        menu = tk.Menu(widget, tearoff=0,
                       background=resolve(CLR_CARD),
                       foreground=resolve(CLR_TEXT),
                       activebackground=resolve(CLR_ACCENT),
                       activeforeground="#FFFFFF",
                       bd=0, relief="flat")
        self._ctx_menus.append(menu)

        def rebuild_menu():
            menu.delete(0, "end")
            menu.add_command(label=self._t("ctx_paste"), command=do_paste)
            menu.add_command(label=self._t("ctx_copy"), command=do_copy)
            menu.add_command(label=self._t("ctx_cut"), command=do_cut)
            menu.add_separator()
            menu.add_command(label=self._t("ctx_select_all"), command=do_select_all)
            menu.add_command(label=self._t("ctx_clear"), command=do_clear)

        rebuild_menu()
        widget._rebuild_context_menu = rebuild_menu
        self._ctx_widgets.append(widget)

        def show_menu(event):
            focus_inner()
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return "break"

        for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            try:
                inner.bind(seq, show_menu, add="+")
            except Exception:
                pass
            try:
                widget.bind(seq, show_menu, add="+")
            except Exception:
                pass

    def _register_textbox(self, textbox):
        self._textboxes.append(textbox)

    def _safe_refresh_embed_ui(self):
        try:
            self._refresh_char_count()
            self._refresh_capacity_bar()
        except Exception:
            pass

    def _refresh_all_context_menus(self):
        for w in self._ctx_widgets:
            try:
                w._rebuild_context_menu()
            except Exception:
                pass

    def _sync_surfaces(self):
        page_bg = resolve(CLR_LAYER)
        for sf in (self.embed_scroll_frame,
                   self.extract_scroll_frame,
                   self.audio_scroll_frame,
                   self.settings_scroll_frame):
            if sf is None:
                continue
            try:
                canvas = getattr(sf, "_parent_canvas", None)
                if canvas is not None:
                    canvas.configure(bg=page_bg, highlightthickness=0, bd=0)
            except Exception:
                pass

        card_bg = resolve(CLR_CARD)
        text_fg = resolve(CLR_TEXT)
        accent = resolve(CLR_ACCENT)
        for m in self._ctx_menus:
            try:
                m.configure(background=card_bg, foreground=text_fg,
                            activebackground=accent, activeforeground="#FFFFFF")
            except Exception:
                pass

    def _watch_appearance(self):
        # Native Tk canvases and menus need explicit refresh when the OS changes theme.
        current = ctk.get_appearance_mode()
        if current != self._last_appearance:
            self._last_appearance = current
            self._sync_surfaces()
        self.after(500, self._watch_appearance)

    def _t(self, key: str) -> str:
        pack = TRANSLATIONS.get(self.language, TRANSLATIONS["English"])
        return pack.get(key, TRANSLATIONS["English"].get(key, key))

    def _filetypes_png(self):
        return [(self._t("ft_png"), "*.png"), (self._t("ft_all"), "*.*")]

    def _filetypes_all(self):
        return [(self._t("ft_all"), "*.*")]

    def change_language(self, lang: str):
        if lang not in TRANSLATIONS:
            return
        self.language = lang

        self.title(self._t("window_title"))
        self.lbl_header_title.configure(text=self._t("header_title"))
        self.lbl_header_sub.configure(text=self._t("header_subtitle"))
        self._rename_tabs()

        self.embed_card_payload_title.configure(text=self._t("card_payload"))
        self.embed_card_pwd_title.configure(text=self._t("card_password"))
        self.embed_card_carrier_title.configure(text=self._t("card_carrier"))
        self.embed_radio_text.configure(text=self._t("radio_text"))
        self.embed_radio_file.configure(text=self._t("radio_file"))
        self.embed_lbl_section.configure(text=self._t("lbl_secret_message"))
        self.embed_lbl_hint.configure(text=self._t("lbl_secret_hint"))
        self._refresh_char_count()
        self.embed_lbl_pwd.configure(text=self._t("lbl_password"))
        self.embed_pwd.configure(placeholder_text=self._t("ph_password_embed"))
        self.embed_chk_pwd.configure(text=self._t("chk_show_password"))
        self.embed_btn_gen_pwd.configure(text=self._t("btn_generate_pwd"))
        self.embed_lbl_pwd_hint.configure(
            text=self._t("lbl_pwd_hint").format(min_len=MIN_PASSWORD_LENGTH))
        self.embed_btn_img.configure(text=self._t("btn_choose_carrier"))
        self.embed_btn_run.configure(text=self._t("btn_encrypt"))
        self.embed_btn_reset.configure(text=self._t("btn_clear"))
        self.embed_btn_secret_file.configure(text=self._t("btn_choose_file"))
        self.embed_chk_wipe.configure(text=self._t("chk_secure_delete"))
        self.embed_lbl_img.configure(
            text=os.path.basename(self._embed_image_path) if self._embed_image_path
            else self._t("lbl_no_file"))
        self.embed_lbl_secret_file.configure(
            text=os.path.basename(self._embed_secret_file) if self._embed_secret_file
            else self._t("lbl_no_secret_file"))
        self._refresh_capacity_label()
        self._refresh_file_size_label()
        self._refresh_capacity_bar()

        self.extract_card_stego_title.configure(text=self._t("card_stego"))
        self.extract_card_pwd_title.configure(text=self._t("card_password"))
        self.extract_card_result_title.configure(text=self._t("card_result"))
        self.extract_btn_img.configure(text=self._t("btn_choose_stego"))
        self.extract_lbl_img.configure(
            text=os.path.basename(self._extract_image_path) if self._extract_image_path
            else self._t("lbl_no_file"))
        self.extract_lbl_pwd.configure(text=self._t("lbl_password"))
        self.extract_pwd.configure(placeholder_text=self._t("ph_password_extract"))
        self.extract_chk_pwd.configure(text=self._t("chk_show_password"))
        self.extract_btn_run.configure(text=self._t("btn_extract"))
        self.extract_btn_reset.configure(text=self._t("btn_clear"))
        self.extract_lbl_result.configure(text=self._t("lbl_decrypted"))
        self.extract_btn_copy.configure(text=self._t("btn_copy"))
        self.extract_lbl_audit_title.configure(text=self._t("card_audit"))
        self._rerender_audit_labels()

        self.settings_lbl_title.configure(text=self._t("settings_title"))
        self.settings_lbl_hint.configure(text=self._t("settings_hint"))
        self.settings_lbl_appearance.configure(text=self._t("settings_appearance"))
        self.settings_lbl_language.configure(text=self._t("settings_language"))
        self.settings_lbl_about.configure(text=self._t("settings_about"))
        self._theme_values = [self._t("theme_dark"), self._t("theme_light"),
                              self._t("theme_system")]
        self.settings_theme.configure(values=self._theme_values)
        self.settings_theme.set(
            self._theme_values[THEME_MODES.index(self._theme_mode)])
        self.settings_lang.set(self.language)
        self._refresh_audio_language()

        self._refresh_all_context_menus()
        self._render_status()

    def _rename_tabs(self):
        current = self.tabview.get()
        active_key = next(
            (k for k, v in self._tab_names.items() if v == current), "embed")
        new = {k: self._t(f"tab_{k}") for k in ("embed", "extract")}
        for key in ("embed", "extract"):
            old_n, new_n = self._tab_names[key], new[key]
            if old_n != new_n:
                try:
                    self.tabview.rename(old_n, new_n)
                except Exception:
                    new[key] = old_n
        self._tab_names = new
        try:
            self.tabview.set(self._tab_names[active_key])
        except Exception:
            pass
        current = self.main_tabs.get()
        active_key = next((k for k, v in self._main_tab_names.items() if v == current), "image")
        new_main = {k: self._t(f"tab_{k}") for k in ("image", "audio", "settings")}
        for key in new_main:
            if self._main_tab_names[key] != new_main[key]:
                self.main_tabs.rename(self._main_tab_names[key], new_main[key])
        self._main_tab_names = new_main
        self.main_tabs.set(new_main[active_key])

    def _build_header(self):
        hdr = ctk.CTkFrame(self, bg_color=TRANSPARENT, fg_color=TRANSPARENT)
        hdr.pack(fill="x", padx=30, pady=(24, 4))

        brand = ctk.CTkFrame(hdr, bg_color=TRANSPARENT, fg_color=TRANSPARENT)
        brand.pack(anchor="w")

        self.lbl_sigil = ctk.CTkLabel(
            brand, text="◈", font=self.f_sigil,
            text_color=CLR_ACCENT_SOFT,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT)
        self.lbl_sigil.pack(side="left", padx=(0, 14))

        self.lbl_header_title = ctk.CTkLabel(
            brand, text=self._t("header_title"),
            font=self.f_brand, text_color=CLR_TEXT,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
        self.lbl_header_title.pack(side="left")

        self.lbl_header_sub = ctk.CTkLabel(
            hdr, text=self._t("header_subtitle"),
            font=self.f_brand_sub, text_color=CLR_MUTED,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
        self.lbl_header_sub.pack(anchor="w", pady=(6, 0))

        rule = ctk.CTkFrame(hdr, height=1, corner_radius=0,
                            bg_color=TRANSPARENT, fg_color=CLR_BORDER)
        rule.pack(fill="x", pady=(16, 0))

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, bg_color=TRANSPARENT, fg_color=CLR_CARD,
                           corner_radius=CORNER_CTRL,
                           border_width=1, border_color=CLR_BORDER, height=34)
        bar.pack(fill="x", padx=24, pady=(0, 18))
        bar.pack_propagate(False)

        self.status_dot = ctk.CTkLabel(
            bar, text="●", font=self.f_mono_sm, text_color=CLR_GREEN,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, width=16)
        self.status_dot.pack(side="left", padx=(16, 6))

        self.operation_progress = ctk.CTkProgressBar(
            bar, width=180, height=7, fg_color=CLR_CARD_ALT, progress_color=CLR_ACCENT)
        self.operation_progress.pack(side="right", padx=(8, 16))
        self.operation_progress.set(0)

        self.lbl_status = ctk.CTkLabel(
            bar, text=self._t("status_ready"), anchor="w",
            font=self.f_status, text_color=CLR_TEXT_DIM,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT)
        self.lbl_status.pack(side="left", fill="x", expand=True, padx=(0, 16))

    def _toast(self, message):
        previous = getattr(self, "_toast_widget", None)
        if previous is not None:
            previous.destroy()
        toast = ctk.CTkLabel(self, text=message, fg_color=CLR_CARD,
                             text_color=CLR_TEXT, corner_radius=12, padx=18, pady=10)
        self._toast_widget = toast

        def slide(step=0):
            if not toast.winfo_exists():
                return
            toast.place(relx=0.5, rely=1, y=-30 - min(step * 5, 30), anchor="s")
            if step < 6:
                self.after(15, lambda: slide(step + 1))
        slide()
        self.after(2800, lambda: toast.destroy() if toast.winfo_exists() else None)

    def _buttons_busy(self, busy):
        self._busy = busy
        for name in ("embed_btn_run", "extract_btn_run", "embed_btn_reset", "extract_btn_reset",
                     "audio_btn_hide", "audio_btn_extract"):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state="disabled" if busy else "normal")

    def _start_job(self, job, success, error=None):
        if self._busy:
            self._toast(self._t("busy"))
            return
        self._buttons_busy(True)
        self.operation_progress.stop()
        self.operation_progress.set(0)

        def progress(stage, done, total):
            self._events.put(("progress", stage, done, total))

        def worker():
            try:
                self._events.put(("result", success, job(progress)))
            except Exception as exc:
                self._events.put(("error", error or self._operation_error, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                event = self._events.get_nowait()
                if event[0] == "progress":
                    _, stage, done, total = event
                    self._set_status("phase_" + stage)
                    if total is None:
                        if self.operation_progress.cget("mode") != "indeterminate":
                            self.operation_progress.configure(mode="indeterminate")
                            self.operation_progress.start()
                    else:
                        self.operation_progress.stop()
                        self.operation_progress.configure(mode="determinate")
                        self.operation_progress.set(done / total if total else 0)
                elif event[0] in ("result", "error"):
                    self.operation_progress.stop()
                    self.operation_progress.configure(mode="determinate")
                    if event[0] == "error":
                        self.operation_progress.set(0)
                    self._buttons_busy(False)
                    try:
                        event[1](event[2])
                    except Exception as exc:
                        self._operation_error(exc)
                elif event[0] == "preview":
                    _, path, image, target = event
                    current = self._embed_image_path if target == "embed" else self._extract_image_path
                    if image is not None and path == current:
                        thumbnail = ctk.CTkImage(light_image=image, dark_image=image,
                                                 size=(image.width, image.height))
                        setattr(self, "_" + target + "_preview_img", thumbnail)
                        getattr(self, target + "_lbl_preview").configure(image=thumbnail)
        except queue.Empty:
            pass
        finally:
            self.after(50, self._poll_queue)

    def _operation_error(self, exc):
        self._set_status("status_error")
        if isinstance(exc, core.AuthenticationError):
            text = self._t("error_auth")
        elif isinstance(exc, core.CapacityError):
            text = self._t("error_capacity")
        elif isinstance(exc, core.CarrierError):
            text = self._t("error_wrong_carrier")
        else:
            text = self._t("error_generic").format(detail=str(exc))
        messagebox.showerror(self._t("status_error"), text, parent=self)

    def _set_status(self, key: str, **kw):
        self._status_key = key
        self._status_args = kw
        self._render_status()

    def _render_status(self):
        t = self._t(self._status_key)
        try:
            t = t.format(**self._status_args)
        except Exception:
            pass
        try:
            self.lbl_status.configure(text=t)
            if self._status_key == "status_error":
                self.status_dot.configure(text_color=CLR_RED)
            elif self._status_key in ("status_embedding", "status_extracting"):
                self.status_dot.configure(text_color=CLR_YELLOW)
            else:
                self.status_dot.configure(text_color=CLR_GREEN)
        except Exception:
            pass

    def _make_scroll_container(self, tab):
        sf = ctk.CTkScrollableFrame(
            tab,
            bg_color=TRANSPARENT,
            fg_color=CLR_LAYER,
            corner_radius=0,
            scrollbar_button_color=CLR_SCROLL,
            scrollbar_button_hover_color=CLR_SCROLL_HOVER,
        )
        sf.pack(fill="both", expand=True, padx=2, pady=2)
        sf.grid_columnconfigure(0, weight=1)

        try:
            canvas = getattr(sf, "_parent_canvas", None)
            if canvas is not None:
                canvas.configure(bg=resolve(CLR_LAYER),
                                 highlightthickness=0, bd=0)
        except Exception:
            pass

        return sf

    def _make_card(self, parent, row: int, title_key=None, pady=(0, GAP)):
        card = ctk.CTkFrame(parent,
                            bg_color=TRANSPARENT,
                            fg_color=CLR_CARD,
                            corner_radius=CORNER_CARD,
                            border_width=1, border_color=CLR_BORDER)
        card.grid(row=row, column=0, sticky="ew", padx=PAD_OUT, pady=pady)
        card.grid_columnconfigure(0, weight=1)

        title_lbl = None
        body_row = 0
        if title_key is not None:
            title_lbl = ctk.CTkLabel(
                card, text=self._t(title_key),
                font=self.f_card_title, text_color=CLR_ACCENT_SOFT,
                bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
            title_lbl.grid(row=0, column=0, sticky="ew",
                           padx=PAD_CARD, pady=(PAD_CARD - 4, 4))
            body_row = 1

        body = ctk.CTkFrame(card, bg_color=TRANSPARENT, fg_color=TRANSPARENT)
        body.grid(row=body_row, column=0, sticky="ew",
                  padx=PAD_CARD, pady=(4, PAD_CARD))
        body.grid_columnconfigure(0, weight=1)

        return card, body, title_lbl

    def _build_embed_tab(self, tab):
        self.embed_scroll_frame = self._make_scroll_container(tab)
        c = self.embed_scroll_frame
        row = 0

        card_p, body_p, self.embed_card_payload_title = self._make_card(
            c, row, "card_payload")
        row += 1
        br = 0

        mode_f = ctk.CTkFrame(body_p, bg_color=TRANSPARENT, fg_color=CLR_FIELD,
                              corner_radius=CORNER_CTRL,
                              border_width=1, border_color=CLR_BORDER)
        mode_f.grid(row=br, column=0, sticky="w", pady=(0, 18))
        self._embed_mode_var = ctk.StringVar(value=MODE_TEXT)
        self.embed_radio_text = ctk.CTkRadioButton(
            mode_f, text=self._t("radio_text"),
            variable=self._embed_mode_var, value=MODE_TEXT,
            command=self._embed_mode_changed,
            font=self.f_body, text_color=CLR_TEXT, bg_color=TRANSPARENT,
            fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HOVER,
            border_color=CLR_BORDER)
        self.embed_radio_text.pack(side="left", padx=(16, 24), pady=13)
        self.embed_radio_file = ctk.CTkRadioButton(
            mode_f, text=self._t("radio_file"),
            variable=self._embed_mode_var, value=MODE_FILE,
            command=self._embed_mode_changed,
            font=self.f_body, text_color=CLR_TEXT, bg_color=TRANSPARENT,
            fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HOVER,
            border_color=CLR_BORDER)
        self.embed_radio_file.pack(side="left", padx=(0, 18), pady=13)
        br += 1

        self.embed_lbl_section = ctk.CTkLabel(
            body_p, text=self._t("lbl_secret_message"),
            font=self.f_label, text_color=CLR_TEXT,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
        self.embed_lbl_section.grid(row=br, column=0, sticky="ew", pady=(0, 4))
        br += 1

        self.embed_lbl_hint = ctk.CTkLabel(
            body_p, text=self._t("lbl_secret_hint"),
            font=self.f_hint, text_color=CLR_MUTED,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w",
            justify="left", wraplength=WIN_W - 200)
        self.embed_lbl_hint.grid(row=br, column=0, sticky="ew", pady=(0, 12))
        br += 1

        self._embed_txt_wrap = ctk.CTkFrame(
            body_p, height=TEXTBOX_HEIGHT,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT)
        self._embed_txt_wrap.grid(row=br, column=0, sticky="ew", pady=(0, 8))
        self._embed_txt_wrap.pack_propagate(False)
        self._embed_txt_wrap.grid_propagate(False)

        self.embed_txt = ctk.CTkTextbox(
            self._embed_txt_wrap,
            wrap="word",
            corner_radius=CORNER_CTRL,
            font=self.f_mono,
            bg_color=TRANSPARENT,
            fg_color=CLR_FIELD,
            border_width=1,
            border_color=CLR_BORDER,
            text_color=CLR_TEXT,
            scrollbar_button_color=CLR_SCROLL,
            scrollbar_button_hover_color=CLR_SCROLL_HOVER)
        self.embed_txt.pack(fill="both", expand=True)
        self._bind_shortcuts(self.embed_txt)
        self._register_textbox(self.embed_txt)
        self.embed_txt.bind("<KeyRelease>",
                            lambda e: self._on_embed_text_changed(), add="+")
        self._embed_txt_row = br
        br += 1

        self.embed_lbl_chars = ctk.CTkLabel(
            body_p, text=self._t("lbl_char_count").format(chars=0, bytes=0),
            font=self.f_mono_xs, text_color=CLR_MUTED,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="e")
        self.embed_lbl_chars.grid(row=br, column=0, sticky="ew")
        self._embed_chars_row = br
        br += 1

        self.embed_file_panel = ctk.CTkFrame(
            body_p, corner_radius=CORNER_CTRL,
            bg_color=TRANSPARENT, fg_color=CLR_FIELD,
            border_width=1, border_color=CLR_BORDER)
        self._embed_file_row = br
        self.embed_file_panel.grid_columnconfigure(0, weight=1)

        self.embed_btn_secret_file = ctk.CTkButton(
            self.embed_file_panel, text=self._t("btn_choose_file"),
            height=42, corner_radius=CORNER_CTRL, font=self.f_btn_sm,
            bg_color=TRANSPARENT,
            fg_color=CLR_CARD_ALT, hover_color=CLR_ACCENT_DIM,
            text_color=CLR_TEXT, border_width=1, border_color=CLR_BORDER,
            command=self._embed_choose_secret_file)
        self.embed_btn_secret_file.grid(row=0, column=0, sticky="ew",
                                        padx=18, pady=(18, 12))

        self.embed_lbl_secret_file = ctk.CTkLabel(
            self.embed_file_panel, text=self._t("lbl_no_secret_file"),
            text_color=CLR_TEXT_DIM, bg_color=TRANSPARENT, fg_color=TRANSPARENT,
            anchor="w", font=self.f_mono_sm)
        self.embed_lbl_secret_file.grid(row=1, column=0, sticky="ew",
                                        padx=18, pady=(0, 4))

        self.embed_lbl_file_size = ctk.CTkLabel(
            self.embed_file_panel, text="", text_color=CLR_MUTED,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT,
            anchor="w", font=self.f_mono_xs)
        self.embed_lbl_file_size.grid(row=2, column=0, sticky="ew",
                                      padx=18, pady=(0, 12))

        self._embed_wipe_var = ctk.IntVar(value=0)
        self.embed_chk_wipe = ctk.CTkCheckBox(
            self.embed_file_panel, text=self._t("chk_secure_delete"),
            variable=self._embed_wipe_var,
            text_color=CLR_RED, font=self.f_hint, bg_color=TRANSPARENT,
            fg_color=CLR_RED, hover_color=CLR_RED,
            border_color=CLR_BORDER)
        self.embed_chk_wipe.grid(row=3, column=0, sticky="w",
                                 padx=18, pady=(0, 18))
        self.embed_file_panel.grid_remove()
        br += 1

        card_k, body_k, self.embed_card_pwd_title = self._make_card(
            c, row, "card_password")
        row += 1
        kr = 0

        self.embed_lbl_pwd = ctk.CTkLabel(
            body_k, text=self._t("lbl_password"),
            font=self.f_label, text_color=CLR_TEXT,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
        self.embed_lbl_pwd.grid(row=kr, column=0, sticky="ew", pady=(0, 10))
        kr += 1

        pwd_row = ctk.CTkFrame(body_k, bg_color=TRANSPARENT, fg_color=TRANSPARENT)
        pwd_row.grid(row=kr, column=0, sticky="ew", pady=(0, 10))
        pwd_row.grid_columnconfigure(0, weight=1)
        pwd_row.grid_columnconfigure(1, weight=0)

        self.embed_pwd = ctk.CTkEntry(
            pwd_row, show="*",
            placeholder_text=self._t("ph_password_embed"),
            height=46, corner_radius=CORNER_CTRL,
            font=self.f_mono, bg_color=TRANSPARENT,
            fg_color=CLR_FIELD, border_color=CLR_BORDER, border_width=1,
            text_color=CLR_TEXT, placeholder_text_color=CLR_MUTED)
        self.embed_pwd.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        self._bind_shortcuts(self.embed_pwd)

        self.embed_btn_gen_pwd = ctk.CTkButton(
            pwd_row, text=self._t("btn_generate_pwd"),
            width=178, height=46, corner_radius=CORNER_CTRL,
            font=self.f_btn_sm, bg_color=TRANSPARENT,
            fg_color=CLR_ACCENT_DIM, hover_color=CLR_ACCENT,
            text_color=CLR_ACCENT_SOFT, border_width=1,
            border_color=CLR_ACCENT,
            command=self._embed_generate_pwd)
        self.embed_btn_gen_pwd.grid(row=0, column=1, sticky="e")
        kr += 1

        self.embed_lbl_pwd_hint = ctk.CTkLabel(
            body_k,
            text=self._t("lbl_pwd_hint").format(min_len=MIN_PASSWORD_LENGTH),
            font=self.f_mono_xs, text_color=CLR_MUTED,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w",
            justify="left", wraplength=WIN_W - 200)
        self.embed_lbl_pwd_hint.grid(row=kr, column=0, sticky="ew", pady=(0, 12))
        kr += 1

        self.embed_chk_pwd = ctk.CTkCheckBox(
            body_k, text=self._t("chk_show_password"),
            text_color=CLR_TEXT_DIM, font=self.f_hint, bg_color=TRANSPARENT,
            fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HOVER,
            border_color=CLR_BORDER,
            command=self._embed_toggle_pwd)
        self.embed_chk_pwd.grid(row=kr, column=0, sticky="w")
        kr += 1

        card_c, body_c, self.embed_card_carrier_title = self._make_card(
            c, row, "card_carrier")
        row += 1
        body_c.grid_columnconfigure(0, weight=0)
        body_c.grid_columnconfigure(1, weight=1)

        self._embed_preview_img = _placeholder_preview()
        self.embed_lbl_preview = ctk.CTkLabel(
            body_c, text="", image=self._embed_preview_img,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT,
            corner_radius=CORNER_CTRL)
        self.embed_lbl_preview.grid(row=0, column=0, rowspan=3,
                                    padx=(0, 20), pady=(2, 2), sticky="n")

        self.embed_btn_img = ctk.CTkButton(
            body_c, text=self._t("btn_choose_carrier"),
            height=44, corner_radius=CORNER_CTRL, font=self.f_btn_sm,
            bg_color=TRANSPARENT,
            fg_color=CLR_CARD_ALT, hover_color=CLR_ACCENT_DIM,
            text_color=CLR_TEXT, border_width=1, border_color=CLR_BORDER,
            command=self._embed_choose_image)
        self.embed_btn_img.grid(row=0, column=1, sticky="ew", pady=(2, 10))

        self.embed_lbl_img = ctk.CTkLabel(
            body_c, text=self._t("lbl_no_file"),
            text_color=CLR_TEXT_DIM, bg_color=TRANSPARENT, fg_color=TRANSPARENT,
            anchor="w", font=self.f_mono_sm)
        self.embed_lbl_img.grid(row=1, column=1, sticky="ew", pady=(0, 2))

        self.embed_lbl_cap = ctk.CTkLabel(
            body_c, text="", text_color=CLR_MUTED,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT,
            anchor="w", font=self.f_mono_xs)
        self.embed_lbl_cap.grid(row=2, column=1, sticky="ew", pady=(0, 14))

        bar_f = ctk.CTkFrame(body_c, bg_color=TRANSPARENT, fg_color=TRANSPARENT)
        bar_f.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        bar_f.grid_columnconfigure(0, weight=1)

        self.embed_bar = ctk.CTkProgressBar(
            bar_f, height=6, corner_radius=3, bg_color=TRANSPARENT,
            progress_color=CLR_ACCENT, fg_color=CLR_CARD_ALT)
        self.embed_bar.grid(row=0, column=0, sticky="ew")
        self.embed_bar.set(0)

        self.embed_lbl_usage = ctk.CTkLabel(
            bar_f, text=self._t("lbl_usage_empty"),
            text_color=CLR_MUTED, bg_color=TRANSPARENT, fg_color=TRANSPARENT,
            anchor="w", font=self.f_mono_xs)
        self.embed_lbl_usage.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        af = ctk.CTkFrame(c, bg_color=TRANSPARENT, fg_color=TRANSPARENT)
        af.grid(row=row, column=0, sticky="ew",
                padx=PAD_OUT, pady=(6, PAD_OUT))
        af.grid_columnconfigure(0, weight=1)
        row += 1

        self.embed_btn_run = ctk.CTkButton(
            af, text=self._t("btn_encrypt"),
            height=54, corner_radius=CORNER_CTRL, font=self.f_btn_lg,
            bg_color=TRANSPARENT,
            fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HOVER,
            text_color=CLR_ON_ACCENT,
            command=self._embed_run)
        self.embed_btn_run.grid(row=0, column=0, sticky="ew", padx=(0, 14))

        self.embed_btn_reset = ctk.CTkButton(
            af, text=self._t("btn_clear"),
            width=138, height=54, corner_radius=CORNER_CTRL, font=self.f_btn_sm,
            bg_color=TRANSPARENT,
            fg_color=CLR_CARD, border_width=1, border_color=CLR_BORDER,
            hover_color=CLR_CARD_ALT, text_color=CLR_TEXT_DIM,
            command=self._embed_reset)
        self.embed_btn_reset.grid(row=0, column=1, sticky="e")

        self._embed_mode_changed()

    def _embed_generate_pwd(self):
        pwd = generate_secure_password(GENERATED_PASSWORD_LENGTH)
        self.embed_pwd.delete(0, "end")
        self.embed_pwd.insert(0, pwd)
        self.embed_chk_pwd.select()
        self.embed_pwd.configure(show="")
        try:
            self.embed_pwd.configure(border_color=CLR_GREEN)
            self.after(1100,
                       lambda: self.embed_pwd.configure(border_color=CLR_BORDER))
        except Exception:
            pass
        self._set_status("status_pwd_generated")

    def _on_embed_text_changed(self):
        self._refresh_char_count()
        self._refresh_capacity_bar()

    def _get_embed_text(self) -> str:
        return self.embed_txt.get("1.0", "end-1c")

    def _refresh_char_count(self):
        if not hasattr(self, "embed_lbl_chars"):
            return
        text = self._get_embed_text()
        self.embed_lbl_chars.configure(
            text=self._t("lbl_char_count").format(
                chars=f"{len(text):,}",
                bytes=f"{len(text.encode('utf-8')):,}"))

    def _embed_mode_changed(self):
        self._embed_mode = self._embed_mode_var.get()
        if self._embed_mode == MODE_TEXT:
            self.embed_lbl_section.grid()
            self.embed_lbl_hint.grid()
            self._embed_txt_wrap.grid()
            self.embed_lbl_chars.grid()
            self.embed_file_panel.grid_remove()
        else:
            self.embed_lbl_section.grid_remove()
            self.embed_lbl_hint.grid_remove()
            self._embed_txt_wrap.grid_remove()
            self.embed_lbl_chars.grid_remove()
            self.embed_file_panel.grid(
                row=self._embed_file_row, column=0, sticky="ew", pady=(0, 2))
        self._refresh_capacity_bar()

    def _embed_toggle_pwd(self):
        self.embed_pwd.configure(show="" if self.embed_chk_pwd.get() else "*")

    def _refresh_capacity_label(self):
        if self._embed_capacity_error:
            self.embed_lbl_cap.configure(
                text=self._t("lbl_capacity_error").format(
                    error=self._embed_capacity_error))
        elif self._embed_capacity is not None:
            self.embed_lbl_cap.configure(
                text=self._t("lbl_capacity").format(
                    size=f"{self._embed_capacity:,}",
                    kb=self._embed_capacity // 1024))
        else:
            self.embed_lbl_cap.configure(text="")

    def _refresh_file_size_label(self):
        if self._embed_secret_file and os.path.isfile(self._embed_secret_file):
            sz = os.path.getsize(self._embed_secret_file)
            self.embed_lbl_file_size.configure(
                text=self._t("lbl_file_size").format(
                    size=f"{sz:,}", kb=sz // 1024))
        else:
            self.embed_lbl_file_size.configure(text="")

    def _refresh_capacity_bar(self):
        if not hasattr(self, "embed_bar"):
            return
        if self._embed_capacity is None or self._embed_capacity <= 0:
            self.embed_bar.set(0)
            self.embed_bar.configure(progress_color=CLR_ACCENT)
            self.embed_lbl_usage.configure(
                text=self._t("lbl_usage_empty"), text_color=CLR_MUTED)
            return

        est = 0
        try:
            if self._embed_mode == MODE_TEXT:
                msg = self._get_embed_text()
                if msg.strip():
                    est = core.estimate_encrypted_size_text(msg)
            else:
                if self._embed_secret_file and os.path.isfile(self._embed_secret_file):
                    est = core.estimate_encrypted_size_file(self._embed_secret_file)
        except Exception:
            est = 0

        self._embed_estimated_size = est

        if self._embed_image_path:
            try:
                raw_cap = (core.image_capacity_bits(self._embed_image_path) -
                           core.LENGTH_HEADER_BITS) // 8
            except Exception:
                raw_cap = self._embed_capacity + core.SALT_SIZE + core.NONCE_SIZE + 16
        else:
            raw_cap = self._embed_capacity + core.SALT_SIZE + core.NONCE_SIZE + 16
        raw_cap = max(raw_cap, 1)

        ratio = est / raw_cap if est > 0 else 0.0
        pct = min(int(ratio * 100), 9999)
        self.embed_bar.set(min(ratio, 1.0))

        if ratio > 1.0:
            self.embed_bar.configure(progress_color=CLR_RED)
            self.embed_lbl_usage.configure(
                text=self._t("lbl_usage_exceeded").format(pct=pct),
                text_color=CLR_RED)
        elif ratio > 0.85:
            self.embed_bar.configure(progress_color=CLR_YELLOW)
            self.embed_lbl_usage.configure(
                text=self._t("lbl_usage").format(pct=pct),
                text_color=CLR_YELLOW)
        else:
            self.embed_bar.configure(progress_color=CLR_ACCENT)
            self.embed_lbl_usage.configure(
                text=self._t("lbl_usage").format(pct=pct),
                text_color=CLR_TEXT_DIM)

    def _embed_choose_image(self):
        path = filedialog.askopenfilename(
            title=self._t("dlg_open_carrier"), filetypes=self._filetypes_png())
        if not path:
            return
        self._set_embed_image(path)

    def _load_preview_async(self, path, target):
        def worker():
            self._events.put(("preview", path, _make_thumbnail(path), target))
        threading.Thread(target=worker, daemon=True).start()

    def _set_embed_image(self, path):
        try:
            capacity = core.image_capacity_bytes(path)
        except Exception as exc:
            self._operation_error(exc)
            return
        self._embed_image_path = path
        self.embed_lbl_img.configure(text=os.path.basename(path))
        self._embed_capacity = capacity
        self._embed_capacity_error = None
        self._load_preview_async(path, "embed")
        self._refresh_capacity_label()
        self._refresh_capacity_bar()

    def _embed_choose_secret_file(self):
        path = filedialog.askopenfilename(
            title=self._t("dlg_open_secret"), filetypes=self._filetypes_all())
        if not path:
            return
        self._set_embed_secret(path)

    def _set_embed_secret(self, path):
        self._embed_secret_file = path
        self.embed_lbl_secret_file.configure(text=os.path.basename(path))
        self._refresh_file_size_label()
        self._refresh_capacity_bar()

    def _embed_run(self):
        pwd = self.embed_pwd.get()

        if not pwd:
            messagebox.showwarning(self._t("warn_empty_pwd_t"),
                                   self._t("warn_empty_pwd_enc"))
            self.embed_pwd.focus_set()
            return

        if len(pwd) < MIN_PASSWORD_LENGTH:
            try:
                self.embed_pwd.configure(border_color=CLR_RED)
                self.after(1600,
                           lambda: self.embed_pwd.configure(border_color=CLR_BORDER))
            except Exception:
                pass
            messagebox.showwarning(
                self._t("warn_pwd_weak_t"),
                self._t("warn_pwd_weak_m").format(
                    min_len=MIN_PASSWORD_LENGTH, current=len(pwd)))
            self.embed_pwd.focus_set()
            return

        if not self._embed_image_path:
            messagebox.showwarning(self._t("warn_no_img_t"),
                                   self._t("warn_no_img_enc"))
            return

        mode = self._embed_mode
        msg_text = ""
        if mode == MODE_TEXT:
            msg_text = self._get_embed_text()
            if not msg_text.strip():
                messagebox.showwarning(self._t("warn_empty_msg_t"),
                                       self._t("warn_empty_msg_m"))
                return
        else:
            if not self._embed_secret_file:
                messagebox.showwarning(self._t("warn_no_secret_t"),
                                       self._t("warn_no_secret_m"))
                return

        base = os.path.splitext(os.path.basename(self._embed_image_path))[0]
        out = filedialog.asksaveasfilename(
            title=self._t("dlg_save"), defaultextension=".png",
            initialfile=f"{base}_stego.png",
            filetypes=[(self._t("ft_png"), "*.png")])
        if not out:
            return

        if core._same_path(self._embed_image_path, out) or (
                mode == MODE_FILE and (core._same_path(self._embed_secret_file, out) or
                                       core._same_path(self._embed_secret_file, self._embed_image_path))):
            messagebox.showerror(self._t("status_error"), self._t("source_conflict"))
            return
        wipe = (self._embed_wipe_var.get() == 1 and mode == MODE_FILE)
        wipe_path = self._embed_secret_file if wipe else None
        if wipe and wipe_path:
            if not messagebox.askyesno(self._t("confirm_wipe_t"),
                                       self._t("warning_wipe") + "\n\n" + wipe_path):
                return

        img_path = self._embed_image_path
        secret_file = self._embed_secret_file

        def work(progress):
            if mode == MODE_TEXT:
                core.embed_data_in_image(img_path, out, msg_text, pwd, progress)
            else:
                core.embed_file_in_image(img_path, out, secret_file, pwd, progress)
            if wipe_path:
                try:
                    # Verify the saved output and current source before any deletion.
                    restored = core.extract_raw_from_image(out, pwd, progress)
                    original = core._read_file(wipe_path)
                    if not secrets.compare_digest(core.hashlib.sha256(restored.data).digest(),
                                                  core.hashlib.sha256(original).digest()):
                        raise core.PhantomError("Source changed after embedding")
                except Exception as exc:
                    return out, ("wipe_failed", str(exc))
                try:
                    core.secure_delete(wipe_path)
                except Exception as exc:
                    return out, ("wipe_partial", str(exc))
            return out, None

        self._start_job(work, self._embed_ok)

    def _embed_ok(self, outcome):
        path, warning = outcome
        self._set_status("status_done", path=path)
        if warning:
            key, detail = warning
            messagebox.showwarning(self._t("status_error"), self._t(key).format(detail=detail))
        else:
            self._toast(self._t("file_saved"))

    def _embed_err(self, exc):
        self.embed_btn_run.configure(state="normal")
        self._set_status("status_error")
        messagebox.showerror(self._t("err_encrypt_t"), str(exc))

    def _embed_reset(self):
        self.embed_txt.delete("1.0", "end")
        self.embed_pwd.delete(0, "end")
        self.embed_pwd.configure(border_color=CLR_BORDER)
        self.embed_chk_pwd.deselect()
        self.embed_pwd.configure(show="*")
        self._embed_image_path = None
        self._embed_secret_file = None
        self._embed_capacity = None
        self._embed_capacity_error = None
        self._embed_estimated_size = None
        self._embed_wipe_var.set(0)
        self.embed_lbl_img.configure(text=self._t("lbl_no_file"))
        self.embed_lbl_secret_file.configure(text=self._t("lbl_no_secret_file"))
        self.embed_lbl_file_size.configure(text="")
        self._refresh_capacity_label()
        self._refresh_char_count()
        self._embed_preview_img = _placeholder_preview()
        self.embed_lbl_preview.configure(image=self._embed_preview_img)
        self._embed_mode_var.set(MODE_TEXT)
        self._embed_mode_changed()
        self.embed_bar.set(0)
        self.embed_bar.configure(progress_color=CLR_ACCENT)
        self.embed_lbl_usage.configure(
            text=self._t("lbl_usage_empty"), text_color=CLR_MUTED)
        self._set_status("status_ready")

    def _build_extract_tab(self, tab):
        self.extract_scroll_frame = self._make_scroll_container(tab)
        c = self.extract_scroll_frame
        row = 0

        card_s, body_s, self.extract_card_stego_title = self._make_card(
            c, row, "card_stego")
        row += 1
        body_s.grid_columnconfigure(0, weight=0)
        body_s.grid_columnconfigure(1, weight=1)

        self._extract_preview_img = _placeholder_preview()
        self.extract_lbl_preview = ctk.CTkLabel(
            body_s, text="", image=self._extract_preview_img,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT,
            corner_radius=CORNER_CTRL)
        self.extract_lbl_preview.grid(row=0, column=0, rowspan=2,
                                      padx=(0, 20), pady=2, sticky="n")

        self.extract_btn_img = ctk.CTkButton(
            body_s, text=self._t("btn_choose_stego"),
            height=44, corner_radius=CORNER_CTRL, font=self.f_btn_sm,
            bg_color=TRANSPARENT,
            fg_color=CLR_CARD_ALT, hover_color=CLR_ACCENT_DIM,
            text_color=CLR_TEXT, border_width=1, border_color=CLR_BORDER,
            command=self._extract_choose_image)
        self.extract_btn_img.grid(row=0, column=1, sticky="ew", pady=(2, 10))

        self.extract_lbl_img = ctk.CTkLabel(
            body_s, text=self._t("lbl_no_file"),
            text_color=CLR_TEXT_DIM, bg_color=TRANSPARENT, fg_color=TRANSPARENT,
            anchor="w", font=self.f_mono_sm)
        self.extract_lbl_img.grid(row=1, column=1, sticky="ew", pady=(0, 2))

        card_k, body_k, self.extract_card_pwd_title = self._make_card(
            c, row, "card_password")
        row += 1
        kr = 0

        self.extract_lbl_pwd = ctk.CTkLabel(
            body_k, text=self._t("lbl_password"),
            font=self.f_label, text_color=CLR_TEXT,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
        self.extract_lbl_pwd.grid(row=kr, column=0, sticky="ew", pady=(0, 10))
        kr += 1

        self.extract_pwd = ctk.CTkEntry(
            body_k, show="*", placeholder_text=self._t("ph_password_extract"),
            height=46, corner_radius=CORNER_CTRL, font=self.f_mono,
            bg_color=TRANSPARENT,
            fg_color=CLR_FIELD, border_color=CLR_BORDER, border_width=1,
            text_color=CLR_TEXT, placeholder_text_color=CLR_MUTED)
        self.extract_pwd.grid(row=kr, column=0, sticky="ew", pady=(0, 12))
        self._bind_shortcuts(self.extract_pwd)
        kr += 1

        self.extract_chk_pwd = ctk.CTkCheckBox(
            body_k, text=self._t("chk_show_password"),
            text_color=CLR_TEXT_DIM, font=self.f_hint, bg_color=TRANSPARENT,
            fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HOVER,
            border_color=CLR_BORDER,
            command=self._extract_toggle_pwd)
        self.extract_chk_pwd.grid(row=kr, column=0, sticky="w")
        kr += 1

        af = ctk.CTkFrame(c, bg_color=TRANSPARENT, fg_color=TRANSPARENT)
        af.grid(row=row, column=0, sticky="ew", padx=PAD_OUT, pady=(2, GAP))
        af.grid_columnconfigure(0, weight=1)
        row += 1

        self.extract_btn_run = ctk.CTkButton(
            af, text=self._t("btn_extract"),
            height=54, corner_radius=CORNER_CTRL, font=self.f_btn_lg,
            bg_color=TRANSPARENT,
            fg_color=CLR_ACCENT, hover_color=CLR_ACCENT_HOVER,
            text_color=CLR_ON_ACCENT,
            command=self._extract_run)
        self.extract_btn_run.grid(row=0, column=0, sticky="ew", padx=(0, 14))

        self.extract_btn_reset = ctk.CTkButton(
            af, text=self._t("btn_clear"),
            width=138, height=54, corner_radius=CORNER_CTRL, font=self.f_btn_sm,
            bg_color=TRANSPARENT,
            fg_color=CLR_CARD, border_width=1, border_color=CLR_BORDER,
            hover_color=CLR_CARD_ALT, text_color=CLR_TEXT_DIM,
            command=self._extract_reset)
        self.extract_btn_reset.grid(row=0, column=1, sticky="e")

        self.extract_audit_card = ctk.CTkFrame(
            c, corner_radius=CORNER_CARD,
            bg_color=TRANSPARENT, fg_color=CLR_CARD,
            border_width=1, border_color=CLR_BORDER)
        self.extract_audit_card.grid(row=row, column=0, sticky="ew",
                                     padx=PAD_OUT, pady=(0, GAP))
        self.extract_audit_card.grid_columnconfigure(0, weight=1)
        self._extract_audit_row = row
        row += 1

        self.extract_lbl_audit_title = ctk.CTkLabel(
            self.extract_audit_card, text=self._t("card_audit"),
            font=self.f_card_title, text_color=CLR_ACCENT_SOFT,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
        self.extract_lbl_audit_title.grid(row=0, column=0, sticky="ew",
                                          padx=PAD_CARD,
                                          pady=(PAD_CARD - 4, 8))

        self.extract_lbl_integrity_result = ctk.CTkLabel(
            self.extract_audit_card, text="", font=self.f_btn_sm,
            text_color=CLR_TEXT,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
        self.extract_lbl_integrity_result.grid(row=1, column=0, sticky="ew",
                                               padx=PAD_CARD, pady=(0, 10))

        self.extract_lbl_hash_expected = ctk.CTkLabel(
            self.extract_audit_card, text="",
            font=self.f_mono_xs, text_color=CLR_MUTED,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
        self.extract_lbl_hash_expected.grid(row=2, column=0, sticky="ew",
                                            padx=PAD_CARD)

        self.extract_lbl_hash_actual = ctk.CTkLabel(
            self.extract_audit_card, text="",
            font=self.f_mono_xs, text_color=CLR_MUTED,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
        self.extract_lbl_hash_actual.grid(row=3, column=0, sticky="ew",
                                          padx=PAD_CARD, pady=(2, 10))

        self.extract_lbl_compression = ctk.CTkLabel(
            self.extract_audit_card, text="",
            font=self.f_mono_xs, text_color=CLR_MUTED,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
        self.extract_lbl_compression.grid(row=4, column=0, sticky="ew",
                                          padx=PAD_CARD, pady=(0, PAD_CARD))

        self.extract_audit_card.grid_remove()

        card_r, body_r, self.extract_card_result_title = self._make_card(
            c, row, "card_result", pady=(0, PAD_OUT))
        row += 1
        rr = 0

        self.extract_lbl_result = ctk.CTkLabel(
            body_r, text=self._t("lbl_decrypted"),
            font=self.f_label, text_color=CLR_TEXT,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
        self.extract_lbl_result.grid(row=rr, column=0, sticky="ew", pady=(0, 12))
        rr += 1

        self._extract_txt_wrap = ctk.CTkFrame(
            body_r, height=TEXTBOX_HEIGHT,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT)
        self._extract_txt_wrap.grid(row=rr, column=0, sticky="ew", pady=(0, 14))
        self._extract_txt_wrap.pack_propagate(False)
        self._extract_txt_wrap.grid_propagate(False)

        self.extract_txt = ctk.CTkTextbox(
            self._extract_txt_wrap, wrap="word", corner_radius=CORNER_CTRL,
            font=self.f_mono, bg_color=TRANSPARENT,
            fg_color=CLR_FIELD, border_width=1, border_color=CLR_BORDER,
            text_color=CLR_TEXT,
            scrollbar_button_color=CLR_SCROLL,
            scrollbar_button_hover_color=CLR_SCROLL_HOVER)
        self.extract_txt.pack(fill="both", expand=True)
        self.extract_txt.configure(state="disabled")
        self._bind_shortcuts(self.extract_txt)
        self._register_textbox(self.extract_txt)
        rr += 1

        self.extract_btn_copy = ctk.CTkButton(
            body_r, text=self._t("btn_copy"),
            width=158, height=40, corner_radius=CORNER_CTRL, font=self.f_btn_sm,
            bg_color=TRANSPARENT,
            fg_color=CLR_CARD_ALT, border_width=1, border_color=CLR_BORDER,
            hover_color=CLR_ACCENT_DIM, text_color=CLR_TEXT_DIM,
            command=self._extract_copy)
        self.extract_btn_copy.grid(row=rr, column=0, sticky="e")

    def _extract_toggle_pwd(self):
        self.extract_pwd.configure(show="" if self.extract_chk_pwd.get() else "*")

    def _extract_choose_image(self):
        path = filedialog.askopenfilename(
            title=self._t("dlg_open_stego"), filetypes=self._filetypes_png())
        if not path:
            return
        self._set_extract_image(path)

    def _set_extract_image(self, path):
        try:
            core.image_capacity_bits(path)
        except Exception as exc:
            self._operation_error(exc)
            return
        self._extract_image_path = path
        self.extract_lbl_img.configure(text=os.path.basename(path))
        self._load_preview_async(path, "extract")

    def _extract_run(self):
        pwd = self.extract_pwd.get()
        if not self._extract_image_path:
            messagebox.showwarning(self._t("warn_no_img_t"),
                                   self._t("warn_no_img_dec"))
            return
        if not pwd:
            messagebox.showwarning(self._t("warn_empty_pwd_t"),
                                   self._t("warn_empty_pwd_dec"))
            return

        stego = self._extract_image_path
        self._start_job(lambda progress: core.extract_raw_from_image(stego, pwd, progress),
                        lambda result: self._extract_ok(result, stego))

    def _extract_ok(self, result, source):
        self._last_audit = result
        self._render_audit(result)

        if result.payload_type == core.TYPE_FILE:
            self._save_extracted(result, source, self.extract_txt)
        else:
            try:
                text = result.data.decode("utf-8")
            except UnicodeDecodeError:
                self._operation_error(core.AuthenticationError("Invalid UTF-8"))
                return
            self._set_status("status_decrypted")
            self.extract_txt.configure(state="normal")
            self.extract_txt.delete("1.0", "end")
            self.extract_txt.insert("1.0", text)
            self.extract_txt.configure(state="disabled")

    def _render_audit(self, r):
        self.extract_audit_card.grid(
            row=self._extract_audit_row, column=0, sticky="ew",
            padx=PAD_OUT, pady=(0, GAP))

        if r.integrity_ok:
            self.extract_lbl_integrity_result.configure(
                text=self._t("lbl_integrity_ok"), text_color=CLR_GREEN)
            self.extract_audit_card.configure(border_color=CLR_GREEN)
        else:
            self.extract_lbl_integrity_result.configure(
                text=self._t("lbl_integrity_fail"), text_color=CLR_RED)
            self.extract_audit_card.configure(border_color=CLR_RED)

        self.extract_lbl_hash_expected.configure(
            text=self._t("lbl_hash_expected").format(h=r.hash_expected[:40] + "…"))
        self.extract_lbl_hash_actual.configure(
            text=self._t("lbl_hash_actual").format(h=r.hash_actual[:40] + "…"))

        if r.original_size > 0:
            ratio = int((1 - r.compressed_size / r.original_size) * 100)
            self.extract_lbl_compression.configure(
                text=self._t("lbl_compression").format(
                    orig=f"{r.original_size:,}",
                    comp=f"{r.compressed_size:,}",
                    ratio=ratio))
        else:
            self.extract_lbl_compression.configure(text="")

    def _rerender_audit_labels(self):
        if self._last_audit:
            self._render_audit(self._last_audit)

    def _extract_err(self, exc):
        self.extract_btn_run.configure(state="normal")
        self._set_status("status_error")
        details = str(exc) or exc.__class__.__name__
        messagebox.showerror(self._t("err_decrypt_t"),
                             self._t("err_decrypt_m").format(details=details))

    def _extract_copy(self):
        text = self.extract_txt.get("1.0", "end-1c")
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._set_status("status_copied")

    def _extract_reset(self):
        self._extract_image_path = None
        self._last_audit = None
        self.extract_lbl_img.configure(text=self._t("lbl_no_file"))
        self._extract_preview_img = _placeholder_preview()
        self.extract_lbl_preview.configure(image=self._extract_preview_img)
        self.extract_pwd.delete(0, "end")
        self.extract_chk_pwd.deselect()
        self.extract_pwd.configure(show="*")
        self.extract_txt.configure(state="normal")
        self.extract_txt.delete("1.0", "end")
        self.extract_txt.configure(state="disabled")
        self.extract_audit_card.grid_remove()
        self.extract_audit_card.configure(border_color=CLR_BORDER)
        self.extract_lbl_integrity_result.configure(text="")
        self.extract_lbl_hash_expected.configure(text="")
        self.extract_lbl_hash_actual.configure(text="")
        self.extract_lbl_compression.configure(text="")
        self._set_status("status_ready")

    def _save_extracted(self, result, source, target_widget):
        """Choose a destination on the UI thread; perform disk writes in a worker."""
        name = result.filename or "extracted_file"
        out = filedialog.asksaveasfilename(title=self._t("dlg_save_file"),
                                           initialfile=name,
                                           defaultextension=os.path.splitext(name)[1],
                                           filetypes=self._filetypes_all())
        if not out:
            self._set_status("status_ready")
            return
        if core._same_path(source, out):
            messagebox.showerror(self._t("status_error"), self._t("source_conflict"))
            return

        def save(progress):
            fd, temporary = tempfile.mkstemp(prefix=".phantom-extracted-",
                                              dir=os.path.dirname(os.path.abspath(out)))
            try:
                with os.fdopen(fd, "wb") as handle:
                    view = memoryview(result.data)
                    for offset in range(0, len(view), 1024 * 1024):
                        handle.write(view[offset:offset + 1024 * 1024])
                        progress("save", min(offset + 1024 * 1024, len(view)), len(view))
                os.replace(temporary, out)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return out

        def saved(path):
            self._set_status("status_file_saved", path=path)
            target_widget.configure(state="normal")
            target_widget.delete("1.0", "end")
            target_widget.insert("1.0", f"{result.filename}\n{path}")
            target_widget.configure(state="disabled")
            self._toast(self._t("file_saved"))
        self._start_job(save, saved)

    def _audio_label(self, widget, key, option="text"):
        self._audio_i18n.append((widget, option, key))
        widget.configure(**{option: self._t(key)})
        return widget

    def _build_audio_tab(self, tab):
        self.audio_scroll_frame = self._make_scroll_container(tab)
        page = self.audio_scroll_frame
        row = 0
        title = ctk.CTkLabel(page, font=self.f_section, text_color=CLR_TEXT, anchor="w")
        self._audio_label(title, "tab_audio").grid(row=row, column=0, sticky="ew", padx=PAD_OUT, pady=(PAD_OUT, 2))
        row += 1
        subtitle = ctk.CTkLabel(page, font=self.f_hint, text_color=CLR_MUTED, anchor="w")
        self._audio_label(subtitle, "audio_hint").grid(row=row, column=0, sticky="ew", padx=PAD_OUT, pady=(0, GAP))
        row += 1

        _, body, title = self._make_card(page, row, "card_carrier")
        self._audio_i18n.append((title, "text", "card_carrier"))
        row += 1
        self.audio_btn_carrier = ctk.CTkButton(body, height=44, corner_radius=CORNER_CTRL,
                                               fg_color=CLR_CARD_ALT, hover_color=CLR_ACCENT_DIM,
                                               text_color=CLR_TEXT, command=self._audio_choose)
        self._audio_label(self.audio_btn_carrier, "audio_choose").grid(row=0, column=0, sticky="ew")
        self.audio_carrier_label = ctk.CTkLabel(body, text=self._t("audio_drop"),
                                               text_color=CLR_TEXT_DIM, anchor="w")
        self.audio_carrier_label.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.audio_capacity_label = ctk.CTkLabel(body, text="", text_color=CLR_MUTED, anchor="w")
        self.audio_capacity_label.grid(row=2, column=0, sticky="ew", pady=(5, 0))

        _, body, title = self._make_card(page, row, "card_payload")
        self._audio_i18n.append((title, "text", "card_payload"))
        row += 1
        choices = ctk.CTkFrame(body, fg_color="transparent")
        choices.grid(row=0, column=0, sticky="w", pady=(0, 10))
        self._audio_mode_var = tk.StringVar(value=MODE_TEXT)
        self.audio_radio_text = ctk.CTkRadioButton(choices, variable=self._audio_mode_var,
                                                   value=MODE_TEXT, command=self._audio_mode_changed)
        self._audio_label(self.audio_radio_text, "audio_text").pack(side="left", padx=(0, 25))
        self.audio_radio_file = ctk.CTkRadioButton(choices, variable=self._audio_mode_var,
                                                   value=MODE_FILE, command=self._audio_mode_changed)
        self._audio_label(self.audio_radio_file, "audio_file").pack(side="left")
        self.audio_txt = ctk.CTkTextbox(body, height=135, corner_radius=CORNER_CTRL,
                                         fg_color=CLR_FIELD, text_color=CLR_TEXT,
                                         border_width=1, border_color=CLR_BORDER)
        self.audio_txt.grid(row=1, column=0, sticky="ew")
        self._bind_shortcuts(self.audio_txt)
        self._register_textbox(self.audio_txt)
        self.audio_btn_secret = ctk.CTkButton(body, command=self._audio_choose_secret,
                                               fg_color=CLR_CARD_ALT, hover_color=CLR_ACCENT_DIM,
                                               text_color=CLR_TEXT, height=42)
        self._audio_label(self.audio_btn_secret, "audio_secret").grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.audio_secret_label = ctk.CTkLabel(body, text=self._t("lbl_no_file"),
                                              text_color=CLR_MUTED, anchor="w")
        self.audio_secret_label.grid(row=3, column=0, sticky="ew", pady=(4, 0))

        _, body, title = self._make_card(page, row, "card_password")
        self._audio_i18n.append((title, "text", "card_password"))
        row += 1
        self.audio_pwd = ctk.CTkEntry(body, show="*", height=46, fg_color=CLR_FIELD,
                                       border_color=CLR_BORDER, text_color=CLR_TEXT)
        self._audio_label(self.audio_pwd, "audio_password", "placeholder_text").grid(row=0, column=0, sticky="ew")
        self._bind_shortcuts(self.audio_pwd)
        self.audio_show = tk.BooleanVar(value=False)
        show = ctk.CTkCheckBox(body, variable=self.audio_show,
                               command=lambda: self.audio_pwd.configure(show="" if self.audio_show.get() else "*"))
        self._audio_label(show, "chk_show_password").grid(row=1, column=0, sticky="w", pady=(12, 0))

        buttons = ctk.CTkFrame(page, fg_color="transparent")
        buttons.grid(row=row, column=0, sticky="ew", padx=PAD_OUT, pady=(0, GAP))
        buttons.grid_columnconfigure((0, 1), weight=1)
        row += 1
        self.audio_btn_hide = ctk.CTkButton(buttons, height=48, corner_radius=CORNER_CTRL,
                                            fg_color=CLR_ACCENT, command=self._audio_hide)
        self._audio_label(self.audio_btn_hide, "audio_hide").grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.audio_btn_extract = ctk.CTkButton(buttons, height=48, corner_radius=CORNER_CTRL,
                                               fg_color=CLR_CARD_ALT, text_color=CLR_TEXT,
                                               command=self._audio_extract)
        self._audio_label(self.audio_btn_extract, "audio_extract").grid(row=0, column=1, sticky="ew", padx=(6, 0))

        _, body, title = self._make_card(page, row, "audio_result")
        self._audio_i18n.append((title, "text", "audio_result"))
        self.audio_result = ctk.CTkTextbox(body, height=120, fg_color=CLR_FIELD,
                                           text_color=CLR_TEXT, state="disabled")
        self.audio_result.grid(row=0, column=0, sticky="ew")
        copy = ctk.CTkButton(body, command=self._audio_copy, fg_color=CLR_CARD_ALT,
                             hover_color=CLR_ACCENT_DIM, text_color=CLR_TEXT)
        self._audio_label(copy, "btn_copy").grid(row=1, column=0, sticky="e", pady=(10, 0))
        self._audio_mode_changed()

    def _audio_mode_changed(self):
        if self._audio_mode_var.get() == MODE_TEXT:
            self.audio_txt.grid()
            self.audio_btn_secret.grid_remove()
            self.audio_secret_label.grid_remove()
        else:
            self.audio_txt.grid_remove()
            self.audio_btn_secret.grid()
            self.audio_secret_label.grid()

    def _refresh_audio_language(self):
        for widget, option, key in self._audio_i18n:
            widget.configure(**{option: self._t(key)})
        if self._audio_path:
            self.audio_capacity_label.configure(text=self._t("audio_capacity").format(size=f"{self._audio_capacity:,}"))
        else:
            self.audio_carrier_label.configure(text=self._t("audio_drop"))
        if not self._audio_secret_file:
            self.audio_secret_label.configure(text=self._t("lbl_no_file"))

    def _audio_choose(self):
        path = filedialog.askopenfilename(filetypes=[(self._t("audio_filetypes"), "*.wav *.flac"),
                                                      (self._t("ft_all"), "*.*")])
        if path:
            self._set_audio(path)

    def _set_audio(self, path):
        try:
            engine = AudioSteganographyEngine()
            info = engine._info(path)
            capacity = engine.check_capacity(path)
        except Exception as exc:
            self._operation_error(exc)
            return
        self._audio_path = path
        self._audio_format = info.format
        self._audio_capacity = capacity
        self.audio_carrier_label.configure(text=os.path.basename(path))
        self.audio_capacity_label.configure(text=self._t("audio_capacity").format(size=f"{capacity:,}"))

    def _audio_choose_secret(self):
        path = filedialog.askopenfilename(filetypes=self._filetypes_all())
        if path:
            self._set_audio_secret(path)

    def _set_audio_secret(self, path):
        self._audio_secret_file = path
        self.audio_secret_label.configure(text=os.path.basename(path))
        self._audio_mode_var.set(MODE_FILE)
        self._audio_mode_changed()

    def _audio_hide(self):
        source, password = self._audio_path, self.audio_pwd.get()
        if not source:
            messagebox.showwarning(self._t("warn_no_img_t"), self._t("audio_choose"))
            return
        if len(password) < MIN_PASSWORD_LENGTH:
            messagebox.showwarning(self._t("warn_pwd_weak_t"), self._t("warn_pwd_weak_m").format(
                min_len=MIN_PASSWORD_LENGTH, current=len(password)))
            return
        mode, secret = self._audio_mode_var.get(), self._audio_secret_file
        text = self.audio_txt.get("1.0", "end-1c")
        if mode == MODE_TEXT and not text:
            messagebox.showwarning(self._t("warn_empty_msg_t"), self._t("warn_empty_msg_m"))
            return
        if mode == MODE_FILE and not secret:
            messagebox.showwarning(self._t("warn_no_secret_t"), self._t("warn_no_secret_m"))
            return
        extension = ".wav" if self._audio_format in ("WAV", "WAVEX") else ".flac"
        output = filedialog.asksaveasfilename(title=self._t("audio_save"), defaultextension=extension,
                     initialfile=Path(source).stem + "_stego" + extension,
                     filetypes=[(self._t("audio_filetypes"), "*" + extension)])
        if not output:
            return
        if core._same_path(source, output) or (mode == MODE_FILE and
                (core._same_path(secret, source) or core._same_path(secret, output))):
            messagebox.showerror(self._t("status_error"), self._t("source_conflict"))
            return

        def work(progress):
            data = core._read_file(secret, progress) if mode == MODE_FILE else text
            AudioSteganographyEngine().hide(source, output, data, password,
                      filename=os.path.basename(secret) if mode == MODE_FILE else None, progress=progress)
            return output
        self._start_job(work, self._audio_saved)

    def _audio_saved(self, path):
        self._set_status("status_done", path=path)
        self._toast(self._t("file_saved"))

    def _audio_extract(self):
        source, password = self._audio_path, self.audio_pwd.get()
        if not source:
            messagebox.showwarning(self._t("warn_no_img_t"), self._t("audio_choose"))
            return
        if not password:
            messagebox.showwarning(self._t("warn_empty_pwd_t"), self._t("warn_empty_pwd_dec"))
            return
        self._start_job(lambda progress: AudioSteganographyEngine().extract(source, password, progress=progress),
                        lambda result: self._audio_extracted(result, source))

    def _audio_extracted(self, result, source):
        if result.payload_type == core.TYPE_FILE:
            self._save_extracted(result, source, self.audio_result)
            return
        try:
            value = result.data.decode("utf-8")
        except UnicodeDecodeError:
            self._operation_error(core.AuthenticationError("Invalid UTF-8"))
            return
        self.audio_result.configure(state="normal")
        self.audio_result.delete("1.0", "end")
        self.audio_result.insert("1.0", value)
        self.audio_result.configure(state="disabled")
        self._set_status("status_decrypted")
        self._toast(self._t("phase_done"))

    def _audio_copy(self):
        value = self.audio_result.get("1.0", "end-1c")
        if value:
            self.clipboard_clear()
            self.clipboard_append(value)
            self._toast(self._t("copied"))

    def _register_drop_targets(self):
        if not self._dnd_enabled:
            return
        targets = ((self.embed_btn_img, self._set_embed_image),
                   (self.embed_lbl_preview, self._set_embed_image),
                   (self.extract_btn_img, self._set_extract_image),
                   (self.extract_lbl_preview, self._set_extract_image),
                   (self.embed_btn_secret_file, self._set_embed_secret),
                   (self.audio_btn_carrier, self._set_audio),
                   (self.audio_carrier_label, self._set_audio),
                   (self.audio_btn_secret, self._set_audio_secret))
        for widget, callback in targets:
            def on_drop(event, callback=callback):
                if self._busy:
                    return
                paths = self.tk.splitlist(event.data)
                if paths:
                    callback(paths[0])
            # CustomTkinter often draws a control into child canvases or labels.
            for target in (widget, getattr(widget, "_canvas", None),
                           getattr(widget, "_text_label", None), getattr(widget, "_label", None)):
                if target is not None and hasattr(target, "drop_target_register"):
                    try:
                        target.drop_target_register(DND_FILES)
                        target.dnd_bind("<<Drop>>", on_drop)
                    except tk.TclError:
                        pass

    def _build_settings_tab(self, tab):
        self.settings_scroll_frame = self._make_scroll_container(tab)
        c = self.settings_scroll_frame
        row = 0

        head = ctk.CTkFrame(c, bg_color=TRANSPARENT, fg_color=TRANSPARENT)
        head.grid(row=row, column=0, sticky="ew",
                  padx=PAD_OUT, pady=(PAD_OUT, GAP))
        head.grid_columnconfigure(0, weight=1)
        row += 1

        self.settings_lbl_title = ctk.CTkLabel(
            head, text=self._t("settings_title"),
            font=self.f_section, text_color=CLR_TEXT,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w")
        self.settings_lbl_title.grid(row=0, column=0, sticky="ew")

        self.settings_lbl_hint = ctk.CTkLabel(
            head, text=self._t("settings_hint"),
            font=self.f_hint, text_color=CLR_MUTED,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT, anchor="w",
            justify="left", wraplength=WIN_W - 180)
        self.settings_lbl_hint.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        card_t, body_t, self.settings_lbl_appearance = self._make_card(
            c, row, "settings_appearance")
        row += 1
        body_t.grid_columnconfigure(0, weight=1)

        self._theme_values = [self._t("theme_dark"), self._t("theme_light"),
                              self._t("theme_system")]
        self.settings_theme = ctk.CTkSegmentedButton(
            body_t, values=self._theme_values,
            command=self._on_theme_change,
            corner_radius=CORNER_CTRL, height=44, font=self.f_body,
            bg_color=TRANSPARENT, fg_color=CLR_CARD_ALT,
            selected_color=CLR_SEGMENT_SELECTED, selected_hover_color=CLR_SEGMENT_HOVER,
            unselected_color=CLR_CARD_ALT, unselected_hover_color=CLR_ACCENT_DIM,
            text_color=CLR_TEXT)
        self.settings_theme.set(
            self._theme_values[THEME_MODES.index(self._theme_mode)])
        self.settings_theme.grid(row=0, column=0, sticky="ew")

        card_l, body_l, self.settings_lbl_language = self._make_card(
            c, row, "settings_language")
        row += 1
        body_l.grid_columnconfigure(0, weight=1)

        self.settings_lang = ctk.CTkSegmentedButton(
            body_l, values=LANGUAGES, command=self.change_language,
            corner_radius=CORNER_CTRL, height=44, font=self.f_body,
            bg_color=TRANSPARENT, fg_color=CLR_CARD_ALT,
            selected_color=CLR_SEGMENT_SELECTED, selected_hover_color=CLR_SEGMENT_HOVER,
            unselected_color=CLR_CARD_ALT, unselected_hover_color=CLR_ACCENT_DIM,
            text_color=CLR_TEXT)
        self.settings_lang.set(self.language)
        self.settings_lang.grid(row=0, column=0, sticky="ew")

        about_card, about_body, _ = self._make_card(
            c, row, None, pady=(0, PAD_OUT))
        row += 1
        self.settings_lbl_about = ctk.CTkLabel(
            about_body, text=self._t("settings_about"),
            font=self.f_mono_xs, text_color=CLR_MUTED,
            bg_color=TRANSPARENT, fg_color=TRANSPARENT,
            anchor="w", justify="left")
        self.settings_lbl_about.grid(row=0, column=0, sticky="ew")

    def _on_theme_change(self, value: str):
        try:
            idx = self._theme_values.index(value)
        except ValueError:
            idx = 0
        new_appearance_mode = THEME_MODES[idx]
        self._theme_mode = new_appearance_mode

        ctk.set_appearance_mode(new_appearance_mode)

        self.after(50, self._sync_surfaces)
        self._set_status("status_theme", mode=value)


def force_macos_redraw(window):
    try:
        window.update_idletasks()
        geo = window.geometry()
        size_part = geo.split("+")[0]
        w, h = (int(x) for x in size_part.split("x"))
        pos = geo[len(size_part):]
        window.geometry(f"{w + 1}x{h + 1}{pos}")
        window.update_idletasks()
        window.geometry(f"{w}x{h}{pos}")
        window.update_idletasks()
    except Exception as exc:
        print(f"[force_macos_redraw] skipped: {exc}")


if __name__ == "__main__":
    app = App()
    app.after(100, lambda: force_macos_redraw(app))
    app.mainloop()
