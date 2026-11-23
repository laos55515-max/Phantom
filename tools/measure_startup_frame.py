"""Measure how long the window takes to *show its content*, not to be built.

The owner's report, on a MacBook Pro M1 Pro under Tcl/Tk 9.1 Aqua: on repeated
launches from Terminal a PHANTOM frame appears with nothing in it, the pointer
shows the wait cursor for three to five seconds, and the interface only appears
once the window is clicked.  The internal ``ready`` mark is 0.48–0.56 s, which
measures the *build*: ``StartupTrace`` stops when ``<Map>`` arrives, and a mapped
window is not a painted one.  Nothing in the project measured pixels, so nothing
could tell the two apart — this tool does.

What it measures, in one process, on the real window and the real event loop:

``build_ms``     empty ``CTk`` root to constructed ``PhantomApp``.
``loop_ms``      ``mainloop`` entry (first ``after(0)``).
``mapped_ms``    first ``<Map>`` on the toplevel after the loop starts.
``expose_ms``    first ``<Expose>`` on the toplevel or its first content child.
``idle_ms``      first ``after_idle`` callback served: is the loop running at all.
``worst_gap_ms`` longest gap between 10 ms heartbeat ticks (stalled loop / beachball).
``first_paint``  the first screen capture in which the window's own rectangle is
                 **not** a uniform colour — i.e. the content is on screen.  The
                 capture is taken by ``screencapture`` from a thread, decoded here
                 (no Tk from that thread), and the window rectangle comes from
                 ``winfo_rootx/rooty/width/height`` scaled by the capture.
``focus_ms``     when the window becomes visible only after being pushed forward
                 (``lift``/``-topmost`` — the script-level equivalent of the click
                 the owner uses), this is when that made the difference.
``tab_ms``       invoking the "Метаданные" tab button on the segmented tab bar,
                 timed, plus whether the tab is selected and mapped.
``close_ms``     the command the window's close button runs, timed, plus whether the
                 toplevel really went away.

Screenshots are the only honest way to answer "is it drawn": Tk on Aqua offers no
API for it, ``canvas.postscript`` describes the canvas model rather than the
screen, and ``update idletasks`` is exactly the call this project refuses on this
platform (a refused call returns instantly and would report a beautiful number for
a blank window).  When the capture cannot be taken or decoded — no screen recording
permission for the runner, an unknown PNG layout — the tool says ``capture=no`` and
does not pretend: the pixel question is then unanswered, everything else still is.

One run per process: this platform is the one where a process that builds and
destroys several windows crashes inside Tk itself (docs/macos-aqua-crash.md §15),
so "repeated launches" means repeated processes.

    python tools/measure_startup_frame.py --label before --json /tmp/before.json

Every line starts with ``FRAME:``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import zlib
from dataclasses import dataclass, field
from typing import Any, Final

#: How long one run watches the window before it closes itself.
WATCH_SECONDS: Final = 8.0

#: When the "push the window forward" experiment happens, if nothing is drawn yet.
PUSH_AT: Final = 2.5

#: How often the capture thread takes a screenshot.
SAMPLE_INTERVAL: Final = 0.25

#: A rectangle of the window is "drawn" when it holds at least this many distinct
#: quantised colours and this fraction of its pixels differs from the commonest one.
#: An empty Aqua window is one colour plus its title bar; a built PHANTOM window is
#: cards, captions, buttons and a dark background.
MIN_COLOURS: Final = 5
MIN_FRACTION: Final = 0.02


def say(message: str) -> None:
    print(f"FRAME: {message}", flush=True)


# ---------------------------------------------------------------------------
# PNG: just enough of it to sample pixels
# ---------------------------------------------------------------------------
def _unfilter(raw: bytes, height: int, channels: int, stride: int) -> list[bytearray]:
    """Undo PNG's per-scanline filters; ``raw`` is the decompressed IDAT stream."""
    rows: list[bytearray] = []
    previous = bytearray(stride)
    position = 0
    for _ in range(height):
        if position >= len(raw):
            break
        kind = raw[position]
        position += 1
        line = bytearray(raw[position : position + stride])
        position += stride
        if len(line) < stride:
            line.extend(b"\x00" * (stride - len(line)))
        if kind == 1:  # Sub
            for index in range(channels, stride):
                line[index] = (line[index] + line[index - channels]) & 0xFF
        elif kind == 2:  # Up
            for index in range(stride):
                line[index] = (line[index] + previous[index]) & 0xFF
        elif kind == 3:  # Average
            for index in range(stride):
                left = line[index - channels] if index >= channels else 0
                line[index] = (line[index] + ((left + previous[index]) >> 1)) & 0xFF
        elif kind == 4:  # Paeth
            for index in range(stride):
                left = line[index - channels] if index >= channels else 0
                up = previous[index]
                up_left = previous[index - channels] if index >= channels else 0
                estimate = left + up - up_left
                distance_left = abs(estimate - left)
                distance_up = abs(estimate - up)
                distance_up_left = abs(estimate - up_left)
                if distance_left <= distance_up and distance_left <= distance_up_left:
                    predictor = left
                elif distance_up <= distance_up_left:
                    predictor = up
                else:
                    predictor = up_left
                line[index] = (line[index] + predictor) & 0xFF
        rows.append(line)
        previous = line
    return rows


@dataclass(slots=True)
class Pixels:
    width: int
    height: int
    rows: list[bytearray]
    channels: int

    def at(self, x: int, y: int) -> tuple[int, int, int]:
        index = x * self.channels
        row = self.rows[y]
        if self.channels >= 3:
            return row[index], row[index + 1], row[index + 2]
        return row[index], row[index], row[index]


def decode_png(path: pathlib.Path) -> Pixels:
    """Decode a non-interlaced 8-bit PNG into rows; ``ValueError`` if unsupported."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    position = 8
    width = height = channels = depth = colour_type = interlace = 0
    payload = bytearray()
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        position += 12 + length
        if kind == b"IHDR":
            width, height, depth, colour_type, _compression, _filter, interlace = struct.unpack(
                ">IIBBBBB", body
            )
        elif kind == b"IDAT":
            payload.extend(body)
        elif kind == b"IEND":
            break
    if depth != 8 or interlace != 0 or colour_type not in (2, 6):
        raise ValueError(
            f"unsupported PNG: depth={depth} colour={colour_type} interlace={interlace}"
        )
    channels = 3 if colour_type == 2 else 4
    stride = width * channels
    rows = _unfilter(zlib.decompress(bytes(payload)), height, channels, stride)
    if not rows:
        raise ValueError("empty PNG")
    return Pixels(width=width, height=height, rows=rows, channels=channels)


def sample(image: Pixels, left: int, top: int, right: int, bottom: int) -> tuple[int, float]:
    """``(distinct colours, fraction of pixels that differ from the commonest)``."""
    colours, fraction, _bg = sample_detail(image, left, top, right, bottom)
    return colours, fraction


def sample_detail(
    image: Pixels, left: int, top: int, right: int, bottom: int
) -> tuple[int, float, str]:
    """``(distinct colours, changed fraction, commonest quantised hex colour)``."""
    left = max(0, min(left, image.width - 1))
    right = max(left + 1, min(right, image.width))
    top = max(0, min(top, image.height - 1))
    bottom = max(top + 1, min(bottom, image.height))
    step_x = max(1, (right - left) // 96)
    step_y = max(1, (bottom - top) // 96)
    counts: dict[tuple[int, int, int], int] = {}
    total = 0
    for y in range(top, bottom, step_y):
        for x in range(left, right, step_x):
            red, green, blue = image.at(x, y)
            key = (red >> 4, green >> 4, blue >> 4)
            counts[key] = counts.get(key, 0) + 1
            total += 1
    if not total:
        return 0, 0.0, "#000000"
    common_key, commonest = max(counts.items(), key=lambda item: item[1])
    bg_hex = f"#{common_key[0] * 17:02x}{common_key[1] * 17:02x}{common_key[2] * 17:02x}"
    return len(counts), 1.0 - commonest / total, bg_hex


# ---------------------------------------------------------------------------
# the capture thread: no Tk in here
# ---------------------------------------------------------------------------
@dataclass
class Capture:
    """Screenshots taken while the window builds and paints itself."""

    origin: float = 0.0
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    screen: tuple[int, int] = (0, 0)
    samples: list[tuple[float, int, float, str, str]] = field(default_factory=list)
    available: bool = False
    reason: str = ""

    def run(self, until: float) -> None:
        directory = tempfile.mkdtemp(prefix="phantom-frame-")
        path = pathlib.Path(directory) / "shot.png"
        while time.perf_counter() < until:
            if self.rect == (0, 0, 0, 0):
                # Before the first <Configure> there is no rectangle to capture.
                time.sleep(SAMPLE_INTERVAL / 4)
                continue
            left, top, width, height = self.rect
            taken = time.perf_counter()
            done = subprocess.run(
                ["screencapture", "-x", "-o", "-R", f"{left},{top},{width},{height}", str(path)],
                check=False,
                capture_output=True,
            )
            if done.returncode != 0:
                self.reason = (done.stderr or b"?").decode("utf-8", "replace").strip()[:200]
                self.available = False
                return
            try:
                image = decode_png(path)
            except (ValueError, zlib.error, OSError) as exc:
                self.reason = f"{type(exc).__name__}: {exc}"
                self.available = False
                return
            # The capture is in points on this display, the image is in pixels: on a
            # Retina panel they differ by the backing scale, so the whole image is
            # the window when the rectangle was asked for in points.
            colours, fraction, bg_hex = sample_detail(image, 0, 0, image.width, image.height)
            self.samples.append(
                (taken - self.origin, colours, fraction, bg_hex, f"{image.width}x{image.height}")
            )
            self.available = True
            time.sleep(SAMPLE_INTERVAL)


def first_paint(capture: Capture) -> float | None:
    for at, colours, fraction, *_rest in capture.samples:
        if colours >= MIN_COLOURS and fraction >= MIN_FRACTION:
            return at
    return None


# ---------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------
def run(label: str, watch: float) -> dict[str, Any]:
    import tkinter as tk

    import customtkinter as ctk

    from phantom.ui.app import PhantomApp
    from phantom.ui.startup import StartupTrace
    from phantom.ui.widgets import suppressed_flush_count

    ctk.set_default_color_theme("blue")
    started = time.perf_counter()
    trace = StartupTrace(enabled=False)

    # Watch the main thread from outside: if building or the event loop stalls
    # for >= 0.30 s without advancing the heartbeat, sample the Python stack so we
    # know *where* the 3-5 s delay happens instead of guessing.
    main_tid = threading.get_ident()
    phase = ["build"]
    last_beat = [started]
    worst_gap_ms = [0.0]
    worst_gap_at_ms = [0.0]
    beats = [0]
    stalls: list[str] = []
    done_flag = [False]

    def watchdog() -> None:
        while not done_flag[0]:
            time.sleep(0.20)
            if done_flag[0]:
                break
            now = time.perf_counter()
            gap = now - last_beat[0]
            if gap >= 0.30 and len(stalls) < 8:
                frame = sys._current_frames().get(main_tid)
                where = "unknown"
                if frame is not None:
                    extracted = traceback.extract_stack(frame)
                    tail = extracted[-6:]
                    where = " -> ".join(
                        f"{pathlib.Path(item.filename).name}:{item.lineno}({item.name})"
                        for item in tail
                    )
                stalls.append(
                    f"at={now - started:.2f}s phase={phase[0]} gap={gap:.2f}s stack={where}"
                )

    dog = threading.Thread(target=watchdog, daemon=True)
    dog.start()

    # The product's own path, in the product's own order (``app.main``): a plain
    # ``CTk`` root, the window built on it, then ``refresh_window`` and the loop.
    # Nothing is withdrawn: a withdrawn window has no surface to draw into, which is
    # a different measurement from the one the owner reports.
    root = ctk.CTk()
    tk_version = f"{tk.TkVersion} {root.tk.call('info', 'patchlevel')}"
    app = PhantomApp(root, trace=trace)
    build_ms = (time.perf_counter() - started) * 1000.0
    phase[0] = "loop_setup"

    window = app.i18n.t("tab_metadata")
    #: The screenshots taken while the window appears; the thread that takes them never
    #: touches Tk, and the main thread only writes the rectangle it should look at.
    capture = Capture(origin=started)
    result: dict[str, Any] = {
        "label": label,
        "tk": tk_version,
        "build_ms": round(build_ms, 1),
        "window": window,
    }
    marks: dict[str, Any] = {}
    counts: dict[str, int] = {"configure": 0, "expose": 0}

    def mark(name: str) -> None:
        marks.setdefault(name, (time.perf_counter() - started) * 1000.0)

    def on_map(event: Any) -> None:
        if getattr(event, "widget", None) is root:
            mark("mapped")

    expose_widgets: list[str] = []

    def on_expose_any(event: Any) -> None:
        counts["expose"] += 1
        mark("expose_any")
        w = getattr(event, "widget", None)
        if w is root:
            counts["expose_root"] = counts.get("expose_root", 0) + 1
            mark("expose_root")
        if len(expose_widgets) < 20:
            expose_widgets.append(type(w).__name__ if w is not None else "?")
        if counts["expose"] >= 20:
            # Stop after 20 events: binding <Expose> globally on Tk 9.1 Aqua fires
            # ~800k times if an expose storm happens, adding 20s of Python overhead.
            with contextlib.suppress(Exception):
                root.unbind_all("<Expose>")

    def on_visibility(event: Any) -> None:
        if getattr(event, "widget", None) is root:
            mark("visibility")

    def on_focus_in(event: Any) -> None:
        if getattr(event, "widget", None) is root:
            mark("focus_in")

    def on_activate(event: Any) -> None:
        if getattr(event, "widget", None) is root:
            mark("activate")

    def on_configure(event: Any) -> None:
        """The window's own rectangle, in the points ``screencapture -R`` expects."""
        if getattr(event, "widget", None) is not root:
            return
        counts["configure"] += 1
        mark("first_configure")
        marks["last_configure"] = (time.perf_counter() - started) * 1000.0
        width, height = root.winfo_width(), root.winfo_height()
        if width > 1 and height > 1:
            capture.rect = (root.winfo_rootx(), root.winfo_rooty(), width, height)

    def on_idle() -> None:
        mark("idle")

    def on_loop() -> None:
        mark("loop")

    def beat() -> None:
        now = time.perf_counter()
        gap_ms = (now - last_beat[0]) * 1000.0
        if gap_ms > worst_gap_ms[0]:
            worst_gap_ms[0] = gap_ms
            worst_gap_at_ms[0] = (now - started) * 1000.0
        last_beat[0] = now
        beats[0] += 1
        if not done_flag[0]:
            root.after(10, beat)

    root.bind("<Map>", on_map, add="+")
    root.bind("<Visibility>", on_visibility, add="+")
    root.bind("<FocusIn>", on_focus_in, add="+")
    root.bind("<Activate>", on_activate, add="+")
    root.bind("<Configure>", on_configure, add="+")
    root.bind_all("<Expose>", on_expose_any, add="+")
    root.after_idle(on_idle)
    root.after(0, on_loop)
    root.after(10, beat)

    # ``main`` paints once before the loop; on this platform the guard refuses it and
    # remembers the request, so this is also the moment the deferred pass is armed.
    refreshed = time.perf_counter()
    app.refresh_window()
    result["refresh_ms"] = round((time.perf_counter() - refreshed) * 1000.0, 1)

    capture.screen = (root.winfo_screenwidth(), root.winfo_screenheight())
    sampler = threading.Thread(target=capture.run, args=(started + watch,), daemon=True)
    sampler.start()

    pushed: dict[str, Any] = {}

    def push() -> None:
        """The script-level equivalent of clicking the window: bring it forward."""
        pushed["at"] = (time.perf_counter() - started) * 1000.0
        try:
            root.lift()
            root.focus_force()
            root.attributes("-topmost", True)
            root.after(120, lambda: root.attributes("-topmost", False))
        except Exception as exc:  # noqa: BLE001 - a platform may refuse any of these
            pushed["error"] = f"{type(exc).__name__}: {exc}"

    def to_push() -> None:
        if first_paint(capture) is None:
            push()
        else:
            pushed["skipped"] = (time.perf_counter() - started) * 1000.0

    root.after(int(PUSH_AT * 1000), to_push)

    clicks: dict[str, Any] = {}

    def click_tab() -> None:
        clicks["start"] = (time.perf_counter() - started) * 1000.0
        try:
            buttons = getattr(app.tab_bar, "_buttons_dict", None)
            if isinstance(buttons, dict) and window in buttons:
                buttons[window].invoke()
            else:
                app._on_tab_click(window)
        except Exception as exc:  # noqa: BLE001 - the window must not hide a failure
            clicks["error"] = f"{type(exc).__name__}: {exc}"
        clicks["done"] = (time.perf_counter() - started) * 1000.0
        clicks["current_tab"] = app.current_tab
        try:
            clicks["bar_value"] = app.tab_bar.get()
        except Exception:  # noqa: BLE001
            clicks["bar_value"] = ""

    def watch_tab() -> None:
        frame = app.tab_frames.get("metadata")
        clicks.setdefault("checked", (time.perf_counter() - started) * 1000.0)
        if frame is not None and bool(frame.winfo_ismapped()):
            clicks.setdefault("mapped", (time.perf_counter() - started) * 1000.0)

    root.after(int(PUSH_AT * 1000) + 400, click_tab)
    for offset in (450, 650, 1000, 1500):
        root.after(int(PUSH_AT * 1000) + offset, watch_tab)

    closing: dict[str, Any] = {}

    def close() -> None:
        closing["start"] = (time.perf_counter() - started) * 1000.0
        done_flag[0] = True
        try:
            app._on_close()  # the command the window's close button runs
        except Exception as exc:  # noqa: BLE001 - report, never swallow
            closing["error"] = f"{type(exc).__name__}: {exc}"
        closing["done"] = (time.perf_counter() - started) * 1000.0
        try:
            closing["exists"] = float(root.winfo_exists())
        except Exception:  # noqa: BLE001 - destroyed root can raise TclError
            closing["exists"] = 0.0

    def failsafe_exit() -> None:
        done_flag[0] = True
        with contextlib.suppress(Exception):
            root.destroy()

    root.after(int((watch - 1.0) * 1000), close)
    root.after(int((watch + 0.5) * 1000), failsafe_exit)

    # Mark the moment just before entering Tk's mainloop, so we can tell whether
    # any pause happens inside mainloop() itself (Aqua initialization on Tk 9.1,
    # for example) rather than in the Python setup before it.
    mark("pre_mainloop")
    # Arm the heartbeat right before entering mainloop, so the first gap
    # measurement starts from the moment the event loop takes over.
    last_beat[0] = time.perf_counter()

    phase[0] = "mainloop"
    try:
        root.mainloop()
    finally:
        done_flag[0] = True

    painted = first_paint(capture)
    stages = [
        f"{name}={delta:.1f}ms"
        for name, _at, delta in trace.marks
        if delta >= 15.0
    ]
    result.update(
        {
            "pre_mainloop_ms": round(marks.get("pre_mainloop", -1), 1),
            "loop_ms": round(marks.get("loop", -1), 1),
            "mapped_ms": round(marks.get("mapped", -1), 1),
            "expose_any_ms": round(marks.get("expose_any", -1), 1),
            "expose_root_ms": round(marks.get("expose_root", -1), 1),
            "visibility_ms": round(marks.get("visibility", -1), 1),
            "focus_in_ms": round(marks.get("focus_in", -1), 1),
            "activate_ms": round(marks.get("activate", -1), 1),
            "first_configure_ms": round(marks.get("first_configure", -1), 1),
            "last_configure_ms": round(marks.get("last_configure", -1), 1),
            "configure_count": counts["configure"],
            "expose_count": counts["expose"],
            "expose_root_count": counts.get("expose_root", 0),
            "expose_widgets": expose_widgets[:8],
            "idle_ms": round(marks.get("idle", -1), 1),
            "beats": beats[0],
            "worst_gap_ms": round(worst_gap_ms[0], 1),
            "worst_gap_at_ms": round(worst_gap_at_ms[0], 1),
            "stalls": stalls,
            "slow_stages": stages,
            "refit_passes": app._refit_passes,
            "resize_events": app._resize_events,
            "display_passes": app._display_passes,
            "refused_flushes": suppressed_flush_count(),
            "capture": "yes" if capture.available else "no",
            "capture_reason": capture.reason,
            "samples": [
                [round(at, 2), colours, round(fraction, 4), bg_hex, size]
                for at, colours, fraction, bg_hex, size in capture.samples
            ],
            "first_paint_ms": round(painted * 1000.0, 1) if painted is not None else None,
            "push_ms": round(pushed.get("at", -1), 1) if "at" in pushed else None,
            "push_skipped_ms": round(pushed["skipped"], 1) if "skipped" in pushed else None,
            "push_error": pushed.get("error", ""),
            "tab_click_ms": (
                round(clicks["done"] - clicks["start"], 1)
                if "done" in clicks and "start" in clicks
                else None
            ),
            "tab_visible": "mapped" in clicks,
            "tab_current": clicks.get("current_tab", ""),
            "tab_bar_value": clicks.get("bar_value", ""),
            "tab_mapped_ms": round(clicks["mapped"], 1) if "mapped" in clicks else None,
            "tab_error": clicks.get("error", ""),
            "close_ms": (
                round(closing["done"] - closing["start"], 1)
                if "done" in closing and "start" in closing
                else None
            ),
            "close_exists": bool(closing.get("exists", 1.0)),
            "close_error": closing.get("error", ""),
        }
    )
    return result


def report(result: dict[str, Any]) -> None:
    say(f"label={result['label']} tk={result['tk']}")
    say(
        "pre_mainloop_ms={pre_mainloop_ms} build_ms={build_ms} refresh_ms={refresh_ms} "
        "loop_ms={loop_ms} mapped_ms={mapped_ms} expose_any_ms={expose_any_ms} "
        "idle_ms={idle_ms}".format(**result)
    )
    say(
        "visibility_ms={visibility_ms} focus_in_ms={focus_in_ms} activate_ms={activate_ms} "
        "configure={configure_count}({first_configure_ms}..{last_configure_ms}ms) "
        "expose_count={expose_count} expose_root={expose_root_count} "
        "expose_widgets={expose_widgets}".format(**result)
    )
    say(
        "beats={beats} worst_gap_ms={worst_gap_ms} at={worst_gap_at_ms}ms "
        "resize_events={resize_events} refit_passes={refit_passes} "
        "display_passes={display_passes} refused_flushes={refused_flushes}".format(**result)
    )
    if result["slow_stages"]:
        say("slow_stages=" + " ".join(result["slow_stages"]))
    for entry in result["stalls"]:
        say(f"stall {entry}")
    say(
        f"capture={result['capture']}"
        + (f" ({result['capture_reason']})" if result["capture_reason"] else "")
    )
    for at, colours, fraction, bg_hex, size in result["samples"]:
        say(f"sample at={at:.2f}s colours={colours} changed={fraction:.3f} bg={bg_hex} size={size}")
    if result["first_paint_ms"] is None:
        say("visible=no — no capture showed the window with content")
    else:
        say(f"visible=yes at={result['first_paint_ms'] / 1000.0:.2f}s")
    if result["push_ms"] is not None:
        say(f"pushed_forward_at={result['push_ms'] / 1000.0:.2f}s {result['push_error']}".strip())
    elif result["push_skipped_ms"] is not None:
        say(f"pushed_forward=not_needed (visible at {result['push_skipped_ms'] / 1000.0:.2f}s)")
    else:
        say("pushed_forward=never_ran")
    if result["tab_error"]:
        say(f"tab_click=error {result['tab_error']}")
    else:
        say(
            f"tab_click_ms={result['tab_click_ms']} tab_visible={result['tab_visible']} "
            f"tab_current={result['tab_current']!r} "
            + (
                f"tab_mapped_at={result['tab_mapped_ms'] / 1000.0:.2f}s"
                if result["tab_mapped_ms"] is not None
                else "tab_never_mapped"
            )
        )
    if result["close_error"]:
        say(f"close=error {result['close_error']}")
    else:
        say(f"close_ms={result['close_ms']} still_open={result['close_exists']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="run", help="name for this run in the report")
    parser.add_argument("--watch", type=float, default=WATCH_SECONDS, help="seconds to watch")
    parser.add_argument("--json", help="write the raw numbers here")
    args = parser.parse_args(argv)

    try:
        result = run(args.label, args.watch)
    except Exception as exc:  # noqa: BLE001 - a measurement reports, it never raises
        say(f"label={args.label} error={type(exc).__name__}: {exc}")
        return 1
    report(result)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(result, indent=1), encoding="utf-8")
    # The Aqua teardown can hang after the numbers are printed; the numbers are the
    # measurement, so leave with them in the log.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
