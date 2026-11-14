"""Core: constants, crypto container, payload codec, file safety, public API."""

from __future__ import annotations

from phantom.core.api import (
    CarrierKind,
    CarrierProbe,
    EmbedOutcome,
    ExtractOutcome,
    capacity_of,
    detect_carrier_kind,
    embed_payload,
    extract_from_path,
    probe_carrier,
)
from phantom.core.constants import APP_VERSION
from phantom.core.errors import (
    CapacityError,
    IntegrityError,
    InvalidInput,
    InvalidPassword,
    OperationCancelled,
    OutputIsCarrierError,
    PhantomError,
    PhantomIOError,
    UnsafePathError,
    UnsupportedCarrier,
    UnsupportedFormat,
    WipeError,
    WrongPasswordOrNoData,
)

__all__ = [
    "APP_VERSION",
    "CapacityError",
    "CarrierKind",
    "CarrierProbe",
    "EmbedOutcome",
    "ExtractOutcome",
    "IntegrityError",
    "InvalidInput",
    "InvalidPassword",
    "OperationCancelled",
    "OutputIsCarrierError",
    "PhantomError",
    "PhantomIOError",
    "UnsafePathError",
    "UnsupportedCarrier",
    "UnsupportedFormat",
    "WipeError",
    "WrongPasswordOrNoData",
    "capacity_of",
    "detect_carrier_kind",
    "embed_payload",
    "extract_from_path",
    "probe_carrier",
]
