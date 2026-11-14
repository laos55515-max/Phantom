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
    for name in ("README.md", "README.ru.md", "SECURITY.md", "CHANGELOG.md"):
        path = ROOT / name
        assert path.exists(), f"missing {name}"
        assert path.stat().st_size > 200, f"{name} is suspiciously short"


def test_license_decision_is_recorded() -> None:
    """Until the maintainer picks a licence, the repository must say so."""
    declared = _pyproject()["project"]["license"]["text"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert declared
    assert "## License" in readme
    assert declared.lower() in readme.lower() or "unlicensed" in readme.lower()


def test_every_python_file_compiles() -> None:
    import py_compile

    targets = [ROOT / "app.py"]
    targets += sorted((ROOT / "src").rglob("*.py"))
    targets += sorted((ROOT / "tools").rglob("*.py"))
    for path in targets:
        py_compile.compile(str(path), doraise=True)
