#!/usr/bin/env python3
"""Read a macOS crash report (``.ips``) and print where the process died.

The ``gui (macOS Aqua, Tk 9.1)`` job dies with ``exit 139`` — SIGSEGV — and until
now the only evidence that reached the pull request was the 2 000-character header
of the report, which names the process and nothing else.  The stack is in the same
file: ``threads[faultingThread].frames`` with an image and a symbol for each frame.

This tool prints it, one line per frame, symbol first:

    CRASH proc=Python signal=SIGSEGV type=EXC_BAD_ACCESS faulting_thread=0
    #0  libtk9.1.dylib  TkMacOSX... + 0x1c
    #1  _tkinter.cpython-312-darwin.so  ...

Usage::

    python tools/parse_crash_report.py                 # the newest report in ~/Library
    python tools/parse_crash_report.py path/to.ips     # a specific one
    python tools/parse_crash_report.py --json out.json # machine-readable summary

Exit codes: 0 a report was parsed, 2 no report found, 1 a report could not be read.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any, Final

#: Where macOS keeps crash reports for the current user.
REPORT_GLOB: Final = "Library/Logs/DiagnosticReports/*.ips"

#: How many frames of the faulting thread to print: enough for a cause, not a dump.
FRAME_LIMIT: Final = 40


def newest_reports(limit: int = 1, home: pathlib.Path | None = None) -> list[pathlib.Path]:
    """The ``limit`` newest ``.ips`` files under ``home``, newest last."""
    base = pathlib.Path.home() if home is None else home
    try:
        reports = sorted(
            (path for path in base.glob(REPORT_GLOB) if path.is_file()),
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return []
    return reports[-limit:]


def _load(path: pathlib.Path) -> dict[str, Any]:
    """Both halves of an ``.ips`` file: the header line and the JSON body.

    macOS writes the header on the first line and the report as a second JSON
    object; some tools rewrite the pair as one object.  Both are read, and a file
    that yields neither raises ``json.JSONDecodeError`` — an unreadable report must
    not be printed as a crash with every field empty.
    """
    text = path.read_text(errors="replace")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            single = json.loads(stripped)
        except json.JSONDecodeError:
            single = None
        if isinstance(single, dict) and ("threads" in single or "exception" in single):
            return single
    lines = text.splitlines()
    for index, line in enumerate(lines):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            header = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        body = "\n".join(lines[index + 1 :]).strip()
        if not body:
            return {"header": header}
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise json.JSONDecodeError(
                f"the header parsed but the report body did not: {exc.msg}", body, exc.pos
            ) from exc
        payload.setdefault("header", header)
        return payload
    raise json.JSONDecodeError("no JSON object anywhere in this file", text[:200], 0)


def _image_name(payload: dict[str, Any], index: int) -> str:
    images = payload.get("usedImages") or []
    if not isinstance(images, list) or not 0 <= index < len(images):
        return f"image[{index}]"
    image = images[index]
    if not isinstance(image, dict):
        return f"image[{index}]"
    return str(image.get("name") or image.get("path") or f"image[{index}]")


def summarise(payload: dict[str, Any]) -> dict[str, Any]:
    """The parts of a report worth printing, with no Tk knowledge at all."""
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    exception = payload.get("exception") if isinstance(payload.get("exception"), dict) else {}
    termination = payload.get("termination") if isinstance(payload.get("termination"), dict) else {}
    threads = payload.get("threads") if isinstance(payload.get("threads"), list) else []
    index = payload.get("faultingThread")
    faulting = None
    if isinstance(index, int) and 0 <= index < len(threads):
        faulting = threads[index]
    elif threads:
        for thread in threads:
            if isinstance(thread, dict) and thread.get("triggered"):
                faulting = thread
                break
    frames: list[str] = []
    if isinstance(faulting, dict):
        for number, frame in enumerate(faulting.get("frames") or []):
            if number >= FRAME_LIMIT or not isinstance(frame, dict):
                break
            image = _image_name(payload, int(frame.get("imageIndex", 0) or 0))
            symbol = frame.get("symbol")
            offset = frame.get("imageOffset")
            if symbol:
                where = f"{symbol} + {offset}" if offset is not None else str(symbol)
            else:
                where = f"image + {offset}"
            frames.append(f"#{number} {image} {where}")
    return {
        "path": str(payload.get("path", "")),
        "proc": str(payload.get("procName") or header.get("app_name") or "?"),
        "timestamp": str(header.get("timestamp") or payload.get("captureTime") or "?"),
        "signal": str(exception.get("signal") or termination.get("indicator") or "?"),
        "type": str(exception.get("type") or "?"),
        "subtype": str(exception.get("subtype") or ""),
        "indicator": str(termination.get("indicator") or ""),
        "faulting_thread": payload.get("faultingThread"),
        "frames": frames,
    }


def format_summary(summary: dict[str, Any]) -> str:
    """The block the CI annotation carries, joined on one line by the caller."""
    head = (
        f"CRASH proc={summary['proc']} signal={summary['signal']} "
        f"type={summary['type']} thread={summary['faulting_thread']}"
    )
    if summary["indicator"]:
        head += f" indicator={summary['indicator']!r}"
    if summary["subtype"]:
        head += f" subtype={summary['subtype']!r}"
    lines = [head]
    lines.extend(summary["frames"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", help="a .ips file; default: the newest one")
    parser.add_argument("--json", metavar="PATH", help="write the summary here")
    parser.add_argument("--limit", type=int, default=1, help="how many newest reports to read")
    parser.add_argument(
        "--since",
        type=float,
        metavar="SECONDS",
        help="only reports written in the last SECONDS (a run of its own)",
    )
    args = parser.parse_args(argv)

    reports = (
        [pathlib.Path(args.report)] if args.report else newest_reports(max(1, args.limit))
    )
    if args.since is not None:
        import time

        cutoff = time.time() - args.since
        reports = [path for path in reports if path.stat().st_mtime >= cutoff]
    if not reports:
        print("NO REPORT: nothing in ~/Library/Logs/DiagnosticReports")
        return 2

    summaries: list[dict[str, Any]] = []
    for report in reports:
        try:
            payload = _load(report)
        except OSError as exc:
            print(f"UNREADABLE: {report}: {exc}")
            return 1
        except json.JSONDecodeError as exc:
            print(f"UNREADABLE: {report}: not JSON ({exc})")
            return 1
        summary = summarise(payload)
        summary["path"] = str(report)
        summaries.append(summary)
        print(f"--- {report.name}")
        print(format_summary(summary))

    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(summaries, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
