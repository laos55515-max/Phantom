"""Start-up measurement without a window: the clock, the marks, the summary.

The point of ``PHANTOM_STARTUP_DEBUG=1`` is that a freeze becomes a table of
numbers a maintainer can read.  The clock is injected, so the arithmetic is
pinned here instead of being copied out of a terminal.
"""

from __future__ import annotations

import pytest

from phantom.ui import startup


class FakeClock:
    """A clock that only moves when the test says so."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, milliseconds: float) -> None:
        self.now += milliseconds / 1000.0


def _trace(**kwargs: object) -> tuple[startup.StartupTrace, FakeClock, list[str]]:
    clock = FakeClock()
    lines: list[str] = []
    trace = startup.StartupTrace(clock=clock, sink=lines.append, **kwargs)
    return trace, clock, lines


# ---------------------------------------------------------------------------
# the switch
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_the_variable_switches_the_timings_on(value: str) -> None:
    assert startup.debug_enabled({startup.ENV: value}) is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "verbose"])
def test_anything_else_leaves_them_off(value: str) -> None:
    assert startup.debug_enabled({startup.ENV: value}) is False


def test_no_variable_at_all_is_off() -> None:
    assert startup.debug_enabled({}) is False


# ---------------------------------------------------------------------------
# the marks
# ---------------------------------------------------------------------------
def test_a_mark_reports_the_time_since_the_trace_started() -> None:
    trace, clock, _lines = _trace(enabled=True)
    clock.advance(120.0)

    assert trace.mark("root") == pytest.approx(120.0)
    clock.advance(80.0)
    assert trace.mark("build") == pytest.approx(200.0)


def test_each_mark_carries_its_own_duration_too() -> None:
    trace, clock, lines = _trace(enabled=True)
    clock.advance(50.0)
    trace.mark("import:gui")
    clock.advance(25.0)
    trace.mark("root")

    assert trace.marks == [("import:gui", pytest.approx(50.0), pytest.approx(50.0)),
                           ("root", pytest.approx(75.0), pytest.approx(25.0))]
    assert "stage=root ms=75.0 (+25.0)" in lines[-1]


def test_a_stage_times_the_block_even_when_it_raises() -> None:
    trace, clock, _lines = _trace(enabled=True)

    with pytest.raises(RuntimeError), trace.stage("build:tabs"):
        clock.advance(30.0)
        raise RuntimeError("the tab builder failed")

    assert trace.at("build:tabs") == pytest.approx(30.0)


def test_the_first_occurrence_of_a_name_is_what_at_returns() -> None:
    trace, clock, _lines = _trace(enabled=True)
    clock.advance(10.0)
    trace.mark("tab")
    clock.advance(10.0)
    trace.mark("tab")

    assert trace.at("tab") == pytest.approx(10.0)
    assert trace.at("never-happened") is None


def test_the_summary_names_the_moments_the_report_talks_about() -> None:
    trace, clock, _lines = _trace(enabled=True)
    clock.advance(900.0)
    trace.mark("first_frame")
    clock.advance(300.0)
    trace.mark("ready")

    summary = trace.summary()

    assert summary["first_frame"] == pytest.approx(900.0)
    assert summary["ready"] == pytest.approx(1200.0)
    assert summary["total"] == pytest.approx(1200.0)
    assert summary["stages"] == 2


def test_the_summary_line_is_one_line_and_readable() -> None:
    trace, clock, lines = _trace(enabled=True)
    clock.advance(940.0)
    trace.mark("first_frame")
    clock.advance(235.0)
    trace.mark("ready")

    trace.log_summary()

    assert lines[-1].startswith("startup: total=")
    assert "first_frame=940 ms" in lines[-1]
    assert "ready=1175 ms" in lines[-1]
    assert "\n" not in lines[-1]


def test_nothing_is_logged_when_the_switch_is_off() -> None:
    trace, clock, lines = _trace(enabled=False)
    clock.advance(500.0)
    trace.mark("root")
    trace.log_summary()

    assert lines == [], "a silent launch must stay silent"
    assert trace.marks, "the marks are still collected: they cost nothing"
    assert trace.at("root") == pytest.approx(500.0)


def test_the_slowest_stages_are_named_biggest_first() -> None:
    trace, clock, _lines = _trace(enabled=True)
    for name, duration in (("import", 10.0), ("tabs", 300.0), ("theme", 40.0)):
        clock.advance(duration)
        trace.mark(name)

    slowest = trace.slowest(2)

    assert [name for name, _ms in slowest] == ["tabs", "theme"]
    assert slowest[0][1] == pytest.approx(300.0)


def test_the_trace_from_the_environment_is_enabled_only_on_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(startup.ENV, raising=False)
    assert startup.trace_from_env().enabled is False
    monkeypatch.setenv(startup.ENV, "1")
    assert startup.trace_from_env().enabled is True
