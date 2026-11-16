"""Progress reporting and cooperative cancellation.

The whole core/stego/audio surface accepts ``progress(stage, done, total)`` and
``cancel: threading.Event`` so that the UI can move work off the Tk thread and
still show truthful, deterministic progress.  Two phases cannot report a fraction
honestly — the Argon2id run and the wavelet cost map, each one opaque pass over
its input — so they announce themselves as indeterminate and the interface shows
a spinner with the elapsed time instead of a frozen 0 %.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum, unique
from typing import Final, Protocol

from phantom.core.errors import OperationCancelled


@unique
class Stage(Enum):
    """Pipeline stages. The value doubles as an i18n key suffix (``stage.<v>``)."""

    READING = "reading"
    COMPRESSING = "compressing"
    ESTIMATING = "estimating"
    ANALYSING = "analysing"
    KDF = "kdf"
    ENCRYPTING = "encrypting"
    EMBEDDING = "embedding"
    WRITING = "writing"
    EXTRACTING = "extracting"
    DECRYPTING = "decrypting"
    DECOMPRESSING = "decompressing"
    VERIFYING = "verifying"
    WIPING = "wiping"


#: Stages whose duration cannot be predicted honestly: the Argon2id run and the
#: wavelet cost map.  ``done``/``total`` carry no meaning for these.
INDETERMINATE_STAGES: Final[frozenset[Stage]] = frozenset({Stage.KDF, Stage.ANALYSING})


class ProgressFn(Protocol):
    def __call__(self, stage: Stage, done: int, total: int) -> None:
        """Report ``done`` out of ``total`` units for ``stage``."""


def noop_progress(stage: Stage, done: int, total: int) -> None:
    """Default progress sink: intentionally silent."""


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    stage: Stage
    done: int
    total: int

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, max(0.0, self.done / self.total))

    @property
    def indeterminate(self) -> bool:
        return self.total <= 0


def check_cancel(cancel: threading.Event | None) -> None:
    """Raise :class:`OperationCancelled` if the user asked to stop."""
    if cancel is not None and cancel.is_set():
        raise OperationCancelled("operation cancelled")


def check_cancel_and_report(
    cancel: threading.Event | None,
    progress: ProgressFn,
    stage: Stage,
    done: int,
    total: int,
) -> None:
    check_cancel(cancel)
    progress(stage, done, total)
