"""Follow the operating-system appearance in "System" theme mode (D14).

CustomTkinter tracks the OS theme on its own, but only while it owns the
"system" appearance mode; this watcher adds an explicit, testable fallback and
lets the app react *without a restart*.  It is a cheap 2 s timer, not an
animation loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

LOGGER: Final = logging.getLogger("phantom.theme")


def _detect_darkdetect() -> str | None:
    try:
        import darkdetect  # noqa: PLC0415
    except ImportError:
        return None
    try:
        detected = darkdetect.theme()
    except Exception as exc:  # noqa: BLE001 - pragma: no cover, platform dependent
        LOGGER.warning("darkdetect failed: %s", exc)
        return None
    if detected in ("Dark", "Light"):
        return str(detected)
    return None


class SystemThemeWatcher:
    """Converts OS theme changes into update calls for the Tk layer."""

    def __init__(
        self,
        *,
        interval_ms: int = 2000,
        detect: Callable[[], str | None] = _detect_darkdetect,
    ) -> None:
        self.interval_ms = interval_ms
        self._detect = detect
        self._last: str | None = None
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self._last = None

    def poll(self) -> str | None:
        """Return the new theme name when the OS theme changed, else None."""
        if not self._enabled:
            return None
        detected = self._detect()
        if detected is None:
            return None
        if self._last is None:
            self._last = detected
            return None
        if detected != self._last:
            self._last = detected
            return detected
        return None
