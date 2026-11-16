"""Password policy, progress plumbing and the small API helpers."""

from __future__ import annotations

import threading

import pytest

from phantom.core import constants as C
from phantom.core import kdf, security
from phantom.core.errors import OperationCancelled
from phantom.core.kdf import MessageKeys
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


def test_only_the_opaque_passes_are_indeterminate() -> None:
    """A fraction is reported everywhere it exists; these two passes have none.

    The Argon2id run and the wavelet cost map are each one opaque sweep over their
    input, so the interface shows a spinner and the elapsed time instead of a
    progress bar that would have to invent a percentage.
    """
    assert frozenset(INDETERMINATE_STAGES) == frozenset({Stage.KDF, Stage.ANALYSING})


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


def test_secure_zeroize_overwrites_the_buffer() -> None:
    buffer = bytearray(b"correct horse battery staple")
    security.secure_zeroize(buffer)
    assert bytes(buffer) == b"\x00" * 28
    assert len(buffer) == 28, "the buffer keeps its length, the content is gone"


def test_secure_zeroize_keeps_the_object_identity() -> None:
    """Callers hold references; wiping must change them, not replace them."""
    buffer = bytearray(b"secret")
    identity = id(buffer)
    security.secure_zeroize(buffer)
    assert id(buffer) == identity


def test_secure_zeroize_accepts_an_empty_buffer() -> None:
    buffer = bytearray()
    security.secure_zeroize(buffer)
    assert bytes(buffer) == b""


@pytest.mark.parametrize("value", [b"bytes are immutable", "a str", memoryview(b"mv"), None, 42])
def test_secure_zeroize_refuses_what_it_cannot_wipe(value: object) -> None:
    with pytest.raises(TypeError):
        security.secure_zeroize(value)  # type: ignore[arg-type]


def test_kdf_wipes_through_secure_zeroize(monkeypatch: pytest.MonkeyPatch) -> None:
    """One implementation: the KDF wipe has to be the checked one."""
    seen: list[bytearray] = []

    def spy(buffer: bytearray) -> None:
        seen.append(buffer)
        security.secure_zeroize(buffer)

    monkeypatch.setattr(kdf, "secure_zeroize", spy)
    keys = bytearray(b"key material")
    kdf.wipe_all(keys)
    assert seen and bytes(keys) == b"\x00" * len(keys)


def test_message_keys_wipe_zeroes_every_field() -> None:
    keys = MessageKeys(
        length_key=bytearray(b"a" * 32),
        payload_key=bytearray(b"b" * 32),
        payload_nonce=bytearray(b"c" * 12),
        position_key=bytearray(b"d" * 32),
    )
    keys.wipe()
    for buffer in (keys.length_key, keys.payload_key, keys.payload_nonce, keys.position_key):
        assert bytes(buffer) == b"\x00" * len(buffer)
