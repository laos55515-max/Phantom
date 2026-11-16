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
from phantom.core.api import CarrierKind
from phantom.core.payload import ExtractedPayload, carried_bytes
from phantom.core.progress import Stage
from phantom.stego.engine import Capacity


class Translator(Protocol):
    """The slice of :class:`phantom.i18n.I18n` used here."""

    def t(self, key: str, **params: object) -> str: ...


#: Locale key per layout version this build can read and write.  ``C.CONTAINER_VERSION``
#: is deliberately absent: it *is* one of the four, and listing it twice would
#: silently drop a label, so the mapping names layouts rather than "current".
LAYOUT_KEYS: Final[dict[int, str]] = {
    C.KEYED_CONTAINER_VERSION: "layout_keyed",
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
    """Human label such as ``v5 · syndrome-trellis positions`` for one version."""
    return translator.t(layout_key(version), version=version)


#: Layout actually written for an audio carrier.  The syndrome-trellis code needs
#: an image's wavelet cost map, so WAV/FLAC output falls back to the keyed layout —
#: telling the user "v5" for an audio carrier would be a promise the writer never
#: keeps.
AUDIO_LAYOUT_VERSION: Final = C.KEYED_CONTAINER_VERSION


def written_layout_version(kind: CarrierKind | None) -> int:
    """Layout version this build writes for ``kind`` (``None`` = not chosen yet)."""
    if kind is CarrierKind.AUDIO:
        return AUDIO_LAYOUT_VERSION
    return C.CONTAINER_VERSION


def written_layout_text(translator: Translator, kind: CarrierKind | None = None) -> str:
    """Label for the layout this build writes, e.g. ``v5 · syndrome-trellis``.

    Audio carriers get v3 because that is what they really get; an image
    carrier — or no carrier at all yet — gets the v5 default.  (A *transparent*
    PNG falls back the same way, but only the writer knows that for certain.)
    """
    return layout_text(translator, written_layout_version(kind))


def stage_line(
    translator: Translator,
    key: str,
    *,
    stage: Stage | None = None,
    pct: str | None = None,
    seconds: int | None = None,
) -> str:
    """One footer line: the stage text, with the elapsed time while a job runs.

    ``Встраивание  ·  25%`` becomes ``Встраивание  ·  25% · 12 с``.  A phase with no
    honest fraction says so instead (``Анализ изображения…``), and the clock is the
    same either way — half a minute of silence reads as a hang, a number that
    keeps counting does not.

    The stage is passed as the enum, not as finished text: the footer repaints
    itself while an operation runs (once a second for the clock, and again if the
    language changes), so the name has to be translated at paint time rather than
    frozen when the event arrived.

    Kept here, free of Tk, so the wording is testable without a display.
    """
    params: dict[str, str] = {}
    if stage is not None:
        params["stage"] = translator.t(f"stage.{stage.value}")
    if pct is not None:
        params["pct"] = pct
    text = translator.t(key, **params)
    if seconds is None:
        return text
    return translator.t("lbl_progress_elapsed", progress=text, seconds=seconds)


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
