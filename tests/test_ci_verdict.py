"""The CI verdict: what a job is allowed to tolerate, and what it never is.

The rule these tests protect is the one that let a segfault stay green: a step with
``continue-on-error: true`` reports success whatever happened inside it.  A crash —
a signal, ``exit 139``, a kill — always fails the verdict; a declared diagnostic may
fail a test; a probe of an old tree may time out.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

TOOLS = pathlib.Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import ci_verdict  # noqa: E402 - after the path insert above


def _steps(*rows: tuple[str, int, str]) -> list[ci_verdict.Step]:
    return [ci_verdict.Step(name=name, code=code, kind=kind) for name, code, kind in rows]


def test_a_clean_run_is_a_pass() -> None:
    acceptable, lines = ci_verdict.verdict(_steps(("gate", 0, "gate"), ("diag", 0, "diagnostic")))
    assert acceptable is True
    assert any("verdict: 2 steps, 0 crashed" in line for line in lines)


def test_a_crash_inside_a_diagnostic_step_is_still_a_failure() -> None:
    """This is the case that was green in CI run 44837469975."""
    acceptable, lines = ci_verdict.verdict(_steps(("suite", 139, "diagnostic")))
    assert acceptable is False
    assert any("::error::crash: suite" in line and "signal 11" in line for line in lines)


def test_a_crash_is_a_failure_for_every_kind() -> None:
    for kind in ci_verdict.KINDS:
        acceptable, _ = ci_verdict.verdict(_steps(("x", 139, kind)))
        assert acceptable is False, kind
        acceptable, _ = ci_verdict.verdict(_steps(("x", -11, kind)))
        assert acceptable is False, kind


def test_a_declared_diagnostic_may_fail_but_is_printed() -> None:
    acceptable, lines = ci_verdict.verdict(_steps(("suite", 1, "diagnostic")))
    assert acceptable is True
    assert any("::warning::failed-but-declared: suite" in line for line in lines)


def test_a_gate_may_not_fail() -> None:
    acceptable, lines = ci_verdict.verdict(_steps(("editor smoke", 1, "gate")))
    assert acceptable is False
    assert any("::error::failed: editor smoke" in line for line in lines)


def test_a_probe_may_time_out_because_the_timeout_is_the_measurement() -> None:
    acceptable, lines = ci_verdict.verdict(_steps(("before", 124, "probe")))
    assert acceptable is True
    assert any("::warning::timeout: before" in line for line in lines)


def test_a_timeout_in_a_diagnostic_is_not_tolerated() -> None:
    acceptable, lines = ci_verdict.verdict(_steps(("suite", 124, "diagnostic")))
    assert acceptable is False
    assert any("::error::timeout: suite" in line for line in lines)


def test_an_empty_or_missing_file_is_not_a_pass(tmp_path: pathlib.Path) -> None:
    assert ci_verdict.main([str(tmp_path / "absent.tsv")]) == 2
    empty = tmp_path / "empty.tsv"
    empty.write_text("# nothing here\n", encoding="utf-8")
    assert ci_verdict.main([str(empty)]) == 2


def test_a_malformed_line_is_refused(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "broken.tsv"
    path.write_text("suite\tnot-a-code\tgate\n", encoding="utf-8")
    assert ci_verdict.main([str(path)]) == 2
    path.write_text("suite\t0\tunknown-kind\n", encoding="utf-8")
    assert ci_verdict.main([str(path)]) == 2
    path.write_text("suite only two columns\n", encoding="utf-8")
    assert ci_verdict.main([str(path)]) == 2


def test_the_written_file_round_trips(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "status.tsv"
    path.write_text(
        "theme smoke\t0\tgate\nsuite\t139\tdiagnostic\nbefore\t124\tprobe\n", encoding="utf-8"
    )
    steps = ci_verdict.parse(path.read_text(encoding="utf-8"))
    assert [step.name for step in steps] == ["theme smoke", "suite", "before"]
    acceptable, _ = ci_verdict.verdict(steps)
    assert acceptable is False, "the 139 in the middle still fails the run"


def test_the_verdict_exit_code_matches_the_rules(tmp_path: pathlib.Path) -> None:
    good = tmp_path / "good.tsv"
    good.write_text("suite\t0\tgate\nbefore\t124\tprobe\n", encoding="utf-8")
    assert ci_verdict.main([str(good)]) == 0
    bad = tmp_path / "bad.tsv"
    bad.write_text("suite\t139\tdiagnostic\n", encoding="utf-8")
    assert ci_verdict.main([str(bad)]) == 1


@pytest.mark.parametrize("code", [130, 137, 139, 143])
def test_killed_processes_are_recognised_as_crashes(code: int) -> None:
    assert ci_verdict.Step(name="x", code=code, kind="diagnostic").crashed is True


@pytest.mark.parametrize("code", [125, 126, 127])
def test_a_command_that_never_started_is_not_a_test_failure(code: int) -> None:
    """``126``/``127`` mean the shell could not run it: that is not "the suite failed"."""
    step = ci_verdict.Step(name="x", code=code, kind="diagnostic")
    assert step.crashed is False
    assert step.acceptable is False
    acceptable, lines = ci_verdict.verdict([step])
    assert acceptable is False
    assert any("::error::unstarted: x" in line for line in lines)


def test_a_missing_mandatory_row_fails_the_run_even_when_every_row_is_green(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The measured hole: all green, ``gui suite (tk9)`` never written, exit 0.

    A step that is skipped writes nothing, so counting the rows that are there
    cannot see it: the file had five green rows and the one that decides the
    platform was absent.
    """
    path = tmp_path / "incomplete.tsv"
    path.write_text(
        "theme smoke\t0\tgate\neditor smoke\t0\tgate\ncrash reports\t0\tgate\n",
        encoding="utf-8",
    )
    assert ci_verdict.main([str(path)]) == 0, "without a requirement the rows are green"
    capsys.readouterr()

    code = ci_verdict.main([str(path), "--require", "gui suite (tk9)"])
    out = capsys.readouterr().out
    assert code == 1, "a mandatory step that recorded nothing cannot pass the run"
    assert "::error::missing: gui suite (tk9)" in out, "the failure must name the step"
    assert "1 of 1 required steps missing" in out


def test_a_required_row_that_is_present_keeps_the_run_green(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "complete.tsv"
    path.write_text(
        "gui suite (tk9)\t0\tgate\ncrash reports\t0\tgate\n", encoding="utf-8"
    )
    assert ci_verdict.main([str(path), "--require", "gui suite (tk9)", "--require", "crash reports"]) == 0


def test_a_required_name_must_match_exactly() -> None:
    """``gui suite`` is not ``gui suite (tk9)``: the near miss is the whole point."""
    steps = _steps(("gui suite", 0, "gate"))
    acceptable, lines = ci_verdict.verdict(steps, ["gui suite (tk9)"])
    assert acceptable is False
    assert any("::error::missing: gui suite (tk9)" in line for line in lines)
    assert ci_verdict.missing_rows(steps, ["gui suite"]) == []


def test_every_missing_name_is_reported_once_and_in_order() -> None:
    steps = _steps(("theme smoke", 0, "gate"))
    required = ["gui suite (tk9)", "theme smoke", "crash reports"]
    assert ci_verdict.missing_rows(steps, required) == ["gui suite (tk9)", "crash reports"]
    acceptable, lines = ci_verdict.verdict(steps, required)
    assert acceptable is False
    missing = [line for line in lines if "::error::missing: " in line]
    assert len(missing) == 2
    assert "gui suite (tk9)" in missing[0] and "crash reports" in missing[1]


def test_a_crash_and_a_missing_row_are_both_reported() -> None:
    """Neither may mask the other: the run has to say everything that went wrong."""
    acceptable, lines = ci_verdict.verdict(_steps(("suite", -11, "gate")), ["gui suite (tk9)"])
    assert acceptable is False
    assert any("::error::crash: suite" in line for line in lines)
    assert any("::error::missing: gui suite (tk9)" in line for line in lines)


def test_the_table_reaches_the_pr_when_annotations_are_on(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every row is an annotation, so a *missing* row is visible from the PR too.

    Run 44844048485: ``verdict: 5 steps`` while seven steps had run, and the two that
    were missing were exactly the two that failed — the table only existed in the raw
    step log, which is where nobody looked.
    """
    path = tmp_path / "rows.tsv"
    path.write_text("suite (diagnostic)\t139\tdiagnostic\nbefore\t124\tprobe\n", encoding="utf-8")
    assert ci_verdict.main([str(path), "--annotate"]) == 1
    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if line.startswith("::notice::step: ")]
    assert len(rows) == 2
    assert "suite (diagnostic) — diagnostic — CRASHED" in rows[0]


def test_without_annotations_the_output_is_the_plain_table(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "rows.tsv"
    path.write_text("suite\t0\tgate\n", encoding="utf-8")
    assert ci_verdict.main([str(path)]) == 0
    out = capsys.readouterr().out
    assert "::notice::step:" not in out
    assert "suite" in out
