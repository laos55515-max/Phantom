"""Lightweight, cancellable UI animations (no infinite loops, no idle CPU).

The animator owns no Tk objects and schedules nothing itself: the view calls
:meth:`Animator.step` from an ``after`` callback *only while* ``active`` is true,
so an idle window burns no CPU.  Every tween is interruptible and every value is
clamped to its target.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from phantom.core import constants as C

EASINGS: Final[dict[str, Callable[[float], float]]] = {
    "linear": lambda t: t,
    "ease_out_cubic": lambda t: 1.0 - (1.0 - t) ** 3,
    "ease_in_out_quad": lambda t: 2 * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 2 / 2,
}


@dataclass(slots=True)
class Tween:
    key: str
    start: float
    target: float
    duration: float
    started_at: float
    easing: str
    on_update: Callable[[float], None] | None
    on_done: Callable[[], None] | None
    done: bool = False


class Animator:
    """Tracks numeric tweens and tells the view when to schedule the next tick."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        tick_ms: int = C.ANIMATION_TICK_MS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = enabled
        self.tick_ms = tick_ms
        self._clock = clock
        self._tweens: dict[str, Tween] = {}
        self._values: dict[str, float] = {}

    # -- queries -----------------------------------------------------------
    @property
    def active(self) -> bool:
        return any(not tween.done for tween in self._tweens.values())

    def value(self, key: str, default: float = 0.0) -> float:
        return self._values.get(key, default)

    def has(self, key: str) -> bool:
        return key in self._tweens

    # -- commands ----------------------------------------------------------
    def set(self, key: str, value: float, *, notify: Callable[[float], None] | None = None) -> None:
        """Jump to a value, dropping any tween for that key."""
        self._tweens.pop(key, None)
        self._values[key] = value
        if notify is not None:
            notify(value)

    def to(
        self,
        key: str,
        target: float,
        *,
        duration: float = 0.18,
        easing: str = "ease_out_cubic",
        on_update: Callable[[float], None] | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> bool:
        """Animate ``key`` towards ``target``; returns True if a tween was created."""
        if not self.enabled or duration <= 0:
            self.set(key, target, notify=on_update)
            if on_done is not None:
                on_done()
            return False
        if easing not in EASINGS:
            raise ValueError(f"unknown easing: {easing}")
        now = self._clock()
        start = self._values.get(key, target)
        tween = Tween(
            key=key,
            start=start,
            target=target,
            duration=duration,
            started_at=now,
            easing=easing,
            on_update=on_update,
            on_done=on_done,
        )
        self._tweens[key] = tween
        self._values[key] = start
        return True

    def cancel(self, key: str, *, snap_to_target: bool = False) -> None:
        tween = self._tweens.pop(key, None)
        if tween is None:
            return
        tween.done = True
        if snap_to_target:
            self._values[key] = tween.target
            if tween.on_update is not None:
                tween.on_update(tween.target)
        if tween.on_done is not None:
            tween.on_done()

    def cancel_all(self) -> None:
        for key in list(self._tweens):
            self.cancel(key)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = bool(enabled)
        if not enabled:
            for key in list(self._tweens):
                self.cancel(key, snap_to_target=True)

    # -- tick --------------------------------------------------------------
    def step(self) -> float | None:
        """Advance all tweens; return seconds until the next tick (None = idle)."""
        if not self._tweens:
            return None
        now = self._clock()
        finished: list[str] = []
        for key, tween in list(self._tweens.items()):
            if tween.done:
                finished.append(key)
                continue
            progress = 1.0 if tween.duration <= 0 else (now - tween.started_at) / tween.duration
            progress = min(1.0, max(0.0, progress))
            eased = EASINGS[tween.easing](progress)
            value = tween.start + (tween.target - tween.start) * eased
            self._values[key] = value
            if tween.on_update is not None:
                tween.on_update(value)
            if progress >= 1.0:
                tween.done = True
                self._values[key] = tween.target
                if tween.on_update is not None:
                    tween.on_update(tween.target)
                if tween.on_done is not None:
                    tween.on_done()
                finished.append(key)
        for key in finished:
            self._tweens.pop(key, None)
        return None if not self._tweens else self.tick_ms / 1000.0
