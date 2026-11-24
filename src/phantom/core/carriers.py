"""Built-in offline PNG carriers and capacity-based auto-selection.

Uses the existing core prober (:func:`phantom.core.api.probe_carrier`) so
capacity numbers and container overhead match embedding exactly.
"""

from __future__ import annotations

import functools
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from phantom.core import api
from phantom.core.errors import InvalidInput

BUILTIN_CARRIERS_DIR: Final[Path] = api.RESOURCES_DIR / "carriers"

# Conservative upper bound matching the desktop estimator:
# PAYLOAD_META_OVERHEAD (39) + GCM tag (16) + name + worst-case zlib slack (~0.4%).
_BASE_OVERHEAD: Final = 39 + 16


@dataclass(frozen=True, slots=True)
class BuiltinCarrierSelection:
    """Result of auto-selecting a built-in PNG carrier for a payload."""

    carrier: api.CarrierProbe | None
    fits: bool
    required_bytes: int
    max_capacity_bytes: int
    suitable_count: int
    index: int


def estimate_required_bytes(
    *,
    secret_path: str | None = None,
    text: str | None = None,
    required_bytes: int | None = None,
) -> int:
    """Estimate the container payload bytes needed for text, a file, or an explicit size."""
    provided = sum(x is not None for x in (secret_path, text, required_bytes))
    if provided != 1:
        raise InvalidInput("provide exactly one of secret_path, text, or required_bytes")

    if required_bytes is not None:
        if required_bytes < 0:
            raise InvalidInput("required_bytes must be non-negative")
        return required_bytes

    if secret_path is not None:
        if not os.path.isfile(secret_path):
            raise InvalidInput("secret file not found", detail=secret_path)
        user_bytes = os.path.getsize(secret_path)
        name_bytes = len(os.path.basename(secret_path).encode("utf-8"))
    else:
        assert text is not None
        user_bytes = len(text.encode("utf-8"))
        name_bytes = 8

    return int(user_bytes + _BASE_OVERHEAD + name_bytes + math.ceil(user_bytes / 256))


@functools.lru_cache(maxsize=1)
def list_builtin_carriers() -> tuple[api.CarrierProbe, ...]:
    """Return all bundled PNG carriers probed through the core, sorted by capacity."""
    if not BUILTIN_CARRIERS_DIR.is_dir():
        return ()
    paths = sorted(
        p for p in BUILTIN_CARRIERS_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".png"
    )
    probes = [api.probe_carrier(str(path)) for path in paths]
    probes.sort(key=lambda p: (p.capacity.payload_bytes, os.path.basename(p.path)))
    return tuple(probes)


def builtin_carrier_id(probe: api.CarrierProbe) -> str:
    """Return the stable cross-process identifier for a built-in PNG carrier."""
    return os.path.basename(probe.path)


def resolve_builtin_carrier(builtin_id: str) -> api.CarrierProbe:
    """Resolve a stable built-in carrier identifier against bundled resources only."""
    if not builtin_id or "/" in builtin_id or "\\" in builtin_id:
        raise InvalidInput("unknown builtin carrier", detail=builtin_id)
    for probe in list_builtin_carriers():
        if builtin_carrier_id(probe) == builtin_id:
            return probe
    raise InvalidInput("unknown builtin carrier", detail=builtin_id)


def select_builtin_carrier(
    *,
    required_bytes: int,
    exclude_id: str | None = None,
    exclude_path: str | None = None,
    offset: int = 0,
) -> BuiltinCarrierSelection:
    """Pick a built-in PNG carrier that fits ``required_bytes``.

    When ``exclude_id`` (or legacy ``exclude_path``) matches one of the suitable
    carriers and there are multiple suitable options, advances to the next
    suitable carrier so the "Another option" action always rotates through valid
    choices.
    """
    if required_bytes < 0:
        raise InvalidInput("required_bytes must be non-negative")

    all_carriers = list_builtin_carriers()
    max_capacity = max((c.capacity.payload_bytes for c in all_carriers), default=0)
    suitable = [c for c in all_carriers if c.capacity.payload_bytes >= required_bytes]

    if not suitable:
        return BuiltinCarrierSelection(
            carrier=None,
            fits=False,
            required_bytes=required_bytes,
            max_capacity_bytes=max_capacity,
            suitable_count=0,
            index=0,
        )

    if exclude_id is not None:
        for idx, candidate in enumerate(suitable):
            if builtin_carrier_id(candidate) == exclude_id:
                chosen_idx = (idx + 1) % len(suitable)
                return BuiltinCarrierSelection(
                    carrier=suitable[chosen_idx],
                    fits=True,
                    required_bytes=required_bytes,
                    max_capacity_bytes=max_capacity,
                    suitable_count=len(suitable),
                    index=chosen_idx,
                )

    if exclude_path is not None:
        norm_exclude = os.path.abspath(exclude_path)
        for idx, candidate in enumerate(suitable):
            if os.path.abspath(candidate.path) == norm_exclude:
                chosen_idx = (idx + 1) % len(suitable)
                return BuiltinCarrierSelection(
                    carrier=suitable[chosen_idx],
                    fits=True,
                    required_bytes=required_bytes,
                    max_capacity_bytes=max_capacity,
                    suitable_count=len(suitable),
                    index=chosen_idx,
                )

    chosen_idx = offset % len(suitable)
    return BuiltinCarrierSelection(
        carrier=suitable[chosen_idx],
        fits=True,
        required_bytes=required_bytes,
        max_capacity_bytes=max_capacity,
        suitable_count=len(suitable),
        index=chosen_idx,
    )
