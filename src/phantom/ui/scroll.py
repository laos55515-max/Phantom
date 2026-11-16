"""Mouse-wheel routing by direct binding: every widget of a scrollable area.

``bind_all`` looked like the obvious fix and is not one: Aqua Tk delivers
``<MouseWheel>`` to the window, and CustomTkinter's own global handler — which
resolves the target from ``event.widget`` — gives up when that widget is the
root.  Binding the wheel directly on the frame, the canvas and every descendant
puts the handler where Tk actually delivers the event, so a card, a label or a
button inside a tab scrolls the tab it belongs to.

``install_tab_wheel`` is the entry point: it walks one scrollable area and
returns a :class:`TabWheel` that can be refreshed (children appear later) and
uninstalled.  Two macOS behaviours are handled there and not in the bindings
themselves, because both are per-gesture state:

* a trackpad reports **fractional** deltas — ``int(delta)`` turns every one of
  them into zero and the wheel appears dead, so the leftover fraction is carried
  over to the next event of the same tab;
* Tk 9 reports two-finger scrolling as its own ``<TouchpadScroll>`` sequence,
  which nothing used to bind at all.

Two kinds of widget must stay untouched:

* text areas (``CTkTextbox`` and the Tk ``Text`` inside it) — they scroll their
  own content, and swallowing the event would move the page instead;
* scrollbars (``CTkScrollbar``/``Scrollbar``) — they have their own drag
  handling and would move twice.

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

#: Tk 9 delivers two-finger scrolling on macOS as its own event; Tk 8.6 has no
#: such sequence, and binding it there raises ``TclError`` (which the binder
#: swallows per widget, so the same code runs on both).
TOUCHPAD_SEQUENCE: Final = "<TouchpadScroll>"

#: Every sequence one widget carries.  The classic three plus the Tk 9 trackpad
#: event: on 8.6 the last one simply fails to bind and is skipped.
WHEEL_SEQUENCES: Final = ("<MouseWheel>", "<Button-4>", "<Button-5>", TOUCHPAD_SEQUENCE)

#: macOS reports ±1 per notch, so one notch has to move a few lines to be felt.
MACOS_UNITS_PER_STEP: Final = 3

#: The Windows convention: one wheel notch is 120 delta units.
WINDOWS_DELTA_UNIT: Final = 120

#: Marks the sequences this module already bound on a widget, so a second pass
#: over the same tree (``install`` after ``refresh``) cannot scroll twice.
_BOUND_MARK: Final = "_phantom_wheel_sequences"

_TEXT_AREA_NAMES: Final = frozenset({"CTkTextbox", "Text", "ScrolledText"})
_SCROLLBAR_NAMES: Final = frozenset({"CTkScrollbar", "Scrollbar", "TScrollbar"})


def is_text_like(widget: Any) -> bool:
    """True for a Tk Text widget (selection tags and its own vertical view)."""
    return callable(getattr(widget, "tag_add", None)) and callable(
        getattr(widget, "yview", None)
    )


def is_text_area(widget: Any) -> bool:
    """True for a text widget or for the CustomTkinter wrapper around one."""
    return (
        is_text_like(widget)
        or getattr(widget, "_textbox", None) is not None
        or type(widget).__name__ in _TEXT_AREA_NAMES
    )


def is_scrollbar(widget: Any) -> bool:
    """True for a scrollbar, which owns its drag handling and must not scroll."""
    if type(widget).__name__ in _SCROLLBAR_NAMES:
        return True
    return callable(getattr(widget, "set", None)) and callable(
        getattr(widget, "get", None)
    ) and "Scrollbar" in type(widget).__name__


def wheel_delta(event: Any, *, platform: str | None = None) -> float:
    """Signed ``yview_scroll`` units for one event, fraction included.

    Positive means "towards the end of the content".  macOS sends a small,
    already inverted delta and a trackpad sends fractions of one notch, Windows
    sends multiples of 120 and X11 uses Button-4/5.  Kept as a float because
    dropping the fraction is exactly what made a trackpad look broken.
    """
    number = getattr(event, "num", None)
    if number in (4, 5):
        return -1.0 if number == 4 else 1.0
    try:
        delta = float(getattr(event, "delta", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    if delta == 0:
        return 0.0
    if (platform or sys.platform).lower().startswith("darwin"):
        # delta is ±1 per notch and positive means "scroll up", while
        # ``yview_scroll`` counts positive as "towards the end": flip the sign
        # and multiply.  Dividing by 120 — the Windows convention — rounds every
        # notch to zero units, which is the "only the scrollbar works" report.
        return -delta * MACOS_UNITS_PER_STEP
    return -delta / WINDOWS_DELTA_UNIT


def wheel_steps(event: Any, *, platform: str | None = None) -> int:
    """Whole ``yview_scroll`` units for a single event.

    One event, one move: a delta smaller than a unit still scrolls a line rather
    than nothing.  :class:`WheelAccumulator` is what a live tab uses instead, so
    that a stream of small trackpad deltas adds up to smooth motion.
    """
    value = wheel_delta(event, platform=platform)
    if value == 0:
        return 0
    whole = int(value)
    if whole == 0:
        return 1 if value > 0 else -1
    return whole


@dataclass(slots=True)
class WheelAccumulator:
    """Carries the fraction of a trackpad gesture from one event to the next.

    A macOS trackpad emits a long stream of deltas around ±0.1.  Rounded once
    each, every event scrolls nothing; accumulated, nine of them scroll a line.
    The state belongs to one scrollable area, so two tabs cannot borrow each
    other's leftover.
    """

    leftover: float = 0.0

    def units(self, event: Any, *, platform: str | None = None) -> int:
        """Whole units this event contributes, keeping the remainder."""
        total = self.leftover + wheel_delta(event, platform=platform)
        whole = int(total)
        self.leftover = total - whole
        # A long gesture must not bank a stray fraction forever.
        if abs(self.leftover) < 1e-6:
            self.leftover = 0.0
        return whole

    def reset(self) -> None:
        self.leftover = 0.0


def scroll_canvas(canvas: Any, steps: int) -> bool:
    """Apply ``steps`` to a canvas; ``False`` when it cannot scroll."""
    scroll = getattr(canvas, "yview_scroll", None)
    if not callable(scroll) or steps == 0:
        return False
    try:
        scroll(steps, "units")
    except Exception as exc:  # noqa: BLE001 - destroyed canvas
        LOGGER.debug("canvas scroll failed: %s", exc)
        return False
    return True


@dataclass(slots=True, frozen=True)
class WheelBinding:
    """One ``widget``/``sequence`` binding this module created."""

    widget: Any
    sequence: str
    funcid: str


def _marked(widget: Any) -> set[str]:
    marks = getattr(widget, _BOUND_MARK, None)
    return marks if isinstance(marks, set) else set()


def _mark(widget: Any, sequence: str) -> None:
    marks = getattr(widget, _BOUND_MARK, None)
    if not isinstance(marks, set):
        marks = set()
        try:
            setattr(widget, _BOUND_MARK, marks)
        except Exception as exc:  # noqa: BLE001 - a few Tk objects refuse attributes
            LOGGER.debug("cannot mark %r: %s", widget, exc)
            return
    marks.add(sequence)


def _children(widget: Any) -> list[Any]:
    children = getattr(widget, "winfo_children", None)
    if not callable(children):
        return []
    try:
        return list(children())
    except Exception as exc:  # noqa: BLE001 - destroyed widget
        LOGGER.debug("winfo_children failed: %s", exc)
        return []


def _descendants(widget: Any) -> Iterator[Any]:
    """Every descendant of ``widget``, depth first."""
    for child in _children(widget):
        yield child
        yield from _descendants(child)


def _handler(
    canvas: Any,
    platform: str | None,
    accumulator: WheelAccumulator | None = None,
) -> Any:
    """Build the wheel handler for one canvas.

    ``accumulator`` keeps the fractional part of a trackpad gesture; without one
    every event is rounded on its own (the old behaviour, still what the plain
    ``wheel_steps`` callers get).
    """
    state = accumulator if accumulator is not None else WheelAccumulator()

    def handle(event: Any) -> str | None:
        steps = (
            state.units(event, platform=platform)
            if accumulator is not None
            else wheel_steps(event, platform=platform)
        )
        if steps == 0:
            return None
        return "break" if scroll_canvas(canvas, steps) else None

    return handle


def _forwards_bind(widget: Any) -> bool:
    """True for a CustomTkinter wrapper that forwards ``bind`` to its innards.

    Such a wrapper defines ``bind`` itself and applies it to the Tk widgets it
    is made of, returning nothing.  Binding the wrapper as well as those inner
    widgets would run the handler twice per notch, and unbinding the wrapper
    would take away whatever those widgets already had — so the wrapper is left
    alone and the walk binds the widgets underneath it.

    ``CTkScrollableFrame`` is the exception that matters: it does not define
    ``bind``, it *is* a ``tkinter.Frame``, and it is bound like any other.
    """
    cls = type(widget)
    module = getattr(cls, "__module__", "") or ""
    return module.startswith("customtkinter") and "bind" in vars(cls)


def _bind_one(
    widget: Any,
    handler: Any,
    canvas: Any,
    platform: str | None,
    bound: list[WheelBinding],
) -> None:
    if not (is_text_area(widget) or is_scrollbar(widget) or _forwards_bind(widget)):
        binder = getattr(widget, "bind", None)
        if callable(binder):
            for sequence in WHEEL_SEQUENCES:
                if sequence in _marked(widget):
                    continue
                try:
                    funcid = binder(sequence, handler, "+")
                except Exception as exc:  # noqa: BLE001 - CTkBaseClass raises
                    LOGGER.debug("bind %s on %r failed: %s", sequence, widget, exc)
                    continue
                if not funcid:
                    # Not one of ours to keep: a wrapper that did not forward.
                    continue
                _mark(widget, sequence)
                bound.append(
                    WheelBinding(widget=widget, sequence=sequence, funcid=str(funcid))
                )
    # Bound here so the recursion below never needs to know about the canvas.
    if canvas is not None and widget is not canvas:
        _bind_one(canvas, handler, None, platform, bound)


def _bind_to_children(
    widget: Any,
    canvas: Any,
    *,
    platform: str | None = None,
    handler: Any | None = None,
    bound: list[WheelBinding] | None = None,
) -> list[WheelBinding]:
    """Bind ``<MouseWheel>`` on ``widget``, on ``canvas`` and on every descendant.

    Children that appear later are covered by :meth:`MouseWheelRouter.refresh`,
    which walks the same tree again.  Text areas and scrollbars are skipped, and
    the walk still descends into them: a ``CTkTextbox`` is a frame that owns an
    inner ``Text``, and neither of the two should be bound.
    """
    collected = [] if bound is None else bound
    wheel = handler if handler is not None else _handler(canvas, platform)
    _bind_one(widget, wheel, canvas, platform, collected)
    for child in _children(widget):
        _bind_to_children(
            child, canvas, platform=platform, handler=wheel, bound=collected
        )
    return collected


@dataclass(slots=True)
class TabWheel:
    """One scrollable area, the canvas that scrolls, and everything bound for it.

    Created by :func:`install_tab_wheel`.  ``refresh`` picks up children that
    were built after installation (every card in a tab), ``uninstall`` removes
    exactly the bindings this object created and nothing else.
    """

    frame: Any
    canvas: Any
    root: Any = None
    platform: str | None = None
    bindings: list[WheelBinding] = field(default_factory=list)
    accumulator: WheelAccumulator = field(default_factory=WheelAccumulator)

    @property
    def bound_count(self) -> int:
        """How many widget/sequence bindings this tab currently owns."""
        return len(self.bindings)

    def refresh(self) -> int:
        """Bind children that appeared since the last walk; returns the total."""
        handler = _handler(self.canvas, self.platform, self.accumulator)
        self.bindings.extend(
            _bind_to_children(
                self.frame, self.canvas, platform=self.platform, handler=handler
            )
        )
        return self.bound_count

    def uninstall(self) -> None:
        """Remove every binding this tab created (safe to call twice)."""
        for binding in self.bindings:
            try:
                binding.widget.unbind(binding.sequence, binding.funcid)
            except Exception as exc:  # noqa: BLE001 - the widget may be gone
                LOGGER.debug("unbind %s failed: %s", binding.sequence, exc)
            _marked(binding.widget).discard(binding.sequence)
        self.bindings.clear()
        self.accumulator.reset()


def _clear_builtin_handler(root: Any) -> None:
    """Drop CustomTkinter's global wheel handler on ``root``.

    Each ``CTkScrollableFrame`` installs one through ``bind_all``.  It resolves
    the target from ``event.widget``, which on macOS is the window, so it scrolls
    nothing there — and where it does work it would scroll a second time on top
    of the per-widget bindings below.
    """
    unbind_all = getattr(root, "unbind_all", None)
    if not callable(unbind_all):
        return
    try:
        unbind_all("<MouseWheel>")
    except Exception as exc:  # noqa: BLE001 - nothing bound, or Tk refused
        LOGGER.debug("unbind_all(<MouseWheel>) failed: %s", exc)


def install_tab_wheel(
    root: Any,
    frame: Any,
    canvas: Any,
    *,
    platform: str | None = None,
) -> TabWheel:
    """Give one scrollable area of one tab the wheel, and return its handle.

    Three things make this work where a single global binding does not:

    * the handler is bound on the frame, the canvas **and every descendant**, so
      the event is caught where Tk actually delivers it (a label inside a card
      inside a tab), not on the window;
    * the fraction of a trackpad gesture is carried between events, so a stream
      of ±0.1 deltas scrolls instead of rounding to nothing;
    * Tk 9's ``<TouchpadScroll>`` is bound as well, and Tk 8.6 simply refuses the
      sequence per widget — the same code runs on both.

    Text areas and scrollbars are left alone: they scroll their own content and
    would move twice.  Nothing here uses ``bind_all``.
    """
    _clear_builtin_handler(root)
    wheel = TabWheel(frame=frame, canvas=canvas, root=root, platform=platform)
    wheel.refresh()
    return wheel
