"""The capacity panel's numbers and words, without Tk.

Two rules shape everything here:

* every number comes from the existing capacity and estimate APIs
  (:class:`phantom.stego.engine.Capacity` and
  :class:`phantom.core.payload.SizeEstimate`) — this module divides what they
  already computed and never re-derives a limit of its own;
* the *preliminary* estimate is never dressed up as the engine's final check.
  The engine still measures the real container at embed time; the panel says
  "~", names the unit and can only be trusted as an approximation.

The scale is stated once: ``payload_bytes`` and the estimate are measured on the
same "prepared payload" scale (header + name + SHA-256 + length + compressed
body), while :meth:`Capacity.user_bytes` is the raw user-data scale.  Mixing the
two is the classic way to show a payload that "does not fit" in a carrier that
holds it, so the panel keeps the prepared scale and shows the raw one only as a
separate, clearly named figure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from enum import Enum, unique
from typing import Final

from phantom.core.api import CarrierProbe
from phantom.core.payload import SizeEstimate
from phantom.ui.format import Translator

KIB: Final = 1024.0


@unique
class CapacityState(Enum):
    """What the panel has to say, in the order the user meets it."""

    NO_CARRIER = "no_carrier"
    NO_PAYLOAD = "no_payload"
    CALCULATING = "calculating"
    FITS = "fits"
    OVERFLOW = "overflow"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CapacityReport:
    """One consistent snapshot for the panel, the fill bar and the embed button."""

    state: CapacityState
    chars: int = 0
    utf8_bytes: int = 0
    prepared_bytes: int | None = None
    capacity_bytes: int = 0
    usable_bytes: int = 0
    free_bytes: int | None = None
    excess_bytes: int = 0
    percent: float = 0.0
    fill: float = 0.0
    on_disk_bytes: int | None = None
    detail: str = ""

    @property
    def can_embed(self) -> bool:
        """Only a fresh estimate that fits unlocks the button."""
        return self.state is CapacityState.FITS

    @property
    def settled(self) -> bool:
        """False while the numbers on screen belong to older text."""
        return self.state not in (CapacityState.CALCULATING,)


def human_bytes(value: int | None) -> str:
    """Binary units, because the capacity is counted in bytes of payload."""
    if value is None:
        return "—"
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / KIB:.1f} KiB"
    return f"{value / (KIB * KIB):.2f} MiB"


def _on_disk(path: str) -> int | None:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def build_report(
    *,
    carrier: CarrierProbe | None,
    estimate: SizeEstimate | None,
    has_payload: bool,
    chars: int,
    utf8_bytes: int,
    name: str = "",
    waiting: bool = False,
    failed: bool = False,
) -> CapacityReport:
    """Assemble the panel from what the view model already knows.

    ``waiting`` means "the text changed and no estimate for it has landed yet":
    the old numbers are dropped instead of being shown as if they still applied.
    ``failed`` means the last estimate raised, so the state says so rather than
    leaving the last good number up forever.
    """
    if carrier is None:
        return CapacityReport(
            state=CapacityState.NO_CARRIER,
            chars=chars,
            utf8_bytes=utf8_bytes,
        )
    capacity = carrier.capacity.payload_bytes
    usable = carrier.capacity.user_bytes(name)
    base = CapacityReport(
        state=CapacityState.NO_PAYLOAD,
        chars=chars,
        utf8_bytes=utf8_bytes,
        capacity_bytes=capacity,
        usable_bytes=usable,
        on_disk_bytes=_on_disk(carrier.path),
        detail=carrier.description,
    )
    if failed:
        return replace(base, state=CapacityState.ERROR)
    if not has_payload:
        return base
    if waiting or estimate is None:
        return replace(base, state=CapacityState.CALCULATING)
    prepared = estimate.payload_bytes
    over = estimate.exceeded_capacity or prepared > capacity
    excess = max(0, prepared - capacity)
    ratio = carrier.capacity.fill_ratio(prepared)
    return CapacityReport(
        state=CapacityState.OVERFLOW if over else CapacityState.FITS,
        chars=chars,
        utf8_bytes=utf8_bytes,
        prepared_bytes=prepared,
        capacity_bytes=capacity,
        usable_bytes=usable,
        free_bytes=max(0, capacity - prepared),
        excess_bytes=excess,
        percent=max(0.0, 100.0 * ratio),
        fill=min(1.0, max(0.0, ratio)),
        on_disk_bytes=_on_disk(carrier.path),
        detail=carrier.description,
    )


def _format_name(path: str) -> str:
    if not path:
        return "—"
    extension = os.path.splitext(path)[1].lstrip(".").upper()
    return f"{os.path.basename(path)} · {extension}" if extension else os.path.basename(path)


def container_line(report: CapacityReport, probe: CarrierProbe | None, t: Translator) -> str:
    """Name, format, on-disk size and the resolution/duration that matters."""
    if probe is None:
        return t.t("cap_no_carrier")
    name = _format_name(probe.path)
    if probe.image is not None:
        shape = t.t(
            "cap_image_shape",
            width=probe.image.width,
            height=probe.image.height,
            mode=probe.image.mode,
        )
    elif probe.audio is not None:
        shape = t.t(
            "cap_audio_shape",
            seconds=f"{probe.audio.duration_seconds:.1f}",
            rate=probe.audio.sample_rate,
            bits=probe.audio.bits,
            channels=probe.audio.channels,
        )
    else:
        shape = probe.description
    return t.t(
        "cap_container",
        name=name,
        shape=shape,
        disk=human_bytes(report.on_disk_bytes),
    )


def payload_line(report: CapacityReport, t: Translator) -> str:
    """Characters, UTF-8 bytes and the prepared size the estimate measured.

    A file payload has no character count, so it gets the file wording instead of a
    row of zeroes that would look like an empty message.
    """
    if report.chars == 0:
        if report.prepared_bytes is None:
            return t.t("cap_payload_file_waiting", bytes=human_bytes(report.utf8_bytes))
        return t.t(
            "cap_payload_file",
            bytes=human_bytes(report.utf8_bytes),
            prepared=human_bytes(report.prepared_bytes),
        )
    if report.prepared_bytes is None:
        return t.t(
            "cap_payload_waiting",
            chars=report.chars,
            bytes=human_bytes(report.utf8_bytes),
        )
    return t.t(
        "cap_payload",
        chars=report.chars,
        bytes=human_bytes(report.utf8_bytes),
        prepared=human_bytes(report.prepared_bytes),
    )


def capacity_line(report: CapacityReport, t: Translator) -> str:
    """Available, occupied, free and the percentage — one unit scale."""
    if not report.settled:
        return t.t("cap_capacity_waiting", available=human_bytes(report.capacity_bytes))
    if report.state is CapacityState.NO_CARRIER:
        return t.t("cap_no_carrier")
    return t.t(
        "cap_capacity",
        available=human_bytes(report.capacity_bytes),
        occupied=human_bytes(report.prepared_bytes or 0),
        free=human_bytes(report.free_bytes or 0),
        pct=f"{report.percent:.1f}",
    )


def panel_lines(
    report: CapacityReport, probe: CarrierProbe | None, t: Translator
) -> tuple[str, ...]:
    """The four lines the panel shows; a fifth only when something is wrong."""
    if report.state is CapacityState.NO_CARRIER:
        return (t.t("cap_no_carrier"),)
    lines = [
        container_line(report, probe, t),
        payload_line(report, t),
        capacity_line(report, t),
    ]
    if report.state is CapacityState.OVERFLOW:
        lines.append(
            t.t(
                "cap_over",
                excess=human_bytes(report.excess_bytes),
                available=human_bytes(report.usable_bytes),
            )
        )
    elif report.state is CapacityState.CALCULATING:
        lines.append(t.t("cap_calculating"))
    elif report.state is CapacityState.ERROR:
        lines.append(t.t("cap_error"))
    return tuple(lines)
