"""Mouse-wheel and trackpad scrolling for the whole window.

CustomTkinter binds ``<MouseWheel>`` globally through ``bind_all`` and then
decides whether the event belongs to a scrollable frame by walking ``master``
from ``event.widget``.  On macOS that widget is not the one under the pointer
(Tk delivers the event to the window), the walk hits the root and gives up, and
the page does not move: only the scrollbar works.  Binding the wheel on every
widget instead does not help either — a plain Tk widget only receives the event
when Tk addresses it, and composite CTk widgets refuse ``bind()`` outright.

So this module owns the wheel for the whole window: one ``bind_all`` on the
root, a hit test for the widget under the pointer, a walk up the ``master``
chain to the scrollable frame that contains it, and a platform-correct step
count.  Text areas are left alone so their own content scrolls.

No ``tkinter``/``customtkinter`` import at module level (D14): every function
works on duck-typed objects, so the router is unit-testable without a display.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Final

LOGGER: Final = logging.getLogger("phantom.scroll")

WHEEL_SEQUENCES: Final = ("<MouseWheel>", "<Button-4>", "<Button-5>")

#: macOS reports ±1 per notch, so one notch has to move a few lines to be felt.
MACOS_UNITS_PER_STEP: Final = 3

#: The Windows convention: one wheel notch is 120 delta units.
WINDOWS_DELTA_UNIT: Final = 120


def is_text_like(widget: Any) -> bool:
    """True for a Tk Text widget (selection tags and its own vertical view)."""
    return callable(getattr(widget, "tag_add", None)) and callable(
        getattr(widget, "yview", None)
    )


def is_text_area(widget: Any) -> bool:
    """True for a text widget or for anything that wraps one (``CTkTextbox``)."""
    node = widget
    for _ in range(32):
        if node is None:
            return False
        if is_text_like(node) or getattr(node, "_textbox", None) is not None:
            return True
        if type(node).__name__ in {"CTkTextbox", "Text", "ScrolledText"}:
            return True
        node = getattr(node, "master", None)
    return False


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


@dataclass(slots=True)
class ScrollTarget:
    """A scrollable frame together with the canvas that actually scrolls."""

    frame: Any
    canvas: Any


@dataclass(slots=True)
class MouseWheelRouter:
    """Routes wheel events for the whole window to the area under the pointer."""

    root: Any
    platform: str | None = None
    targets: list[ScrollTarget] = field(default_factory=list)
    installed: bool = False

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
        return len(WHEEL_SEQUENCES) if self.installed else 0

    # -- binding -------------------------------------------------------
    def install(self) -> int:
        """Take over the wheel for the whole window; returns bindings created."""
        if self.installed:
            return self.bound_count
        bind_all = getattr(self.root, "bind_all", None)
        if not callable(bind_all):
            LOGGER.debug("root cannot bind globally")
            return 0
        self._clear_builtin_handler()
        bound = 0
        for sequence in WHEEL_SEQUENCES:
            try:
                bind_all(sequence, self.dispatch, "+")
            except Exception as exc:  # noqa: BLE001 - Tk dependent
                LOGGER.debug("bind_all %s failed: %s", sequence, exc)
                continue
            bound += 1
        self.installed = bound > 0
        return bound

    def refresh(self) -> int:
        """Re-assert the global binding; areas are found by hit test, not bound."""
        if not self.installed:
            self.install()
        return self.bound_count

    def uninstall(self) -> None:
        """Remove every global binding this router created (safe to call twice)."""
        unbind_all = getattr(self.root, "unbind_all", None)
        if callable(unbind_all):
            for sequence in WHEEL_SEQUENCES:
                try:
                    unbind_all(sequence)
                except Exception as exc:  # noqa: BLE001 - the window may be gone
                    LOGGER.debug("unbind_all %s failed: %s", sequence, exc)
        self.installed = False

    def _clear_builtin_handler(self) -> None:
        """Drop CustomTkinter's own global wheel handler.

        Each ``CTkScrollableFrame`` installs one with ``bind_all``.  It resolves
        the target from ``event.widget``, which is the window on macOS, so it
        scrolls nothing there; and on the platforms where it does work it would
        scroll a second time on top of us.
        """
        unbind_all = getattr(self.root, "unbind_all", None)
        if not callable(unbind_all):
            return
        try:
            unbind_all("<MouseWheel>")
        except Exception as exc:  # noqa: BLE001 - nothing bound, or Tk refused
            LOGGER.debug("unbind_all(<MouseWheel>) failed: %s", exc)

    # -- event handling ------------------------------------------------
    def widget_at(self, event: Any) -> Any:
        """The widget under the pointer, with ``event.widget`` as the fallback."""
        containing = getattr(self.root, "winfo_containing", None)
        if callable(containing):
            try:
                node = containing(
                    int(getattr(event, "x_root", 0)), int(getattr(event, "y_root", 0))
                )
            except Exception as exc:  # noqa: BLE001 - pointer outside the window
                LOGGER.debug("winfo_containing failed: %s", exc)
                node = None
            if node is not None:
                return node
        return getattr(event, "widget", None)

    def dispatch(self, event: Any) -> str | None:
        """Handle one wheel event; returns ``"break"`` when it was consumed."""
        widget = self.widget_at(event)
        if widget is None:
            return None
        if is_text_area(widget):
            # The text area scrolls its own content: stealing the event here
            # would move the page instead of the paragraph under the pointer.
            return None
        target = self._target_for(widget)
        if target is None:
            return None
        steps = wheel_steps(event, platform=self.platform)
        if steps == 0:
            return None
        if not self._scroll(target, steps):
            return None
        return "break"

    def _target_for(self, widget: Any) -> ScrollTarget | None:
        """Climb ``master`` until a registered scrollable frame is reached."""
        node = widget
        for _ in range(64):
            if node is None:
                return None
            for target in self.targets:
                if node is target.frame or node is target.canvas:
                    return target
            node = getattr(node, "master", None)
        return None

    @staticmethod
    def _scroll(target: ScrollTarget, steps: int) -> bool:
        scroll = getattr(target.canvas, "yview_scroll", None)
        if not callable(scroll):
            return False
        try:
            scroll(steps, "units")
        except Exception as exc:  # noqa: BLE001 - destroyed canvas
            LOGGER.debug("canvas scroll failed: %s", exc)
            return False
        return True


def install_mousewheel(
    root: Any, frame: Any, canvas: Any, *, platform: str | None = None
) -> MouseWheelRouter:
    """Convenience wrapper: route one scrollable area for the whole window."""
    router = MouseWheelRouter(root=root, platform=platform)
    router.register(frame, canvas)
    router.install()
    return router
