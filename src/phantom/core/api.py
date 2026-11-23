"""High-level operations shared by the UI, the CLI probes and the tests.

Every public entry point is thread-agnostic, accepts ``progress``/``cancel``,
verifies what it wrote and never deletes anything without being told to.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, unique
from pathlib import Path
from typing import Final

from phantom.audio import (
    AudioInfo,
    embed_file_into_audio,
    embed_into_audio,
    extract_from_audio,
    probe_audio,
)
from phantom.core import constants as C
from phantom.core import files as fsutil
from phantom.core.errors import (
    IntegrityError,
    InvalidInput,
    InvalidPassword,
    PhantomError,
    PhantomIOError,
    UnsupportedCarrier,
)
from phantom.core.kdf import KdfProfile
from phantom.core.payload import ExtractedPayload, Payload, PayloadKind
from phantom.core.progress import ProgressFn, Stage, check_cancel, noop_progress
from phantom.stego.engine import Capacity, EmbedReport
from phantom.stego.image import (
    ImageInfo,
    embed_file_into_image,
    embed_into_image,
    extract_from_image,
    probe_image,
)

#: Built-in resources shipped with the app.  They are read-only examples and
#: must never be overwritten or deleted by any PHANTOM operation.
RESOURCES_DIR: Final = Path(__file__).resolve().parent.parent / "resources"


@unique
class CarrierKind(Enum):
    IMAGE = "image"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True)
class CarrierProbe:
    kind: CarrierKind
    path: str
    capacity: Capacity
    notices: tuple[str, ...] = ()
    image: ImageInfo | None = None
    audio: AudioInfo | None = None

    @property
    def description(self) -> str:
        if self.image is not None:
            return f"{self.image.width}x{self.image.height} {self.image.mode}"
        if self.audio is not None:
            return (
                f"{self.audio.frames} samples · {self.audio.channels} ch · "
                f"{self.audio.bits} bit · {self.audio.sample_rate} Hz"
            )
        return self.path


@dataclass(slots=True)
class EmbedOutcome:
    output_path: str
    kind: CarrierKind
    payload_kind: PayloadKind
    payload_bytes: int
    user_bytes: int
    capacity: Capacity
    verified: bool
    wiped_source: bool
    wipe_error: str | None = None
    notices: tuple[str, ...] = ()
    profile_label: str = ""
    duration_seconds: float = 0.0
    report: EmbedReport | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ExtractOutcome:
    payload: ExtractedPayload
    carrier_kind: CarrierKind
    duration_seconds: float

    @property
    def verified(self) -> bool:
        """True only when the recovered bytes matched the sealed SHA-256 (D12).

        ``unpack_payload`` raises on a mismatch, so this is True for every
        outcome that exists at all: the property is the single place the UI reads
        before it is allowed to say "extracted".
        """
        return self.payload.integrity_ok


#: Extension the stego layer forces on an output file, per carrier kind.
_FORCED_OUTPUT_EXTENSIONS: Final[dict[CarrierKind, tuple[str, ...]]] = {
    CarrierKind.IMAGE: (".png",),
    CarrierKind.AUDIO: (".wav", ".flac"),
}


def output_path_variants(output_path: str, kind: CarrierKind) -> tuple[str, ...]:
    """Every real path an output request can land on once the format is enforced.

    ``a.jpg`` is written as ``a.png`` and ``a.ogg`` as ``a.wav`` (or ``a.flac``),
    so checking the requested name alone can miss the file that is really
    written.  Both lossless audio extensions are included because the carrier
    decides which one wins: refusing one name too many is far cheaper than
    overwriting the source a user asked to hide and wipe.
    """
    stem = os.path.splitext(output_path)[0]
    names = [output_path]
    names.extend(f"{stem}{suffix}" for suffix in _FORCED_OUTPUT_EXTENSIONS[kind])
    return tuple(dict.fromkeys(names))


def detect_carrier_kind(path: str) -> CarrierKind:
    """Content first, extension second (D14: no guessing by filename alone)."""
    extension = os.path.splitext(path)[1].lower()
    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
    except OSError as exc:
        raise PhantomIOError("cannot read file", detail=str(exc)) from exc
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return CarrierKind.IMAGE
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return CarrierKind.AUDIO
    if head[:4] == b"fLaC":
        return CarrierKind.AUDIO
    if extension in C.IMAGE_EXTENSIONS:
        return CarrierKind.IMAGE
    if extension in C.AUDIO_WAV_EXTENSIONS or extension in C.AUDIO_FLAC_EXTENSIONS:
        return CarrierKind.AUDIO
    if extension in C.LOSSY_AUDIO_EXTENSIONS:
        return CarrierKind.AUDIO
    raise UnsupportedCarrier("unsupported carrier file", detail=extension or "no extension")


def probe_carrier(path: str, *, cancel: threading.Event | None = None) -> CarrierProbe:
    if not os.path.isfile(path):
        raise PhantomIOError("file not found", detail=path)
    check_cancel(cancel)
    kind = detect_carrier_kind(path)
    if kind is CarrierKind.IMAGE:
        info = probe_image(path)
        return CarrierProbe(
            kind=kind, path=path, capacity=info.capacity, notices=info.notices, image=info
        )
    audio = probe_audio(path)
    return CarrierProbe(
        kind=kind, path=path, capacity=audio.capacity, notices=audio.notices, audio=audio
    )


def _verify_output(
    output_path: str,
    kind: CarrierKind,
    expected_sha256: str,
    expected_size: int,
    password: str,
    *,
    cancel: threading.Event | None,
    progress: ProgressFn,
) -> None:
    """Test-extract the file we just wrote, before anything is deleted (D12)."""
    progress(Stage.VERIFYING, 0, 1)
    extracted = extract_from_path(
        output_path, password, kind=kind, cancel=cancel, progress=progress, allow_legacy=False
    )
    payload = extracted.payload
    if payload.sha256_actual != expected_sha256 or payload.original_size != expected_size:
        raise IntegrityError("self-check failed: extracted data differs from the input")
    progress(Stage.VERIFYING, 1, 1)


def extract_from_path(
    path: str,
    password: str,
    *,
    kind: CarrierKind | None = None,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
    allow_legacy: bool = True,
) -> ExtractOutcome:
    started = time.perf_counter()
    kind = kind or detect_carrier_kind(path)
    check_cancel(cancel)
    if kind is CarrierKind.IMAGE:
        payload = extract_from_image(
            path, password, cancel=cancel, progress=progress, allow_legacy=allow_legacy
        )
    else:
        payload = extract_from_audio(path, password, cancel=cancel, progress=progress)
    return ExtractOutcome(
        payload=payload, carrier_kind=kind, duration_seconds=time.perf_counter() - started
    )


def embed_payload(
    carrier_path: str,
    output_path: str,
    payload: Payload | None = None,
    *,
    secret_path: str | None = None,
    password: str,
    profile: KdfProfile | None = None,
    wipe_source: bool = False,
    cancel: threading.Event | None = None,
    progress: ProgressFn = noop_progress,
    verify: bool = True,
) -> EmbedOutcome:
    """Embed text/file data, verify the result, optionally wipe the source file."""
    if (payload is None) == (secret_path is None):
        raise InvalidInput("provide exactly one of payload or secret_path")
    if not os.path.isfile(carrier_path):
        raise PhantomIOError("carrier not found", detail=carrier_path)

    # Built-in resources are read-only: never write into them and never delete
    # them.  They may still be *read* as a carrier or as a source attachment, so
    # only the *output* path and *wipe_source* are refused here — a user may
    # embed the built-in example_secret.txt, but may not save next to it or wipe it.
    try:
        resources = RESOURCES_DIR.resolve()
    except (OSError, ValueError):
        resources = None
    if resources is not None and output_path is not None:
        try:
            resolved = Path(output_path).resolve()
        except (OSError, ValueError):
            resolved = None
        if resolved is not None and (
            resources in resolved.parents or resolved == resources
        ):
            raise InvalidInput(
                "built-in resource is read-only: output",
                detail=str(resolved),
            )
    if wipe_source and secret_path is not None:
        try:
            resolved_secret = Path(secret_path).resolve()
        except (OSError, ValueError):
            resolved_secret = None
        if (
            resources is not None
            and resolved_secret is not None
            and (resources in resolved_secret.parents or resolved_secret == resources)
        ):
            raise InvalidInput(
                "refusing to wipe a built-in resource",
                detail=str(resolved_secret),
            )

    kind = detect_carrier_kind(carrier_path)
    variants = output_path_variants(output_path, kind)
    # The writers below are handed a staged temp name, so they can only prove the
    # temp does not sit on the carrier.  Every real name the request can land on
    # has to be checked here instead, or "output = carrier" walks straight
    # through and destroys the carrier it was supposed to read.
    for variant in variants:
        fsutil.ensure_distinct(carrier_path, variant)
    if secret_path is not None:
        # Refuse an output that would replace the very file we are about to hide
        # (and maybe wipe) *before* a single byte is written: once the write has
        # happened the source is already gone, whatever we raise afterwards.  The
        # check covers every path the forced extension can produce.
        fsutil.ensure_output_not_source(secret_path, variants)
    started = time.perf_counter()
    # Write to a sibling nobody can see, verify *that*, and only then let it take
    # the destination's name: a failed self-check must cost a temp file, not the
    # file the user already had at that path.
    staged = fsutil.staged_path(output_path)
    published = False

    if payload is not None:
        payload_kind = payload.kind
        if kind is CarrierKind.IMAGE:
            image_result = embed_into_image(
                carrier_path,
                staged,
                payload,
                password,
                profile=profile,
                cancel=cancel,
                progress=progress,
            )
        else:
            audio_result = embed_into_audio(
                carrier_path,
                staged,
                payload,
                password,
                profile=profile,
                cancel=cancel,
                progress=progress,
            )
    else:
        assert secret_path is not None
        payload_kind = PayloadKind.FILE
        if kind is CarrierKind.IMAGE:
            image_result = embed_file_into_image(
                carrier_path,
                staged,
                secret_path,
                password,
                profile=profile,
                cancel=cancel,
                progress=progress,
            )
        else:
            audio_result = embed_file_into_audio(
                carrier_path,
                staged,
                secret_path,
                password,
                profile=profile,
                cancel=cancel,
                progress=progress,
            )

    if kind is CarrierKind.IMAGE:
        staged_file = image_result.path
        report = image_result.report
        notices = image_result.notices
        packed = image_result.packed
    else:
        staged_file = audio_result.path
        report = audio_result.report
        notices = audio_result.notices
        packed = audio_result.packed

    # The writer forces its own extension, so the published name may differ from
    # the requested one (``a.jpg`` is written as ``a.png``).
    final_path = fsutil.published_path(output_path, staged_file)
    try:
        if secret_path is not None:
            # Belt and braces: the name about to be published must not be the source.
            fsutil.ensure_output_not_source(secret_path, (final_path,))
        check_cancel(cancel)
        verified = False
        if verify:
            try:
                _verify_output(
                    staged_file,
                    kind,
                    packed.sha256_hex,
                    packed.original_size,
                    password,
                    cancel=cancel,
                    progress=progress,
                )
            except PhantomError:
                # A failed self-check must never publish anything; the staged file
                # goes, whatever already lived at the destination stays.
                fsutil.discard_staged(staged_file)
                raise
            verified = True
        fsutil.publish_staged(staged_file, final_path)
        published = True
    finally:
        if not published:
            # Whatever went wrong — cancel, verification, a read-only directory on
            # the rename — no temp file may survive the call.
            with contextlib.suppress(OSError):
                os.unlink(staged_file)

    if wipe_source and not verified:
        raise InvalidInput(
            "refusing to wipe a source whose output was not verified",
            detail="enable verification before secure deletion",
        )

    wiped = False
    wipe_error: str | None = None
    if wipe_source:
        if secret_path is None:
            raise InvalidInput("only a source file can be wiped")
        fsutil.ensure_output_not_source(secret_path, (final_path,))
        check_cancel(cancel)
        from phantom.core.files import secure_wipe  # noqa: PLC0415

        try:
            secure_wipe(secret_path, cancel=cancel, progress=progress)
            wiped = True
        except PhantomError as exc:
            # The stego file is already written and verified: never pretend the
            # operation failed, and never hide that the file was not destroyed.
            wipe_error = f"{exc.message}: {exc.detail}" if exc.detail else exc.message

    return EmbedOutcome(
        output_path=final_path,
        kind=kind,
        payload_kind=payload_kind,
        payload_bytes=report.payload_bytes,
        user_bytes=packed.original_size,
        capacity=Capacity(
            usable_bits=report.usable_bits,
            payload_bytes=max(0, (report.usable_bits - C.HEADER_BITS) // 8 - C.TAG_SIZE),
        ),
        verified=verified,
        wiped_source=wiped,
        wipe_error=wipe_error,
        notices=tuple(notices),
        profile_label=report.profile.label,
        duration_seconds=time.perf_counter() - started,
        report=report,
    )


def read_all(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise PhantomIOError("cannot read file", detail=str(exc)) from exc


def capacity_of(path: str) -> Capacity:
    return probe_carrier(path).capacity


def ensure_password_strength(password: str, *, minimum: int = C.MIN_PASSWORD_LENGTH) -> None:
    if len(password) < minimum:
        raise InvalidPassword(
            "password too short", detail=f"minimum {minimum} characters"
        )


def estimate_fill_percent(carrier_path: str, user_bytes: int) -> float:
    capacity = capacity_of(carrier_path)
    if capacity.payload_bytes <= 0:
        return 100.0
    overhead = C.PAYLOAD_META_OVERHEAD + len(os.path.basename(carrier_path).encode())
    used = min(capacity.payload_bytes, user_bytes + overhead)
    return min(100.0, 100.0 * used / capacity.payload_bytes)


__all__ = [
    "CarrierKind",
    "CarrierProbe",
    "EmbedOutcome",
    "ExtractOutcome",
    "capacity_of",
    "detect_carrier_kind",
    "embed_payload",
    "ensure_password_strength",
    "estimate_fill_percent",
    "extract_from_path",
    "output_path_variants",
    "probe_carrier",
]
