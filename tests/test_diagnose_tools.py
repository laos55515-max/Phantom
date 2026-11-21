"""The two diagnostic tools have to be right about what they report.

``tools/parse_crash_report.py`` is what turns a macOS ``.ips`` into the frames of
the faulting thread — the only evidence there is for a crash that leaves no
traceback — and ``tools/diagnose_tk_crash.py`` is what names the variant that died.
Both are checked here on synthetic input: a wrong parser would report a crash the
run never had, or hide one it did.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

TOOLS = pathlib.Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import diagnose_tk_crash  # noqa: E402 - after the path insert above
import parse_crash_report  # noqa: E402 - after the path insert above

HEADER = {
    "app_name": "Python",
    "timestamp": "2026-11-20 23:33:50.00 +0000",
    "bug_type": "309",
    "os_version": "macOS 26.7.2 (25G332)",
}

BODY = {
    "procName": "Python",
    "captureTime": "2026-11-20 23:33:50",
    "exception": {
        "type": "EXC_BAD_ACCESS",
        "signal": "SIGSEGV",
        "subtype": "KERN_INVALID_ADDRESS at 0x0000000000000010",
    },
    "termination": {"indicator": "Segmentation fault: 11", "code": 11},
    "faultingThread": 0,
    "threads": [
        {
            "triggered": True,
            "frames": [
                {
                    "imageIndex": 3,
                    "imageOffset": 1234,
                    "symbol": "TkMacOSXFlushWindows",
                    "symbolLocation": 28,
                },
                {"imageIndex": 1, "imageOffset": 88},
                {"imageIndex": 9, "imageOffset": 4},  # no such image: must not raise
            ],
        },
        {"triggered": False, "frames": [{"imageIndex": 0, "imageOffset": 1}]},
    ],
    "usedImages": [
        {"name": "dyld"},
        {"name": "_tkinter.cpython-312-darwin.so"},
        {},
        {"name": "libtk9.1.dylib", "path": "/opt/homebrew/lib/libtk9.1.dylib"},
    ],
}


def _write_ips(path: pathlib.Path, *, one_object: bool = False) -> pathlib.Path:
    if one_object:
        path.write_text(json.dumps({**HEADER, **BODY}), encoding="utf-8")
    else:
        path.write_text(
            json.dumps(HEADER) + "\n" + json.dumps(BODY, indent=2) + "\n", encoding="utf-8"
        )
    return path


def test_a_two_part_report_is_read_as_macos_writes_it(tmp_path: pathlib.Path) -> None:
    payload = parse_crash_report._load(_write_ips(tmp_path / "crash.ips"))
    summary = parse_crash_report.summarise(payload)
    assert summary["proc"] == "Python"
    assert summary["signal"] == "SIGSEGV"
    assert summary["type"] == "EXC_BAD_ACCESS"
    assert summary["faulting_thread"] == 0
    assert summary["timestamp"].startswith("2026-11-20")
    assert summary["frames"][0] == "#0 libtk9.1.dylib TkMacOSXFlushWindows + 1234"
    assert summary["frames"][1] == "#1 _tkinter.cpython-312-darwin.so image + 88"
    # A frame that points at an image the report does not list is still printed.
    assert summary["frames"][2].startswith("#2 image[9]")


def test_a_report_written_as_one_object_is_read_too(tmp_path: pathlib.Path) -> None:
    payload = parse_crash_report._load(_write_ips(tmp_path / "one.ips", one_object=True))
    assert parse_crash_report.summarise(payload)["signal"] == "SIGSEGV"


def test_the_printed_block_names_the_signal_and_the_first_frames(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write_ips(tmp_path / "crash.ips")
    assert parse_crash_report.main([str(report)]) == 0
    printed = capsys.readouterr().out
    assert "CRASH proc=Python signal=SIGSEGV" in printed
    assert "#0 libtk9.1.dylib TkMacOSXFlushWindows" in printed


def test_no_report_is_not_a_crash(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert parse_crash_report.newest_reports(1, home=tmp_path) == []
    assert parse_crash_report.main(["--limit", "3", "--since", "60"]) in {0, 2}
    if parse_crash_report.main(["--limit", "3", "--since", "60"]) == 2:
        assert "NO REPORT" in capsys.readouterr().out


def test_the_newest_report_wins(tmp_path: pathlib.Path) -> None:
    reports = tmp_path / "Library" / "Logs" / "DiagnosticReports"
    reports.mkdir(parents=True)
    first = _write_ips(reports / "Python-1.ips")
    second = _write_ips(reports / "Python-2.ips")
    first.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
    second.write_text(second.read_text(encoding="utf-8"), encoding="utf-8")
    newest = parse_crash_report.newest_reports(2, home=tmp_path)
    assert [path.name for path in newest] == ["Python-1.ips", "Python-2.ips"]


def test_a_truncated_report_is_reported_not_crashed(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.ips"
    broken.write_text("{not json at all", encoding="utf-8")
    assert parse_crash_report.main([str(broken)]) == 1
    assert "UNREADABLE" in capsys.readouterr().out


# -- the variant table --------------------------------------------------------
def test_every_variant_is_described_and_typed() -> None:
    assert diagnose_tk_crash.VARIANTS
    for name, (kind, description) in diagnose_tk_crash.VARIANTS.items():
        assert name and description, name
        assert kind in {"product", "harness", "probe"}, name


def test_only_product_variants_gate() -> None:
    """A harness crash is reported; a crash in the application's own code is red."""
    assert set(diagnose_tk_crash.GATED_KINDS) == {"product"}
    kinds = {kind for kind, _ in diagnose_tk_crash.VARIANTS.values()}
    assert "product" in kinds and "harness" in kinds


def test_an_exit_code_is_reported_the_way_a_shell_sees_it() -> None:
    assert diagnose_tk_crash._exit_text(0) == "exit 0"
    assert diagnose_tk_crash._exit_text(-11) == "signal 11 (exit 139)"
    assert diagnose_tk_crash._exit_text(3) == "exit 3"


def test_a_machine_without_a_display_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    try:
        import tkinter  # noqa: F401, PLC0415 - the check the tool itself makes
    except Exception:  # noqa: BLE001 - no tkinter here: the tool must not raise
        assert diagnose_tk_crash.main([]) == 3
        assert "tkinter is not usable" in capsys.readouterr().out
    else:
        pytest.skip("this machine has tkinter: the no-display path cannot be measured here")


def test_an_unknown_variant_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    code = diagnose_tk_crash.main(["--only", "nope"])
    assert code in {3}
    assert "unknown variants" in capsys.readouterr().out
