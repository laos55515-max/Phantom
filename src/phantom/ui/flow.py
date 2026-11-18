"""Layout arithmetic without Tk: measured widths and the wrapping rule.

:mod:`phantom.ui.responsive` builds the widgets; everything that can be decided
with numbers lives here so it is testable on a machine with no display.  That is
also the reason this module imports nothing from ``tkinter`` or ``customtkinter``:
a regression in the wrapping rule must be catchable in a unit test, not only in
the GUI suite that needs an X server.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from typing import Any, Final

LOGGER: Final = logging.getLogger("phantom.flow")

#: Logical pixels of padding added to each side of a button caption.
BUTTON_PAD: Final = 15
#: Extra room for the checkbox square and the gap between it and its caption.
CHECKBOX_EXTRA: Final = 30
#: Extra room for the arrow of an option menu.
OPTION_EXTRA: Final = 34
#: Gap between two items of one row, and between two rows of one flow.
FLOW_GAP: Final = 10
FLOW_ROW_GAP: Final = 8
#: A wrapped label never becomes narrower than this, or text turns into a column.
MIN_WRAP: Final = 240
#: Padding inside a card: the label is packed with ``padx=18`` on both sides.
DEFAULT_INSET: Final = 36


def flow_rows(
    widths: Sequence[int],
    available: int,
    gap: int = FLOW_GAP,
) -> tuple[tuple[int, int], ...]:
    """Place ``widths`` into rows that fit ``available`` pixels.

    Returns one ``(row, column)`` pair per item, in the order they were given.
    An item wider than the whole row gets a row of its own and is allowed to be
    clipped: wrapping cannot help there, and hiding a button is worse.
    """
    layout: list[tuple[int, int]] = []
    row = 0
    column = 0
    used = 0
    for width in widths:
        item = max(0, int(width))
        if column == 0:
            used = item
            layout.append((row, column))
            column = 1
            continue
        needed = used + gap + item
        if available > 0 and needed > available:
            row += 1
            column = 0
            used = item
        else:
            used = needed
        layout.append((row, column))
        column += 1
    return tuple(layout)


def row_count(layout: Sequence[tuple[int, int]]) -> int:
    """How many visual rows a layout produced."""
    return max((row for row, _column in layout), default=-1) + 1


def measure_text(font: Any, text: str) -> int:
    """Width of ``text`` in this font, in pixels; 0 when the font cannot answer.

    A shared ``CTkFont`` is a real Tk font, so it can measure itself.  The
    fallback keeps a broken font, or a widget whose font is gone, from crashing a
    layout; it is not silence, a debug line says which font failed.
    """
    if not text:
        return 0
    try:
        return int(font.measure(text))
    except Exception as exc:  # noqa: BLE001 - any font object may refuse
        LOGGER.debug("cannot measure %r: %s", text[:24], exc)
        return 0


def fitted_width(
    font: Any,
    text: str,
    *,
    measure: Any = None,
    pad: int = BUTTON_PAD,
    extra: int = 0,
    minimum: int = 0,
    scaling: float = 1.0,
) -> int:
    """Logical width that shows all of ``text`` in ``font``; never below ``minimum``.

    ``measure`` defaults to :func:`measure_text`; it is a parameter so a test can
    substitute a font that measures by arithmetic instead of by Tk.
    """
    pixels = (measure or measure_text)(font, text)
    logical = 0 if pixels <= 0 else math.ceil(pixels / (scaling or 1.0))
    return max(int(minimum), logical + 2 * pad + extra)


def apply_width(widget: Any, width: int) -> int:
    """Set the widget's logical width unless it is already right; returns it."""
    try:
        current = int(widget.cget("width"))
    except Exception as exc:  # noqa: BLE001 - a widget without width is not our business
        LOGGER.debug("no width on %r: %s", widget, exc)
        current = -1
    if current != width:
        try:
            widget.configure(width=width)
        except Exception as exc:  # noqa: BLE001 - never break a repaint
            LOGGER.debug("cannot set width=%s on %r: %s", width, widget, exc)
            return current
    return width
