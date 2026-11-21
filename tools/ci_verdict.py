#!/usr/bin/env python3
"""Turn a list of step exit codes into one honest verdict.

GitHub treats ``continue-on-error: true`` as "this step cannot fail the job", which
is how a process that died with ``exit 139`` sat inside a green run: the runner
annotated *Process completed with exit code 139* and the job stayed green.  A crash
is never a measurement, so the codes are collected into one file and one step
decides:

    name<TAB>code<TAB>kind

Kinds, and what a non-zero code means for each:

``gate``        the run decides the job: any non-zero fails.
``diagnostic``  a failure is a known limitation of the platform and is printed as a
                warning — but a *crash* (a signal, ``exit >= 128``, or a kill) still
                fails: a diagnostic may fail, it may not die.
``probe``       measuring something that is expected to struggle (the old tree that
                never finishes a resize): a failure or a timeout is tolerated and
                printed; a crash is not.

A row that was never written
---------------------------
A step that was skipped — because an earlier step failed, or because a condition did
not hold — leaves no row, and a *missing* row cannot be told apart from a green one by
counting what is there.  Measured: with every other row green and ``gui suite (tk9)``
absent, the verdict was 0.  So the steps that must always report are named on the
command line with ``--require``, and an absent one is a failure that names itself::

    python tools/ci_verdict.py ci-status.tsv \
        --require 'gui suite (tk9)' --require 'crash reports'

``tests/test_repo.py`` keeps that list equal to the gate rows the job actually writes,
so a new mandatory step cannot be added without a name here.

Usage::

    python tools/ci_verdict.py ci-status.tsv [--require NAME ...] [--annotate]

Exit codes: 0 the run is acceptable, 1 it is not, 2 the file is missing or empty.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

#: Kinds this tool understands.
KINDS: Final = ("gate", "diagnostic", "probe")

#: A timeout is not a crash, but on this project it is never a pass either.
TIMEOUT_CODE: Final = 124

#: ``125`` (docker), ``126`` (not executable) and ``127`` (not found): the command
#: never really ran, so the result cannot be read as "the tests failed".
BROKEN_CODES: Final = (125, 126, 127)


@dataclass(slots=True, frozen=True)
class Step:
    name: str
    code: int
    kind: str

    @property
    def crashed(self) -> bool:
        """Killed by a signal (the shell reports ``128 + N``; Python reports ``-N``)."""
        return self.code < 0 or self.code >= 128

    @property
    def timed_out(self) -> bool:
        return self.code == TIMEOUT_CODE

    @property
    def broken(self) -> bool:
        """The process never really ran: killed, timed out, or could not start."""
        return self.crashed or self.timed_out or self.code in BROKEN_CODES

    @property
    def acceptable(self) -> bool:
        if self.code == 0:
            return True
        if self.crashed or self.code in BROKEN_CODES:
            return False
        if self.kind == "gate":
            return False
        if self.kind == "probe":
            return True  # the old tree that never finishes is the measurement
        # diagnostic: a failing test is tolerated, a hang is not.
        return self.code != TIMEOUT_CODE


def parse(text: str) -> list[Step]:
    """Read the TSV; a malformed line is an error, never a silent pass."""
    steps: list[Step] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split("\t")
        if len(parts) != 3:
            raise ValueError(f"line {number}: expected name<TAB>code<TAB>kind, got {line!r}")
        name, code_text, kind = (part.strip() for part in parts)
        if kind not in KINDS:
            raise ValueError(f"line {number}: unknown kind {kind!r} (one of {', '.join(KINDS)})")
        try:
            code = int(code_text)
        except ValueError as exc:
            raise ValueError(f"line {number}: {code_text!r} is not an exit code") from exc
        steps.append(Step(name=name, code=code, kind=kind))
    return steps


def describe(step: Step) -> str:
    if step.code in BROKEN_CODES:
        return f"never ran (exit {step.code}: shell could not start the command)"
    if step.crashed:
        signal = -step.code if step.code < 0 else step.code - 128
        return f"CRASHED (signal {signal}, exit {step.code})"
    if step.timed_out:
        return "TIMED OUT"
    if step.code == 0:
        return "ok"
    return f"failed (exit {step.code})"


def missing_rows(steps: list[Step], required: Sequence[str]) -> list[str]:
    """The mandatory step names that recorded nothing, in the order asked for."""
    seen = {step.name for step in steps}
    return [name for name in required if name not in seen]


def verdict(steps: list[Step], required: Sequence[str] = ()) -> tuple[bool, list[str]]:
    """``(acceptable, lines)``: the lines are the annotations, in order of weight.

    ``required`` names steps that must have written a row: a job whose decisive step
    was skipped is not a job that passed, and the counts alone cannot say so.
    """
    lines: list[str] = []
    unseen = missing_rows(steps, required)
    crashes = [step for step in steps if step.crashed]
    unstarted = [step for step in steps if step.code in BROKEN_CODES]
    timed_out = [step for step in steps if step.broken and not step.crashed and step.code != 0]
    failed = [step for step in steps if step.code != 0 and not step.broken]
    tolerated = [step for step in failed if step.acceptable]
    blocking = [step for step in failed if not step.acceptable]

    for step in crashes:
        lines.append(f"::error::crash: {step.name} {describe(step)} — a signal is never a pass")
    for step in unstarted:
        lines.append(f"::error::unstarted: {step.name} {describe(step)}")
    for name in unseen:
        lines.append(
            f"::error::missing: {name} — a mandatory step recorded no exit code "
            f"(it was skipped, or it died before it could write one)"
        )
    for step in timed_out:
        level = "warning" if step.acceptable else "error"
        lines.append(f"::{level}::timeout: {step.name} {describe(step)}")
    for step in blocking:
        lines.append(f"::error::failed: {step.name} {describe(step)} (kind {step.kind})")
    for step in tolerated:
        lines.append(
            f"::warning::failed-but-declared: {step.name} {describe(step)} "
            f"(kind {step.kind}: the failure is a known platform limitation, the crash rule "
            f"above still applies)"
        )
    lines.append(
        f"::notice::verdict: {len(steps)} steps, {len(crashes)} crashed, "
        f"{len(blocking)} blocking failures, {len(tolerated)} tolerated, "
        f"{len(unseen)} of {len(required)} required steps missing"
    )
    stuck = any(step.broken and not step.acceptable and step.code != 0 for step in steps)
    acceptable = not crashes and not unstarted and not blocking and not stuck and not unseen
    return acceptable, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("status", help="the TSV written by the steps")
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="NAME",
        help="a step that must have written a row; repeat for each one",
    )
    parser.add_argument(
        "--annotate",
        action="store_true",
        help="also print each row as an annotation, so the table reaches the PR",
    )
    args = parser.parse_args(argv)

    path = pathlib.Path(args.status)
    if not path.exists():
        print(f"::error::verdict: {path} is missing — no step reported its exit code")
        return 2
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        steps = parse(text)
    except ValueError as exc:
        print(f"::error::verdict: {path}: {exc}")
        return 2
    if not steps:
        print(f"::error::verdict: {path} lists no steps — nothing was verified")
        return 2

    for step in steps:
        print(f"{step.name:34s} {step.kind:11s} {describe(step)}")
        if args.annotate:
            # A workflow command's message ends at the first newline, so one row per
            # annotation: the table used to live only in the raw step log, so a row
            # that was never written could not be told apart from a row that passed.
            print(f"::notice::step: {step.name} — {step.kind} — {describe(step)}")
    acceptable, lines = verdict(steps, args.require)
    for line in lines:
        print(line)
    return 0 if acceptable else 1


if __name__ == "__main__":
    sys.exit(main())
