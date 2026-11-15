"""Password generation, policy and buffer hygiene (no UI, no globals)."""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from typing import Final

from phantom.core import constants as C

UPPER: Final = string.ascii_uppercase
LOWER: Final = string.ascii_lowercase
DIGITS: Final = string.digits
SYMBOLS: Final = "!@#$%^&*()-_=+[]{};:,.<>?/~|"
ALPHABET: Final = UPPER + LOWER + DIGITS + SYMBOLS


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    minimum_length: int = C.MIN_PASSWORD_LENGTH
    require_mixed_case: bool = True
    require_digit: bool = True
    require_symbol: bool = True


def secure_zeroize(buffer: bytearray) -> None:
    """Overwrite a secret buffer in place, and refuse anything that cannot be.

    Python cannot wipe an immutable ``str`` or ``bytes``: the text the user typed
    lives in the interpreter until it is collected, and a copy may hide in any
    object the code built.  This helper is for the buffers the code *does* own —
    key material and the encoded password — and it raises instead of pretending
    when handed something immutable, so ``secure_zeroize(password_str)`` cannot
    look like it worked.
    """
    if not isinstance(buffer, bytearray):
        raise TypeError("secure_zeroize accepts a bytearray, not str or bytes")
    for index in range(len(buffer)):
        buffer[index] = 0


def generate_password(
    length: int = C.GENERATED_PASSWORD_LENGTH,
    policy: PasswordPolicy | None = None,
) -> str:
    """CSPRNG password that satisfies every class requirement by construction."""
    policy = policy or PasswordPolicy()
    if length < 4:
        raise ValueError("password length must be >= 4")
    pools = [UPPER, LOWER, DIGITS]
    if policy.require_symbol:
        pools.append(SYMBOLS)
    chars = [secrets.choice(pool) for pool in pools]
    chars.extend(secrets.choice(ALPHABET) for _ in range(length - len(pools)))
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def password_issue(password: str, policy: PasswordPolicy | None = None) -> str | None:
    """Return an i18n-neutral reason code, or ``None`` when acceptable."""
    policy = policy or PasswordPolicy()
    if len(password) < policy.minimum_length:
        return "too_short"
    if policy.require_mixed_case and not (
        any(c in UPPER for c in password) and any(c in LOWER for c in password)
    ):
        return "missing_case"
    if policy.require_digit and not any(c in DIGITS for c in password):
        return "missing_digit"
    if policy.require_symbol and not any(c in SYMBOLS for c in password):
        return "missing_symbol"
    return None
