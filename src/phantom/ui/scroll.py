"""Mouse-wheel and trackpad scrolling for the CustomTkinter layout.

CustomTkinter binds ``<MouseWheel>`` with ``bind_all`` and then walks the widget
chain itself.  That path is fragile in exactly the cases macOS users hit: the
event is dropped for widgets it does not recognise, ``delta`` is converted with
an integer division that rounds a trackpad notch to zero, and subtrees such as
``CTkTextbox`` are opted out entirely.  The result is the classic "only the
scrollbar works" complaint.

This module binds the wheel events *explicitly* on every widget of a scrollable
area (and on the children added later), converts the platform delta correctly,
hands the event to a text widget while that widget can still scroll by itself,
and only then forwards it to the enclosing scrollable frame.

No ``tkinter``/``customtkinter`` import at module level (D14): every function
works on duck-typed objects, so the whole router is unit-testable without a
display.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Final

LOGGER: Final = logging.getLogger("phantom.scroll")

WHEEL_SEQUENCES: Final = ("<MouseWheel>", "<Button-4>", "<Button-5>")


def is_text_like(widget: Any) -> bool:
    """True for a Text widget (selection tags and its own vertical view)."""
    return callable(getattr(widget, "tag_add", None)) and callable(
        getattr(widget, "yview", None)
    )


def wheel_steps(event: Any, *, platform: str | None = None) -> int:
    """Convert a Tk wheel event into ``yview_scroll`` units.

    Positive means "scroll down" (the view moves towards the end of the
    content).  macOS reports a small, already inverted delta, Windows reports
    multiples of 120 and X11 uses Button-4/5.
    """
    number = getattr(event, "num", None)
    if number in (4, 5):
        return -1 if number == 4 else 1
    delta = getattr(event, "delta", 0)
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        return 0
    if delta == 0:
        return 0
    if (platform or sys.platform).lower().startswith("darwin"):
        # Aqua Tk sends ±1 per wheel notch (and small pixel steps for a
        # trackpad), already inverted relative to ``yview_scroll``: scrolling
        # down arrives as delta = -1.  Dividing by 120 — the Windows
        # convention — would round every notch to zero and the area would not
        # move at all, which is exactly the "only the scrollbar works" report.
        # Two units per step keeps one notch visibly useful.
        return int(-delta) * 2
    steps = round(delta / 120)
    return -steps if steps else (-1 if delta > 0 else 1)


@dataclass(slots=True)
class ScrollTarget:
    """A scrollable frame together with the canvas that actually scrolls."""

    frame: Any
    canvas: Any


@dataclass(slots=True)
class _Bound:
    widget: Any
    sequence: str
    funcid: str


@dataclass(slots=True)
class MouseWheelRouter:
    """Routes wheel events from any descendant widget to its scrollable frame."""

    root: Any
    platform: str | None = None
    targets: list[ScrollTarget] = field(default_factory=list)
    _bound: list[_Bound] = field(default_factory=list)

    # -- registration --------------------------------------------------
    def register(self, frame: Any, canvas: Any) -> ScrollTarget:
        """Add a scrollable area; returns the stored target (idempotent)."""
        for existing in self.targets:
            if existing.frame is frame:
                existing.canvas = canvas
                return existing
        target = ScrollTarget(frame=frame, canvas=canvas)
        self.targets.append(target)
        if self._bound:
            self._bind_target(target)
        return target

    def unregister(self, frame: Any) -> None:
        self.targets = [target for target in self.targets if target.frame is not frame]

    @property
    def bound_count(self) -> int:
        return len(self._bound)

    # -- binding -------------------------------------------------------
    def install(self) -> int:
        """Bind every registered area; returns how many bindings were created."""
        for target in self.targets:
            self._bind_target(target)
        return len(self._bound)

    def refresh(self) -> int:
        """Bind widgets that appeared after :meth:`install` (dynamic content)."""
        known = {(item.widget, item.sequence) for item in self._bound}
        for target in self.targets:
            for widget in _area_widgets(target):
                for sequence in WHEEL_SEQUENCES:
                    if (widget, sequence) in known:
                        continue
                    self._bind_one(widget, sequence)
        return len(self._bound)

    def uninstall(self) -> None:
        """Remove every binding this router created (safe to call twice)."""
        for item in self._bound:
            try:
                item.widget.unbind(item.sequence, item.funcid)
            except Exception as exc:  # noqa: BLE001 - the widget may be gone already
                LOGGER.debug("unbind %s failed: %s", item.sequence, exc)
        self._bound.clear()

    def _bind_target(self, target: ScrollTarget) -> None:
        for widget in _area_widgets(target):
            for sequence in WHEEL_SEQUENCES:
                self._bind_one(widget, sequence)

    def _bind_one(self, widget: Any, sequence: str) -> None:
        binder = getattr(widget, "bind", None)
        if not callable(binder):
            return
        try:
            existing = binder(sequence)  # query: '' when nothing is bound
        except Exception as exc:  # noqa: BLE001 - Tk refuses some sequences
            LOGGER.debug("bind query failed: %s", exc)
            return
        if existing:
            # CustomTkinter already handles this widget (the canvas); binding
            # again would scroll twice per notch.
            return
        try:
            funcid = binder(sequence, self.dispatch, "+")
        except Exception as exc:  # noqa: BLE001 - destroyed or unsupported widget
            LOGGER.debug("bind %s failed: %s", sequence, exc)
            return
        if funcid:
            self._bound.append(_Bound(widget=widget, sequence=sequence, funcid=str(funcid)))

    # -- event handling ------------------------------------------------
    def dispatch(self, event: Any) -> str | None:
        """Handle one wheel event; returns ``"break"`` when it was consumed."""
        widget = getattr(event, "widget", None)
        steps = wheel_steps(event, platform=self.platform)
        if widget is None or steps == 0:
            return None
        if is_text_like(widget) and self._scroll_text(widget, steps):
            return "break"
        target = self._target_for(widget) or self._target_at(event)
        if target is None:
            return None
        _scroll_canvas(target.canvas, steps)
        return "break"

    def _scroll_text(self, widget: Any, steps: int) -> bool:
        """Scroll a Text widget itself while it still has room in that direction."""
        try:
            first, last = widget.yview()
        except Exception as exc:  # noqa: BLE001 - destroyed widget
            LOGGER.debug("text yview failed: %s", exc)
            return False
        can_scroll = first > 0.0 if steps < 0 else last < 1.0
        if not can_scroll:
            return False
        try:
            widget.yview_scroll(steps, "units")
        except Exception as exc:  # noqa: BLE001 - destroyed widget
            LOGGER.debug("text scroll failed: %s", exc)
            return False
        return True

    def _target_for(self, widget: Any) -> ScrollTarget | None:
        node = widget
        depth = 0
        while node is not None and depth < 64:
            for target in self.targets:
                if node is target.frame or node is target.canvas:
                    return target
            node = getattr(node, "master", None)
            depth += 1
        return None

    def _target_at(self, event: Any) -> ScrollTarget | None:
        """Last resort: hit-test the pointer position (Tk only)."""
        containing = getattr(self.root, "winfo_containing", None)
        if not callable(containing):
            return None
        try:
            node = containing(getattr(event, "x_root", 0), getattr(event, "y_root", 0))
        except Exception as exc:  # noqa: BLE001 - no widget under the pointer
            LOGGER.debug("winfo_containing failed: %s", exc)
            return None
        return self._target_for(node) if node is not None else None


def _scroll_canvas(canvas: Any, steps: int) -> None:
    scroll = getattr(canvas, "yview_scroll", None)
    if not callable(scroll):
        return
    try:
        scroll(steps, "units")
    except Exception as exc:  # noqa: BLE001 - destroyed canvas
        LOGGER.debug("canvas scroll failed: %s", exc)


def _area_widgets(target: ScrollTarget) -> Iterator[Any]:
    """The canvas, the frame and every descendant of the frame."""
    yield target.canvas
    yield target.frame
    yield from _descendants(target.frame)


def _descendants(widget: Any) -> Iterator[Any]:
    children = getattr(widget, "winfo_children", None)
    if not callable(children):
        return
    try:
        kids = list(children())
    except Exception as exc:  # noqa: BLE001 - destroyed widget
        LOGGER.debug("winfo_children failed: %s", exc)
        return
    for child in kids:
        yield child
        yield from _descendants(child)


def install_mousewheel(
    root: Any, frame: Any, canvas: Any, *, platform: str | None = None
) -> MouseWheelRouter:
    """Convenience wrapper: route one scrollable area, ready to install."""
    router = MouseWheelRouter(root=root, platform=platform)
    router.register(frame, canvas)
    router.install()
    return router
