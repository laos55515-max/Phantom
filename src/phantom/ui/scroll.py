"""Wheel and touchpad routing for the whole application, by pointer position.

Why this is not "bind the wheel on every widget"
------------------------------------------------
Tk 9 changed how a trackpad reports scrolling, and CustomTkinter has not caught
up (TomSchimansky/CustomTkinter#2858).  On macOS a two-finger gesture on a
precise device no longer produces ``<MouseWheel>``: the Aqua backend queues a
``<TouchpadScroll>`` event instead (``macosx/tkMacOSXMouseEvent.c``), and on
Windows a high-resolution wheel or touchpad does the same (``win/tkWinX.c``).
``CTkScrollableFrame`` binds ``<MouseWheel>``/``<Button-4>``/``<Button-5>`` with
``bind_all`` and nothing for ``<TouchpadScroll>``, so the gesture arrives, finds
no handler, and the canvas does not move — while dragging the scrollbar still
works, because that path never involves the wheel.

Three things follow, and they are the shape of this module:

* the two event kinds are handled separately and are *not* interchangeable.
  ``<TouchpadScroll>`` carries both deltas packed into the ``%D`` substitution:
  the X delta in the high 16 bits, the Y delta in the low 16, in pixels.  Tk
  ships ``::tk::PreciseScrollDeltas`` for exactly this and we call it, so the
  decoding cannot drift from the Tcl side; a Python fallback covers a Tk whose
  library forgot the proc.  ``<MouseWheel>`` stays an integer notch count, but
  how many deltas make a notch depends on the platform *and* the Tk version
  (Aqua Tk 8.6 reports the raw NSEvent delta, ±1 per notch; Aqua Tk 9.0 and
  Windows report ±120), so the conversion is derived from the running Tk.
* routing is done once, at the application level, from the pointer position.
  Per-widget bindings cannot see a gesture that Tk delivers to a window we did
  not bind, and re-walking the tree after every rebuild (language switch, new
  card) was both fragile and pointless — the widget under the pointer is
  knowable at event time with ``winfo containing``.  This is the one place a
  global binding is justified, and it replaces CustomTkinter's, which is
  removed so nothing scrolls twice.
* what the pointer is over decides *whether* to scroll at all: a text area
  keeps its own scrolling (its class binding has already run by the time the
  global one does), a scrollbar is left alone, an unmapped tab is not a target.

Diagnostics
-----------
Set ``PHANTOM_SCROLL_DEBUG=1`` to log every scroll event to stderr, or to a
path to log into that file: event type and serial, Tk patchlevel, windowing
system, raw ``%D``, the decoded deltas, the widget under the pointer, the target
canvas, and the ``yview`` before and after.  Without the variable nothing is
logged, and the module holds no state beyond the registry.

No ``tkinter`` import at module level (D14): everything works on duck-typed
objects, so the decode, the geometry and the routing are unit-testable without a
display.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

LOGGER: Final = logging.getLogger("phantom.scroll")

#: Event sequences this router takes over.  ``<TouchpadScroll>`` only exists in
#: Tk 9+; on 8.6 binding it raises ``TclError`` and it is skipped.
WHEEL_SEQUENCE: Final = "<MouseWheel>"
X11_SEQUENCES: Final = ("<Button-4>", "<Button-5>")
TOUCHPAD_SEQUENCE: Final = "<TouchpadScroll>"

WHEEL_SEQUENCES: Final = (WHEEL_SEQUENCE, *X11_SEQUENCES, TOUCHPAD_SEQUENCE)

#: Environment variable that turns the event trace on: ``1`` for stderr, or a
#: file path to append the trace to.
DIAGNOSTICS_ENV: Final = "PHANTOM_SCROLL_DEBUG"

#: A unit is one scroll increment of the widget; these are the pixel sizes the
#: CustomTkinter scrollable canvas uses per increment (``_set_scroll_increments``),
#: which is why "3 units per notch" means "about three lines" only on macOS.
UNITS_PER_NOTCH_MACOS: Final = 3.0
UNITS_PER_NOTCH_WINDOWS: Final = 20.0
UNITS_PER_NOTCH_X11: Final = 1.0

#: Raw ``%D`` delta per wheel notch, per backend.  Aqua Tk 8.6 passes the raw
#: NSEvent delta through (one per notch); everything newer reports ±120.
DELTA_PER_NOTCH_CLASSIC: Final = 120.0
DELTA_PER_NOTCH_AQUA_86: Final = 1.0

_TEXT_AREA_NAMES: Final = frozenset({"CTkTextbox", "Text", "ScrolledText"})
_SCROLLBAR_NAMES: Final = frozenset({"CTkScrollbar", "Scrollbar", "TScrollbar"})

#: Widgets Tk scrolls (or, for a slider, adjusts) on its own ``<MouseWheel>``
#: class binding.  None of them is used by the interface today — this is the list
#: of things that would scroll twice if a future card introduced one.
_SELF_SCROLLING_NAMES: Final = frozenset(
    {"CTkSlider", "Scale", "TScale", "Listbox", "Treeview", "TCombobox"}
)

_LEADING_DIGITS: Final = re.compile(r"\d+")
_PRERELEASE_PATCH: Final = re.compile(r"[A-Za-z](\d+)$")

#: What a single touchpad event may move at most, in pixels.  A momentum fling
#: can report hundreds; letting it through in one ``moveto`` looks like a jump.
MAX_PIXELS_PER_EVENT: Final = 400.0


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


def keeps_its_own_wheel(widget: Any) -> bool:
    """True when the widget scrolls itself, so the router must leave it alone."""
    return (
        is_text_area(widget)
        or is_scrollbar(widget)
        or type(widget).__name__ in _SELF_SCROLLING_NAMES
    )


def is_scrollbar(widget: Any) -> bool:
    """True for a scrollbar, which owns its drag handling and must not scroll."""
    if type(widget).__name__ in _SCROLLBAR_NAMES:
        return True
    return callable(getattr(widget, "set", None)) and callable(
        getattr(widget, "get", None)
    ) and "Scrollbar" in type(widget).__name__


# ---------------------------------------------------------------------------
# environment: which Tk, on which windowing system
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Environment:
    """The three facts the conversion depends on, read from the running Tk."""

    tk_version: tuple[int, int, int]
    windowing_system: str

    @classmethod
    def detect(cls, root: Any) -> Environment:
        """Ask Tk for its patchlevel and windowing system.

        ``info patchlevel`` is the only string that carries all three numbers
        (``TkVersion`` is a float on 8.6 and a float on 9.0 too, so ``9.0.3``
        and ``9.0.1`` are indistinguishable through it).
        """
        patchlevel = "0.0.0"
        system = "unknown"
        try:
            patchlevel = str(root.tk.call("info", "patchlevel"))
        except Exception as exc:  # noqa: BLE001 - a stub root in a test
            LOGGER.debug("cannot read the Tk patchlevel: %s", exc)
        try:
            system = str(root.tk.call("tk", "windowingsystem")).lower()
        except Exception as exc:  # noqa: BLE001 - a stub root in a test
            LOGGER.debug("cannot read the windowing system: %s", exc)
        return cls(tk_version=_parse_patchlevel(patchlevel), windowing_system=system)

    @property
    def major(self) -> int:
        return self.tk_version[0]

    @property
    def is_aqua(self) -> bool:
        return self.windowing_system == "aqua"

    @property
    def is_windows(self) -> bool:
        return self.windowing_system == "win32"

    @property
    def is_x11(self) -> bool:
        return self.windowing_system == "x11"

    @property
    def label(self) -> str:
        version = ".".join(str(part) for part in self.tk_version)
        return f"Tk {version} · {self.windowing_system}"

    def wheel_geometry(self) -> WheelGeometry:
        return WheelGeometry.for_environment(self)


def _parse_patchlevel(text: str) -> tuple[int, int, int]:
    """``"9.0.3"`` → ``(9, 0, 3)``; anything unparsable becomes ``(0, 0, 0)``.

    Prereleases are ``"8.7a1"``/``"9.0b2"``: the trailing number is the patch
    level, not more digits of the minor version — gluing them together would
    report 8.7a1 as Tk 8.71.
    """
    parts: list[int] = []
    prerelease_patch: int | None = None
    for chunk in text.strip().split("."):
        leading = _LEADING_DIGITS.match(chunk)
        if leading is None:
            break
        parts.append(int(leading.group()))
        tail = _PRERELEASE_PATCH.search(chunk)
        if tail is not None and prerelease_patch is None:
            prerelease_patch = int(tail.group(1))
    if len(parts) == 2 and prerelease_patch is not None:
        parts.append(prerelease_patch)
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


@dataclass(frozen=True, slots=True)
class WheelGeometry:
    """How a raw ``<MouseWheel>`` delta becomes ``yview_scroll`` units."""

    units_per_notch: float
    delta_per_notch: float

    @classmethod
    def for_environment(cls, environment: Environment) -> WheelGeometry:
        if environment.is_aqua:
            # Aqua 8.6 passes the Cocoa delta through untouched (one per notch);
            # Aqua 9 reports ±120 like every other backend, which is why a fixed
            # multiplier of the raw delta — the old code — scrolls 360 units a
            # notch on Tk 9 and 3 on 8.6.
            delta = (
                DELTA_PER_NOTCH_AQUA_86
                if environment.major < 9
                else DELTA_PER_NOTCH_CLASSIC
            )
            return cls(UNITS_PER_NOTCH_MACOS, delta)
        if environment.is_windows:
            return cls(UNITS_PER_NOTCH_WINDOWS, DELTA_PER_NOTCH_CLASSIC)
        return cls(UNITS_PER_NOTCH_X11, DELTA_PER_NOTCH_CLASSIC)

    def notches(self, delta: float) -> float:
        """Signed ``<MouseWheel>`` delta → signed notches (positive = up)."""
        if self.delta_per_notch <= 0:
            return 0.0
        return delta / self.delta_per_notch

    def units(self, delta: float) -> float:
        """Signed ``<MouseWheel>`` delta → signed ``yview_scroll`` units."""
        # "Scrolling up" is a delta the platform reports as positive, and
        # ``yview_scroll`` counts positive as "towards the end", so the sign flips.
        return -self.notches(delta) * self.units_per_notch


# ---------------------------------------------------------------------------
# the packed touchpad delta
# ---------------------------------------------------------------------------
def decode_touchpad_delta(raw: int) -> tuple[int, int]:
    """Unpack ``%D`` of a ``<TouchpadScroll>`` into ``(dx, dy)`` pixels.

    Same arithmetic as Tcl's ``::tk::PreciseScrollDeltas``: X in the high 16
    bits, Y in the low 16, both signed.  Kept as a fallback for a Tk whose
    library lacks the proc, and pinned by a test that compares the two.
    """
    value = int(raw) & 0xFFFFFFFF
    high = (value >> 16) & 0xFFFF
    low = value & 0xFFFF
    delta_x = high - 0x10000 if high >= 0x8000 else high
    delta_y = low - 0x10000 if low >= 0x8000 else low
    return delta_x, delta_y


def precise_scroll_deltas(root: Any, raw: int) -> tuple[int, int]:
    """``(dx, dy)`` from Tk itself, falling back to :func:`decode_touchpad_delta`."""
    try:
        result = root.tk.call("::tk::PreciseScrollDeltas", int(raw))
    except Exception as exc:  # noqa: BLE001 - 8.6 has no such proc
        LOGGER.debug("::tk::PreciseScrollDeltas unavailable (%s)", exc)
        return decode_touchpad_delta(raw)
    try:
        delta_x, delta_y = (int(part) for part in result)
    except (TypeError, ValueError) as exc:
        LOGGER.debug("undecodable touchpad delta %r: %s", result, exc)
        return decode_touchpad_delta(raw)
    return delta_x, delta_y


# ---------------------------------------------------------------------------
# scrolling primitives
# ---------------------------------------------------------------------------
def scroll_canvas(canvas: Any, steps: int) -> bool:
    """Apply ``steps`` units to a canvas; ``False`` when it cannot scroll."""
    scroll = getattr(canvas, "yview_scroll", None)
    if not callable(scroll) or steps == 0:
        return False
    try:
        scroll(steps, "units")
    except Exception as exc:  # noqa: BLE001 - destroyed canvas
        LOGGER.debug("canvas scroll failed: %s", exc)
        return False
    return True


def _content_height(canvas: Any) -> float:
    """Height in pixels that ``yview moveto`` addresses, i.e. the scroll region.

    ``yview`` fractions are fractions of the scroll region, not of the visible
    window, so dividing a pixel delta by ``winfo height`` would over-scroll by the
    ratio between the two — a factor of five on a long tab.  A canvas that never
    set a region reports an empty string; its window height is then the best
    guess available.
    """
    try:
        region = canvas.cget("scrollregion")
    except Exception:  # noqa: BLE001 - not a canvas, or a destroyed one
        region = ""
    numbers: list[float] = []
    for chunk in str(region).split():
        try:
            numbers.append(float(chunk))
        except ValueError:
            numbers = []
            break
    if len(numbers) == 4 and numbers[3] - numbers[1] > 1.0:
        return numbers[3] - numbers[1]
    return float(canvas.winfo_height())


def scroll_canvas_by_pixels(canvas: Any, pixels: float) -> bool:
    """Move a canvas by ``pixels``; positive moves the view towards the end.

    Uses ``yview moveto`` on the fraction of the scroll region, which is what
    ``::tk::ScrollByPixels`` does: it means the same thing on a canvas whose
    increment is 1 px (Windows), 8 px (macOS) or 30 px (X11), and it honours a
    fractional remainder instead of rounding every event to nothing.
    """
    if pixels == 0:
        return False
    try:
        height = _content_height(canvas)
        start, _end = canvas.yview()
    except Exception as exc:  # noqa: BLE001 - destroyed canvas, or not a canvas
        LOGGER.debug("canvas geometry unavailable: %s", exc)
        return False
    if height <= 0:
        return False
    target = float(start) + pixels / height
    target = max(target, 0.0)
    try:
        canvas.yview_moveto(min(1.0, target))
    except Exception as exc:  # noqa: BLE001 - destroyed canvas
        LOGGER.debug("canvas moveto failed: %s", exc)
        return False
    return True


def canvas_fraction(canvas: Any) -> float | None:
    """First element of ``yview``, or ``None`` when it cannot be read."""
    try:
        return float(canvas.yview()[0])
    except Exception:  # noqa: BLE001 - destroyed canvas
        return None


@dataclass(slots=True)
class WheelAccumulator:
    """Carries a fractional wheel notch from one event to the next.

    A macOS trackpad on Tk 8.6 reports integer deltas that are often smaller
    than a notch, and Windows reports 120s but games can send less; rounding
    each event on its own loses them.  The state belongs to one scroll area, so
    two tabs cannot borrow each other's remainder.
    """

    leftover: float = 0.0

    def units(self, delta: float, geometry: WheelGeometry) -> int:
        total = self.leftover + geometry.units(delta)
        whole = int(total)
        self.leftover = total - whole
        if abs(self.leftover) < 1e-6:
            self.leftover = 0.0
        return whole

    def reset(self) -> None:
        self.leftover = 0.0


# ---------------------------------------------------------------------------
# areas and routing
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ScrollArea:
    """One scrollable frame together with the canvas that actually scrolls."""

    frame: Any
    canvas: Any
    name: str = ""
    accumulator: WheelAccumulator = field(default_factory=WheelAccumulator)

    def describe(self) -> str:
        return self.name or _widget_path(self.frame)


def _widget_path(widget: Any) -> str:
    try:
        return str(widget)
    except Exception:  # noqa: BLE001 - destroyed widget
        return "<gone>"


@dataclass(slots=True)
class ScrollDiagnostics:
    """Optional trace of every scroll event the router sees."""

    enabled: bool = False
    sink: Callable[[str], None] | None = None

    @classmethod
    def from_environment(cls) -> ScrollDiagnostics:
        setting = os.environ.get(DIAGNOSTICS_ENV, "").strip()
        if not setting or setting == "0":
            return cls()
        if setting in {"1", "true", "yes", "on"}:
            return cls(enabled=True)
        sink = _file_sink(setting)
        return cls(enabled=sink is not None, sink=sink)

    def emit(self, message: str) -> None:
        if not self.enabled:
            return
        if self.sink is not None:
            self.sink(message)
            return
        LOGGER.warning("scroll debug: %s", message)


def _file_sink(path: str) -> Callable[[str], None] | None:
    try:
        handle = open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError as exc:
        LOGGER.warning("cannot open the scroll trace file %s: %s", path, exc)
        return None

    def write(line: str) -> None:
        try:
            handle.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        except OSError as exc:
            LOGGER.debug("scroll trace write failed: %s", exc)

    return write


class ScrollRouter:
    """Application-wide wheel routing, resolved by the pointer at event time."""

    def __init__(
        self,
        root: Any,
        *,
        environment: Environment | None = None,
        diagnostics: ScrollDiagnostics | None = None,
    ) -> None:
        self.root = root
        self.environment = environment or Environment.detect(root)
        self.geometry = self.environment.wheel_geometry()
        self.areas: list[ScrollArea] = []
        self.diagnostics = diagnostics or ScrollDiagnostics.from_environment()
        self.installed = False
        self._bound: list[str] = []

    # -- registry ------------------------------------------------------
    def register(self, frame: Any, canvas: Any, *, name: str = "") -> ScrollArea:
        """Add a scrollable area (idempotent: re-registering updates the canvas)."""
        for existing in self.areas:
            if existing.frame is frame:
                existing.canvas = canvas
                existing.name = name or existing.name
                return existing
        area = ScrollArea(frame=frame, canvas=canvas, name=name)
        self.areas.append(area)
        return area

    def unregister(self, frame: Any) -> None:
        self.areas = [area for area in self.areas if area.frame is not frame]

    @property
    def area_count(self) -> int:
        return len(self.areas)

    # -- install -------------------------------------------------------
    def install(self) -> int:
        """Take over the wheel globally; returns how many sequences bound.

        CustomTkinter's own handler is removed first: it is bound with
        ``bind_all`` on ``<MouseWheel>``/``<Button-4>``/``<Button-5>``, resolves
        the target from ``event.widget`` (which on Aqua is the window, not the
        canvas) and would otherwise scroll a second time on top of ours.
        """
        if self.installed:
            return len(self._bound)
        self._clear_builtin_handlers()
        add = getattr(self.root, "bind_all", None)
        if not callable(add):
            LOGGER.warning("no bind_all on %r: the wheel cannot be routed", self.root)
            return 0
        bindings = {
            WHEEL_SEQUENCE: self._on_mouse_wheel,
            "<Button-4>": self._on_x11_wheel,
            "<Button-5>": self._on_x11_wheel,
            TOUCHPAD_SEQUENCE: self._on_touchpad,
        }
        for sequence, handler in bindings.items():
            try:
                add(sequence, handler, "+")
            except Exception as exc:  # noqa: BLE001 - 8.6 has no TouchpadScroll
                LOGGER.debug("bind_all(%s) refused: %s", sequence, exc)
                continue
            self._bound.append(sequence)
        self.installed = True
        LOGGER.info("scroll router installed: %s", self.describe())
        return len(self._bound)

    def uninstall(self) -> None:
        """Remove every binding this router created (safe to call twice)."""
        self.installed = False
        unbind = getattr(self.root, "unbind_all", None)
        if callable(unbind):
            for sequence in self._bound:
                try:
                    unbind(sequence)
                except Exception as exc:  # noqa: BLE001 - already gone, or Tk refused
                    LOGGER.debug("unbind_all(%s) failed: %s", sequence, exc)
        self._bound.clear()

    def describe(self) -> str:
        return (
            f"{self.environment.label} · areas={self.area_count} "
            f"· wheel={self.geometry.units_per_notch:g} units per "
            f"{self.geometry.delta_per_notch:g} delta "
            f"· touchpad={'bound' if TOUCHPAD_SEQUENCE in self._bound else 'no'}"
            f" · diagnostics={'on' if self.diagnostics.enabled else 'off'}"
        )

    def _clear_builtin_handlers(self) -> None:
        unbind = getattr(self.root, "unbind_all", None)
        if not callable(unbind):
            return
        for sequence in (WHEEL_SEQUENCE, *X11_SEQUENCES):
            try:
                unbind(sequence)
            except Exception as exc:  # noqa: BLE001 - nothing bound, or Tk refused
                LOGGER.debug("unbind_all(%s) failed: %s", sequence, exc)

    # -- resolution ----------------------------------------------------
    def widget_at(self, x_root: int, y_root: int) -> Any:
        """The widget under a screen position, or ``None`` when unknowable."""
        containing = getattr(self.root, "winfo_containing", None)
        if not callable(containing):
            return None
        try:
            widget = containing(x_root, y_root)
        except Exception as exc:  # noqa: BLE001 - Tk refused the coordinates
            LOGGER.debug("winfo_containing failed: %s", exc)
            return None
        return widget or None

    def resolve(self, x_root: int, y_root: int) -> ScrollArea | None:
        """The scroll area under a screen position, or ``None``.

        This is the whole point of routing at the application level: the widget
        that received the event tells us nothing on Aqua (the event goes to the
        window under the pointer), so the pointer position is the only reliable
        input.  A text area, a scrollbar and an unmapped tab all resolve to
        ``None`` — the first two scroll themselves, the last is not on screen.
        """
        widget = self.widget_at(x_root, y_root)
        if widget is None:
            return None
        return self.area_for_widget(widget)

    def area_for_widget(self, widget: Any) -> ScrollArea | None:
        """Walk up from ``widget`` to the nearest registered, visible area."""
        current = widget
        for _step in range(64):  # a widget tree is never this deep
            if current is None:
                return None
            if keeps_its_own_wheel(current):
                return None
            for area in self.areas:
                if area.frame is current:
                    return area if _is_visible(area) else None
            current = _parent(current)
        return None

    # -- event handlers ------------------------------------------------
    def _on_mouse_wheel(self, event: Any) -> str | None:
        delta = getattr(event, "delta", 0)
        area = self._pointer_area(event)
        before = canvas_fraction(area.canvas) if area is not None else None
        moved = False
        if area is not None:
            try:
                units = area.accumulator.units(float(delta or 0), self.geometry)
            except (TypeError, ValueError):
                units = 0
            moved = scroll_canvas(area.canvas, units) if units else False
        self._trace(event, "MouseWheel", raw=delta, decoded=None, area=area, before=before)
        return "break" if moved else None

    def _on_x11_wheel(self, event: Any) -> str | None:
        number = getattr(event, "num", None)
        if number not in (4, 5):
            return None
        area = self._pointer_area(event)
        before = canvas_fraction(area.canvas) if area is not None else None
        units = -1 if number == 4 else 1
        moved = area is not None and scroll_canvas(area.canvas, units)
        self._trace(event, "Button", raw=number, decoded=None, area=area, before=before)
        return "break" if moved else None

    def _on_touchpad(self, event: Any) -> str | None:
        raw = getattr(event, "delta", 0)
        delta_x, delta_y = precise_scroll_deltas(self.root, int(raw or 0))
        area = self._pointer_area(event)
        before = canvas_fraction(area.canvas) if area is not None else None
        moved = False
        if area is not None and delta_y:
            pixels = _clamp(-float(delta_y), MAX_PIXELS_PER_EVENT)
            moved = scroll_canvas_by_pixels(area.canvas, pixels)
        self._trace(
            event,
            "TouchpadScroll",
            raw=raw,
            decoded=(delta_x, delta_y),
            area=area,
            before=before,
        )
        return "break" if moved else None

    def _pointer_area(self, event: Any) -> ScrollArea | None:
        """The area under the pointer; the event's own widget only as a fallback.

        The fallback exists for coordinates we cannot resolve at all (the pointer
        left the window between queueing and dispatch).  When the pointer *is*
        over a text area, that is a deliberate miss, not a failure, so falling
        back would scroll the page underneath the text that already scrolled
        itself.
        """
        x_root = getattr(event, "x_root", None)
        y_root = getattr(event, "y_root", None)
        if isinstance(x_root, int) and isinstance(y_root, int):
            widget = self.widget_at(x_root, y_root)
            if widget is not None:
                return self.area_for_widget(widget)
        widget = getattr(event, "widget", None)
        return self.area_for_widget(widget) if widget is not None else None

    # -- diagnostics ---------------------------------------------------
    def _trace(
        self,
        event: Any,
        kind: str,
        *,
        raw: object,
        decoded: tuple[int, int] | None,
        area: ScrollArea | None,
        before: float | None,
    ) -> None:
        if not self.diagnostics.enabled:
            return
        after = canvas_fraction(area.canvas) if area is not None else None
        fields = [
            f"event={kind}",
            f"serial={getattr(event, 'serial', '?')}",
            f"tk={'.'.join(str(part) for part in self.environment.tk_version)}",
            f"sys={self.environment.windowing_system}",
            f"raw=%D={raw!r}",
        ]
        if decoded is not None:
            fields.append(f"decoded=(dx={decoded[0]},dy={decoded[1]})")
        fields.extend(
            [
                f"widget={_widget_path(getattr(event, 'widget', None))}",
                f"at=({getattr(event, 'x_root', '?')},{getattr(event, 'y_root', '?')})",
                f"area={area.describe() if area is not None else 'none'}",
                f"yview={_fraction_text(before)}->{_fraction_text(after)}",
                f"moved={'yes' if before != after else 'no'}",
            ]
        )
        self.diagnostics.emit(" ".join(fields))


def _fraction_text(value: float | None) -> str:
    return "?" if value is None else f"{value:.4f}"


def _clamp(value: float, limit: float) -> float:
    if value > limit:
        return limit
    if value < -limit:
        return -limit
    return value


def _parent(widget: Any) -> Any:
    """The parent widget, through ``master`` (CTk) or ``nametowidget`` (Tk)."""
    parent = getattr(widget, "master", None)
    if parent is not None:
        return parent
    try:
        name = widget.winfo_parent()
        return widget.nametowidget(name) if name else None
    except Exception:  # noqa: BLE001 - destroyed widget
        return None


def _is_visible(area: ScrollArea) -> bool:
    """True when the area is on screen: a hidden tab must not scroll."""
    for widget in (area.frame, area.canvas):
        mapped = getattr(widget, "winfo_ismapped", None)
        if not callable(mapped):
            continue
        try:
            if not mapped():
                return False
        except Exception:  # noqa: BLE001 - destroyed widget
            return False
    return True


def install_scroll_router(
    root: Any,
    *,
    environment: Environment | None = None,
    diagnostics: ScrollDiagnostics | None = None,
) -> ScrollRouter:
    """Bind the wheel for the whole application; returns the router."""
    router = ScrollRouter(
        root, environment=environment, diagnostics=diagnostics
    )
    router.install()
    return router


def platform_name() -> str:
    """Python's platform name, for callers that have no Tk window handy."""
    return sys.platform
