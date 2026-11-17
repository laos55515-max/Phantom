"""Scroll routing without a display: environment, decoding, geometry, precedence.

The real event loop is exercised in ``test_gui.py`` (a synthetic
``<TouchpadScroll>`` and a wheel delivered to the root window); this module pins
the parts that can be decided without Tk, so a regression is localised instead of
showing up as "the trackpad does not scroll".

The scenarios here are the ones the macOS bug was made of:

* a packed ``<TouchpadScroll>`` delta read as a wheel notch (4 294 967 336 units);
* Aqua Tk 9 reporting ±120 per notch where Aqua Tk 8.6 reported ±1;
* routing to a tab that is not under the pointer, or to a hidden one;
* the page scrolling underneath a text box that scrolled itself.
"""

from __future__ import annotations

import pytest

from phantom.ui import scroll

# ---------------------------------------------------------------------------
# fakes: no tkinter, no display
# ---------------------------------------------------------------------------


class FakeTk:
    """``root.tk`` with the three calls the router makes."""

    def __init__(self, patchlevel: str = "9.0.3", system: str = "aqua") -> None:
        self.patchlevel = patchlevel
        self.system = system
        self.calls: list[tuple[str, ...]] = []

    def call(self, *args: object) -> object:
        self.calls.append(tuple(str(item) for item in args))
        if args[:2] == ("info", "patchlevel"):
            return self.patchlevel
        if args[:2] == ("tk", "windowingsystem"):
            return self.system
        if args[0] == "::tk::PreciseScrollDeltas":
            return _tcl_precise_deltas(int(args[1]))  # type: ignore[arg-type]
        raise AssertionError(f"unexpected Tk call: {args}")


def _tcl_precise_deltas(dxdy: int) -> tuple[int, int]:
    """A literal transcription of ``::tk::PreciseScrollDeltas`` (library/tk.tcl)."""
    delta_x = dxdy >> 16
    low = dxdy & 0xFFFF
    delta_y = low if low < 0x8000 else low - 0x10000
    return delta_x, delta_y


class FakeCanvas:
    def __init__(self, *, height: int = 500, mapped: bool = True, scrollregion: str = "") -> None:
        self.height = height
        self.mapped = mapped
        self.scrollregion = scrollregion
        self.top = 0.2
        self.units: list[tuple[int, str]] = []
        self.moves: list[float] = []

    def winfo_height(self) -> int:
        return self.height

    def cget(self, option: str) -> object:
        return self.scrollregion if option == "scrollregion" else ""

    def winfo_ismapped(self) -> bool:
        return self.mapped

    def yview(self, *args: object) -> tuple[float, float]:
        return (self.top, min(1.0, self.top + 0.4))

    def yview_scroll(self, steps: int, what: str) -> None:
        self.units.append((steps, what))
        self.top = min(1.0, max(0.0, self.top + steps * 8 / self.height))

    def yview_moveto(self, fraction: float) -> None:
        self.moves.append(fraction)
        self.top = fraction


class FakeWidget:
    def __init__(self, master: object | None = None, name: str = "widget") -> None:
        self.master = master
        self.name = name

    def __str__(self) -> str:
        return self.name

    def winfo_ismapped(self) -> bool:
        return True


class FakeText(FakeWidget):
    """A Tk Text: selection tags and its own view."""

    def tag_add(self, *args: object) -> None:
        return None

    def yview(self, *args: object) -> tuple[int, int]:
        return (0, 0, 1, 1)


class FakeScrollbar(FakeWidget):
    def set(self, *args: object) -> None:
        return None

    def get(self) -> tuple[float, float]:
        return (0.0, 1.0)


class FakeRoot(FakeWidget):
    def __init__(self, *, patchlevel: str = "9.0.3", system: str = "aqua") -> None:
        super().__init__(None, ".")
        self.tk = FakeTk(patchlevel, system)
        self.bound: dict[str, object] = {}
        self.unbound: list[str] = []
        self.pointer: object | None = None

    def bind_all(self, sequence: str, handler: object, add: object = None) -> None:
        self.bound[sequence] = handler

    def unbind_all(self, sequence: str) -> None:
        self.unbound.append(sequence)
        self.bound.pop(sequence, None)

    def winfo_containing(self, x: int, y: int) -> object | None:
        return self.pointer


class FakeEvent:
    def __init__(
        self,
        widget: object,
        *,
        delta: int = 0,
        num: int | None = None,
        x_root: int = 10,
        y_root: int = 20,
        serial: int = 1,
    ) -> None:
        self.widget = widget
        self.delta = delta
        self.num = num
        self.x_root = x_root
        self.y_root = y_root
        self.serial = serial


def _scene(
    *, patchlevel: str = "9.0.3", system: str = "aqua", height: int = 500
) -> tuple[scroll.ScrollRouter, FakeRoot, FakeWidget, FakeCanvas]:
    root = FakeRoot(patchlevel=patchlevel, system=system)
    router = scroll.ScrollRouter(root)
    frame = FakeWidget(root, "tab1")
    canvas = FakeCanvas(height=height)
    router.register(frame, canvas, name="tab1")
    router.install()
    root.pointer = frame
    return router, root, frame, canvas


# ---------------------------------------------------------------------------
# environment and geometry
# ---------------------------------------------------------------------------
def test_the_environment_is_read_from_the_running_tk() -> None:
    environment = scroll.Environment.detect(FakeRoot(patchlevel="9.0.3"))

    assert environment.tk_version == (9, 0, 3)
    assert environment.windowing_system == "aqua"
    assert environment.major == 9
    assert environment.is_aqua and not environment.is_windows and not environment.is_x11
    assert environment.label == "Tk 9.0.3 · aqua"


@pytest.mark.parametrize(
    ("patchlevel", "expected"),
    [
        ("8.6.16", (8, 6, 16)),
        ("9.0.3", (9, 0, 3)),
        ("9.0", (9, 0, 0)),
        ("8.7a1", (8, 7, 1)),
        ("", (0, 0, 0)),
        ("nonsense", (0, 0, 0)),
    ],
)
def test_the_patchlevel_survives_every_shape_tk_reports(
    patchlevel: str, expected: tuple[int, int, int]
) -> None:
    assert scroll.Environment.detect(FakeRoot(patchlevel=patchlevel)).tk_version == expected


def test_aqu_a_tk9_reports_one_hundred_and_twenty_deltas_per_notch() -> None:
    """The bug behind the 360-units-per-notch jump: Aqua 9 changed the scale."""
    geometry = scroll.Environment((9, 0, 3), "aqua").wheel_geometry()

    assert geometry.delta_per_notch == 120.0
    assert geometry.units(120.0) == -3.0
    assert geometry.units(-120.0) == 3.0


def test_aqua_tk86_keeps_the_raw_delta_scale() -> None:
    """Aqua 8.6 passes the Cocoa delta through: ±1 per notch is one notch."""
    geometry = scroll.Environment((8, 6, 16), "aqua").wheel_geometry()

    assert geometry.delta_per_notch == 1.0
    assert geometry.units(1.0) == -3.0
    assert geometry.units(3.0) == -9.0


def test_windows_and_x11_keep_their_own_feel() -> None:
    windows = scroll.Environment((9, 0, 0), "win32").wheel_geometry()
    x11 = scroll.Environment((8, 6, 16), "x11").wheel_geometry()

    # 20 px per notch on Windows (its canvas increments by 1 px), 30 px on X11.
    assert windows.units(120.0) == -20.0
    assert x11.units(120.0) == -1.0


def test_a_small_wheel_delta_still_moves_when_it_accumulates() -> None:
    """Games and some drivers send less than a notch; rounding each is a dead wheel."""
    geometry = scroll.Environment((9, 0, 3), "aqua").wheel_geometry()
    accumulator = scroll.WheelAccumulator()

    steps = [accumulator.units(20.0, geometry) for _ in range(6)]

    assert sum(steps) == -3, steps
    assert accumulator.leftover == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# the packed touchpad delta
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, (0, 0)),
        (40, (0, 40)),
        (0xFFD8, (0, -40)),  # X delta 0, Y delta -40, as Tk packs it
        ((3 << 16) | 40, (3, 40)),
        ((-1 << 16) | 40, (-1, 40)),
        ((-1 << 16) | (-40 & 0xFFFF), (-1, -40)),
        (0x7FFF, (0, 32767)),
        (0x8000, (0, -32768)),
    ],
)
def test_the_packed_delta_decodes_like_tk(raw: int, expected: tuple[int, int]) -> None:
    assert scroll.decode_touchpad_delta(raw) == expected


def test_the_python_decoder_matches_the_tcl_proc() -> None:
    """Kept as a fallback, so it must not drift from ``::tk::PreciseScrollDeltas``."""
    values = [
        0,
        7,
        -7,
        (2 << 16) | 9,
        (-4 << 16) | 9,
        (1 << 16) | 0xFFF0,
        -(1 << 31),
        (1 << 31) - 1,
    ]
    for raw in values:
        assert scroll.decode_touchpad_delta(raw) == _tcl_precise_deltas(raw), raw


def test_the_router_asks_tk_to_decode_when_it_can() -> None:
    root = FakeRoot()
    raw = (-2 << 16) | 33
    decoded = scroll.precise_scroll_deltas(root, raw)

    assert decoded == (-2, 33)
    # Exactly what ``%D`` would substitute: a decimal string, sign included, so
    # the proc sees the same bits Tk would have handed it.
    assert ("::tk::PreciseScrollDeltas", str(raw)) in root.tk.calls


def test_the_router_falls_back_when_tk_lacks_the_proc() -> None:
    """Tk 8.6 has no ``::tk::PreciseScrollDeltas``; the event type is absent too."""

    class OldTk(FakeTk):
        def call(self, *args: object) -> object:
            if args[0] == "::tk::PreciseScrollDeltas":
                raise RuntimeError('invalid command name "::tk::PreciseScrollDeltas"')
            return super().call(*args)

    root = FakeRoot(patchlevel="8.6.16", system="aqua")
    root.tk = OldTk("8.6.16", "aqua")

    assert scroll.precise_scroll_deltas(root, 40) == (0, 40)


def test_a_packed_delta_is_never_treated_as_a_wheel_notch() -> None:
    """The regression: 4 294 967 336 units from one horizontal millimetre."""
    _router, root, frame, canvas = _scene()

    root.pointer = frame
    root.bound["<TouchpadScroll>"](FakeEvent(frame, delta=((-1 << 16) | 40) & 0xFFFFFFFF))

    assert canvas.units == [], "a touchpad event went through the wheel maths"
    assert canvas.moves, "the touchpad event did not move the canvas at all"
    assert canvas.moves[0] == pytest.approx(0.2 - 40 / 500)


def test_the_touchpad_scrolls_by_pixels_and_follows_tks_sign() -> None:
    """``yview scroll -dy pixels``, the formula Tk's own Text and Listbox use."""
    _router, root, frame, canvas = _scene()

    root.bound["<TouchpadScroll>"](FakeEvent(frame, delta=60))
    assert canvas.moves[-1] == pytest.approx(0.2 - 60 / 500)

    canvas.top = 0.6
    root.bound["<TouchpadScroll>"](FakeEvent(frame, delta=-20))
    assert canvas.moves[-1] == pytest.approx(0.6 + 20 / 500)


def test_an_absurd_momentum_delta_is_clamped() -> None:
    """A fling reports hundreds of pixels; one moveto of that looks like a jump."""
    _router, root, frame, canvas = _scene()

    root.bound["<TouchpadScroll>"](FakeEvent(frame, delta=30_000))

    assert canvas.moves[-1] == 0.0, "the move was not clamped at the top of the document"


# ---------------------------------------------------------------------------
# routing precedence
# ---------------------------------------------------------------------------
def test_the_pointer_wins_over_the_event_widget() -> None:
    """Aqua delivers the event to the window, so the pointer is the only input."""
    router, root, frame, canvas = _scene()
    other = FakeWidget(root, "tab2")
    other_canvas = FakeCanvas()
    router.register(other, other_canvas, name="tab2")

    root.pointer = frame
    root.bound["<MouseWheel>"](FakeEvent(root, delta=120))

    assert canvas.units == [(-3, "units")]
    assert other_canvas.units == [], "the wrong tab scrolled"


def test_a_text_area_resolves_to_nothing_and_keeps_its_own_scrolling() -> None:
    _router, root, frame, canvas = _scene()
    text = FakeText(frame, "text")

    root.pointer = text
    assert root.bound["<MouseWheel>"](FakeEvent(text, delta=120)) is None
    assert canvas.units == []


def test_a_scrollbar_resolves_to_nothing() -> None:
    _router, root, frame, canvas = _scene()
    bar = FakeScrollbar(frame, "bar")

    root.pointer = bar
    assert root.bound["<MouseWheel>"](FakeEvent(bar, delta=120)) is None
    assert canvas.units == []


@pytest.mark.parametrize("name", ["Listbox", "Treeview", "TCombobox", "TScale"])
def test_a_widget_with_its_own_class_binding_is_left_alone(name: str) -> None:
    """Tk scrolls these itself; scrolling the page too would double every notch."""
    _router, root, frame, canvas = _scene()
    widget = type(name, (FakeWidget,), {})(frame, name)
    root.pointer = widget

    assert root.bound["<MouseWheel>"](FakeEvent(widget, delta=120)) is None
    assert canvas.units == []


def test_a_hidden_tab_cannot_be_scrolled_through() -> None:
    _router, root, frame, canvas = _scene()
    canvas.mapped = False

    root.pointer = frame
    assert root.bound["<MouseWheel>"](FakeEvent(frame, delta=120)) is None
    assert canvas.units == []


def test_a_point_outside_every_area_scrolls_nothing() -> None:
    _router, root, _frame, canvas = _scene()
    stranger = FakeWidget(root, "tab-bar")

    root.pointer = stranger
    assert root.bound["<MouseWheel>"](FakeEvent(root, delta=120)) is None
    assert root.bound["<TouchpadScroll>"](FakeEvent(root, delta=120)) is None
    assert canvas.units == [] and canvas.moves == []


def test_a_deep_child_resolves_to_its_own_area() -> None:
    _router, root, frame, canvas = _scene()
    card = FakeWidget(frame, "card")
    label = FakeWidget(card, "label")
    root.pointer = label

    root.bound["<MouseWheel>"](FakeEvent(label, delta=-120))

    assert canvas.units == [(3, "units")]


def test_the_event_widget_is_used_only_when_the_pointer_is_unknown() -> None:
    """Pointer outside the window: fall back to the widget the event named."""
    _router, root, frame, canvas = _scene()
    root.pointer = None

    root.bound["<MouseWheel>"](FakeEvent(frame, delta=120))

    assert canvas.units == [(-3, "units")]


def test_x11_button_events_keep_their_own_meaning() -> None:
    _router, root, frame, canvas = _scene(patchlevel="8.6.16", system="x11")

    root.bound["<Button-4>"](FakeEvent(frame, num=4))
    root.bound["<Button-5>"](FakeEvent(frame, num=5))

    assert canvas.units == [(-1, "units"), (1, "units")]


def test_a_handled_event_breaks_the_chain_so_nothing_scrolls_twice() -> None:
    _router, root, frame, canvas = _scene()

    assert root.bound["<MouseWheel>"](FakeEvent(frame, delta=120)) == "break"
    assert root.bound["<TouchpadScroll>"](FakeEvent(frame, delta=40)) == "break"
    assert len(canvas.units) + len(canvas.moves) == 2, "the event was applied twice"


def test_an_unhandled_event_is_left_to_the_rest_of_the_bindings() -> None:
    _router, root, frame, _canvas = _scene()
    text = FakeText(frame, "text")
    root.pointer = text

    assert root.bound["<MouseWheel>"](FakeEvent(text, delta=120)) is None


# ---------------------------------------------------------------------------
# installation
# ---------------------------------------------------------------------------
def test_installation_removes_the_customtkinter_handlers_and_binds_ours() -> None:
    router, root, _frame, _canvas = _scene()

    assert set(root.unbound) == {"<MouseWheel>", "<Button-4>", "<Button-5>"}
    assert set(root.bound) == {
        "<MouseWheel>",
        "<Button-4>",
        "<Button-5>",
        "<TouchpadScroll>",
    }
    assert router.installed is True


def test_installation_survives_a_tk_without_touchpad_events() -> None:
    """Tk 8.6 raises for ``<TouchpadScroll>``; the rest must still bind."""

    class OldRoot(FakeRoot):
        def bind_all(self, sequence: str, handler: object, add: object = None) -> None:
            if sequence == scroll.TOUCHPAD_SEQUENCE:
                raise RuntimeError("bad event type or keysym")
            super().bind_all(sequence, handler, add)

    root = OldRoot(patchlevel="8.6.16", system="aqua")
    router = scroll.ScrollRouter(root)
    frame = FakeWidget(root, "tab1")
    router.register(frame, FakeCanvas(), name="tab1")

    assert router.install() == 3
    assert scroll.TOUCHPAD_SEQUENCE not in root.bound
    assert "touchpad=no" in router.describe()


def test_installing_twice_does_not_bind_twice() -> None:
    router, root, _frame, _canvas = _scene()

    assert router.install() == 4
    assert len(root.bound) == 4


def test_uninstall_removes_the_bindings_it_created() -> None:
    router, root, _frame, _canvas = _scene()
    bound = set(root.bound)
    assert bound, "nothing was bound to release"

    router.uninstall()

    assert router.installed is False
    assert not bound & set(root.bound), "a sequence this router bound is still bound"
    assert bound <= set(root.unbound)
    root.unbound.clear()
    router.uninstall()  # idempotent: nothing left to unbind
    assert root.unbound == []


def test_registering_the_same_frame_twice_updates_the_canvas() -> None:
    router, _root, frame, _canvas = _scene()
    replacement = FakeCanvas()

    router.register(frame, replacement, name="tab1")

    assert router.area_count == 1
    assert router.areas[0].canvas is replacement


def test_describe_names_the_environment_and_the_maths() -> None:
    router, _root, _frame, _canvas = _scene()

    described = router.describe()

    assert "Tk 9.0.3" in described
    assert "aqua" in described
    assert "3 units per 120 delta" in described
    assert "touchpad=bound" in described


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------
def test_diagnostics_are_off_unless_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(scroll.DIAGNOSTICS_ENV, raising=False)

    assert scroll.ScrollDiagnostics.from_environment().enabled is False


def test_diagnostics_can_go_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(scroll.DIAGNOSTICS_ENV, "1")

    diagnostics = scroll.ScrollDiagnostics.from_environment()

    assert diagnostics.enabled is True
    assert diagnostics.sink is None


def test_diagnostics_can_go_to_a_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "scroll.log"
    monkeypatch.setenv(scroll.DIAGNOSTICS_ENV, str(target))

    diagnostics = scroll.ScrollDiagnostics.from_environment()
    diagnostics.emit("event=MouseWheel")

    assert diagnostics.enabled is True
    assert "event=MouseWheel" in target.read_text(encoding="utf-8")


def test_a_diagnostic_line_carries_everything_needed_for_a_bug_report() -> None:
    """Event, Tk, backend, raw %, decoded deltas, widget, area, yview, movement."""
    router, root, frame, _canvas = _scene()
    lines: list[str] = []
    router.diagnostics = scroll.ScrollDiagnostics(enabled=True, sink=lines.append)

    root.bound["<TouchpadScroll>"](
        FakeEvent(frame, delta=(0 << 16) | 12, serial=7, x_root=40, y_root=50)
    )

    assert len(lines) == 1
    line = lines[0]
    for token in (
        "event=TouchpadScroll",
        "serial=7",
        "tk=9.0.3",
        "sys=aqua",
        "raw=%D=12",
        "decoded=(dx=0,dy=12)",
        "widget=tab1",
        "at=(40,50)",
        "area=tab1",
        "yview=",
        "moved=yes",
    ):
        assert token in line, (token, line)


def test_diagnostics_stay_quiet_when_disabled() -> None:
    router, root, frame, _canvas = _scene()
    lines: list[str] = []
    router.diagnostics = scroll.ScrollDiagnostics(enabled=False, sink=lines.append)

    root.bound["<MouseWheel>"](FakeEvent(frame, delta=120))

    assert lines == []


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------
def test_scrolling_by_pixels_divides_by_the_scroll_region() -> None:
    """``yview moveto`` addresses the scroll region, not the visible window.

    Dividing by the window height instead would over-scroll by the ratio between
    the two — five times on a long tab.
    """
    canvas = FakeCanvas(height=400, scrollregion="0 0 300 2400")

    assert scroll.scroll_canvas_by_pixels(canvas, 40) is True
    assert canvas.moves == [pytest.approx(0.2 + 40 / 2400)]


def test_a_canvas_without_a_scroll_region_falls_back_to_its_window() -> None:
    canvas = FakeCanvas(height=400)

    assert scroll.scroll_canvas_by_pixels(canvas, 40) is True
    assert canvas.moves == [pytest.approx(0.2 + 40 / 400)]


@pytest.mark.parametrize("region", ["", "0 0", "0 0 0 0", "nonsense", "0 0 x 4000"])
def test_a_broken_scroll_region_never_breaks_the_gesture(region: str) -> None:
    canvas = FakeCanvas(height=400, scrollregion=region)

    assert scroll.scroll_canvas_by_pixels(canvas, 40) is True
    assert canvas.moves == [pytest.approx(0.2 + 40 / 400)]


def test_scrolling_by_pixels_never_leaves_the_document() -> None:
    canvas = FakeCanvas(height=400)

    scroll.scroll_canvas_by_pixels(canvas, -10_000)

    assert canvas.moves == [0.0]


def test_scrolling_by_pixels_refuses_a_collapsed_canvas() -> None:
    """A tab that is not laid out yet has no height; no division by zero."""
    canvas = FakeCanvas(height=0)

    assert scroll.scroll_canvas_by_pixels(canvas, 40) is False
    assert canvas.moves == []


def test_a_destroyed_canvas_does_not_raise() -> None:
    class Gone(FakeCanvas):
        def yview(self, *args: object) -> tuple[float, float]:
            raise RuntimeError("application has been destroyed")

    assert scroll.scroll_canvas_by_pixels(Gone(), 10) is False
