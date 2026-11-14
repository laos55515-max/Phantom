"""Background execution with a queue, cooperative cancel and a watchdog (D11).

Design rules
------------
* the worker thread never touches Tk — it only pushes immutable events into a
  :class:`queue.Queue`; the main thread drains them with :meth:`poll`;
* one operation at a time; starting a second one is rejected, not queued;
* threads are **not** daemons: closing the window joins them after cancellation;
* every stage reports deterministic ``done/total``; only the KDF reports an
  indeterminate ``total == 0``.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Any, Final

from phantom.core import constants as C
from phantom.core.errors import InternalError, OperationCancelled, PhantomError
from phantom.core.progress import Stage

LOGGER: Final = logging.getLogger("phantom.worker")

#: Coalesce progress reports to ~25 fps so a fast loop cannot flood the queue.
_PROGRESS_MIN_INTERVAL: Final = 0.04


@unique
class WorkerEventType(Enum):
    PROGRESS = "progress"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class WorkerEvent:
    kind: WorkerEventType
    stage: Stage | None = None
    done: int = 0
    total: int = 0
    value: Any = None
    error: PhantomError | None = None
    operation: str = ""

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, max(0.0, self.done / self.total))

    @property
    def indeterminate(self) -> bool:
        return self.total <= 0


@dataclass(slots=True)
class _Job:
    name: str
    function: Callable[[Callable[[Stage, int, int], None], threading.Event], Any]
    started_at: float = field(default_factory=time.monotonic)
    last_progress_at: float = field(default_factory=time.monotonic)
    last_emit_at: float = 0.0


class WorkerService:
    """Single-slot background executor with an event queue."""

    def __init__(self, *, watchdog_seconds: float = C.WATCHDOG_SECONDS) -> None:
        self._events: queue.Queue[WorkerEvent] = queue.Queue()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._job: _Job | None = None
        self._lock = threading.Lock()
        self._watchdog_seconds = watchdog_seconds
        self._watchdog_fired = False

    # -- state -------------------------------------------------------------
    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def operation(self) -> str:
        with self._lock:
            return self._job.name if self._job else ""

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel

    def seconds_since_progress(self) -> float:
        with self._lock:
            job = self._job
        if job is None:
            return 0.0
        return time.monotonic() - job.last_progress_at

    # -- lifecycle ---------------------------------------------------------
    def start(
        self,
        name: str,
        function: Callable[[Callable[[Stage, int, int], None], threading.Event], Any],
    ) -> bool:
        """Start ``function(report, cancel)`` in a worker thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                LOGGER.debug("worker busy, rejecting %s", name)
                return False
            self._cancel = threading.Event()
            job = _Job(name=name, function=function)
            self._job = job
            self._watchdog_fired = False
            thread = threading.Thread(
                target=self._run,
                args=(job, self._cancel),
                name=f"phantom-worker-{name}",
                daemon=False,
            )
            self._thread = thread
        thread.start()
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Signal cancellation and wait for the worker to finish."""
        self._cancel.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            if thread.is_alive():
                LOGGER.warning("worker thread did not stop within %.1fs", timeout)

    # -- main-thread API ---------------------------------------------------
    def poll(self) -> list[WorkerEvent]:
        """Drain pending events. Must be called from the main thread."""
        drained: list[WorkerEvent] = []
        while True:
            try:
                drained.append(self._events.get_nowait())
            except queue.Empty:
                break
        return drained

    def watchdog_expired(self) -> bool:
        """True once, when an operation stalls longer than the watchdog window."""
        if not self.busy:
            return False
        if self._watchdog_fired:
            return False
        if self.seconds_since_progress() < self._watchdog_seconds:
            return False
        self._watchdog_fired = True
        return True

    def reset_watchdog(self) -> None:
        self._watchdog_fired = False
        with self._lock:
            if self._job is not None:
                self._job.last_progress_at = time.monotonic()

    # -- worker side -------------------------------------------------------
    def _run(
        self,
        job: _Job,
        cancel: threading.Event,
    ) -> None:
        job_stage_holder: list[Stage | None] = [None]

        def report(stage: Stage, done: int, total: int) -> None:
            now = time.monotonic()
            with self._lock:
                job.last_progress_at = now
                should_emit = (
                    stage != job_stage_holder[0]
                    or done == total
                    or now - job.last_emit_at >= _PROGRESS_MIN_INTERVAL
                )
                if should_emit:
                    job.last_emit_at = now
                job_stage_holder[0] = stage
            if should_emit:
                self._events.put(
                    WorkerEvent(
                        kind=WorkerEventType.PROGRESS,
                        stage=stage,
                        done=done,
                        total=total,
                        operation=job.name,
                    )
                )

        try:
            value = job.function(report, cancel)
        except OperationCancelled:
            self._finish(WorkerEventType.CANCELLED, job.name)
        except PhantomError as exc:
            LOGGER.info("operation %s failed: %s", job.name, exc)
            self._finish(WorkerEventType.ERROR, job.name, error=exc)
        except Exception as exc:  # unexpected: still a typed failure for the UI
            LOGGER.exception("operation %s crashed", job.name)
            self._finish(
                WorkerEventType.ERROR,
                job.name,
                error=InternalError("unexpected failure", detail=str(exc)),
            )
        else:
            self._finish(WorkerEventType.DONE, job.name, value=value)

    def _finish(
        self,
        kind: WorkerEventType,
        operation: str,
        *,
        value: Any = None,
        error: PhantomError | None = None,
    ) -> None:
        with self._lock:
            self._job = None
        self._events.put(
            WorkerEvent(kind=kind, value=value, error=error, operation=operation)
        )
