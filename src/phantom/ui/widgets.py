"""Small CustomTkinter building blocks and the single private-API adapter.

CustomTkinter has no public API for a few things the design needs (the canvas
behind a scrollable frame, the inner Tk widget of a textbox/entry, the font
list of the Tk root).  Every such access is isolated here so a future CTk
release only requires changes in one file (D14).
"""

from __future__ import annotations

import logging
import tkinter.font as tkfont
from collections.abc import Sequence
from typing import Any, Final

import customtkinter as ctk

from phantom.ui import theme as T

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
