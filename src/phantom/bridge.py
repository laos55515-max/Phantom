"""Stdio JSON bridge between the PHANTOM desktop shell (Tauri) and the core.

Protocol
--------
One JSON object per line on ``stdin``; exactly one JSON object per line on
``stdout``.  The process answers one request and stays alive until ``stdin``
closes; the Tauri side spawns a fresh process per user action.

Security rules
--------------
* Passwords and document contents travel **only** inside the stdin line —
  never in ``argv``, never in ``stderr``, never in logs.  ``argv`` is ignored
  completely so a caller cannot accidentally leak a secret through it.
* ``stderr`` carries at most one line per command (``cmd``, outcome, error
  code).  Set ``PHANTOM_BRIDGE_DEBUG=1`` to also get a traceback on ``stderr``
  — never on ``stdout``, which is reserved for the protocol.
* Output file bytes are written by :func:`extract_to_file` with ``0600``
  permissions; the destination directory is the user's choice (save dialog).

Requests
--------
``{"cmd": "version"}``
    → ``{"ok": true, "version", "protocol"}``

``{"cmd": "probe", "path": str}``
    → carrier kind, capacity and description (no bytes of the file are read
    beyond what the prober needs).

``{"cmd": "hide", "carrier_path", "output_path", "password",
   "secret_path"? | "text"?, "kdf_profile"?}``
    → embeds and self-verifies exactly like the desktop UI does; the file at
    ``output_path`` is published only after the re-extract SHA-256 matches.

``{"cmd": "extract", "carrier_path", "password", "mode": "info"}``
    → decrypts, verifies and returns metadata.  Text payloads return ``text``
    (capped at ``MAX_RETURNED_TEXT``); file payloads return ``suggested_name``
    and, for bounded JPEG/PNG inputs, an optional resized image preview. Original
    bytes are never returned; ``save_file`` writes the original verified payload.

``{"cmd": "extract", "carrier_path", "password", "mode": "verify"}``
    → decrypts and verifies the full payload in memory, returns only success.

``{"cmd": "extract", "carrier_path", "password", "mode": "save_file",
   "output_path": str}``
    → decrypts, verifies and writes the payload to ``output_path``.

``{"cmd": "example_info"}`` / ``{"cmd": "example_hide", "output_path", "password"}``
    → the bundled demo: a tiny example PDF hidden in the bundled example
    carrier, so a first run can be verified without any real data.

``{"cmd": "metadata_inspect", "path"}``
    → the whole :func:`phantom.metadata.inspect_path` report (fields grouped by
    category, counts, structure, provenance), the safe
    :func:`phantom.metadata.default_selection`, and the cleaning plan that
    selection would produce.  Nothing is written.

``{"cmd": "metadata_plan", "path", "selection": [str]}``
    → re-reads the file and answers what :func:`phantom.metadata.plan` says the
    selection would remove, keep or refuse to touch.  Re-reading keeps the
    preview and the later clean on exactly the same bytes.

``{"cmd": "metadata_output_path", "path", "output_path", "overwrite"?: bool}``
    → :func:`phantom.metadata.output_path`: detects the source's real format
    from its bytes, puts that format's extension on the requested name, and
    answers where a clean would write — or refuses with ``source_overwrite``
    (the final path is the source, directly or through a link) or
    ``output_exists`` (something already lives there and ``overwrite`` is
    false).  The shell asks this *before* the save dialog so the path the
    dialog confirms is the path that will be written.

``{"cmd": "metadata_clean", "path", "output_path", "selection": [str],
   "overwrite"?: bool}``
    → :func:`phantom.metadata.clean`: the source is only read, the result is
    staged, its decoded pixels or PCM samples are compared with the source and
    the copy is published only when they are identical.  An ``output_path``
    that resolves to the source is refused with ``source_overwrite``; an
    existing destination without ``overwrite`` with ``output_exists``; a folder
    that cannot publish a new file atomically with ``publish_unavailable``; a
    file carrying a content credential is refused with ``signed_media``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
import traceback
import warnings
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from phantom import __version__
from phantom import metadata as metadata_mod
from phantom.core import api
from phantom.core import carriers as builtin_carriers
from phantom.core import constants as C
from phantom.core.errors import (
    InternalError,
    InvalidInput,
    InvalidPassword,
    PhantomError,
)
from phantom.core.files import ensure_distinct, sanitise_filename, write_bytes_verified
from phantom.core.kdf import KdfProfile
from phantom.core.payload import ExtractedPayload, Payload, PayloadKind
from phantom.metadata.cleaner import CleaningOutcome, CleaningPlan
from phantom.metadata.models import MetadataField, MetadataReport

PROTOCOL = 1

#: Largest text payload returned inline by ``extract``; bigger texts are meant
#: to be saved to a file instead of crossing the bridge.
MAX_RETURNED_TEXT = 4 * 1024 * 1024

_DEBUG = os.environ.get("PHANTOM_BRIDGE_DEBUG") == "1"

_RESOURCES = Path(__file__).resolve().parent / "resources"
EXAMPLE_CARRIER = _RESOURCES / "example_carrier.png"
EXAMPLE_PDF = _RESOURCES / "example_payload.pdf"

_KDF_BY_NAME = {profile.label: profile for profile in C.KDF_PROFILES.values()}


# --------------------------------------------------------------------------- helpers

def _ok(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def _fail(error: PhantomError) -> dict[str, Any]:
    params = dict(error.params)
    body: dict[str, Any] = {
        "code": error.code.value,
        "message": error.message or error.code.value,
        "detail": error.detail,
    }
    if params:
        body["params"] = params
    return {"ok": False, "error": body}


def _require_str(request: dict[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"missing or invalid {key}")
    return value


def _resolve_kdf(request: dict[str, Any]) -> KdfProfile | None:
    name = request.get("kdf_profile")
    if name is None:
        return None  # core default: Argon2id, never a silent downgrade
    if not isinstance(name, str):
        raise InvalidInput("kdf_profile must be a string")
    profile = _KDF_BY_NAME.get(name)
    if profile is None:
        raise InvalidInput("unknown kdf_profile", detail=name)
    return profile


def _check_password(password: object) -> str:
    if not isinstance(password, str) or not password:
        raise InvalidPassword("password is empty")
    if len(password) < C.MIN_PASSWORD_LENGTH:
        raise InvalidPassword(
            "password too short",
            detail=f"minimum {C.MIN_PASSWORD_LENGTH} characters",
        ).with_params(minimum=C.MIN_PASSWORD_LENGTH)
    return password


def _kind_str(kind: PayloadKind) -> str:
    return "text" if kind is PayloadKind.TEXT else "file"


def _file_fields(path: str) -> dict[str, Any]:
    stat = os.stat(path)
    return {"name": os.path.basename(path), "size_bytes": stat.st_size}


def _carrier_fields(probe: api.CarrierProbe) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": probe.kind.value,
        "path": probe.path,
        "capacity_bytes": probe.capacity.payload_bytes,
        "usable_bits": probe.capacity.usable_bits,
        "description": probe.description,
        "notices": list(probe.notices),
    }
    out.update(_file_fields(probe.path))
    if probe.image is not None:
        out["image"] = {
            "width": probe.image.width,
            "height": probe.image.height,
            "mode": probe.image.mode,
        }
    if probe.audio is not None:
        out["audio"] = {
            "frames": probe.audio.frames,
            "channels": probe.audio.channels,
            "bits": probe.audio.bits,
            "sample_rate": probe.audio.sample_rate,
        }
    return out


# Preview limits are independent of the core's container/file-size limits.
# Only a small, freshly encoded image crosses IPC; the original stays in memory.
PREVIEW_MAX_SOURCE_BYTES = 8 * 1024 * 1024
PREVIEW_MAX_PIXELS = 20_000_000
PREVIEW_MAX_EDGE = 1024
PREVIEW_MAX_BYTES = 1024 * 1024


def _image_preview(data: bytes) -> dict[str, Any] | None:
    if data.startswith(b"\xff\xd8\xff"):
        kind = "JPEG"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "PNG"
    else:
        return None
    result: dict[str, Any] = {"type": kind, "status": "unavailable"}
    if len(data) > PREVIEW_MAX_SOURCE_BYTES:
        result["reason"] = "size"
        return result
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=[kind]) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > PREVIEW_MAX_PIXELS:
                    result["reason"] = "size"
                    return result
                # Decode only after checking dimensions. Strip metadata and alpha;
                # the preview is not the original and is never used for saving.
                source.load()
                oriented = ImageOps.exif_transpose(source)
                oriented.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.Resampling.LANCZOS)
                if "A" in oriented.getbands() or "transparency" in oriented.info:
                    rgba = oriented.convert("RGBA")
                    preview = Image.new("RGB", rgba.size, "white")
                    preview.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    preview = oriented.convert("RGB")
                with io.BytesIO() as out:
                    preview.save(out, format="JPEG", quality=80, optimize=True)
                    encoded = out.getvalue()
                if len(encoded) > PREVIEW_MAX_BYTES:
                    result["reason"] = "size"
                    return result
                result.update(status="available", mime="image/jpeg", width=preview.width,
                              height=preview.height, data=base64.b64encode(encoded).decode("ascii"))
                return result
    except Exception:  # noqa: BLE001 - preview is optional; decoding must never block saving
        result["reason"] = "decode"
        return result


# --------------------------------------------------------------------------- commands

def cmd_version(_: dict[str, Any]) -> dict[str, Any]:
    return _ok(version=__version__, protocol=PROTOCOL)


def cmd_probe(request: dict[str, Any]) -> dict[str, Any]:
    path = _require_str(request, "path")
    probe = api.probe_carrier(path)
    return _ok(**_carrier_fields(probe))


def cmd_hide(request: dict[str, Any]) -> dict[str, Any]:
    carrier_path = request.get("carrier_path")
    builtin_id = request.get("builtin_carrier_id")
    has_path = isinstance(carrier_path, str) and bool(carrier_path)
    has_builtin = isinstance(builtin_id, str) and bool(builtin_id)
    if has_path == has_builtin:
        raise InvalidInput("provide exactly one of carrier_path or builtin_carrier_id")
    if has_builtin:
        assert isinstance(builtin_id, str)
        carrier = builtin_carriers.resolve_builtin_carrier(builtin_id).path
    else:
        assert isinstance(carrier_path, str)
        carrier = carrier_path

    output = _require_str(request, "output_path")
    password = _check_password(request.get("password"))
    profile = _resolve_kdf(request)

    text = request.get("text")
    secret_path = request.get("secret_path")
    if (text is None) == (secret_path is None):
        raise InvalidInput("provide exactly one of text or secret_path")

    if secret_path is not None:
        if not isinstance(secret_path, str) or not os.path.isfile(secret_path):
            detail = secret_path if isinstance(secret_path, str) else None
            raise InvalidInput("secret file not found", detail=detail)
        payload = None
    else:
        if not isinstance(text, str):
            raise InvalidInput("text must be a string")
        payload = Payload.text(text)

    outcome = api.embed_payload(
        carrier,
        output,
        payload,
        secret_path=secret_path,
        password=password,
        profile=profile,
        wipe_source=False,
        verify=True,
    )
    return _ok(
        output_path=outcome.output_path,
        kind=outcome.kind.value,
        payload_kind=_kind_str(outcome.payload_kind),
        payload_bytes=outcome.payload_bytes,
        user_bytes=outcome.user_bytes,
        capacity_bytes=outcome.capacity.payload_bytes,
        verified=outcome.verified,
        wiped_source=outcome.wiped_source,
        notices=list(outcome.notices),
        profile_label=outcome.profile_label,
        duration_seconds=round(outcome.duration_seconds, 3),
    )


def _extract_verified(path: str, password: str) -> api.ExtractOutcome:
    outcome = api.extract_from_path(path, password)
    if not outcome.payload.integrity_ok:  # defence in depth; unpack raises first
        raise InternalError("integrity flag lost between core and bridge")
    return outcome


def cmd_extract(request: dict[str, Any]) -> dict[str, Any]:
    path = _require_str(request, "carrier_path")
    password = _check_password(request.get("password"))
    mode = request.get("mode", "info")
    if mode not in ("info", "save_file", "verify"):
        raise InvalidInput("mode must be 'info', 'verify' or 'save_file'")

    if mode == "verify":
        _extract_verified(path, password)
        return _ok(verified=True)

    if mode == "save_file":
        output = _require_str(request, "output_path")
        ensure_distinct(path, output)
        outcome = _extract_verified(path, password)
        write_bytes_verified(
            output, outcome.payload.data, expected_sha256=outcome.payload.sha256_actual
        )
        payload = outcome.payload
        return _ok(
            kind=payload.kind.value,
            output_path=output,
            original_size=payload.original_size,
            sha256=payload.sha256_actual,
            integrity_ok=payload.integrity_ok,
            container_version=payload.container_version,
            kdf_label=payload.kdf_label,
            suggested_name=_suggested_name(payload),
            duration_seconds=round(outcome.duration_seconds, 3),
        )

    outcome = _extract_verified(path, password)
    payload = outcome.payload
    result: dict[str, Any] = {
        "kind": _kind_str(payload.kind),
        "carrier_kind": outcome.carrier_kind.value,
        "original_size": payload.original_size,
        "sha256": payload.sha256_actual,
        "integrity_ok": payload.integrity_ok,
        "container_version": payload.container_version,
        "kdf_label": payload.kdf_label,
        "suggested_name": _suggested_name(payload),
        "duration_seconds": round(outcome.duration_seconds, 3),
    }
    if payload.kind is PayloadKind.FILE:
        preview = _image_preview(payload.data)
        if preview is not None:
            result["image_preview"] = preview
    if payload.kind is PayloadKind.TEXT:
        text_bytes = payload.data
        if len(text_bytes) <= MAX_RETURNED_TEXT:
            result["text"] = text_bytes.decode("utf-8")
        else:
            result["text_omitted"] = True
    return _ok(**result)


def _suggested_name(payload: ExtractedPayload | Payload) -> str:
    """A harmless default file name for the save dialog (file payloads only)."""
    if payload.kind is not PayloadKind.FILE:
        return "phantom-extracted.txt"
    try:
        return sanitise_filename(payload.name)
    except PhantomError:
        return "phantom-extracted.bin"


def cmd_example_info(_: dict[str, Any]) -> dict[str, Any]:
    for item in (EXAMPLE_CARRIER, EXAMPLE_PDF):
        if not item.is_file():
            raise InvalidInput("bundled example is incomplete", detail=item.name)
    return _ok(
        demo=True,
        carrier=_file_fields(str(EXAMPLE_CARRIER)),
        secret=_file_fields(str(EXAMPLE_PDF)),
    )


def cmd_example_hide(request: dict[str, Any]) -> dict[str, Any]:
    output = _require_str(request, "output_path")
    password = _check_password(request.get("password"))
    if not EXAMPLE_CARRIER.is_file() or not EXAMPLE_PDF.is_file():
        raise InvalidInput("bundled example is incomplete")
    outcome = api.embed_payload(
        str(EXAMPLE_CARRIER),
        output,
        None,
        secret_path=str(EXAMPLE_PDF),
        password=password,
        wipe_source=False,
        verify=True,
    )
    return _ok(
        output_path=outcome.output_path,
        kind=outcome.kind.value,
        payload_kind=_kind_str(outcome.payload_kind),
        payload_bytes=outcome.payload_bytes,
        user_bytes=outcome.user_bytes,
        capacity_bytes=outcome.capacity.payload_bytes,
        verified=outcome.verified,
        notices=list(outcome.notices),
        profile_label=outcome.profile_label,
        duration_seconds=round(outcome.duration_seconds, 3),
        demo=True,
    )


def _builtin_carrier_fields(probe: api.CarrierProbe) -> dict[str, Any]:
    return {
        **_carrier_fields(probe),
        "builtin": True,
        "builtin_id": builtin_carriers.builtin_carrier_id(probe),
    }


def cmd_builtin_carriers(_: dict[str, Any]) -> dict[str, Any]:
    probes = builtin_carriers.list_builtin_carriers()
    items = [_builtin_carrier_fields(p) for p in probes]
    max_cap = max((p.capacity.payload_bytes for p in probes), default=0)
    return _ok(count=len(items), max_capacity_bytes=max_cap, carriers=items)


def cmd_select_carrier(request: dict[str, Any]) -> dict[str, Any]:
    secret_path = request.get("secret_path")
    text = request.get("text")
    required_bytes_raw = request.get("required_bytes")

    if secret_path is not None and not isinstance(secret_path, str):
        raise InvalidInput("secret_path must be a string")
    if text is not None and not isinstance(text, str):
        raise InvalidInput("text must be a string")
    if required_bytes_raw is not None and (
        isinstance(required_bytes_raw, bool) or not isinstance(required_bytes_raw, int)
    ):
        raise InvalidInput("required_bytes must be an integer")

    exclude_id = request.get("exclude_id")
    if exclude_id is not None and not isinstance(exclude_id, str):
        raise InvalidInput("exclude_id must be a string")

    exclude_path = request.get("exclude_path")
    if exclude_path is not None and not isinstance(exclude_path, str):
        raise InvalidInput("exclude_path must be a string")

    offset = request.get("offset", 0)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidInput("offset must be a non-negative integer")

    req_bytes = builtin_carriers.estimate_required_bytes(
        secret_path=secret_path,
        text=text,
        required_bytes=required_bytes_raw,
    )
    selection = builtin_carriers.select_builtin_carrier(
        required_bytes=req_bytes,
        exclude_id=exclude_id,
        exclude_path=exclude_path,
        offset=offset,
    )
    carrier_payload = (
        _builtin_carrier_fields(selection.carrier)
        if selection.carrier is not None
        else None
    )
    return _ok(
        fits=selection.fits,
        required_bytes=selection.required_bytes,
        max_capacity_bytes=selection.max_capacity_bytes,
        suitable_count=selection.suitable_count,
        index=selection.index,
        carrier=carrier_payload,
    )


# --------------------------------------------------------------- metadata tab

#: The interface is not a report writer: it renders the same field objects the
#: Python panel does, so the two surfaces cannot disagree about what a field is.
#: These serialisers add the locale keys and their placeholder values that the
#: core keeps out of ``to_dict`` (the JSON export is deliberately flat).


def _params(pairs: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return dict(pairs)


def _metadata_field_dict(item: MetadataField) -> dict[str, Any]:
    body = item.to_dict()
    body.update(
        reason_params=_params(item.reason_params),
        value_params=_params(item.value_params),
        protected=item.protected,
        sensitive=item.sensitive,
        category_rank=item.category.rank,
    )
    return body


def _metadata_report_dict(report: MetadataReport) -> dict[str, Any]:
    # ``sorted_fields`` is category order first, so the interface can render
    # groups in the order the model defines instead of re-sorting them.
    return {
        "path": report.path,
        "format": report.fmt.value,
        "size_bytes": report.size_bytes,
        "sha256": report.sha256,
        "counts": report.counts.to_dict(),
        "derived_counts": report.derived_counts.to_dict(),
        "technical": report.technical,
        "technical_summary": metadata_mod.technical_summary(report),
        "checksum_ok": report.checksum_ok,
        "warnings": list(report.warnings),
        "fields": [_metadata_field_dict(item) for item in report.sorted_fields()],
        "chunks": [chunk.to_dict() for chunk in report.chunks],
        "provenance": report.provenance.to_dict(),
        "removable": sorted(report.removable_keys),
    }


def _metadata_plan_dict(decision: CleaningPlan) -> dict[str, Any]:
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "remove": [_metadata_field_dict(item) for item in decision.remove],
        "keep": [_metadata_field_dict(item) for item in decision.keep],
        "blocked": [_metadata_field_dict(item) for item in decision.blocked],
        "warnings": list(decision.warnings),
    }


def _metadata_outcome_dict(outcome: CleaningOutcome) -> dict[str, Any]:
    return {
        "source": outcome.source,
        "output": outcome.output,
        "removed": [_metadata_field_dict(item) for item in outcome.removed],
        "kept": [_metadata_field_dict(item) for item in outcome.kept],
        "verified": outcome.verified,
        "checksum_ok": outcome.checksum_ok,
        "source_sha256": outcome.source_sha256,
        "output_sha256": outcome.output_sha256,
        "content": outcome.comparison.to_dict(),
        "warnings": list(outcome.warnings),
        "report_after": _metadata_report_dict(outcome.report_after),
    }


def _metadata_selection(request: dict[str, Any]) -> frozenset[str]:
    """The keys the user ticked; anything but a list of strings is a bad request."""
    raw = request.get("selection", [])
    if not isinstance(raw, list):
        raise InvalidInput("selection must be a list of keys")
    keys: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            raise InvalidInput("selection must contain non-empty strings")
        keys.add(entry)
    return frozenset(keys)


def cmd_metadata_inspect(request: dict[str, Any]) -> dict[str, Any]:
    path = _require_str(request, "path")
    report = metadata_mod.inspect_path(path)
    selection = metadata_mod.default_selection(report)
    # The plan is computed here, not in the shell, so the first preview the user
    # sees is the core's answer rather than a second implementation of the policy.
    decision = metadata_mod.plan(report, selection)
    return _ok(
        report=_metadata_report_dict(report),
        default_selection=sorted(selection),
        plan=_metadata_plan_dict(decision),
    )


def cmd_metadata_plan(request: dict[str, Any]) -> dict[str, Any]:
    path = _require_str(request, "path")
    selection = _metadata_selection(request)
    report = metadata_mod.inspect_path(path)
    decision = metadata_mod.plan(report, selection)
    return _ok(
        report=_metadata_report_dict(report),
        selection=sorted(selection),
        plan=_metadata_plan_dict(decision),
    )


def cmd_metadata_output_path(request: dict[str, Any]) -> dict[str, Any]:
    source = _require_str(request, "path")
    output = _require_str(request, "output_path")
    overwrite = request.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise InvalidInput("overwrite must be a boolean")
    target = metadata_mod.output_path(source, output, overwrite=overwrite)
    return _ok(source=source, output=target)


def cmd_metadata_clean(request: dict[str, Any]) -> dict[str, Any]:
    source = _require_str(request, "path")
    output = _require_str(request, "output_path")
    selection = _metadata_selection(request)
    overwrite = request.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise InvalidInput("overwrite must be a boolean")
    outcome = metadata_mod.clean(
        source,
        output,
        selection,
        overwrite=overwrite,
    )
    return _ok(**_metadata_outcome_dict(outcome))


_COMMANDS = {
    "version": cmd_version,
    "probe": cmd_probe,
    "hide": cmd_hide,
    "extract": cmd_extract,
    "example_info": cmd_example_info,
    "example_hide": cmd_example_hide,
    "builtin_carriers": cmd_builtin_carriers,
    "select_carrier": cmd_select_carrier,
    "metadata_inspect": cmd_metadata_inspect,
    "metadata_plan": cmd_metadata_plan,
    "metadata_output_path": cmd_metadata_output_path,
    "metadata_clean": cmd_metadata_clean,
}

def _log_line(cmd: str, ok: bool, code: str | None = None) -> None:
    suffix = f" code={code}" if code else ""
    state = "true" if ok else "false"
    print(f"phantom-bridge cmd={cmd} ok={state}{suffix}", file=sys.stderr, flush=True)


def _invalid_input(message: str, detail: str | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": "invalid_input", "message": message, "detail": detail},
    }


def handle(request_line: str) -> dict[str, Any]:
    try:
        request = json.loads(request_line)
    except json.JSONDecodeError:
        return _invalid_input("request is not JSON")
    if not isinstance(request, dict):
        return _invalid_input("request must be an object")
    cmd = request.get("cmd")
    handler = _COMMANDS.get(cmd) if isinstance(cmd, str) else None
    if handler is None:
        detail = cmd if isinstance(cmd, str) else None
        return _invalid_input("unknown cmd", detail)
    started = time.perf_counter()
    try:
        response = handler(request)
    except PhantomError as exc:
        _log_line(str(cmd), False, exc.code.value)
        return _fail(exc)
    except Exception as exc:  # noqa: BLE001 - the bridge must answer, never crash, never leak
        _log_line(str(cmd), False, "internal")
        if _DEBUG:
            traceback.print_exc(file=sys.stderr)
        return _fail(InternalError("internal error", detail=type(exc).__name__))
    else:
        _log_line(str(cmd), True)
        response["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        return response


def main() -> int:
    # argv is deliberately never read: a password or document bytes must not be
    # passable on the command line, not even by accident.
    stdin = sys.stdin
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        response = handle(line)
        print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
