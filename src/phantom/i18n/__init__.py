"""Localisation: JSON locales, English fallback, ``{placeholder}`` formatting.

Adding a language = adding ``src/phantom/locales/<code>.json``; no code changes.
Tests enforce that every locale has the same key set and the same placeholders.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

DEFAULT_LANGUAGE: Final = "en"
LOCALES_DIR: Final = Path(__file__).resolve().parent.parent / "locales"
_PLACEHOLDER: Final = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class LocaleError(RuntimeError):
    """Raised when locale files are missing or structurally invalid."""


def available_languages(locales_dir: Path | None = None) -> list[str]:
    directory = Path(locales_dir) if locales_dir is not None else LOCALES_DIR
    if not directory.is_dir():
        raise LocaleError(f"locale directory not found: {directory}")
    return sorted(path.stem for path in directory.glob("*.json"))


def load_locale(code: str, locales_dir: Path | None = None) -> dict[str, str]:
    directory = Path(locales_dir) if locales_dir is not None else LOCALES_DIR
    path = directory / f"{code}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LocaleError(f"locale file missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise LocaleError(f"locale {code} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise LocaleError(f"locale {code} must be a JSON object")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise LocaleError(f"locale {code}: non-string entry {key!r}")
        result[key] = value
    return result


def placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall(template))


class _KeepMissing(dict[str, object]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class I18n:
    """Runtime translator with cached locale dictionaries."""

    def __init__(
        self,
        language: str = DEFAULT_LANGUAGE,
        *,
        default_language: str = DEFAULT_LANGUAGE,
        locales_dir: Path | None = None,
    ) -> None:
        self._dir = Path(locales_dir) if locales_dir is not None else LOCALES_DIR
        self._codes = available_languages(self._dir)
        if not self._codes:
            raise LocaleError("no locale files found")
        self._default = default_language if default_language in self._codes else self._codes[0]
        self._cache: dict[str, dict[str, str]] = {}
        self._language = language if language in self._codes else self._default
        self._load(self._language)
        self._load(self._default)
        self._listeners: list[object] = []

    # -- introspection -----------------------------------------------------
    @property
    def language(self) -> str:
        return self._language

    @property
    def default_language(self) -> str:
        return self._default

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(self._codes)

    def language_name(self, code: str | None = None) -> str:
        code = code or self._language
        return self.t(f"lang.{code}")

    def has(self, key: str) -> bool:
        return key in self._load(self._language)

    def keys(self, code: str | None = None) -> frozenset[str]:
        return frozenset(self._load(code or self._language))

    # -- translation -------------------------------------------------------
    def set_language(self, code: str) -> bool:
        """Switch language; returns False when the code is unknown."""
        if code not in self._codes:
            return False
        changed = code != self._language
        self._language = code
        self._load(code)
        return changed

    def t(self, key: str, **params: object) -> str:
        template = self._lookup(key)
        if not params:
            return template
        try:
            return template.format_map(_KeepMissing(params))
        except (IndexError, ValueError):
            return template

    def _lookup(self, key: str) -> str:
        primary = self._load(self._language)
        if key in primary:
            return primary[key]
        fallback = self._load(self._default)
        if key in fallback:
            return fallback[key]
        return key

    def _load(self, code: str) -> dict[str, str]:
        cached = self._cache.get(code)
        if cached is None:
            cached = load_locale(code, self._dir)
            self._cache[code] = cached
        return cached

    # -- diagnostics -------------------------------------------------------
    def missing_keys(self, code: str | None = None) -> Iterable[str]:
        """Keys present in the default locale but absent from ``code``."""
        reference = self._load(self._default)
        target = self._load(code or self._language)
        return sorted(set(reference) - set(target))

    def extra_keys(self, code: str | None = None) -> Iterable[str]:
        reference = self._load(self._default)
        target = self._load(code or self._language)
        return sorted(set(target) - set(reference))

    def placeholder_mismatches(self, code: str | None = None) -> Mapping[str, tuple[str, str]]:
        """Keys whose placeholders differ from the default locale."""
        code = code or self._language
        reference = self._load(self._default)
        target = self._load(code)
        mismatches: dict[str, tuple[str, str]] = {}
        for key, template in reference.items():
            other = target.get(key)
            if other is None:
                continue
            if placeholders(template) != placeholders(other):
                mismatches[key] = (template, other)
        return mismatches
