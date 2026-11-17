"""Persisted UI preferences (D14): language, theme, animations, last folder.

Secrets are never stored here.  The file lives in the OS config directory:
macOS ``~/Library/Application Support/Phantom``, Windows ``%APPDATA%``, Linux
``~/.config/phantom``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from platformdirs import user_config_dir

from phantom.core import constants as C

APP_DIR_NAME: Final = "Phantom"
SETTINGS_FILENAME: Final = "settings.json"
VALID_THEMES: Final = ("Dark", "Light", "System")
DEFAULT_LANGUAGE_CODE: Final = "en"


def config_dir() -> Path:
    override = os.environ.get("PHANTOM_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(user_config_dir(APP_DIR_NAME, appauthor=False))


@dataclass(slots=True)
class AppSettings:
    """Everything the app remembers between launches."""

    schema_version: int = C.SETTINGS_SCHEMA_VERSION
    language: str = DEFAULT_LANGUAGE_CODE
    theme: str = "System"
    animations: bool = True
    last_dir: str = ""
    window_geometry: str = ""
    last_tab: str = "embed"
    verify_after_embed: bool = True

    def normalized(self) -> AppSettings:
        language = self.language if isinstance(self.language, str) else DEFAULT_LANGUAGE_CODE
        theme = self.theme if self.theme in VALID_THEMES else "System"
        valid_tabs = ("embed", "extract", "metadata", "settings")
        last_tab = self.last_tab if self.last_tab in valid_tabs else "embed"
        return replace(
            self,
            language=language,
            theme=theme,
            animations=bool(self.animations),
            last_dir=self.last_dir if isinstance(self.last_dir, str) else "",
            window_geometry=self.window_geometry if isinstance(self.window_geometry, str) else "",
            last_tab=last_tab,
            verify_after_embed=bool(self.verify_after_embed),
            schema_version=C.SETTINGS_SCHEMA_VERSION,
        )


@dataclass(slots=True)
class SettingsStore:
    """JSON-backed settings store with corrupt-file tolerance."""

    path: Path = field(default_factory=lambda: config_dir() / SETTINGS_FILENAME)

    def load(self) -> AppSettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return AppSettings()
        if not isinstance(raw, dict):
            return AppSettings()
        allowed = set(AppSettings.__slots__)
        payload: dict[str, Any] = {k: v for k, v in raw.items() if k in allowed}
        try:
            return AppSettings(**payload).normalized()
        except TypeError:
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        settings = settings.normalized()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(asdict(settings), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def reset(self) -> AppSettings:
        settings = AppSettings()
        self.save(settings)
        return settings
