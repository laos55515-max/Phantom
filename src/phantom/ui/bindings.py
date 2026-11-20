"""Widget ↔ locale binding registry (D14).

Instead of hand-calling ``configure(text=...)`` on fifty widgets after every
language switch, widgets are registered once with the key they display.  A
single :meth:`I18nBinder.apply` refreshes the whole window.

Tk is not imported here: any object with ``configure``/``cget`` works, which
keeps the registry unit-testable without a display.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from phantom.i18n import I18n

LOGGER: Final = logging.getLogger("phantom.bindings")


@dataclass(slots=True)
class _Binding:
    widget: Any
    key: str
    attribute: str
    params: dict[str, Any] = field(default_factory=dict)
    provider: Callable[[], Mapping[str, Any]] | None = None


class I18nBinder:
    """Registry of (widget, attribute, locale key) triples."""

    def __init__(self, i18n: I18n) -> None:
        self._i18n = i18n
        self._bindings: list[_Binding] = []

    @property
    def i18n(self) -> I18n:
        return self._i18n

    @property
    def count(self) -> int:
        return len(self._bindings)

    def bind(self, widget: Any, key: str, *, attribute: str = "text", **params: object) -> Any:
        self._bindings.append(
            _Binding(widget=widget, key=key, attribute=attribute, params=dict(params))
        )
        self._apply_one(self._bindings[-1])
        return widget

    def bind_map(
        self,
        widget: Any,
        key: str,
        params: Mapping[str, object],
        *,
        attribute: str = "text",
    ) -> Any:
        """Bind with a pre-built mapping (typed dict, not ``**kwargs``)."""
        binding = _Binding(
            widget=widget, key=key, attribute=attribute, params=dict(params)
        )
        self._bindings.append(binding)
        self._apply_one(binding)
        return widget

    def bind_dynamic(
        self,
        widget: Any,
        key: str,
        provider: Callable[[], Mapping[str, Any]],
        *,
        attribute: str = "text",
    ) -> Any:
        binding = _Binding(
            widget=widget, key=key, attribute=attribute, params={}, provider=provider
        )
        self._bindings.append(binding)
        self._apply_one(binding)
        return widget

    def update_params(self, widget: Any, /, **params: Any) -> None:
        for binding in self._bindings:
            if binding.widget is widget:
                binding.params.update(params)
                self._apply_one(binding)

    def apply(self) -> int:
        """Re-render every binding; returns how many widgets were updated."""
        applied = 0
        for binding in self._bindings:
            if self._apply_one(binding):
                applied += 1
        return applied

    def key_of(self, widget: Any, attribute: str = "text") -> str | None:
        for binding in self._bindings:
            if binding.widget is widget and binding.attribute == attribute:
                return binding.key
        return None

    def forget(self, widget: Any) -> None:
        self._bindings = [b for b in self._bindings if b.widget is not widget]

    def _apply_one(self, binding: _Binding) -> bool:
        params: dict[str, Any] = dict(binding.params)
        if binding.provider is not None:
            try:
                params.update(binding.provider())
            except Exception as exc:  # noqa: BLE001 - a provider must never break a language switch
                LOGGER.warning("binding provider for %s failed: %s", binding.key, exc)
                return False
        text = self._i18n.t(binding.key, **params)
        try:
            target = getattr(binding.widget, "configure", None)
            if callable(target):
                target(**{binding.attribute: text})
            else:
                setattr(binding.widget, binding.attribute, text)
        except Exception as exc:  # noqa: BLE001 - a single widget must not break the switch
            LOGGER.warning(
                "cannot set %s=%r on %r: %s", binding.attribute, text, binding.widget, exc
            )
            return False
        # A widget asked to re-measure itself after a caption change (a button
        # whose width follows its localised text).  Duck-typed on purpose: this
        # module knows nothing about Tk.
        after = getattr(binding.widget, "_phantom_after_text", None)
        if callable(after):
            try:
                after()
            except Exception as exc:  # noqa: BLE001 - measuring must not break a switch
                LOGGER.warning("after-text hook for %s failed: %s", binding.key, exc)
        return True

    def text(self, key: str, **params: Any) -> str:
        return self._i18n.t(key, **params)
