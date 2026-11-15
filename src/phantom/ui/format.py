"""Names for the container layout shown in the interface — free of Tk.

The layout version is authenticated *inside* the sealed header, so the extract
tab can only name it after a payload was opened successfully; the embed tab
shows what this build is about to write.  Keeping the mapping in a plain module
means the wording is unit-testable without a display, and every number comes
from :mod:`phantom.core.constants` instead of a locale file.
"""

from __future__ import annotations

from typing import Final, Protocol

from phantom.core import constants as C
from phantom.core.payload import ExtractedPayload, carried_bytes
from phantom.stego.engine import Capacity


class Translator(Protocol):
    """The slice of :class:`phantom.i18n.I18n` used here."""

    def t(self, key: str, **params: object) -> str: ...


#: Locale key per layout version this build can read.
LAYOUT_KEYS: Final[dict[int, str]] = {
    C.CONTAINER_VERSION: "layout_current",
    C.ADAPTIVE_CONTAINER_VERSION: "layout_adaptive",
    C.STC_CONTAINER_VERSION: "layout_trellis",
    C.V2_CONTAINER_VERSION: "layout_legacy",
}
#: Key used for a version this build has never heard of (or no payload yet).
UNKNOWN_LAYOUT_KEY: Final = "layout_unknown"


def layout_key(version: int) -> str:
    """Locale key describing ``version``; unknown versions get a neutral one."""
    return LAYOUT_KEYS.get(version, UNKNOWN_LAYOUT_KEY)


def layout_text(translator: Translator, version: int) -> str:
    """Human label such as ``v3 · keyed positions`` for one layout version."""
    return translator.t(layout_key(version), version=version)


def written_layout_text(translator: Translator) -> str:
    """Label for the layout this build writes, which is always the current one."""
    return layout_text(translator, C.CONTAINER_VERSION)


def size_text(
    payload: ExtractedPayload,
    capacity: Capacity | None,
    translator: Translator,
) -> str:
    """``284 B → 306 B · 12 % of the carrier`` for the audit card.

    Without a probed carrier (or a carrier too small to hold anything) the share
    is left out instead of being invented.
    """
    text = f"{payload.original_size} B → {payload.compressed_size} B"
    if capacity is None or capacity.usable_bits <= 0:
        return text
    share = capacity.carrier_share(carried_bytes(payload))
    return f"{text} · {translator.t('lbl_capacity_share', pct=f'{share:.0f}')}"
