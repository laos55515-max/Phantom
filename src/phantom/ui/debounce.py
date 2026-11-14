"""Debounce helper for expensive UI-triggered estimates (D10).

Tk-free by construction: the scheduler/canceller are injected, so tests drive it
with a fake clock instead of a display.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final, Protocol

from phantom.core import constants as C

LOGGER: Final = logging.getLogger("phantom.debounce")


class Scheduler(Protocol):
    def __call__(self, delay_ms: int, callback: Callable[[], None]) -> str: ...


class Canceller(Protocol):
    def __call__(self, token: str) -> None: ...


class Debouncer:
    """Collapse a burst of calls into one delayed execution."""

    def __init__(
        self,
        scheduler: Scheduler,
        canceller: Canceller,
        *,
        delay_ms: int = C.DEBOUNCE_MS,
    ) -> None:
        self._schedule = scheduler
        self._cancel = canceller
        self._delay = max(0, int(delay_ms))
        self._token: str | None = None

    @property
    def delay_ms(self) -> int:
        return self._delay

    @property
    def pending(self) -> bool:
        return self._token is not None

    def schedule(self, callback: Callable[[], None]) -> None:
        self.abort()
        self._token = self._schedule(self._delay, lambda: self._fire(callback))

    def _fire(self, callback: Callable[[], None]) -> None:
        self._token = None
        callback()

    def abort(self) -> None:
        if self._token is not None:
            token, self._token = self._token, None
            try:
                self._cancel(token)
            except Exception as exc:  # noqa: BLE001 - the token may already have fired
                LOGGER.debug("debounce cancel ignored: %s", exc)
