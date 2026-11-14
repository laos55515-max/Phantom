"""Password policy, progress plumbing and the small API helpers."""

from __future__ import annotations

import threading

import pytest

from phantom.core import constants as C
from phantom.core.errors import OperationCancelled
from phantom.core.progress import (
    INDETERMINATE_STAGES,
    ProgressEvent,
    Stage,
    check_cancel,
    check_cancel_and_report,
    noop_progress,
)
from phantom.core.security import (
    ALPHABET,
    DIGITS,
    LOWER,
    SYMBOLS,
    UPPER,
    PasswordPolicy,
    generate_password,
    password_issue,
)


# ---------------------------------------------------------------------------
# security.py
# ---------------------------------------------------------------------------
def test_generated_password_satisfies_every_class() -> None:
    password = generate_password()
    assert len(password) == C.GENERATED_PASSWORD_LENGTH
    assert any(c in UPPER for c in password)
    assert any(c in LOWER for c in password)
    assert any(c in DIGITS for c in password)
    assert any(c in SYMBOLS for c in password)
    assert set(password) <= set(ALPHABET)
    assert password_issue(password) is None


def test_generated_passwords_differ() -> None:
    assert len({generate_password() for _ in range(50)}) == 50


def test_generate_respects_custom_length() -> None:
    assert len(generate_password(8)) == 8
    assert len(generate_password(64)) == 64


def test_generate_without_symbol_requirement() -> None:
    """``require_symbol=False`` relaxes the policy; it does not forbid symbols."""
    policy = PasswordPolicy(require_symbol=False)
    password = generate_password(20, policy)
    assert len(password) == 20
    assert password_issue(password, policy) is None
    assert any(c in UPPER for c in password) and any(c in LOWER for c in password)
    assert any(c in DIGITS for c in password)


def test_generate_rejects_impossible_lengths() -> None:
    with pytest.raises(ValueError):
        generate_password(3)
    with pytest.raises(ValueError):
        generate_password(3, PasswordPolicy(require_symbol=True))


def test_policy_defaults_come_from_constants() -> None:
    policy = PasswordPolicy()
    assert policy.minimum_length == C.MIN_PASSWORD_LENGTH
    assert (policy.require_mixed_case, policy.require_digit, policy.require_symbol) == (
        True,
        True,
        True,
    )


@pytest.mark.parametrize(
    "password,expected",
    [
        ("short", "too_short"),
        ("alllowercasepassword1!", "missing_case"),
        ("ALLUPPERCASEPASSWORD1!", "missing_case"),
        ("MixedCasePassword!", "missing_digit"),
        ("MixedCasePassword1", "missing_symbol"),
    ],
)
def test_password_issue_reports_the_first_problem(password: str, expected: str) -> None:
    assert password_issue(password) == expected


def test_password_issue_accepts_a_strong_password() -> None:
    assert password_issue("Correct-Horse-9-Battery!") is None


def test_relaxed_policy_accepts_weaker_passwords() -> None:
    relaxed = PasswordPolicy(
        minimum_length=4, require_mixed_case=False, require_digit=False, require_symbol=False
    )
    assert password_issue("abcd", relaxed) is None
    assert password_issue("abc", relaxed) == "too_short"


# ---------------------------------------------------------------------------
# progress.py
# ---------------------------------------------------------------------------
def test_progress_event_fraction_is_clamped() -> None:
    assert ProgressEvent(Stage.READING, 1, 4).fraction == pytest.approx(0.25)
    assert ProgressEvent(Stage.READING, 9, 4).fraction == 1.0
    assert ProgressEvent(Stage.READING, -3, 4).fraction == 0.0
    assert ProgressEvent(Stage.KDF, 0, 0).fraction == 0.0
    assert ProgressEvent(Stage.KDF, 5, 0).indeterminate is True
    assert ProgressEvent(Stage.READING, 5, 10).indeterminate is False


def test_only_the_kdf_is_indeterminate() -> None:
    assert frozenset(INDETERMINATE_STAGES) == frozenset({Stage.KDF})


def test_check_cancel_without_event_is_a_noop() -> None:
    check_cancel(None)


def test_check_cancel_raises_when_set() -> None:
    event = threading.Event()
    event.set()
    with pytest.raises(OperationCancelled):
        check_cancel(event)


def test_check_cancel_does_not_report_after_cancel() -> None:
    event = threading.Event()
    event.set()
    seen: list[tuple[Stage, int, int]] = []
    with pytest.raises(OperationCancelled):
        check_cancel_and_report(event, lambda *args: seen.append(args), Stage.READING, 1, 2)
    assert seen == []


def test_check_cancel_and_report_forwards() -> None:
    seen: list[tuple[Stage, int, int]] = []
    check_cancel_and_report(None, lambda *args: seen.append(args), Stage.EMBEDDING, 3, 7)
    assert seen == [(Stage.EMBEDDING, 3, 7)]


def test_noop_progress_accepts_anything() -> None:
    noop_progress(Stage.WIPING, 0, 0)


def test_every_stage_value_is_a_locale_suffix() -> None:
    for stage in Stage:
        assert stage.value.islower()
        assert "." not in stage.value
