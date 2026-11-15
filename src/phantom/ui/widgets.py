"""Small CustomTkinter building blocks and the single private-API adapter.

CustomTkinter has no public API for a few things the design needs (the canvas
behind a scrollable frame, the inner Tk widget of a textbox/entry, the font
list of the Tk root).  Every such access is isolated here so a future CTk
release only requires changes in one file (D14).
"""

from __future__ import annotations

import logging
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import customtkinter as ctk

from phantom.ui import theme as T
from phantom.ui.clipboard import ClipboardActions

LOGGER: Final = logging.getLogger("phantom.widgets")


class CTkAdapter:
    """The only place that reads CustomTkinter private attributes."""

    @staticmethod
    def scroll_canvas(scrollable_frame: Any) -> Any | None:
        canvas = getattr(scrollable_frame, "_parent_canvas", None)
        if canvas is None:
            LOGGER.debug("CTk scrollable frame without _parent_canvas")
        return canvas

    @staticmethod
    def inner_textbox(textbox: Any) -> Any | None:
        inner = getattr(textbox, "_textbox", None)
        if inner is None:
            LOGGER.debug("CTk textbox without _textbox attribute")
        return inner

    @staticmethod
    def inner_entry(entry: Any) -> Any | None:
        inner = getattr(entry, "_entry", None)
        if inner is None:
            LOGGER.debug("CTk entry without _entry attribute")
        return inner

    @staticmethod
    def family(root: Any, candidates: Sequence[str], fallback: str) -> str:
        try:
            available = set(tkfont.families(root))
        except Exception as exc:  # noqa: BLE001 - pragma: no cover, Tk dependent
            LOGGER.warning("cannot list font families: %s", exc)
            return fallback
        for candidate in candidates:
            if candidate in available:
                return candidate
        return fallback


def display_font(root: Any, size: int, *, weight: str = "normal") -> ctk.CTkFont:
    family = CTkAdapter.family(root, T.FONT_DISPLAY, T.FONT_DISPLAY[1])
    return ctk.CTkFont(family=family, size=size, weight=weight)


def sans_font(root: Any, size: int, *, weight: str = "normal") -> ctk.CTkFont:
    family = CTkAdapter.family(root, T.FONT_SANS, T.FONT_SANS[1])
    return ctk.CTkFont(family=family, size=size, weight=weight)


def mono_font(root: Any, size: int, *, weight: str = "normal") -> ctk.CTkFont:
    family = CTkAdapter.family(root, T.FONT_MONO, T.FONT_MONO[1])
    return ctk.CTkFont(family=family, size=size, weight=weight)


def card(master: Any, **kwargs: Any) -> ctk.CTkFrame:
    options: dict[str, Any] = {
        "fg_color": T.CLR_CARD,
        "corner_radius": 18,
        "border_width": 1,
        "border_color": T.CLR_BORDER,
    }
    options.update(kwargs)
    return ctk.CTkFrame(master, **options)


def card_title(master: Any, root: Any, text: str = "") -> ctk.CTkLabel:
    return ctk.CTkLabel(
        master,
        text=text,
        font=sans_font(root, 11, weight="bold"),
        text_color=T.CLR_MUTED,
        anchor="w",
        height=16,
    )


def hint_label(master: Any, root: Any, text: str = "", *, wrap: int = 520) -> ctk.CTkLabel:
    return ctk.CTkLabel(
        master,
        text=text,
        font=sans_font(root, 12),
        text_color=T.CLR_TEXT_DIM,
        anchor="w",
        justify="left",
        wraplength=wrap,
    )


def value_label(master: Any, root: Any, text: str = "", *, mono: bool = False) -> ctk.CTkLabel:
    return ctk.CTkLabel(
        master,
        text=text,
        font=(mono_font if mono else sans_font)(root, 12),
        text_color=T.CLR_TEXT,
        anchor="w",
        justify="left",
    )


def primary_button(master: Any, root: Any, text: str, command: Any, **kwargs: Any) -> ctk.CTkButton:
    options: dict[str, Any] = {
        "text": text,
        "command": command,
        "height": 42,
        "corner_radius": 12,
        "font": sans_font(root, 13, weight="bold"),
        "fg_color": T.CLR_ACCENT,
        "hover_color": T.CLR_ACCENT_HOVER,
        "text_color": T.CLR_ON_ACCENT,
    }
    options.update(kwargs)
    return ctk.CTkButton(master, **options)


def secondary_button(
    master: Any, root: Any, text: str, command: Any, **kwargs: Any
) -> ctk.CTkButton:
    options: dict[str, Any] = {
        "text": text,
        "command": command,
        "height": 34,
        "corner_radius": 10,
        "font": sans_font(root, 12),
        "fg_color": T.CLR_CARD_ALT,
        "hover_color": T.CLR_ACCENT_DIM,
        "text_color": T.CLR_TEXT,
        "border_width": 1,
        "border_color": T.CLR_BORDER,
    }
    options.update(kwargs)
    return ctk.CTkButton(master, **options)


def separator(master: Any, *, height: int = 1) -> ctk.CTkFrame:
    return ctk.CTkFrame(master, height=height, fg_color=T.CLR_BORDER, corner_radius=0)


# ---------------------------------------------------------------------------
# text editing: clipboard shortcuts, context menu
# ---------------------------------------------------------------------------
#: ``(key, ClipboardActions method)`` pairs bound for Cmd and Ctrl.
SHORTCUT_KEYS: Final = (
    ("c", "copy"),
    ("v", "paste"),
    ("x", "cut"),
    ("a", "select_all"),
)

#: Right-click / two-finger tap on macOS, right-click elsewhere.
MENU_SEQUENCES: Final = ("<Button-2>", "<Button-3>", "<Control-Button-1>")

MENU_LABELS: Final = ("menu_paste", "menu_copy", "menu_cut", "menu_select_all")


def _appearance_color(pair: Any) -> str:
    """Pick the light or dark half of a theme tuple for the current mode."""
    if not isinstance(pair, tuple) or len(pair) < 2:
        return str(pair)
    dark = ctk.get_appearance_mode() == "Dark"
    return str(pair[1] if dark else pair[0])


def inner_text_widget(widget: Any) -> Any:
    """The Tk widget that really holds the text (CTkEntry/CTkTextbox wrapper)."""
    return CTkAdapter.inner_textbox(widget) or CTkAdapter.inner_entry(widget) or widget


@dataclass(slots=True)
class TextShortcuts:
    """Cmd/Ctrl + C/V/X/A and a right-click menu for one text widget."""

    widget: Any
    labels_provider: Callable[[], Mapping[str, str]] | None = None
    actions: ClipboardActions = field(init=False)
    sequences: list[str] = field(default_factory=list)
    _menu: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.actions = ClipboardActions(widget=self.widget)

    @property
    def bound_count(self) -> int:
        return len(self.sequences)

    def copy(self, _event: Any = None) -> str:
        self.actions.copy()
        return "break"

    def paste(self, _event: Any = None) -> str:
        self.actions.paste()
        return "break"

    def cut(self, _event: Any = None) -> str:
        self.actions.cut()
        return "break"

    def select_all(self, _event: Any = None) -> str:
        self.actions.select_all()
        return "break"

    def menu_labels(self) -> dict[str, str]:
        if self.labels_provider is None:
            return {
                "menu_paste": "Paste",
                "menu_copy": "Copy",
                "menu_cut": "Cut",
                "menu_select_all": "Select all",
            }
        try:
            return dict(self.labels_provider())
        except Exception as exc:  # noqa: BLE001 - labels must never break the menu
            LOGGER.warning("context menu labels failed: %s", exc)
            return {}

    def build_menu(self) -> Any:
        """Create the context menu for this widget (Tk ``Menu``)."""
        labels = self.menu_labels()
        menu = tk.Menu(self.widget, tearoff=0)
        try:
            menu.configure(
                bg=_appearance_color(T.CLR_CARD),
                fg=_appearance_color(T.CLR_TEXT),
                activebackground=_appearance_color(T.CLR_ACCENT),
                activeforeground=_appearance_color(T.CLR_ON_ACCENT),
                borderwidth=0,
            )
        except Exception as exc:  # noqa: BLE001 - Aqua ignores most menu colours
            LOGGER.debug("menu colours not supported: %s", exc)
        commands = (
            ("menu_paste", self.paste),
            ("menu_copy", self.copy),
            ("menu_cut", self.cut),
            ("menu_select_all", self.select_all),
        )
        for index, (key, command) in enumerate(commands):
            if index == 3:
                menu.add_separator()
            menu.add_command(label=labels.get(key, key), command=command)
        return menu

    def popup(self, event: Any) -> str:
        """Show the context menu at the pointer; returns ``"break"``."""
        try:
            self._menu = self.build_menu()
            self._menu.tk_popup(
                int(getattr(event, "x_root", 0)), int(getattr(event, "y_root", 0))
            )
        except Exception as exc:  # noqa: BLE001 - no display, or Tk refuses
            LOGGER.warning("context menu failed: %s", exc)
        finally:
            menu = self._menu
            if menu is not None:
                try:
                    menu.grab_release()
                except Exception as exc:  # noqa: BLE001 - release is best effort
                    LOGGER.debug("menu grab release failed: %s", exc)
        return "break"

    def install(self) -> int:
        """Bind the shortcuts and the menu; returns the number of bindings."""
        for key, method in SHORTCUT_KEYS:
            handler = getattr(self, method)
            for sequence in (
                f"<Command-{key}>",
                f"<Control-{key}>",
                f"<Command-{key.upper()}>",
                f"<Control-{key.upper()}>",
            ):
                try:
                    self.widget.bind(sequence, handler)
                except Exception as exc:  # noqa: BLE001 - destroyed widget
                    LOGGER.debug("shortcut %s not bound: %s", sequence, exc)
                    continue
                self.sequences.append(sequence)
        for sequence in MENU_SEQUENCES:
            try:
                self.widget.bind(sequence, self.popup)
            except Exception as exc:  # noqa: BLE001 - platform dependent
                LOGGER.debug("menu binding %s not bound: %s", sequence, exc)
        return self.bound_count


def install_text_shortcuts(
    root: Any,
    widget: Any,
    *,
    labels_provider: Callable[[], Mapping[str, str]] | None = None,
) -> TextShortcuts:
    """Give an entry/textbox working Cmd/Ctrl shortcuts and a context menu.

    ``root`` is accepted for symmetry with the other helpers and for logging;
    the bindings are installed on the inner Tk widget so CustomTkinter's own
    proxies cannot swallow them.
    """
    inner = inner_text_widget(widget)
    shortcuts = TextShortcuts(widget=inner, labels_provider=labels_provider)
    shortcuts.install()
    LOGGER.debug(
        "text shortcuts on %s: %d keyboard bindings, root=%r",
        type(widget).__name__,
        shortcuts.bound_count,
        root,
    )
    return shortcuts
