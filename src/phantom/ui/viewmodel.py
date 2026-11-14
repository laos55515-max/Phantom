"""Display-independent application logic (D10-D12).

Everything the tabs decide — validation, capacity maths, which job to run,
what to save where — lives here so it can be tested without a display.  The Tk
layer only renders state and forwards user intent.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum, unique
from typing import Literal, Protocol

from phantom.core import api as core_api
from phantom.core import constants as C
from phantom.core.api import CarrierKind, CarrierProbe, EmbedOutcome, ExtractOutcome
from phantom.core.errors import CapacityError, InvalidInput, InvalidPassword
from phantom.core.payload import (
    ExtractedPayload,
    Payload,
    SizeEstimate,
    estimate_file_size,
    estimate_size,
)
from phantom.core.progress import ProgressFn
from phantom.ui.settings import AppSettings

IssueLevel = Literal["error", "warn", "info"]


@dataclass(frozen=True, slots=True)
class Issue:
    level: IssueLevel
    key: str
    params: dict[str, object] = field(default_factory=dict)


@unique
class PayloadMode(Enum):
    TEXT = "text"
    FILE = "file"


class Backend(Protocol):
    """The slice of the core API the view models depend on (fake-able in tests)."""

    def probe(self, path: str, *, cancel: threading.Event | None = None) -> CarrierProbe: ...

    def embed(
        self,
        *,
        carrier: str,
        output: str,
        payload: Payload | None,
        secret_path: str | None,
        password: str,
        wipe_source: bool,
        verify: bool,
        cancel: threading.Event | None,
        progress: ProgressFn,
    ) -> EmbedOutcome: ...

    def extract(
        self,
        path: str,
        password: str,
        *,
        cancel: threading.Event | None,
        progress: ProgressFn,
    ) -> ExtractOutcome: ...

    def estimate(
        self,
        *,
        text: str | None,
        path: str | None,
        capacity_bytes: int,
        cancel: threading.Event | None,
        progress: ProgressFn,
    ) -> SizeEstimate: ...


class CoreBackend:
    """Thin adapter over :mod:`phantom.core.api`."""

    def probe(self, path: str, *, cancel: threading.Event | None = None) -> CarrierProbe:
        return core_api.probe_carrier(path, cancel=cancel)

    def embed(
        self,
        *,
        carrier: str,
        output: str,
        payload: Payload | None,
        secret_path: str | None,
        password: str,
        wipe_source: bool,
        verify: bool,
        cancel: threading.Event | None,
        progress: ProgressFn,
    ) -> EmbedOutcome:
        return core_api.embed_payload(
            carrier,
            output,
            payload,
            secret_path=secret_path,
            password=password,
            wipe_source=wipe_source,
            cancel=cancel,
            progress=progress,
            verify=verify,
        )

    def extract(
        self,
        path: str,
        password: str,
        *,
        cancel: threading.Event | None,
        progress: ProgressFn,
    ) -> ExtractOutcome:
        return core_api.extract_from_path(
            path, password, cancel=cancel, progress=progress
        )

    def estimate(
        self,
        *,
        text: str | None,
        path: str | None,
        capacity_bytes: int,
        cancel: threading.Event | None,
        progress: ProgressFn,
    ) -> SizeEstimate:
        if path is not None:
            return estimate_file_size(
                path, capacity_bytes=capacity_bytes, cancel=cancel, progress=progress
            )
        payload = Payload.text(text or "")
        return estimate_size(
            payload, capacity_bytes=capacity_bytes, cancel=cancel, progress=progress
        )


def suggest_output_path(carrier_path: str, kind: CarrierKind) -> str:
    directory, name = os.path.split(carrier_path)
    stem, extension = os.path.splitext(name)
    if kind is CarrierKind.IMAGE:
        return os.path.join(directory, f"{stem}.stego.png")
    extension = extension.lower() if extension.lower() in (".wav", ".flac") else ".wav"
    return os.path.join(directory, f"{stem}.stego{extension}")


@dataclass(frozen=True, slots=True)
class EmbedRequest:
    carrier: str
    output: str
    payload: Payload | None
    secret_path: str | None
    password: str
    wipe_source: bool
    verify: bool


@dataclass(slots=True)
class EmbedViewModel:
    backend: Backend = field(default_factory=CoreBackend)
    settings: AppSettings = field(default_factory=AppSettings)

    carrier_path: str = ""
    carrier: CarrierProbe | None = None
    mode: PayloadMode = PayloadMode.TEXT
    text: str = ""
    secret_path: str = ""
    output_path: str = ""
    password: str = ""
    wipe_source: bool = False
    verify: bool = True
    estimate: SizeEstimate | None = None
    notices: tuple[str, ...] = ()

    # -- state mutation (main thread only) ---------------------------------
    def set_carrier(self, path: str) -> None:
        self.carrier_path = path
        self.carrier = None
        self.estimate = None
        self.notices = ()
        self.output_path = "" if not path else suggest_output_path(path, CarrierKind.IMAGE)

    def apply_probe(self, probe: CarrierProbe) -> None:
        self.carrier = probe
        self.notices = probe.notices
        self.carrier_path = probe.path
        if not self.output_path:
            self.output_path = suggest_output_path(probe.path, probe.kind)

    def set_mode(self, mode: PayloadMode) -> None:
        if mode is not self.mode:
            self.mode = mode
            self.estimate = None

    def set_text(self, text: str) -> None:
        self.text = text
        self.estimate = None

    def set_secret_file(self, path: str) -> None:
        self.secret_path = path
        self.estimate = None

    def set_output(self, path: str) -> None:
        self.output_path = path

    def set_password(self, password: str) -> None:
        self.password = password

    def set_wipe_source(self, value: bool) -> None:
        self.wipe_source = bool(value)

    def set_verify(self, value: bool) -> None:
        self.verify = bool(value)

    def apply_estimate(self, estimate: SizeEstimate) -> None:
        self.estimate = estimate

    def clear(self) -> None:
        self.carrier_path = ""
        self.carrier = None
        self.text = ""
        self.secret_path = ""
        self.output_path = ""
        self.password = ""
        self.wipe_source = False
        self.estimate = None
        self.notices = ()

    # -- derived state -----------------------------------------------------
    @property
    def capacity(self) -> int:
        return self.carrier.capacity.payload_bytes if self.carrier else 0

    def payload_name(self) -> str:
        if self.mode is PayloadMode.FILE and self.secret_path:
            return os.path.basename(self.secret_path)
        return ""

    def user_capacity(self) -> int:
        name = self.payload_name()
        if self.carrier is None:
            return 0
        return self.carrier.capacity.user_bytes(name)

    def estimated_payload_bytes(self) -> int | None:
        if self.estimate is not None:
            return self.estimate.payload_bytes
        if self.mode is PayloadMode.FILE and self.secret_path:
            try:
                return os.path.getsize(self.secret_path) + C.PAYLOAD_META_OVERHEAD
            except OSError:
                return None
        if self.mode is PayloadMode.TEXT and self.text:
            return len(self.text.encode("utf-8")) + C.PAYLOAD_META_OVERHEAD
        return None

    def fill_percent(self) -> float | None:
        """Heuristic fill indicator (not a guarantee)."""
        estimate = self.estimated_payload_bytes()
        if self.carrier is None or estimate is None or self.capacity <= 0:
            return None
        return min(100.0, 100.0 * estimate / self.capacity)

    def exceeds_capacity(self) -> bool:
        estimate = self.estimated_payload_bytes()
        if self.carrier is None or estimate is None:
            return False
        return estimate > self.capacity or bool(self.estimate and self.estimate.exceeded_capacity)

    def has_payload(self) -> bool:
        if self.mode is PayloadMode.FILE:
            return bool(self.secret_path and os.path.isfile(self.secret_path))
        return bool(self.text.strip())

    # -- validation --------------------------------------------------------
    def issues(self) -> list[Issue]:
        found: list[Issue] = []
        if not self.carrier_path:
            found.append(Issue("error", "warn_no_img_enc"))
        elif not os.path.isfile(self.carrier_path):
            found.append(Issue("error", "err.io", {"details": self.carrier_path}))
        if not self.has_payload():
            key = "warn_empty_secret_m" if self.mode is PayloadMode.FILE else "warn_empty_msg_m"
            found.append(Issue("error", key))
        if len(self.password) < C.MIN_PASSWORD_LENGTH:
            found.append(
                Issue(
                    "error",
                    "warn_pwd_weak_m",
                    {"min_len": C.MIN_PASSWORD_LENGTH, "current": len(self.password)},
                )
            )
        if self.carrier is not None and self.exceeds_capacity():
            found.append(
                Issue(
                    "error",
                    "warn_capacity_m",
                    {
                        "needed": self.estimated_payload_bytes() or 0,
                        "available": self.user_capacity(),
                    },
                )
            )
        if self.mode is PayloadMode.FILE and self.secret_path and os.path.isfile(self.secret_path):
            secret_size = os.path.getsize(self.secret_path)
            if secret_size > self.user_capacity() and self.carrier is not None:
                found.append(
                    Issue(
                        "error",
                        "warn_capacity_m",
                        {"needed": secret_size, "available": self.user_capacity()},
                    )
                )
        if self.wipe_source and self.mode is not PayloadMode.FILE:
            found.append(Issue("info", "settings_wipe_warning"))
        return found

    def ensure_ready(self) -> None:
        """Raise a typed error when the request cannot be executed."""
        if not self.carrier_path or not os.path.isfile(self.carrier_path):
            raise InvalidInput("carrier is not selected")
        if not self.has_payload():
            raise InvalidInput("payload is empty")
        if len(self.password) < C.MIN_PASSWORD_LENGTH:
            raise InvalidPassword(
                "password too short", detail=f"minimum {C.MIN_PASSWORD_LENGTH}"
            )
        if self.carrier is not None and self.exceeds_capacity():
            raise CapacityError(
                "payload does not fit", detail=f"capacity {self.user_capacity()} bytes"
            )

    # -- jobs (run in a worker thread; no Tk allowed) ----------------------
    def make_probe_job(self, path: str) -> Callable[[ProgressFn, threading.Event], CarrierProbe]:
        backend = self.backend

        def job(_report: ProgressFn, cancel: threading.Event) -> CarrierProbe:
            return backend.probe(path, cancel=cancel)

        return job

    def make_estimate_job(
        self,
    ) -> Callable[[ProgressFn, threading.Event], SizeEstimate | None]:
        text = self.text if self.mode is PayloadMode.TEXT else None
        path = (
            self.secret_path
            if self.mode is PayloadMode.FILE and self.secret_path
            else None
        )
        capacity = self.capacity or 0
        backend = self.backend

        def job(report: ProgressFn, cancel: threading.Event) -> SizeEstimate | None:
            if path is None and not text:
                return None
            return backend.estimate(
                text=text,
                path=path,
                capacity_bytes=capacity,
                cancel=cancel,
                progress=report,
            )

        return job

    def snapshot(self) -> EmbedRequest:
        self.ensure_ready()
        payload: Payload | None = None
        secret_path: str | None = None
        if self.mode is PayloadMode.FILE:
            secret_path = self.secret_path
        else:
            payload = Payload.text(self.text)
        output = self.output_path.strip() or suggest_output_path(
            self.carrier_path,
            self.carrier.kind if self.carrier else CarrierKind.IMAGE,
        )
        if self.mode is PayloadMode.FILE and self.wipe_source:
            # Wiping needs the file, the payload must come from the path.
            secret_path = self.secret_path
        return EmbedRequest(
            carrier=self.carrier_path,
            output=output,
            payload=payload,
            secret_path=secret_path,
            password=self.password,
            wipe_source=self.wipe_source and self.mode is PayloadMode.FILE,
            verify=self.verify,
        )

    def make_embed_job(self) -> Callable[[ProgressFn, threading.Event], EmbedOutcome]:
        request = self.snapshot()
        backend = self.backend

        def job(report: ProgressFn, cancel: threading.Event) -> EmbedOutcome:
            return backend.embed(
                carrier=request.carrier,
                output=request.output,
                payload=request.payload,
                secret_path=request.secret_path,
                password=request.password,
                wipe_source=request.wipe_source,
                verify=request.verify,
                cancel=cancel,
                progress=report,
            )

        return job


@dataclass(slots=True)
class ExtractViewModel:
    backend: Backend = field(default_factory=CoreBackend)
    settings: AppSettings = field(default_factory=AppSettings)

    stego_path: str = ""
    carrier: CarrierProbe | None = None
    password: str = ""
    result: ExtractedPayload | None = None
    outcome: ExtractOutcome | None = None
    notices: tuple[str, ...] = ()
    save_path: str = ""

    def set_stego(self, path: str) -> None:
        self.stego_path = path
        self.carrier = None
        self.result = None
        self.outcome = None
        self.notices = ()

    def apply_probe(self, probe: CarrierProbe) -> None:
        self.carrier = probe
        self.stego_path = probe.path
        self.notices = probe.notices

    def apply_result(self, outcome: ExtractOutcome) -> None:
        self.outcome = outcome
        self.result = outcome.payload
        if outcome.payload.kind.value == C.TYPE_FILE:
            self.save_path = self.default_save_path()

    def default_save_path(self) -> str:
        if self.result is None:
            return ""
        directory = self.settings.last_dir or os.path.dirname(self.stego_path) or os.getcwd()
        name = self.result.name or "extracted.bin"
        return os.path.join(directory, name)

    def clear(self) -> None:
        self.stego_path = ""
        self.carrier = None
        self.password = ""
        self.result = None
        self.outcome = None
        self.notices = ()
        self.save_path = ""

    def ensure_ready(self) -> None:
        if not self.stego_path or not os.path.isfile(self.stego_path):
            raise InvalidInput("stego file is not selected")
        if not self.password:
            raise InvalidPassword("password is empty")

    def issues(self) -> list[Issue]:
        found: list[Issue] = []
        if not self.stego_path:
            found.append(Issue("error", "warn_no_img_dec"))
        elif not os.path.isfile(self.stego_path):
            found.append(Issue("error", "err.io", {"details": self.stego_path}))
        if not self.password:
            found.append(Issue("error", "warn_empty_pwd_dec"))
        return found

    def make_probe_job(self, path: str) -> Callable[[ProgressFn, threading.Event], CarrierProbe]:
        backend = self.backend

        def job(_report: ProgressFn, cancel: threading.Event) -> CarrierProbe:
            return backend.probe(path, cancel=cancel)

        return job

    def make_extract_job(self) -> Callable[[ProgressFn, threading.Event], ExtractOutcome]:
        self.ensure_ready()
        path = self.stego_path
        password = self.password
        backend = self.backend

        def job(report: ProgressFn, cancel: threading.Event) -> ExtractOutcome:
            return backend.extract(path, password, cancel=cancel, progress=report)

        return job

    def with_last_dir(self, directory: str) -> ExtractViewModel:
        return ExtractViewModel(
            backend=self.backend,
            settings=replace(self.settings, last_dir=directory),
            stego_path=self.stego_path,
            carrier=self.carrier,
            password=self.password,
            result=self.result,
            outcome=self.outcome,
            notices=self.notices,
            save_path=self.save_path,
        )
