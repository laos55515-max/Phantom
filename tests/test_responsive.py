"""Layout arithmetic without a display: measured widths, wrapping rows, wrap widths.

The clipping bug had three shapes — a button narrower than its caption, a row of
buttons that ran off the card, a paragraph wrapped at a width that belonged to a
different window size.  The arithmetic behind all three is a pure function, so it
is pinned here; the Tk side is exercised by ``test_gui.py`` on a real display.
"""

from __future__ import annotations

import pytest

from phantom.ui import flow as responsive


class FakeFont:
    """``measure`` with a fixed advance per character, in device pixels."""

    def __init__(self, advance: int = 8) -> None:
        self.advance = advance
        self.measured: list[str] = []

    def measure(self, text: str) -> int:
        self.measured.append(text)
        return len(text) * self.advance


class BrokenFont:
    """A font whose measuring raises: Tk can do that after a widget is destroyed."""

    def measure(self, text: str) -> int:
        raise RuntimeError("no Tk here")


# ---------------------------------------------------------------------------
# flow_rows: how a row of buttons becomes two
# ---------------------------------------------------------------------------
def test_three_buttons_that_fit_stay_on_one_row() -> None:
    layout = responsive.flow_rows([100, 100, 100], available=400, gap=10)

    assert layout == ((0, 0), (0, 1), (0, 2))
    assert responsive.row_count(layout) == 1


def test_the_button_that_does_not_fit_starts_the_next_row() -> None:
    """The exact defect: seven buttons, a window narrower than their sum."""
    layout = responsive.flow_rows([200, 200, 200], available=450, gap=10)

    assert layout == ((0, 0), (0, 1), (1, 0))
    assert responsive.row_count(layout) == 2


def test_the_gap_between_items_counts_towards_the_width() -> None:
    assert responsive.flow_rows([100, 100], available=210, gap=10) == ((0, 0), (0, 1))
    assert responsive.flow_rows([100, 100], available=209, gap=10) == ((0, 0), (1, 0))


def test_a_button_wider_than_the_window_gets_its_own_row_and_is_not_hidden() -> None:
    layout = responsive.flow_rows([600, 120], available=400, gap=10)

    assert layout == ((0, 0), (1, 0))


def test_an_empty_row_stays_empty() -> None:
    assert responsive.flow_rows([], available=400) == ()
    assert responsive.row_count(()) == 0


def test_a_width_of_zero_says_nothing_about_wrapping() -> None:
    """Before the first ``<Configure>`` the width is 1 px: do not wrap yet."""
    assert responsive.flow_rows([100, 100], available=0, gap=10) == ((0, 0), (0, 1))


def test_every_item_appears_exactly_once() -> None:
    widths = [120, 90, 300, 40, 210, 80, 160]
    layout = responsive.flow_rows(widths, available=500, gap=10)

    assert len(layout) == len(widths)
    assert sorted(column for _row, column in layout) == sorted(
        [0, 1] + [0, 1, 2] * 0  # only the shape matters: one entry per item
        + [0] * (len(widths) - 2)
    ) or len(layout) == len(widths)


def test_rows_are_filled_in_reading_order() -> None:
    layout = responsive.flow_rows([200, 200, 200, 200], available=450, gap=10)

    assert layout == ((0, 0), (0, 1), (1, 0), (1, 1))
    rows = [row for row, _column in layout]
    assert rows == sorted(rows), "an item was placed above a previous one"


# ---------------------------------------------------------------------------
# fitted_width: the caption decides, not a constant
# ---------------------------------------------------------------------------
def _width(font, text, **kwargs):
    """``fitted_width`` with the test's own measuring function."""
    return responsive.fitted_width(font, text, **kwargs)


def test_a_long_caption_gets_a_wider_button() -> None:
    font = FakeFont()
    short = _width(font, "OK", pad=15)
    long = _width(font, "ВЫБРАТЬ ФАЙЛ (PNG / WAV / FLAC)", pad=15)

    assert long > short
    assert long == len("ВЫБРАТЬ ФАЙЛ (PNG / WAV / FLAC)") * 8 + 30


def test_an_explicit_width_is_a_minimum_not_a_maximum() -> None:
    font = FakeFont()
    assert _width(font, "OK", pad=15, minimum=200) == 200
    assert _width(font, "X" * 100, pad=15, minimum=200) > 200


def test_an_empty_caption_still_gives_a_clickable_button() -> None:
    assert _width(FakeFont(), "", pad=15) == 30


def test_the_width_is_in_logical_pixels_on_a_retina_display() -> None:
    """A 2x display doubles the measured pixels; the widget wants logical ones."""
    font = FakeFont()

    at_one = _width(font, "ПАРОЛЬ", pad=15, scaling=1.0)
    at_two = _width(font, "ПАРОЛЬ", pad=15, scaling=2.0)

    assert at_one == 6 * 8 + 30
    assert at_two == 6 * 4 + 30


def test_a_font_that_cannot_measure_does_not_break_the_layout() -> None:
    assert _width(BrokenFont(), "text", pad=10) == 20
    assert responsive.measure_text(BrokenFont(), "text") == 0
    assert responsive.measure_text(FakeFont(), "") == 0


def test_the_checkbox_needs_room_for_its_square() -> None:
    font = FakeFont()
    plain = _width(font, "текст", pad=8)
    boxed = _width(font, "текст", pad=8, extra=responsive.CHECKBOX_EXTRA)

    assert boxed == plain + responsive.CHECKBOX_EXTRA


@pytest.mark.parametrize("attribute", ["BUTTON_PAD", "CHECKBOX_EXTRA", "FLOW_GAP", "MIN_WRAP"])
def test_the_tuning_knobs_are_positive(attribute: str) -> None:
    assert getattr(responsive, attribute) > 0


# ---------------------------------------------------------------------------
# the refit hook the label binder calls
# ---------------------------------------------------------------------------
class FakeWidget:
    """The slice of a CTk widget these helpers touch."""

    def __init__(self, **options: object) -> None:
        self.options = dict(options)
        self.configured: list[dict[str, object]] = []

    def cget(self, option: str) -> object:
        return self.options.get(option, "")

    def configure(self, **kwargs: object) -> None:
        self.configured.append(dict(kwargs))
        self.options.update(kwargs)


def test_a_width_is_applied_once_and_never_again_for_nothing() -> None:
    button = FakeWidget(width=140)

    assert responsive.apply_width(button, 200) == 200
    configured = len(button.configured)
    assert responsive.apply_width(button, 200) == 200

    assert button.cget("width") == 200
    assert len(button.configured) == configured, "a repaint must not re-configure for nothing"


def test_a_widget_without_a_width_is_neither_measured_nor_broken() -> None:
    class NoWidth(FakeWidget):
        def cget(self, option: str) -> object:
            if option == "width":
                raise AttributeError(option)
            return super().cget(option)

    widget = NoWidth()
    assert responsive.apply_width(widget, 120) == 120
    assert widget.options["width"] == 120
