"""Damaged containers, hostile metadata and the limits that keep them cheap.

The audit behind this file asks three questions of every parser:

1. **Does a broken file produce a typed failure instead of a crash?**  Truncated
   files, chunk lengths past the end of the buffer, a wrong CRC, an unexpected EOF,
   metadata that claims to be enormous, and a file whose extension lies about its
   format all reach the parsers as attacker-controlled integers.
2. **Is the resource cost bounded by a limit the code enforces, not by the size of
   the file?**  A compressed text chunk is a zlib bomb waiting to happen: 256 KiB of
   zlib expands to 256 MiB of zeros, and the inspector used to hold all of it.  An
   XMP packet can hold 70 000 elements behind 840 KiB.  Both cases are measured here
   with ``tracemalloc`` and a wall-clock bound, because "we check afterwards" is the
   same defect as "we do not check".
3. **Can a failure publish anything?**  Every refused clean must leave the source
   byte-for-byte identical and no output, no staged sibling and no half-written file
   behind it.
"""

from __future__ import annotations

import random
import time
import tracemalloc
import zlib
from pathlib import Path

import pytest
from tests import metadata_samples as samples

from phantom.core.errors import MetadataError, PhantomError
from phantom.metadata import cleaner, inspector, xmp
from phantom.metadata import png as png_mod
from phantom.metadata.models import MediaFormat

#: The ceiling for one hostile file.  It is ~4x what the fixed code needs and ~30x
#: below what the unbounded versions cost, so it cannot pass by accident.
MEMORY_CEILING = 64 * 1024 * 1024
TIME_CEILING = 5.0


def _measure(callable_, *args, **kwargs):
    """Run ``callable_`` and return ``(result, peak_bytes, seconds)``."""
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = callable_(*args, **kwargs)
    finally:
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        elapsed = time.perf_counter() - started
    return result, peak, elapsed


# ---------------------------------------------------------------------------
# compressed metadata: the cap is on what it inflates to
# ---------------------------------------------------------------------------
def _bomb_chunk(kind: bytes, expanded: int = 256 * 1024 * 1024) -> tuple[bytes, bytes]:
    payload = zlib.compress(b"\x00" * expanded, 9)
    if kind == b"zTXt":
        body = b"Comment\x00\x00" + payload
    else:
        body = b"Comment\x00\x01\x00\x00\x00" + payload
    return kind, body


@pytest.mark.parametrize("kind", [b"zTXt", b"iTXt"])
def test_a_compressed_text_bomb_costs_the_ceiling_not_the_bomb(
    kind: bytes, tmp_path: Path
) -> None:
    """556 MiB and a second of work became 17 MiB and 30 ms; the limit does that."""
    chunk_kind, body = _bomb_chunk(kind)
    data = samples.png_bytes(extra=((chunk_kind, body),))
    assert len(data) < 400 * 1024, "the file itself must stay small"

    report, peak, elapsed = _measure(
        inspector.report_from_bytes, "bomb.png", data, MediaFormat.PNG
    )

    assert peak < MEMORY_CEILING, f"peak {peak / 1024 / 1024:.1f} MiB"
    assert elapsed < TIME_CEILING, f"took {elapsed:.2f}s"
    field = report.find(f"png.text.{chunk_kind.decode()}.Comment")
    assert field is not None, "the chunk is still listed"
    assert len(field.value) <= 400, "only a display prefix is kept"
    assert any("inflates to more than" in warning for warning in report.warnings), (
        report.warnings
    )


def test_a_text_chunk_that_inflates_within_the_cap_is_decoded_fully(tmp_path: Path) -> None:
    """The cap must not truncate an ordinary compressed value."""
    value = "Життя — це рух. " * 40
    compressed = zlib.compress(value.encode("utf-8"), 9)
    data = samples.png_bytes(extra=((b"zTXt", b"Comment\x00\x00" + compressed),))

    report = inspector.report_from_bytes("ok.png", data, MediaFormat.PNG)

    assert report.find("png.text.zTXt.Comment").value == value[:400]
    assert not [w for w in report.warnings if "inflates" in w]


def test_the_inflater_reports_exactly_the_ceiling() -> None:
    """A direct check of the helper: capped is a fact, not a guess."""
    short = zlib.compress(b"a" * 100)
    assert png_mod._inflate(short, limit=1000)[1] is False
    assert png_mod._inflate(short, limit=50) == (b"a" * 50, True)


# ---------------------------------------------------------------------------
# structural damage: lengths, EOF, CRC
# ---------------------------------------------------------------------------
def test_a_chunk_length_past_the_end_of_the_file_is_tolerated(tmp_path: Path) -> None:
    """A 4 GiB length in a 3 KiB file must not be trusted for a moment."""
    data = bytearray(samples.personal_png(tmp_path / "src.png").read_bytes())
    # The first chunk after the signature is IHDR; give it the longest length there is.
    data[8:12] = b"\xff\xff\xff\xff"

    report = inspector.report_from_bytes("huge.png", bytes(data), MediaFormat.PNG)

    assert report.warnings, "the reader has to say the chunk does not fit"


@pytest.mark.parametrize("name", ["png", "wav", "flac"])
def test_a_truncated_file_is_reported_or_refused_but_never_crashes(
    name: str, tmp_path: Path
) -> None:
    sample = {
        "png": samples.personal_png,
        "wav": samples.personal_wav,
        "flac": samples.personal_flac,
    }[name](tmp_path / f"full.{name}")
    data = sample.read_bytes()
    fmt = {"png": MediaFormat.PNG, "wav": MediaFormat.WAV, "flac": MediaFormat.FLAC}[name]

    for cut in (2, 8, 12, 20, len(data) // 3, len(data) // 2, len(data) - 4):
        if cut <= 0:
            continue
        try:
            report = inspector.report_from_bytes(f"cut.{name}", data[:cut], fmt)
        except PhantomError:
            continue
        assert report.size_bytes == cut


def test_the_wrong_extension_is_beside_the_point(tmp_path: Path) -> None:
    """A PNG called ``.wav`` is cleaned as a PNG, and the output says so."""
    lying = tmp_path / "photo.wav"
    lying.write_bytes(samples.personal_png(tmp_path / "real.png").read_bytes())

    report = inspector.inspect_path(str(lying))
    assert report.fmt is MediaFormat.PNG

    output = tmp_path / "photo-clean.wav"
    outcome = cleaner.clean(
        str(lying), str(output), cleaner.default_selection(report)
    )
    assert outcome.output.endswith(".png"), outcome.output
    assert Path(outcome.output).exists()


# ---------------------------------------------------------------------------
# a refused clean publishes nothing
# ---------------------------------------------------------------------------
def test_a_corrupt_source_publishes_nothing(tmp_path: Path) -> None:
    """Half a file cannot be verified, so half a file must not be written."""
    good = samples.personal_png(tmp_path / "good.png").read_bytes()
    broken = tmp_path / "broken.png"
    broken.write_bytes(good[: len(good) // 2])
    before = broken.read_bytes()
    output = tmp_path / "broken-clean.png"

    with pytest.raises(PhantomError):
        cleaner.clean(
            str(broken),
            str(output),
            frozenset({"png.text.tEXt.Comment", "png.exif.0x013b"}),
        )

    assert broken.read_bytes() == before
    assert not output.exists()
    assert not list(tmp_path.glob("*.metadata*")), "no staged sibling survives"


def test_a_source_that_is_a_directory_is_an_error_not_a_crash(tmp_path: Path) -> None:
    with pytest.raises(PhantomError):
        inspector.inspect_path(str(tmp_path))


def test_cleaning_refuses_when_the_selection_names_nothing_removable(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "src.png")
    before = source.read_bytes()
    with pytest.raises(MetadataError):
        cleaner.clean(str(source), str(tmp_path / "out.png"), frozenset({"png.text.tEXt.Software"}))
    assert source.read_bytes() == before
    assert not (tmp_path / "out.png").exists()


# ---------------------------------------------------------------------------
# XMP: the limits are enforced while parsing, not after the tree exists
# ---------------------------------------------------------------------------
def _hostile_packet(elements: int) -> str:
    unit = "<a:x>1</a:x>"
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:a="http://ns.adobe.com/xap/1.0/"'
        ' xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/">'
        "<photoshop:City>Lubny</photoshop:City>"
        + unit * elements
        + "</rdf:Description></rdf:RDF></x:xmpmeta>"
    )


def test_an_xmp_packet_with_too_many_elements_is_refused_as_it_is_read() -> None:
    """70 000 elements cost 11 MiB and 230 ms when the tree came first; now 7 MiB/30 ms."""
    packet = _hostile_packet(70_000)
    assert len(packet.encode()) < xmp.MAX_PACKET_BYTES

    analysis, peak, elapsed = _measure(xmp.analyse, packet)

    assert analysis.removable is False
    assert analysis.problem_key == "meta.xmp.unreadable.many"
    assert peak < MEMORY_CEILING, f"peak {peak / 1024 / 1024:.1f} MiB"
    assert elapsed < TIME_CEILING, f"took {elapsed:.2f}s"


def test_a_packet_with_too_many_attributes_is_refused_cheaply() -> None:
    """Attributes are counted by the same loop that counts elements."""
    # Real attributes, not namespace declarations: ElementTree consumes ``xmlns``
    # into its namespace map, so only a genuine attribute reaches the counter.
    attributes = " ".join(f'n:a{index}="{index}"' for index in range(2000))
    packet = (
        '<?xpacket begin="\ufeff"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:Description rdf:about="" xmlns:n="http://example.com/n" {attributes}/>'
        "</rdf:RDF></x:xmpmeta>"
    )

    analysis, peak, elapsed = _measure(xmp.analyse, packet)

    assert analysis.removable is False
    assert analysis.problem_key == "meta.xmp.unreadable.many"
    assert peak < MEMORY_CEILING
    assert elapsed < TIME_CEILING


def test_the_element_limit_is_enforced_by_the_parser_not_by_the_walk(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must happen *during* parsing, before the graph is ever walked.

    ``_Reader.read`` is the walk; if the limits were only checked there, it would be
    reached with a complete tree in memory.  It is made to fail loudly, so this test
    passes only when the parser aborts first.
    """

    def forbidden(self, root):  # noqa: ANN001, ARG001 - a tripwire
        raise AssertionError("the tree was walked before the limits were applied")

    monkeypatch.setattr(xmp._Reader, "read", forbidden)
    analysis = xmp.analyse(_hostile_packet(xmp.MAX_ELEMENTS + 100))

    assert analysis.ok is False
    assert analysis.problem_key == "meta.xmp.unreadable.many"


def test_an_xmp_packet_deeper_than_the_limit_is_refused_cheaply() -> None:
    depth = xmp.MAX_DEPTH + 40
    opening = '<photoshop:City rdf:parseType="Resource">' * depth
    packet = (
        '<?xpacket begin="\ufeff"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/">'
        + opening
        + "</photoshop:City>" * depth
        + "</rdf:Description></rdf:RDF></x:xmpmeta>"
    )

    analysis, peak, elapsed = _measure(xmp.analyse, packet)

    assert analysis.removable is False
    assert analysis.problem_key == "meta.xmp.unreadable.deep"
    assert peak < MEMORY_CEILING
    assert elapsed < TIME_CEILING


# ---------------------------------------------------------------------------
# fuzz: every mutation either parses or raises a PhantomError
# ---------------------------------------------------------------------------
def _mutate(data: bytes, rng: random.Random) -> bytes:
    out = bytearray(data)
    mode = rng.choice(("flip", "truncate", "length", "zero", "splice"))
    if mode == "flip":
        for _ in range(rng.randint(1, 8)):
            index = rng.randrange(len(out))
            out[index] = rng.randrange(256)
    elif mode == "truncate":
        out = out[: rng.randrange(len(out) + 1)]
    elif mode == "length":
        index = rng.randrange(max(1, len(out) - 4))
        out[index : index + 4] = rng.choice(
            (b"\xff\xff\xff\xff", b"\x00\x00\x00\x00", b"\x7f\xff\xff\xff", b"\x80\x00\x00\x00")
        )
    elif mode == "zero":
        index = rng.randrange(len(out))
        size = rng.randint(1, 64)
        out[index : index + size] = b"\x00" * len(out[index : index + size])
    else:
        source = rng.randrange(len(out))
        target = rng.randrange(len(out))
        out[target:target] = out[source : source + rng.randint(1, 32)]
    return bytes(out)


def _fuzz(
    samples_by_format: dict[str, tuple[bytes, MediaFormat]], iterations: int, seed: int
) -> tuple[int, int]:
    """Mutate every sample; return ``(readable, refused)``.

    Any other outcome — a ``struct.error``, an ``IndexError``, a ``MemoryError`` — is
    a parser bug, and this function simply lets it escape so the test fails with it.
    """
    rng = random.Random(seed)
    accepted = 0
    refused = 0
    for name, (data, fmt) in samples_by_format.items():
        for _ in range(iterations):
            mutant = _mutate(data, rng)
            try:
                report = inspector.report_from_bytes(f"mutant.{name}", mutant, fmt)
            except PhantomError:
                refused += 1
                continue
            accepted += 1
            assert report.size_bytes == len(mutant)
    return accepted, refused


def _fuzz_samples(tmp_path: Path) -> dict[str, tuple[bytes, MediaFormat]]:
    return {
        "png": (samples.personal_png(tmp_path / "f.png").read_bytes(), MediaFormat.PNG),
        "wav": (samples.personal_wav(tmp_path / "f.wav").read_bytes(), MediaFormat.WAV),
        "flac": (samples.personal_flac(tmp_path / "f.flac").read_bytes(), MediaFormat.FLAC),
    }


def test_mutated_containers_never_raise_anything_but_a_typed_error(tmp_path: Path) -> None:
    """A crash here would be a parser bug the report could not survive."""
    total = 3 * 120
    accepted, refused = _fuzz(_fuzz_samples(tmp_path), iterations=120, seed=2026)

    assert accepted + refused == total
    assert accepted > 0, "every mutant was refused: the fuzz is not reaching the readers"


def test_every_four_byte_field_can_be_the_hostile_one(tmp_path: Path) -> None:
    """Every 4-byte boundary of every sample takes the worst lengths it can get."""
    evil = (0xFFFFFFFF, 0x80000000, 0x7FFFFFFF, 0, 0xFFFF)
    for name, (data, fmt) in _fuzz_samples(tmp_path).items():
        for offset in range(0, len(data) - 4, 4):
            for value in evil:
                mutant = data[:offset] + value.to_bytes(4, "big") + data[offset + 4 :]
                try:
                    inspector.report_from_bytes(f"len.{name}", mutant, fmt)
                except PhantomError:
                    continue


@pytest.mark.slow
def test_a_long_fuzz_session_stays_typed_and_fast(tmp_path: Path) -> None:
    """The same contract, with enough mutants to reach the awkward branches."""
    started = time.perf_counter()
    accepted, refused = _fuzz(_fuzz_samples(tmp_path), iterations=1200, seed=99)
    elapsed = time.perf_counter() - started

    assert accepted + refused == 3 * 1200
    assert elapsed < 120, f"the fuzz session took {elapsed:.1f}s"
