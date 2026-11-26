"""The start-up frame measurement: does it read pixels, and does it read them right.

`tools/measure_startup_frame.py` answers the one question the project never asked —
"is the window's *content* on screen, or only its frame" — by taking screenshots and
sampling them.  Nothing else in the suite can check that: Tk on Aqua has no API for
"is it drawn", and the call that would flush a frame (`update idletasks`) is the one
the application refuses on this platform.

So the parts that can be wrong without a Mac are tested here: the PNG decoder (filter
types included), the sampling that decides "uniform" from "drawn", and the rule that
picks the first capture which shows content.  The screenshots themselves are only
taken on a real display, in the `gui-tk9` job.
"""

from __future__ import annotations

import pathlib
import struct
import sys
import zlib

import pytest

TOOLS = pathlib.Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import measure_startup_frame as M  # noqa: E402 - after the path insert above

Colour = tuple[int, int, int]


def _png(width: int, height: int, pixels: list[list[Colour]], filters: list[int] | None = None) -> bytes:
    """A minimal 8-bit RGB PNG, one filter type per row (``0`` unless asked)."""
    channels = 3
    stride = width * channels
    filters = filters or [0] * height
    raw = bytearray()
    previous = bytearray(stride)
    for y in range(height):
        line = bytearray()
        for colour in pixels[y]:
            line.extend(colour)
        kind = filters[y]
        filtered = bytearray(stride)
        for index in range(stride):
            left = line[index - channels] if index >= channels else 0
            up = previous[index]
            up_left = previous[index - channels] if index >= channels else 0
            if kind == 0:
                predictor = 0
            elif kind == 1:
                predictor = left
            elif kind == 2:
                predictor = up
            elif kind == 3:
                predictor = (left + up) >> 1
            else:  # 4, Paeth
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
            filtered[index] = (line[index] - predictor) & 0xFF
        raw.append(kind)
        raw.extend(filtered)
        previous = line

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw)))
        + chunk(b"IEND", b"")
    )


def _write(tmp_path: pathlib.Path, data: bytes) -> pathlib.Path:
    path = tmp_path / "shot.png"
    path.write_bytes(data)
    return path


def test_the_decoder_reads_back_what_was_encoded(tmp_path: pathlib.Path) -> None:
    pixels = [
        [(255, 0, 0), (0, 255, 0), (0, 0, 255)],
        [(10, 20, 30), (40, 50, 60), (70, 80, 90)],
    ]
    image = M.decode_png(_write(tmp_path, _png(3, 2, pixels)))
    assert (image.width, image.height) == (3, 2)
    for y, row in enumerate(pixels):
        for x, colour in enumerate(row):
            assert image.at(x, y) == colour


@pytest.mark.parametrize("kind", [0, 1, 2, 3, 4])
def test_every_png_filter_type_decodes_to_the_same_pixels(
    tmp_path: pathlib.Path, kind: int
) -> None:
    """Sub, Up, Average and Paeth are what a real screenshot is full of."""
    pixels = [
        [(index * 7 + row * 13) % 256 for index in range(9)] or [] for row in range(4)
    ]
    pixels = [
        [((index * 7 + row * 13) % 256, (index * 3 + row) % 256, (index + row * 29) % 256) for index in range(9)]
        for row in range(4)
    ]
    image = M.decode_png(_write(tmp_path, _png(9, 4, pixels, filters=[kind] * 4)))
    for y, row in enumerate(pixels):
        for x, colour in enumerate(row):
            assert image.at(x, y) == colour, (kind, x, y)


def test_a_blank_window_is_not_counted_as_drawn(tmp_path: pathlib.Path) -> None:
    """An empty Aqua frame is one colour: that is the symptom, not a pass."""
    pixels = [[(236, 236, 236)] * 40 for _ in range(30)]
    image = M.decode_png(_write(tmp_path, _png(40, 30, pixels)))
    colours, fraction = M.sample(image, 0, 0, image.width, image.height)
    assert colours == 1
    assert fraction == 0.0
    assert colours < M.MIN_COLOURS or fraction < M.MIN_FRACTION


def test_a_built_window_is_counted_as_drawn(tmp_path: pathlib.Path) -> None:
    """The real window is a dark background with cards, captions and buttons."""
    pixels = []
    for y in range(30):
        row = []
        for x in range(40):
            if 8 <= x < 32 and 6 <= y < 12:
                row.append((198, 122, 26))  # the accent button
            elif 8 <= x < 32 and (16 <= y < 18 or 22 <= y < 24):
                row.append((80, 90, 110))  # captions
            elif 8 <= x < 32 and 26 <= y < 28:
                # text is antialiased: a real window carries a dozen shades, which is
                # exactly what tells it apart from an empty frame
                row.append((190, 195, 205) if x % 3 else (150, 155, 165))
            elif x < 4:
                row.append((60, 64, 72))  # a card edge
            else:
                row.append((30, 32, 38))  # background
        pixels.append(row)
    image = M.decode_png(_write(tmp_path, _png(40, 30, pixels)))
    colours, fraction = M.sample(image, 0, 0, image.width, image.height)
    assert colours >= M.MIN_COLOURS
    assert fraction >= M.MIN_FRACTION


def test_the_first_capture_that_shows_content_is_the_answer() -> None:
    capture = M.Capture()
    capture.samples = [(0.20, 1, 0.0), (0.45, 2, 0.004), (0.70, 7, 0.031), (1.10, 9, 0.2)]
    assert M.first_paint(capture) == 0.70


def test_a_run_that_never_draws_has_no_first_paint() -> None:
    capture = M.Capture()
    capture.samples = [(0.2, 1, 0.0), (0.6, 1, 0.0), (3.0, 2, 0.001)]
    assert M.first_paint(capture) is None


def test_an_undecodable_capture_is_reported_not_guessed(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "junk.png"
    path.write_bytes(b"not a png at all")
    with pytest.raises(ValueError):
        M.decode_png(path)


def test_a_png_this_decoder_cannot_read_is_named_as_such(tmp_path: pathlib.Path) -> None:
    """A palette or 16-bit capture must say so instead of reporting zero pixels."""
    header = struct.pack(">IIBBBBB", 4, 4, 16, 2, 0, 0, 0)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")
    with pytest.raises(ValueError, match="unsupported PNG"):
        M.decode_png(_write(tmp_path, data))


def test_gap_samples_are_strictly_inside_the_interval() -> None:
    gaps = [{"start_ms": 100.0, "end_ms": 500.0}]
    samples = [{"at_ms": at} for at in (99.0, 100.0, 200.0, 500.0, 501.0)]
    M.attach_gap_samples(gaps, samples)
    assert gaps[0]["stack_samples"] == [{"at_ms": 200.0}]


def test_capture_stage_retains_elapsed_and_cpu_time_on_error(monkeypatch) -> None:
    times = iter((10.0, 10.6))
    cpus = iter((2.0, 2.1))
    monkeypatch.setattr(M.time, "perf_counter", lambda: next(times))
    monkeypatch.setattr(M.time, "thread_time", lambda: next(cpus))
    capture = M.Capture(origin=9.0, diagnostic=True)

    def failed():
        raise ValueError("bad PNG")

    with pytest.raises(ValueError, match="bad PNG"):
        capture.timed("decode_png", failed)
    assert capture.timings[0]["stage"] == "decode_png"
    assert capture.timings[0]["start_ms"] == 1000
    assert capture.timings[0]["end_ms"] == pytest.approx(1600)
    assert capture.timings[0]["thread_cpu_ms"] == pytest.approx(100)


def test_default_capture_does_not_collect_diagnostic_timings() -> None:
    capture = M.Capture()
    assert capture.timed("decode_png", lambda: 42) == 42
    assert capture.timings == []


def test_no_capture_cannot_silently_disable_the_required_gate() -> None:
    with pytest.raises(SystemExit) as exc:
        M.main(["--no-capture"])
    assert exc.value.code == 2
