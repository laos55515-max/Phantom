"""Cancelling, closing, and running again: no hangs, no races, no stray files.

The window can be closed, the cancel button pressed, or the same job started a
second time while the first is still unwinding.  The invariants that must survive
all three are the same ones the whole product rests on:

* a cancelled operation publishes **nothing** — the destination keeps whatever was
  there before, and no staged sibling, temp file or half-written copy is left;
* the worker is free afterwards: the next job starts, and it does not inherit the
  previous job's cancel event;
* a job thread never outlives the call that joined it, and the resources it held
  (the collector pause, the progress bar, the disabled buttons) are released even
  when the outcome is a cancellation rather than a success.
"""

from __future__ import annotations

import gc
import threading
import time
from pathlib import Path

import pytest
from tests import metadata_samples as samples

from phantom.core import api
from phantom.core.errors import OperationCancelled, PhantomError
from phantom.core.payload import Payload
from phantom.metadata import cleaner, inspector
from phantom.ui.worker import WorkerEventType, WorkerService

PASSWORD = "Property-passphrase-2026"


def drain_until(
    service: WorkerService, kind: WorkerEventType, timeout: float = 10.0
):
    """The first event of ``kind`` the worker posts, or ``None`` on a timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in service.poll():
            if event.kind is kind:
                return event
        time.sleep(0.005)
    return None


def files_in(directory: Path) -> list[Path]:
    return sorted(path for path in directory.iterdir() if path.is_file())


# ---------------------------------------------------------------------------
# the worker: a cancel is an event, not a wedge
# ---------------------------------------------------------------------------
def test_a_cancelled_job_leaves_the_worker_ready_for_the_next_one() -> None:
    service = WorkerService()
    started_second = threading.Event()

    def first(report, cancel):  # noqa: ANN001, ARG001 - the worker's signature
        while not cancel.is_set():
            time.sleep(0.005)
        raise OperationCancelled("cancelled")

    def second(report, cancel):  # noqa: ANN001, ARG001 - the worker's signature
        started_second.set()
        return "done"

    assert service.start("first", first)
    service.cancel()
    assert drain_until(service, WorkerEventType.CANCELLED) is not None
    assert service.busy is False

    assert service.start("second", second), "the worker refused work after a cancel"
    assert drain_until(service, WorkerEventType.DONE) is not None
    assert started_second.is_set()


def test_the_next_job_does_not_inherit_the_previous_cancel_event() -> None:
    """``start`` replaces the event; a job must not be born cancelled."""
    service = WorkerService()
    first = threading.Event()

    def blocking(report, cancel):  # noqa: ANN001, ARG001
        first.set()
        while not cancel.is_set():
            time.sleep(0.005)
        raise OperationCancelled("cancelled")

    def observes(report, cancel):  # noqa: ANN001, ARG001 - the point of the test
        return cancel.is_set()

    assert service.start("blocking", blocking)
    assert first.wait(5)
    service.cancel()
    assert drain_until(service, WorkerEventType.CANCELLED) is not None

    assert service.start("observes", observes)
    event = drain_until(service, WorkerEventType.DONE)
    assert event is not None and event.value is False


def test_shutdown_cancels_and_waits_for_the_thread() -> None:
    service = WorkerService()
    finished = threading.Event()

    def job(report, cancel):  # noqa: ANN001
        while not cancel.is_set():
            time.sleep(0.005)
        finished.set()
        raise OperationCancelled("cancelled")

    assert service.start("embed", job)
    service.shutdown(timeout=5.0)

    assert finished.is_set(), "shutdown returned without the job stopping"
    assert service.busy is False


def test_shutdown_of_an_idle_worker_is_a_no_op() -> None:
    service = WorkerService()
    service.shutdown(timeout=0.1)  # must not raise, must not block
    assert service.busy is False


# ---------------------------------------------------------------------------
# the metadata cleaner: cancel at any point, nothing published
# ---------------------------------------------------------------------------
def _staged_siblings(directory: Path) -> list[str]:
    return [path.name for path in files_in(directory) if path.suffix != ".png"]


def test_a_cancelled_clean_writes_nothing_and_leaves_no_sibling(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "cancel.png")
    before = source.read_bytes()
    output = tmp_path / "cancel-clean.png"
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(OperationCancelled):
        cleaner.clean(
            str(source), str(output), frozenset({"png.text.tEXt.Comment"}), cancel=cancel
        )

    assert source.read_bytes() == before
    assert not output.exists()
    assert _staged_siblings(tmp_path) == []


def test_the_same_clean_succeeds_after_a_cancel(tmp_path: Path) -> None:
    """A cancel must leave nothing behind that blocks the retry."""
    source = samples.personal_png(tmp_path / "retry.png")
    output = tmp_path / "retry-clean.png"
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(OperationCancelled):
        cleaner.clean(
            str(source), str(output), frozenset({"png.text.tEXt.Comment"}), cancel=cancelled
        )

    outcome = cleaner.clean(str(source), str(output), frozenset({"png.text.tEXt.Comment"}))

    assert outcome.verified is True
    assert output.exists()
    assert _staged_siblings(tmp_path) == []


def test_a_cancel_that_arrives_during_the_run_is_all_or_nothing(tmp_path: Path) -> None:
    """Race the cancel against the run: either the plan finished or nothing exists."""
    source = samples.personal_png(tmp_path / "race.png")
    before = source.read_bytes()
    selection = frozenset({"png.text.tEXt.Comment"})

    for attempt in range(8):
        output = tmp_path / f"race-{attempt}-clean.png"
        cancel = threading.Event()

        def cancel_soon(event: threading.Event = cancel, delay: int = attempt) -> None:
            time.sleep(0.001 * delay)
            event.set()

        threading.Thread(target=cancel_soon, daemon=True).start()
        try:
            cleaner.clean(str(source), str(output), selection, cancel=cancel)
        except OperationCancelled:
            assert not output.exists(), "a cancelled clean published a file"
        else:
            assert output.exists(), "a clean that returned published nothing"
            after = inspector.inspect_path(str(output))
            assert after.find("png.text.tEXt.Comment") is None, (
                "a clean that returned did not remove the selected field"
            )

        assert source.read_bytes() == before
        assert _staged_siblings(tmp_path) == []


def test_cancellation_does_not_leak_the_collector_pause(tmp_path: Path) -> None:
    """The pause around a job is released on the cancelled path too (D11)."""
    was_enabled = gc.isenabled()
    source = samples.personal_png(tmp_path / "gc.png")
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(OperationCancelled):
        cleaner.clean(
            str(source), str(tmp_path / "gc-clean.png"), frozenset({"png.text.tEXt.Comment"}), cancel=cancel
        )

    assert gc.isenabled() == was_enabled


# ---------------------------------------------------------------------------
# the embedder: the same promise, on the crypto side
# ---------------------------------------------------------------------------
def test_a_cancelled_embed_publishes_nothing(tmp_path: Path) -> None:
    carrier = samples.png_file(tmp_path / "carrier.png")
    output = tmp_path / "stego.png"
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(OperationCancelled):
        api.embed_payload(
            str(carrier),
            str(output),
            Payload.text("hello"),
            password=PASSWORD,
            cancel=cancel,
        )

    assert not output.exists()
    assert not list(tmp_path.glob("*.stego.png"))
    assert _staged_siblings(tmp_path) == []


def test_a_cancelled_embed_keeps_the_file_that_was_already_there(tmp_path: Path) -> None:
    """The previous output is not the operation's to delete."""
    carrier = samples.png_file(tmp_path / "carrier.png")
    output = tmp_path / "stego.png"
    output.write_bytes(b"previous result, keep me")
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(OperationCancelled):
        api.embed_payload(
            str(carrier),
            str(output),
            Payload.text("hello"),
            password=PASSWORD,
            cancel=cancel,
        )

    assert output.read_bytes() == b"previous result, keep me"


@pytest.mark.parametrize("kind", ["inspect", "clean"])
def test_the_metadata_jobs_accept_a_cancel_event(kind: str, tmp_path: Path) -> None:
    """Both panel jobs thread the event through; a set event must stop them."""
    source = samples.personal_png(tmp_path / f"{kind}.png")
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(PhantomError):
        if kind == "inspect":
            inspector.inspect_path(str(source), cancel=cancel)
        else:
            cleaner.clean(
                str(source),
                str(tmp_path / "clean-out.png"),
                frozenset({"png.text.tEXt.Comment"}),
                cancel=cancel,
            )

    assert not (tmp_path / "clean-out.png").exists()
