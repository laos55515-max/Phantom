"""R1 enforcement: no stubs, no swallowed exceptions, no placeholder prose.

The scan is AST + token based and covers every Python file in the repository
(the test file itself is excluded only because it must contain the forbidden
tokens as data).
"""

from __future__ import annotations

import ast
import tokenize
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_DIRS = {
    ".venv",
    "build",
    "dist",
    "__pycache__",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}
SELF = Path(__file__).resolve()
FORBIDDEN_TEXT = ("todo", "fixme", "xxx", "not implemented", "недоступно", "аналогично")
FORBIDDEN_PHRASES = (
    "остальное без изменений",
    "rest of the code is unchanged",
    "аналогично предыдущему",
)
#: Placeholder words that must never appear in user-facing security claims.
CLAIM_WORDS = ("невидимо", "необнаружимо", "невзламываемо", "invisible", "undetectable", "unbreakable")


def python_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if path.resolve() == SELF:
            continue
        files.append(path)
    return sorted(files)


def is_protocol_class(node: ast.ClassDef) -> bool:
    for base in node.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if name in ("Protocol", "TypedDict"):
            return True
    return False


def collect_protocol_methods(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and is_protocol_class(node):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    ids.add(id(item))
    return ids


def is_ellipsis(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and node.value.value is Ellipsis
    )


def test_every_python_file_scanned() -> None:
    files = python_files()
    assert len(files) >= 20, f"scan found only {len(files)} files — check the walker"
    assert any("phantom/ui" in str(p) for p in files)


@pytest.mark.parametrize("path", python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_stub_constructs(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    protocol_methods = collect_protocol_methods(tree)

    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            func = node.exc.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in ("NotImplementedError",):
                problems.append(f"line {node.lineno}: raise NotImplementedError")
            if name == "RuntimeError":
                consts = [a.value for a in node.exc.args if isinstance(a, ast.Constant)]
                if any(isinstance(c, str) and "недоступ" in c.lower() for c in consts):
                    problems.append(f"line {node.lineno}: raise RuntimeError('недоступно')")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = [stmt for stmt in node.body if not isinstance(stmt, ast.Expr)]
            only_ellipsis = len(node.body) == 1 and is_ellipsis(node.body[0])
            only_pass = len(body) == 0 and len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
            if (only_ellipsis or only_pass) and id(node) not in protocol_methods:
                problems.append(f"line {node.lineno}: function {node.name} is a stub")
            if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                problems.append(f"line {node.lineno}: function {node.name} body is only 'pass'")
        if isinstance(node, ast.ExceptHandler):
            body = node.body
            if all(isinstance(stmt, ast.Pass) or is_ellipsis(stmt) for stmt in body):
                problems.append(
                    f"line {node.lineno}: except handler swallows the exception "
                    f"(type {getattr(node.type, 'id', '?')})"
                )
    assert not problems, f"{path}:\n" + "\n".join(problems)


@pytest.mark.parametrize("path", python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_forbidden_markers(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    lowered = source.lower()
    for marker in FORBIDDEN_TEXT:
        assert marker not in lowered, f"{path}: forbidden marker {marker!r}"
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in lowered, f"{path}: forbidden phrase {phrase!r}"


@pytest.mark.parametrize("path", python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_unfinished_placeholders(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if text.strip() in {"...", "…"}:
                pytest.fail(f"{path}:{node.lineno}: placeholder string literal")


def test_comments_are_not_hidden_todo_lists() -> None:
    offenders: list[str] = []
    for path in python_files():
        with path.open("rb") as handle:
            for token in tokenize.tokenize(handle.readline):
                if token.type == tokenize.COMMENT:
                    lowered = token.string.lower()
                    if any(marker in lowered for marker in ("todo", "fixme", "xxx")):
                        offenders.append(f"{path}:{token.start[0]}: {token.string}")
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize("path", python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_unbacked_security_claims(path: Path) -> None:
    source = path.read_text(encoding="utf-8").lower()
    found = [word for word in CLAIM_WORDS if word in source]
    assert not found, f"{path}: forbidden security claim words {found}"
