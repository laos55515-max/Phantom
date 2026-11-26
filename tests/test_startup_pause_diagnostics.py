"""Attribution helpers; a correlation must never be presented as a cause."""

from __future__ import annotations

import pathlib
import sys

TOOLS = pathlib.Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import diagnose_startup_pause as D  # noqa: E402


def test_only_capture_stages_overlapping_the_gap_are_attached() -> None:
    stages = [
        {"stage": "before", "start_ms": 1, "end_ms": 100},
        {"stage": "inside", "start_ms": 200, "end_ms": 300},
        {"stage": "unfinished", "start_ms": 400},
        {"stage": "after", "start_ms": 500, "end_ms": 600},
    ]
    assert D.overlapping_work({"start_ms": 100, "end_ms": 500}, stages) == stages[1:3]


def test_native_excerpt_does_not_mistake_a_worker_for_the_main_thread() -> None:
    text = "\n".join([
        "Call graph:", "  Thread_10 DispatchQueue_1: com.apple.main-thread (serial)",
        "    10 Tk_MainLoop", "      10 mach_msg_trap", "  Thread_20 worker",
        "    10 decode_png",
    ])
    excerpt = D.native_main_thread(text)
    assert "Tk_MainLoop" in excerpt
    assert "decode_png" not in excerpt
    assert "not found" in D.native_main_thread("Permission denied")
