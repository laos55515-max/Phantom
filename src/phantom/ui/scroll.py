"""Mouse-wheel routing by direct binding: every widget of a scrollable area.

``bind_all`` looked like the obvious fix and is not one: Aqua Tk delivers
``<MouseWheel>`` to the window, and CustomTkinter's own global handler — which
resolves the target from ``event.widget`` — gives up when that widget is the
root.  Binding the wheel directly on the frame, the canvas and every descendant
puts the handler where Tk actually delivers the event, so a card, a label or a
button inside a tab scrolls the tab it belongs to.

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

WHEEL_SEQUENCES: Final = ("<MouseWheel>", "<Button-4>", "<Button-5>")

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


def wheel_steps(event: Any, *, platform: str | None = None) -> int:
    """Convert a wheel event into ``yview_scroll`` units.

    Positive means "towards the end of the content".  macOS sends a small,
    already inverted delta (±1 per notch), Windows sends multiples of 120 and
    X11 uses Button-4/5.
    """
    number = getattr(event, "num", None)
    if number in (4, 5):
        return -1 if number == 4 else 1
    try:
        delta = int(getattr(event, "delta", 0) or 0)
    except (TypeError, ValueError):
        return 0
    if delta == 0:
        return 0
    if (platform or sys.platform).lower().startswith("darwin"):
        # delta is ±1 per notch and positive means "scroll up", while
        # ``yview_scroll`` counts positive as "towards the end": flip the sign
        # and multiply.  Dividing by 120 — the Windows convention — rounds every
        # notch to zero units, which is the "only the scrollbar works" report.
        return -delta * MACOS_UNITS_PER_STEP
    steps = delta // WINDOWS_DELTA_UNIT
    if steps == 0:
        steps = 1 if delta > 0 else -1
    return -steps


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


def _handler(canvas: Any, platform: str | None) -> Any:
    """Build the ``<MouseWheel>`` handler for one canvas."""

    def handle(event: Any) -> str | None:
        steps = wheel_steps(event, platform=platform)
        if steps == 0:
            return None
        return "break" if scroll_canvas(canvas, steps) else None

    return handle


def _bind_one(
    widget: Any,
    handler: Any,
    canvas: Any,
    platform: str | None,
    bound: list[WheelBinding],
) -> None:
    if is_text_area(widget) or is_scrollbar(widget):
        return
    binder = getattr(widget, "bind", None)
    if not callable(binder):
        return
    for sequence in WHEEL_SEQUENCES:
        if sequence in _marked(widget):
            continue
        try:
            funcid = binder(sequence, handler, "+")
        except Exception as exc:  # noqa: BLE001 - composite widgets refuse bind()
            LOGGER.debug("bind %s on %r failed: %s", sequence, widget, exc)
            continue
        if not funcid:
            continue
        _mark(widget, sequence)
        bound.append(WheelBinding(widget=widget, sequence=sequence, funcid=str(funcid)))
    # Bound here so the walk below never needs to know about the canvas.
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
class ScrollTarget:
    """A scrollable frame together with the canvas that actually scrolls."""

    frame: Any
    canvas: Any


@dataclass(slots=True)
class MouseWheelRouter:
    """Keeps one binding set per scrollable area of the window."""

    root: Any
    platform: str | None = None
    targets: list[ScrollTarget] = field(default_factory=list)
    bindings: list[WheelBinding] = field(default_factory=list)

    # -- registration --------------------------------------------------
    def register(self, frame: Any, canvas: Any) -> ScrollTarget:
        """Add a scrollable area; returns the stored target (idempotent)."""
        for existing in self.targets:
            if existing.frame is frame:
                existing.canvas = canvas
                return existing
        target = ScrollTarget(frame=frame, canvas=canvas)
        self.targets.append(target)
        return target

    def unregister(self, frame: Any) -> None:
        self.targets = [target for target in self.targets if target.frame is not frame]

    @property
    def bound_count(self) -> int:
        return len(self.bindings)

    # -- binding -------------------------------------------------------
    def install(self) -> int:
        """Bind every registered area, including the cards built after registration."""
        self._clear_builtin_handler()
        return self.refresh()

    def refresh(self) -> int:
        """Walk the registered areas again and bind children that appeared later.

        Already bound widgets are skipped by their mark, so calling this on every
        rebuild is cheap and never stacks a second handler.
        """
        created: list[WheelBinding] = []
        for target in self.targets:
            created.extend(
                _bind_to_children(target.frame, target.canvas, platform=self.platform)
            )
        self.bindings.extend(created)
        return self.bound_count

    def uninstall(self) -> None:
        """Remove every binding this router created (safe to call twice)."""
        for binding in self.bindings:
            try:
                binding.widget.unbind(binding.sequence, binding.funcid)
            except Exception as exc:  # noqa: BLE001 - the widget may be gone
                LOGGER.debug("unbind %s failed: %s", binding.sequence, exc)
            _marked(binding.widget).discard(binding.sequence)
        self.bindings.clear()

    def _clear_builtin_handler(self) -> None:
        """Drop CustomTkinter's global wheel handler.

        Each ``CTkScrollableFrame`` installs one through ``bind_all``.  It
        resolves the target from ``event.widget``, which on macOS is the window,
        so it scrolls nothing there — and where it does work it would scroll a
        second time on top of our own bindings.
        """
        unbind_all = getattr(self.root, "unbind_all", None)
        if not callable(unbind_all):
            return
        try:
            unbind_all("<MouseWheel>")
        except Exception as exc:  # noqa: BLE001 - nothing bound, or Tk refused
            LOGGER.debug("unbind_all(<MouseWheel>) failed: %s", exc)


def install_mousewheel(
    root: Any, frame: Any, canvas: Any, *, platform: str | None = None
) -> MouseWheelRouter:
    """Convenience wrapper: bind one scrollable area, ready to use."""
    router = MouseWheelRouter(root=root, platform=platform)
    router.register(frame, canvas)
    router.install()
    return router
