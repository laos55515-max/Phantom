"""One capture/no-capture pair on a fixed product tree, with native macOS samples.

This is evidence collection, NOT an alternative startup gate. Neither a green
exit here nor a sub-2000 ms gap proves the intermittent failure has been fixed.
The required gate in ci.yml remains unchanged.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any


def overlapping_work(gap: dict[str, Any], timings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Overlap is correlation, not evidence that a capture stage blocked Tk."""
    return [
        item for item in timings
        if item["start_ms"] < gap["end_ms"]
        and item.get("end_ms", float("inf")) > gap["start_ms"]
    ]


def native_main_thread(text: str) -> str:
    """A bounded main-thread excerpt for the API annotations; raw file is retained."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "Thread_" in line and ("Main Thread" in line or "com.apple.main-thread" in line):
            kept = [line]
            for following in lines[index + 1:]:
                if "Thread_" in following:
                    break
                kept.append(following)
                if len(kept) >= 35:
                    break
            return " | ".join(kept)[:6000]
    return "Main thread not found; inspect native.txt and native.log (not a valid native sample)."


def measure(output: pathlib.Path, tree: pathlib.Path, label: str, capture: bool) -> dict[str, Any]:
    path = output / f"{label}.json"
    native = output / f"{label}-native.txt"
    command = [
        sys.executable, "-u", "tools/measure_startup_frame.py", "--diagnostic",
        "--label", label, "--watch", "8", "--json", str(path),
    ]
    if not capture:
        command.append("--no-capture")
    launched = time.perf_counter()
    with (output / f"{label}.log").open("w") as log, subprocess.Popen(
        command, stdout=log, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONPATH": str(tree / "src")},
    ) as child:
        # Separate OS process: it can observe the main thread even when a Python
        # thread cannot acquire the GIL. Same sampler and cadence in both modes.
        native_started = time.perf_counter()
        with (output / f"{label}-native.log").open("w") as native_log, subprocess.Popen(
            ["sample", str(child.pid), "10", "10", "-file", str(native)],
            stdout=native_log, stderr=subprocess.STDOUT,
        ) as sampler:
            try:
                code = child.wait(timeout=60)
                native_code = sampler.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                sampler.kill()
                child.wait()
                sampler.wait()
                raise
    data = json.loads(path.read_text()) if path.exists() else {}
    worst = max(data.get("diagnostic_gaps", []), key=lambda item: item["duration_ms"], default={})
    row = {
        "label": label, "capture_requested": capture, "exit": code,
        "native_exit": native_code, "native_file_exists": native.exists(),
        "launch_perf_counter": launched, "native_started_perf_counter": native_started,
        "build_ms": data.get("build_ms"), "loop_ms": data.get("loop_ms"),
        "idle_ms": data.get("idle_ms"), "first_paint_ms": data.get("first_paint_ms"),
        "worst_gap_ms": data.get("worst_gap_ms"), "worst_gap": worst,
        "overlapping_capture_work": (
            overlapping_work(worst, data.get("capture_timings", [])) if worst else []
        ),
        "tab_visible": data.get("tab_visible"), "close_exists": data.get("close_exists"),
    }
    print("::notice::" + json.dumps(row), flush=True)
    excerpt = (
        native_main_thread(native.read_text(errors="replace"))
        if native.exists() else "No native file"
    )
    print(f"::notice::native {label}: {excerpt}", flush=True)
    (output / f"{label}-summary.json").write_text(json.dumps(row, indent=2))
    # A crash, missing measurement, native permission failure or non-answering
    # window is NOT a successful experiment. Heartbeat values are not filtered.
    if code != 0 or not data or not native.exists() or native_code != 0:
        raise RuntimeError(f"Incomplete measurement {label}; inspect raw artifacts")
    if not row["tab_visible"] or row["close_exists"]:
        raise RuntimeError(f"Window did not answer/close in {label}")
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pair", type=int, required=True, choices=(1, 2, 3))
    parser.add_argument("--tree", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    modes = [True, False] if args.pair % 2 else [False, True]
    for capture in modes:
        label = f"pair-{args.pair}-{'capture' if capture else 'no-capture'}"
        measure(args.output.resolve(), args.tree.resolve(), label, capture)


if __name__ == "__main__":
    main()
