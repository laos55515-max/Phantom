"""Typed error hierarchy for PHANTOM.

Rules:
* every failure the user can cause surfaces as a :class:`PhantomError`;
* ``WrongPasswordOrNoData`` deliberately merges "wrong password" and
  "no payload here" so the app cannot be used as a decryption oracle;
* messages are stable i18n keys (``err.<code>``), never English prose.
"""

from __future__ import annotations

from enum import Enum, unique


@unique
class ErrorCode(Enum):
    WRONG_PASSWORD_OR_NO_DATA = "wrong_password_or_no_data"  # noqa: S105 - an error id
    CAPACITY = "capacity"
    UNSUPPORTED_FORMAT = "unsupported_format"
    UNSUPPORTED_CARRIER = "unsupported_carrier"
    INTEGRITY = "integrity"
    CANCELLED = "cancelled"
    INVALID_PASSWORD = "invalid_password"  # noqa: S105 - an error id
    INVALID_INPUT = "invalid_input"
    INVALID_UNICODE = "invalid_unicode"
    IO = "io"
    OUTPUT_IS_CARRIER = "output_is_carrier"
    OUTPUT_EXISTS = "output_exists"
    SOURCE_OVERWRITE = "source_overwrite"
    UNSAFE_PATH = "unsafe_path"
    WIPE_FAILED = "wipe_failed"
    AUDIO_LOSSY = "audio_lossy"
    AUDIO_FORMAT = "audio_format"
    AUDIO_SILENCE = "audio_silence"
    SIGNED_MEDIA = "signed_media"
    METADATA = "metadata"
    METADATA_CONFLICT = "metadata_conflict"
    INTERNAL = "internal"


class PhantomError(Exception):
    """Base class for all expected PHANTOM failures."""

    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(self, message: str = "", *, detail: str | None = None) -> None:
        super().__init__(message or self.code.value)
        self.message = message
        self.detail = detail
        self.params: dict[str, object] = {}

    @property
    def i18n_key(self) -> str:
        return f"err.{self.code.value}"

    def with_params(self, **params: object) -> PhantomError:
        self.params.update(params)
        return self

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message or self.code.value} ({self.detail})"
        return self.message or self.code.value

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code.value!r}, message={self.message!r})"


class WrongPasswordOrNoData(PhantomError):  # noqa: N818 - name is the user-facing contract
    """Wrong password, damaged payload, or simply no payload in the carrier."""

    code = ErrorCode.WRONG_PASSWORD_OR_NO_DATA


class CapacityError(PhantomError):
    code = ErrorCode.CAPACITY


class UnsupportedFormat(PhantomError):  # noqa: N818 - name is the user-facing contract
    code = ErrorCode.UNSUPPORTED_FORMAT


class UnsupportedCarrier(PhantomError):  # noqa: N818 - name is the user-facing contract
    code = ErrorCode.UNSUPPORTED_CARRIER


class IntegrityError(PhantomError):
    code = ErrorCode.INTEGRITY


class OperationCancelled(PhantomError):  # noqa: N818 - name is the user-facing contract
    code = ErrorCode.CANCELLED


class InvalidPassword(PhantomError):  # noqa: N818 - name is the user-facing contract
    code = ErrorCode.INVALID_PASSWORD


class InvalidInput(PhantomError):  # noqa: N818 - name is the user-facing contract
    code = ErrorCode.INVALID_INPUT


class InvalidUnicode(PhantomError):  # noqa: N818 - name is the user-facing contract
    """Text that has no UTF-8 form (a lone surrogate): refused, never rewritten."""

    code = ErrorCode.INVALID_UNICODE


class PhantomIOError(PhantomError):
    code = ErrorCode.IO


class OutputIsCarrierError(PhantomError):
    code = ErrorCode.OUTPUT_IS_CARRIER


class UnsafePathError(PhantomError):
    code = ErrorCode.UNSAFE_PATH


class SourceOverwriteError(PhantomError):
    """The output path would replace the very file the user hid in it."""

    code = ErrorCode.SOURCE_OVERWRITE


class OutputExistsError(PhantomError):
    """The destination already exists and the caller did not ask to replace it."""

    code = ErrorCode.OUTPUT_EXISTS


class WipeError(PhantomError):
    code = ErrorCode.WIPE_FAILED


class LossyAudioError(PhantomError):
    code = ErrorCode.AUDIO_LOSSY


class AudioFormatError(PhantomError):
    code = ErrorCode.AUDIO_FORMAT


class SilenceError(PhantomError):
    code = ErrorCode.AUDIO_SILENCE


class SignedMediaError(PhantomError):
    """A content credential is attached; rewriting the file would invalidate it."""

    code = ErrorCode.SIGNED_MEDIA


class MetadataError(PhantomError):
    """Metadata could not be read, planned or rewritten."""

    code = ErrorCode.METADATA


class MetadataConflictError(PhantomError):
    """The rewrite would have dropped a field the plan promised to keep.

    Raised by the last check before publishing, so it is a bug in the cleaning
    policy rather than something the user did: the sentence it produces says so,
    names the field by label instead of by tag number, and states that the file
    was left alone.
    """

    code = ErrorCode.METADATA_CONFLICT


class InternalError(PhantomError):
    code = ErrorCode.INTERNAL


#: Convenience alias used by the UI to render a localized message.
ERROR_CODES: tuple[ErrorCode, ...] = tuple(ErrorCode)
