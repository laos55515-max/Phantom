"""Responsive layout: buttons sized from their text, rows that wrap, labels that fit.

Three defects shared one cause — a widget whose width was decided before the
text was known:

* a CustomTkinter button keeps the width it was created with (140 logical
  pixels) unless something measures the label, so a long localised caption such
  as ``ВЫБРАТЬ ФАЙЛ (PNG / WAV / FLAC)`` is clipped on both sides;
* a row of buttons was packed with ``side="left"`` and simply ran off the card
  when the window was narrower than the sum of its buttons;
* hint labels carried a hard-coded ``wraplength`` (520/620/760) that had nothing
  to do with the width of the card they ended up in.

The fix is not a bigger minimum window and not a smaller font: every label is
measured in its own font, the width follows the text in the current language,
and a row that no longer fits moves the overflow to the next line.

``flow_rows`` is pure arithmetic so the wrapping rule is tested without a
display; :class:`FlowRow`, :class:`Responsive` and the ``fit_*`` helpers are the
Tk side and are exercised by the GUI tests.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Final

import customtkinter as ctk

from phantom.ui.flow import (
    BUTTON_PAD,
    CHECKBOX_EXTRA,
    DEFAULT_INSET,
    FLOW_GAP,
    FLOW_ROW_GAP,
    MIN_WRAP,
    OPTION_EXTRA,
    apply_width,
    fitted_width,
    flow_rows,
    measure_text,
    row_count,
    width_to_apply,
)

__all__ = [
    "BUTTON_PAD",
    "CHECKBOX_EXTRA",
    "DEFAULT_INSET",
    "FLOW_GAP",
    "FLOW_ROW_GAP",
    "MIN_WRAP",
    "OPTION_EXTRA",
    "FlowRow",
    "Responsive",
    "apply_width",
    "attach_refit",
    "fit_button",
    "fit_button_on_change",
    "fit_checkbox",
    "fit_checkbox_on_change",
    "fit_on_change",
    "fit_option_menu",
    "fit_option_menu_on_change",
    "fit_switch",
    "fit_switch_on_change",
    "fitted_width",
    "flow_rows",
    "row_count",
    "widget_scaling",
    "width_to_apply",
]

LOGGER: Final = logging.getLogger("phantom.responsive")


def widget_scaling(widget: Any) -> float:
    """CustomTkinter's scaling factor for this widget; 1.0 when unavailable."""
    tracker = getattr(ctk, "ScalingTracker", None)
    getter = getattr(tracker, "get_widget_scaling", None)
    if getter is None:
        return 1.0
    try:
        value = float(getter(widget))
    except Exception:  # noqa: BLE001 - pragma: no cover, CTk version dependent
        return 1.0
    return value if value > 0 else 1.0


def _shown(widget: Any) -> bool:
    """Whether the window has already placed this widget on screen."""
    try:
        return bool(widget.winfo_ismapped())
    except Exception:  # noqa: BLE001 - a widget without a window is not shown
        return False


def _set_width(widget: Any, width: int) -> int:
    """``flow.apply_width``, plus the rule that keeps start-up cheap.

    Resizing a CustomTkinter widget redraws it.  Before the window is on screen
    that redraw buys nothing: the widget has never been painted, and a width it
    already has is still wide enough to show its caption.  So during the build the
    width CustomTkinter was given (140 for a button, 100 for a checkbox) acts as a
    floor and only a longer caption widens the widget — one redraw per *long*
    caption instead of one per widget, which was the difference between a 300 ms
    and a 500 ms first frame.  A widget already on screen is measured exactly,
    which is what a language switch needs.
    """
    try:
        current = int(widget.cget("width"))
    except Exception:  # noqa: BLE001 - no width: apply_width reports it
        return apply_width(widget, width)
    return apply_width(widget, width_to_apply(current, width, shown=_shown(widget)))


def _widget_font(widget: Any) -> Any:
    try:
        return widget.cget("font")
    except Exception:  # noqa: BLE001 - pragma: no cover, Tk dependent
        return None


def fit_button(button: Any, *, font: Any = None, minimum: int = 0) -> int:
    """Give a button the width its current caption needs; returns it."""
    target_font = font if font is not None else _widget_font(button)
    if target_font is None:
        return 0
    try:
        text = str(button.cget("text") or "")
    except Exception:  # noqa: BLE001 - pragma: no cover, Tk dependent
        return 0
    if not text.strip():
        # No caption yet: the binder sets it and calls back.  Sizing to an empty
        # string would redraw the button twice for nothing, ~60 times per window.
        return 0
    width = fitted_width(
        target_font,
        text,
        measure=measure_text,
        minimum=minimum,
        scaling=widget_scaling(button),
    )
    return _set_width(button, width)


def fit_checkbox(widget: Any, *, minimum: int = 0) -> int:
    """A checkbox's label is drawn inside the widget, so its width must cover it."""
    target_font = _widget_font(widget)
    if target_font is None:
        return 0
    try:
        text = str(widget.cget("text") or "")
    except Exception:  # noqa: BLE001 - pragma: no cover, Tk dependent
        return 0
    if not text.strip():
        return 0  # nothing to measure yet; the binder calls back with a caption
    width = fitted_width(
        target_font,
        text,
        measure=measure_text,
        pad=8,
        extra=CHECKBOX_EXTRA,
        minimum=minimum,
        scaling=widget_scaling(widget),
    )
    return _set_width(widget, width)


def fit_option_menu(menu: Any, *, minimum: int = 0) -> int:
    """An option menu clips its value just like a button does."""
    target_font = _widget_font(menu)
    if target_font is None:
        return 0
    try:
        text = str(menu.cget("values")[0] if menu.cget("values") else "")
    except Exception:  # noqa: BLE001 - pragma: no cover, Tk dependent
        return 0
    try:
        current = str(menu.get() or "")
        if current:
            text = current
    except Exception as exc:  # noqa: BLE001 - pragma: no cover, CTk version dependent
        LOGGER.debug("option menu value unavailable: %s", exc)
    width = fitted_width(
        target_font,
        text,
        measure=measure_text,
        pad=12,
        extra=OPTION_EXTRA,
        minimum=minimum,
        scaling=widget_scaling(menu),
    )
    return _set_width(menu, width)


def fit_switch(widget: Any, *, minimum: int = 0) -> int:
    """A switch label needs room for the switch itself plus its caption."""
    target_font = _widget_font(widget)
    if target_font is None:
        return 0
    try:
        text = str(widget.cget("text") or "")
    except Exception:  # noqa: BLE001 - pragma: no cover, Tk dependent
        return 0
    if not text.strip():
        return 0  # nothing to measure yet; the binder calls back with a caption
    width = fitted_width(
        target_font,
        text,
        measure=measure_text,
        pad=8,
        extra=CHECKBOX_EXTRA + 18,
        minimum=minimum,
        scaling=widget_scaling(widget),
    )
    return _set_width(widget, width)


def attach_refit(widget: Any, refit: Callable[[], Any]) -> Any:
    """Let the label binder re-measure a widget after it changes its text.

    :class:`phantom.ui.bindings.I18nBinder` calls ``_phantom_after_text`` when it
    has set a translated caption; Tk is not involved, so the hook stays testable
    with a plain fake widget.
    """
    widget._phantom_after_text = refit  # deliberate duck-typed hook
    return widget


def fit_on_change(widget: Any, fitter: Callable[..., int], **kwargs: Any) -> Any:
    """Run ``fitter`` now and after every caption change the binder makes."""
    def refit() -> int:
        return fitter(widget, **kwargs)

    attach_refit(widget, refit)
    refit()
    return widget


def fit_checkbox_on_change(widget: Any, *, minimum: int = 0) -> Any:
    """A checkbox caption is drawn inside the widget: keep the width in step."""
    return fit_on_change(widget, fit_checkbox, minimum=minimum)


def fit_switch_on_change(widget: Any, *, minimum: int = 0) -> Any:
    """``CTkSwitch`` puts its caption next to the switch, inside its own width."""
    return fit_on_change(widget, fit_switch, minimum=minimum)


def fit_option_menu_on_change(menu: Any, *, minimum: int = 0) -> Any:
    """A dropdown shows the selected value inside a fixed width: measure it."""
    return fit_on_change(menu, fit_option_menu, minimum=minimum)


def fit_button_on_change(button: Any, *, font: Any = None, minimum: int = 0) -> Any:
    """``fit_button`` now and after every caption change (language switch)."""
    def refit() -> int:
        return fit_button(button, font=font, minimum=minimum)

    attach_refit(button, refit)
    refit()
    return button


class FlowRow(ctk.CTkFrame):  # type: ignore[misc]
    """A frame that lays its children out in rows and wraps the overflow.

    Children are gridded, not packed, so the row height follows the number of
    lines; the wrap decision is re-made whenever the frame's width changes, and
    children keep their measured width (they are never squeezed).
    """

    def __init__(
        self,
        master: Any,
        *,
        gap: int = FLOW_GAP,
        row_gap: int = FLOW_ROW_GAP,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("fg_color", "transparent")
        super().__init__(master, **kwargs)
        self._items: list[Any] = []
        self._gap = gap
        self._row_gap = row_gap
        self._layout: tuple[tuple[int, int], ...] = ()
        self._bound_width = 0
        self._columns = 0
        # Measured once per caption change, not once per resize: ``_item_width``
        # reaches into Tk for labels (``winfo_reqwidth``), and a drag-resize used to
        # pay for every child of every row on every step.
        self._widths: list[int] = []
        self._cells: list[tuple[int, int]] = []
        self._minsizes: list[int] = []
        # A row inside a tab nobody is looking at: remember that it owes a pass and
        # make it when the tab is opened, instead of laying out hidden widgets.
        self.suspended = False
        self._dirty = False
        # Diagnostics the tests read: passes that did arithmetic, children re-placed.
        self.reflows = 0
        self.placements = 0
        self.bind("<Configure>", self._on_configure, add="+")

    # -- content --------------------------------------------------------
    @property
    def items(self) -> tuple[Any, ...]:
        return tuple(self._items)

    def add(self, widget: Any) -> Any:
        """Append a child; it is placed on the next line when it no longer fits."""
        self._items.append(widget)
        self._layout = ()
        self._cells = []
        self._widths = []
        self.reflow(force=True)
        return widget

    def mark_dirty(self) -> None:
        """This row owes a pass; it runs when the row is next shown."""
        self._dirty = True

    def redraw_now(self, *, force: bool = False) -> tuple[tuple[int, int], ...]:
        """Place the children now, whatever the visible-tab rule says."""
        self.suspended = False
        return self.reflow(force=force)

    def rows(self) -> int:
        return row_count(self._layout)

    # -- layout ---------------------------------------------------------
    def _on_configure(self, event: Any) -> None:
        width = int(getattr(event, "width", 0) or 0)
        if abs(width - self._bound_width) < 2:
            return
        self._bound_width = width
        if self.suspended:
            # A tab nobody is looking at resizes exactly like the visible one, and
            # laying it out costs the same.  The breakpoints are recorded; the work
            # waits for the moment the tab is opened.
            self._dirty = True
            return
        self.reflow(available=width)

    def _item_width(self, widget: Any) -> int:
        """The logical width the item needs; measured, never squeezed."""
        try:
            return int(widget.cget("width"))
        except Exception as exc:  # noqa: BLE001 - labels have no width
            LOGGER.debug("no width on %r: %s", widget, exc)
        try:
            return int(widget.winfo_reqwidth() / (widget_scaling(widget) or 1.0))
        except Exception:  # noqa: BLE001 - pragma: no cover, Tk dependent
            return 0

    def reflow(
        self, *, available: int | None = None, force: bool = False
    ) -> tuple[tuple[int, int], ...]:
        """Re-place the children for ``available`` pixels (default: current width).

        ``force`` re-measures the children; without it the widths measured for the
        current captions are reused, which is what makes a resize cheap.  A pass
        whose breakpoints did not change touches no widget at all, and a pass that
        did change them moves only the children whose cell changed — ``grid``
        re-places a widget, so there is nothing to forget first.
        """
        width = self._bound_width if available is None else int(available)
        if width <= 1:
            return self._layout
        if not self._items:
            return self._layout
        if force or len(self._widths) != len(self._items):
            self._widths = [self._item_width(item) for item in self._items]
        widths = self._widths
        scaling = widget_scaling(self) or 1.0
        layout = flow_rows(widths, int(width / scaling), self._gap)
        self.reflows += 1
        self._dirty = False
        if layout == self._layout:
            return self._layout
        columns = max((column for _row, column in layout), default=-1) + 1
        while len(self._minsizes) < columns:
            self._minsizes.append(-1)
        for column in range(columns):
            widest = max(
                (widths[index] for index, (_row, col) in enumerate(layout) if col == column),
                default=0,
            )
            if self._minsizes[column] != widest:
                self.grid_columnconfigure(column, minsize=widest)
                self._minsizes[column] = widest
        for column in range(columns, len(self._minsizes)):
            if self._minsizes[column] != 0:
                self.grid_columnconfigure(column, minsize=0)
                self._minsizes[column] = 0
        if len(self._cells) != len(layout):
            self._cells = [(-1, -1)] * len(layout)
        for index, (row, column) in enumerate(layout):
            if self._cells[index] == (row, column):
                continue
            self._items[index].grid(
                row=row,
                column=column,
                sticky="w",
                padx=(self._gap if column else 0, 0),
                pady=(self._row_gap if row else 0, 0),
            )
            self._cells[index] = (row, column)
            self.placements += 1
        self._layout = layout
        self._columns = columns
        return layout


class Responsive:
    """Owns the measured widths and the wrap widths of one window.

    ``adopt`` walks the widget tree once, after the window is built: every label
    that asked for a responsive wrap gets its ``wraplength`` kept in step with
    the width of the card it sits in, grouped per card so one ``<Configure>``
    updates all of them.
    """

    def __init__(
        self,
        *,
        inset: int = DEFAULT_INSET,
        minimum: int = MIN_WRAP,
    ) -> None:
        self.inset = inset
        self.minimum = minimum
        self._groups: list[tuple[Any, list[Any], int]] = []
        self._adopted: int = 0
        self._root: Any = None
        # Every flow row with the chain of widgets above it, collected in the one
        # walk ``adopt`` already does.  A resize used to walk the whole tree again
        # to find them, on every ``<Configure>``.
        self._flows: list[tuple[FlowRow, tuple[Any, ...]]] = []
        self._active: Any = None
        # Diagnostics the tests read: tree walks that had to happen.
        self.walks = 0

    @property
    def labels(self) -> int:
        """How many labels follow their card's width."""
        return sum(len(items) for _master, items, _width in self._groups)

    def adopt(self, root: Any) -> int:
        """Register every label built with ``_phantom_autowrap`` under ``root``."""
        self._root = root
        groups: dict[Any, list[Any]] = {}
        chains: dict[Any, tuple[Any, ...]] = {root: (root,)}
        stack = [root]
        while stack:
            widget = stack.pop()
            try:
                children = widget.winfo_children()
            except Exception as exc:  # noqa: BLE001 - not every object is a widget
                LOGGER.debug("no children on %r: %s", widget, exc)
                children = []
            for child in children:
                chains[child] = (*chains.get(widget, ()), child)
                if isinstance(child, FlowRow):
                    self._flows.append((child, chains[child]))
                if getattr(child, "_phantom_autowrap", None) is not None:
                    groups.setdefault(child.master, []).append(child)
                stack.append(child)
        for master, labels in groups.items():
            widths = [0]
            self._groups.append((master, labels, 0))
            index = len(self._groups) - 1

            def on_configure(event: Any, index: int = index, widths: list[int] = widths) -> None:
                width = int(getattr(event, "width", 0) or 0)
                if abs(width - widths[0]) < 4:
                    return
                widths[0] = width
                self._apply(index, width)

            master.bind("<Configure>", on_configure, add="+")
        self._adopted += 1
        return self.labels

    def _apply(self, index: int, width: int) -> None:
        _master, labels, _width = self._groups[index]
        self._groups[index] = (_master, labels, width)
        wrap = max(self.minimum, width - self.inset)
        for label in labels:
            try:
                if int(label.cget("wraplength")) == wrap:
                    continue
                label.configure(wraplength=wrap)
            except Exception as exc:  # noqa: BLE001 - never break a repaint
                LOGGER.debug("cannot wrap %r: %s", label, exc)

    def rewrap(self, width: int | None = None) -> None:
        """Force a wrap recalculation (used after a language switch)."""
        for index, (master, _labels, current) in enumerate(list(self._groups)):
            target = current
            if width is not None:
                target = width
            elif current <= 1:
                try:
                    target = int(master.winfo_width())
                except Exception as exc:  # noqa: BLE001 - pragma: no cover, Tk dependent
                    LOGGER.debug("no width for %r: %s", master, exc)
                    target = current
            if target > 1:
                self._apply(index, target)

    def flows(self, root: Any = None) -> list[FlowRow]:
        """Every :class:`FlowRow` in the window, for a forced re-flow.

        With no argument this is the list ``adopt`` collected; passing a widget
        walks that subtree, which is only needed when something was built after
        the window was adopted.
        """
        if root is None:
            return [row for row, _chain in self._flows]
        start = root
        found: list[FlowRow] = []
        if start is None:
            return found
        self.walks += 1
        stack = [start]
        while stack:
            widget = stack.pop()
            if isinstance(widget, FlowRow):
                found.append(widget)
            try:
                stack.extend(widget.winfo_children())
            except Exception as exc:  # noqa: BLE001 - not every object is a widget
                LOGGER.debug("cannot walk %r: %s", widget, exc)
        return found

    def bind_active(self, active: Any) -> None:
        """Say which tab is on screen; rows under any other tab stop re-flowing.

        Hiding is not free in Tk: a tab that is stacked behind another one still
        resizes, still gets its ``<Configure>``, and used to be laid out on every
        step of a drag-resize.  Nothing of it can be seen, so the pass waits for
        the tab to be opened — and opening it forces one (``refit_flows``).
        """
        self._active = active
        for row, chain in self._flows:
            row.suspended = active is not None and active not in chain
            if row.suspended:
                row.mark_dirty()

    def refit_flows(self, *, force: bool = False, all_tabs: bool = False) -> None:
        """Ask every visible flow row to re-check its line breaks, once.

        ``force`` re-measures the children — the captions changed (a language
        switch), so the widths did too.  ``all_tabs`` includes the tabs that are
        not on screen, for the same reason: their new captions need laying out
        before they are opened, and a language switch is rare enough to pay for it.
        """
        for row, _chain in self._flows:
            if row.suspended and not all_tabs:
                row.mark_dirty()
                continue
            row.reflow(force=force)
