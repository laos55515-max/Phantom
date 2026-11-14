"""Colour palette and appearance-mode handling for CustomTkinter.

The palette keeps the original PHANTOM look (light/dark tuples) and adds the
state colours used by the new progress/notice surfaces.  No widget code lives
here, so the module is importable without a display.
"""

from __future__ import annotations

import sys
from typing import Final, Literal

AppearanceTuple = tuple[str, str]

CLR_BG: Final[AppearanceTuple] = ("#F3F4F6", "#0B0F19")
CLR_LAYER: Final[AppearanceTuple] = ("#EAECF1", "#0F1523")
CLR_CARD: Final[AppearanceTuple] = ("#FFFFFF", "#151B2B")
CLR_CARD_ALT: Final[AppearanceTuple] = ("#F3F4F6", "#1C2438")
CLR_FIELD: Final[AppearanceTuple] = ("#FFFFFF", "#0D121E")
CLR_BORDER: Final[AppearanceTuple] = ("#E5E7EB", "#1F2937")
CLR_BORDER_ACTIVE: Final[AppearanceTuple] = ("#2563EB", "#3B82F6")
CLR_BORDER_OVER: Final[AppearanceTuple] = ("#93C5FD", "#1D4ED8")

CLR_ACCENT: Final[AppearanceTuple] = ("#2563EB", "#2563EB")
CLR_ACCENT_HOVER: Final[AppearanceTuple] = ("#1D4ED8", "#3B82F6")
CLR_ACCENT_SOFT: Final[AppearanceTuple] = ("#2563EB", "#93B4FC")
CLR_ACCENT_DIM: Final[AppearanceTuple] = ("#DBEAFE", "#1B2E4D")

CLR_TEXT: Final[AppearanceTuple] = ("#111827", "#F9FAFB")
CLR_TEXT_DIM: Final[AppearanceTuple] = ("#4B5563", "#9CA3AF")
CLR_MUTED: Final[AppearanceTuple] = ("#6B7280", "#6B7280")
CLR_ON_ACCENT: Final[AppearanceTuple] = ("#FFFFFF", "#FFFFFF")

CLR_GREEN: Final[AppearanceTuple] = ("#047857", "#34D399")
CLR_RED: Final[AppearanceTuple] = ("#B91C1C", "#F87171")
CLR_YELLOW: Final[AppearanceTuple] = ("#B45309", "#FBBF24")

CLR_PROGRESS_TROUGH: Final[AppearanceTuple] = ("#E5E7EB", "#1A2233")
CLR_SCROLL: Final[AppearanceTuple] = ("#D1D5DB", "#243044")
CLR_SCROLL_HOVER: Final[AppearanceTuple] = ("#2563EB", "#3B82F6")

THEME_MODES: Final = ("Dark", "Light", "System")
ThemeMode = Literal["Dark", "Light", "System"]

#: Fonts (kept from the original design), with per-platform fallbacks.
FONT_DISPLAY: Final = (
    ("SF Pro Display", "Helvetica Neue") if sys.platform == "darwin" else
    ("Segoe UI Variable Display", "Segoe UI") if sys.platform.startswith("win") else
    ("Inter", "DejaVu Sans")
)
FONT_SANS: Final = (
    ("SF Pro Text", "Helvetica Neue") if sys.platform == "darwin" else
    ("Segoe UI", "Segoe UI") if sys.platform.startswith("win") else
    ("Inter", "DejaVu Sans")
)
FONT_MONO: Final = (
    ("SF Mono", "Menlo") if sys.platform == "darwin" else
    ("Cascadia Mono", "Consolas") if sys.platform.startswith("win") else
    ("JetBrains Mono", "DejaVu Sans Mono")
)

IS_MACOS: Final = sys.platform == "darwin"
IS_WINDOWS: Final = sys.platform.startswith("win")
IS_LINUX: Final = sys.platform.startswith("linux")


def appearance_for(theme: str) -> str:
    """Map a stored theme choice onto the CTk appearance mode."""
    return theme if theme in ("Dark", "Light") else "System"


def severity_color(severity: str) -> AppearanceTuple:
    return {
        "ok": CLR_GREEN,
        "warn": CLR_YELLOW,
        "error": CLR_RED,
        "info": CLR_ACCENT_SOFT,
    }.get(severity, CLR_TEXT_DIM)
