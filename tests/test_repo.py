"""Repository invariants: one version, pinned dependencies, a real CI job."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import phantom
from phantom.core import constants as C

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


def _pyproject() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def test_single_version_everywhere() -> None:
    version = _pyproject()["project"]["version"]
    assert version == phantom.__version__ == C.APP_VERSION


def test_requirements_match_pyproject() -> None:
    declared = set(_pyproject()["project"]["dependencies"])
    pinned = {
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert declared == pinned, (
        f"only in pyproject: {sorted(declared - pinned)}; only in requirements: {sorted(pinned - declared)}"
    )


def test_dev_requirements_cover_the_toolchain() -> None:
    text = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    for package in ("pytest==", "hypothesis==", "ruff==", "mypy==", "pytest-cov=="):
        assert package in text, f"requirements-dev.txt lacks {package}"


def test_pytest_configuration_is_strict() -> None:
    config = _pyproject()["tool"]["pytest"]["ini_options"]
    assert "--strict-markers" in config["addopts"]
    assert "--strict-config" in config["addopts"]
    markers = " ".join(config["markers"])
    assert "slow:" in markers and "gui:" in markers


def test_mypy_covers_the_whole_library() -> None:
    files = _pyproject()["tool"]["mypy"]["files"]
    for package in ("core", "stego", "audio", "i18n"):
        assert f"src/phantom/{package}" in files


def test_ci_runs_lint_types_tests_and_gui() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "ruff check src tests tools" in workflow
    assert "mypy src" in workflow
    assert 'python -m pytest -ra -m "not gui"' in workflow
    assert "xvfb-run" in workflow
    assert "libsndfile1" in workflow
    assert "python3-tk" in workflow


def test_ci_does_not_use_floating_python() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r'python-version: "3\.1[12]"', workflow)


def test_pyinstaller_spec_targets_the_package_layout() -> None:
    spec = (ROOT / "Phantom.spec").read_text(encoding="utf-8")
    assert "src" in spec
    assert "phantom/locales" in spec
    assert "app.py" in spec


def test_no_virtualenv_or_build_output_is_committed() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".venv/", "build/", "dist/", "__pycache__/"):
        assert pattern in ignore


def test_fixtures_are_small() -> None:
    total = 0
    for path in (ROOT / "tests" / "fixtures").rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    assert total < 2 * 1024 * 1024, f"fixtures weigh {total} bytes, too much for git"


def test_docs_exist() -> None:
    for name in ("README.md", "README.ru.md", "SECURITY.md", "CHANGELOG.md", "LICENSE"):
        path = ROOT / name
        assert path.exists(), f"missing {name}"
        assert path.stat().st_size > 200, f"{name} is suspiciously short"


def test_license_is_mit_everywhere() -> None:
    declared = _pyproject()["project"]["license"]["text"]
    licence = ROOT / "LICENSE"
    assert licence.exists(), "LICENSE is missing"
    text = licence.read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "WITHOUT WARRANTY OF ANY KIND" in text
    assert declared == "MIT"
    for readme in ("README.md", "README.ru.md"):
        body = (ROOT / readme).read_text(encoding="utf-8")
        assert "MIT" in body
        assert "LICENSE" in body


def test_every_python_file_compiles() -> None:
    import py_compile

    targets = [ROOT / "app.py"]
    targets += sorted((ROOT / "src").rglob("*.py"))
    targets += sorted((ROOT / "tools").rglob("*.py"))
    for path in targets:
        py_compile.compile(str(path), doraise=True)


def _workflow_steps() -> list[dict]:
    """Every step in ci.yml, without needing a YAML dependency at test time."""
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    steps: list[dict] = []
    current: dict | None = None
    body: list[str] = []
    def flush() -> None:
        if current is not None:
            current["run"] = "\n".join(body)
            steps.append(current)
    for line in text.splitlines():
        if re.match(r"^\s{6}- (name|uses|if|id|with|env|continue-on-error|timeout-minutes|run):", line):
            flush()
            current, body = {"header": line}, []
        elif current is not None:
            body.append(line)
    flush()
    return steps


def test_every_step_that_records_a_verdict_disables_errexit() -> None:
    """``run:`` is ``bash -e -o pipefail``: a failing command must not skip the record.

    Measured in run 44844048485: the GUI job's ``ci/status.tsv`` was missing entirely
    — the verdict exited 2 — because pytest failed and the shell stopped before the
    line that writes the row, and the ``gui-tk9`` job lost the rows of exactly the
    two steps that failed.  A recorder has to survive its own failure.
    """
    recorders = [step for step in _workflow_steps() if "ci/status.tsv" in step["run"]]
    assert len(recorders) >= 10, "the workflow no longer records step exit codes"
    for step in recorders:
        header = step["header"].strip()
        assert "set +e" in step["run"], f"{header} records a verdict without disabling errexit"


def test_every_job_that_records_codes_ends_with_the_verdict_step() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    jobs_text = text.split("\njobs:\n", 1)[1]
    jobs = [
        job
        for job in re.split(r"^  (?=[a-z][a-z0-9-]*:\s*$)", jobs_text, flags=re.MULTILINE)
        if job.strip()
    ]
    assert len(jobs) >= 6
    recording = [job for job in jobs if "ci/status.tsv" in job]
    # test, gui-macos, gui-tk9, layout-profile; lint and benchmark decide by themselves.
    assert len(recording) >= 4, "the jobs stopped recording their exit codes"
    for job in recording:
        name = job.split(":", 1)[0]
        assert "python tools/ci_verdict.py ci/status.tsv" in job, f"{name} has no verdict step"
        assert "--annotate" in job, f"{name}: the verdict table never reaches the PR"


def test_the_profile_gate_leaves_out_the_phase_that_never_returns() -> None:
    """Measured: ``-fullscreen True`` never comes back on the macOS runner.

    The job was green for months because the 150 s cap was caught and printed; the
    resize numbers are now measured without that phase, and the phase is probed on
    its own so a stall stays visible instead of swallowing the measurement.
    """
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "--skip-fullscreen" in text
    assert '"fullscreen", "src", "probe"' in text
    # The gate reads the SUMMARY line, and a process that needed killing after the
    # numbers still gets a row of its own.
    assert 'measured = "SUMMARY " in output' in text
    assert "exits cleanly" in text
