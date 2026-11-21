"""Colour palette and appearance-mode handling for CustomTkinter.

The palette keeps the original PHANTOM look (light/dark tuples) and adds the
state colours used by the new progress/notice surfaces.  No widget code lives
here, so the module is importable without a display.
"""

from __future__ import annotations

import sys
from typing import Final, Literal

AppearanceTuple = tuple[str, str]

CLR_BG: Final[AppearanceTuple] = ("#F5F6F8", "#15181E")
CLR_LAYER: Final[AppearanceTuple] = ("#EAECEF", "#191D24")
CLR_CARD: Final[AppearanceTuple] = ("#FFFFFF", "#1D2129")
CLR_CARD_ALT: Final[AppearanceTuple] = ("#EBEEF3", "#262A33")
CLR_FIELD: Final[AppearanceTuple] = ("#F1F3F6", "#12151B")
CLR_BORDER: Final[AppearanceTuple] = ("#DCE1EA", "#282D38")
CLR_BORDER_ACTIVE: Final[AppearanceTuple] = ("#0071E3", "#0A84FF")
CLR_BORDER_OVER: Final[AppearanceTuple] = ("#93C5FD", "#1D4ED8")

CLR_ACCENT: Final[AppearanceTuple] = ("#0071E3", "#0071E3")
CLR_ACCENT_HOVER: Final[AppearanceTuple] = ("#0077ED", "#0A84FF")
CLR_ACCENT_SOFT: Final[AppearanceTuple] = ("#0071E3", "#6CB6FF")
CLR_ACCENT_DIM: Final[AppearanceTuple] = ("#DBEAFE", "#162942")
CLR_ACCENT_BG: Final[AppearanceTuple] = ("#E8F2FF", "#162942")
#: A disabled button keeps its shape but loses its accent: CustomTkinter only greys
#: the caption, so an unavailable "save" still looked like a blue, clickable button.
CLR_DISABLED_FILL: Final[AppearanceTuple] = ("#D6DCE6", "#232833")

CLR_TEXT: Final[AppearanceTuple] = ("#111827", "#F3F5F8")
CLR_TEXT_DIM: Final[AppearanceTuple] = ("#4B5563", "#A4ADBC")
#: Secondary text: card headings, captions, the footer stage line.  Each mode
#: gets its own value, both at WCAG AA or better on CLR_CARD and CLR_FIELD.
CLR_MUTED: Final[AppearanceTuple] = ("#4B5563", "#9CA3AF")
CLR_ON_ACCENT: Final[AppearanceTuple] = ("#FFFFFF", "#FFFFFF")

CLR_GREEN: Final[AppearanceTuple] = ("#047857", "#30D158")
CLR_GREEN_BG: Final[AppearanceTuple] = ("#ECFDF5", "#172A21")
CLR_RED: Final[AppearanceTuple] = ("#B91C1C", "#FF453A")
CLR_RED_BG: Final[AppearanceTuple] = ("#FEF2F2", "#2C181C")
CLR_YELLOW: Final[AppearanceTuple] = ("#B45309", "#FFD60A")
CLR_YELLOW_BG: Final[AppearanceTuple] = ("#FFFBEB", "#2B2516")

CLR_PROGRESS_TROUGH: Final[AppearanceTuple] = ("#DCE1EA", "#232833")
CLR_SCROLL: Final[AppearanceTuple] = ("#D1D5DB", "#2A303C")
CLR_SCROLL_HOVER: Final[AppearanceTuple] = ("#0071E3", "#0A84FF")

#: Type scale, in points, as the widgets ask for them.  One place instead of a
#: literal at every call site: the 2026 pass raised body text to 14 and card
#: headings to 14, and a heading smaller than the text it heads is the mistake
#: this scale exists to prevent.
FONT_SIZE_TITLE: Final = 26
FONT_SIZE_CARD_TITLE: Final = 14
FONT_SIZE_BODY: Final = 14
FONT_SIZE_LABEL: Final = 12
FONT_SIZE_HINT: Final = 13
FONT_SIZE_MONO: Final = 13
FONT_SIZE_FOOTER: Final = 11
FONT_SIZE_SMALL: Final = 11

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


def _channel(value: str) -> float:
    """One sRGB channel on the 0..1 scale, gamma-corrected for luminance."""
    raw = int(value, 16) / 255.0
    return raw / 12.92 if raw <= 0.04045 else ((raw + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    """WCAG relative luminance of ``#RRGGBB``."""
    digits = color.lstrip("#")
    red, green, blue = (digits[index : index + 2] for index in (0, 2, 4))
    return (
        0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)
    )


def contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast ratio between two ``#RRGGBB`` colours (1.0 … 21.0)."""
    high, low = sorted(
        (relative_luminance(first), relative_luminance(second)), reverse=True
    )
    return (high + 0.05) / (low + 0.05)
