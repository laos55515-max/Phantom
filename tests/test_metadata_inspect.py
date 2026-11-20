"""The inspector: content detection, per-format facts, categories, honest limits."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import metadata_samples as samples
from phantom.core.errors import (
    MetadataError,
    OperationCancelled,
    PhantomError,
    UnsupportedFormat,
)
from phantom.metadata import inspector
from phantom.metadata import report as meta_report
from phantom.metadata.models import CATEGORY_ORDER, MetadataCategory, ProvenanceState


def _report(path: Path):
    return inspector.inspect_path(str(path))


# ---------------------------------------------------------------------------
# detection
# ---------------------------------------------------------------------------
def test_format_comes_from_the_bytes_not_the_name(tmp_path: Path) -> None:
    """A PNG named ``.wav`` is a PNG; the extension decides nothing."""
    disguised = tmp_path / "actually-a-png.wav"
    disguised.write_bytes(samples.png_bytes(text={"Title": "still a png"}))

    found = _report(disguised)

    assert found.fmt.value == "png"
    assert found.find("png.text.tEXt.Title") is not None


def test_a_file_of_the_wrong_kind_is_refused(tmp_path: Path) -> None:
    liar = tmp_path / "liar.png"
    liar.write_bytes(b"%PDF-1.7 not an image at all" * 8)

    with pytest.raises(UnsupportedFormat):
        _report(liar)


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty.png"
    empty.touch()

    with pytest.raises(MetadataError):
        _report(empty)


def test_a_missing_file_is_reported_as_io(tmp_path: Path) -> None:
    with pytest.raises(PhantomError):
        _report(tmp_path / "does-not-exist.png")


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------
def test_png_reports_gps_serial_dates_and_comments(tmp_path: Path) -> None:
    found = _report(samples.personal_png(tmp_path / "photo.png"))

    keys = {item.key for item in found.fields}
    assert "png.gps.coordinates" in keys
    assert "png.exif.0xa431" in keys  # BodySerialNumber
    assert "png.exif.0x0132" in keys  # DateTime
    assert "png.text.tEXt.Comment" in keys

    coordinates = found.find("png.gps.coordinates")
    assert coordinates is not None
    latitude, longitude = (part.strip() for part in coordinates.value.split(","))
    assert float(latitude) == pytest.approx(50.45, abs=0.01)
    assert float(longitude) == pytest.approx(30.52, abs=0.01)
    assert coordinates.category is MetadataCategory.PERSONAL
    assert coordinates.removable is False, (
        "the position is a view of the four GPS tags: offering it as its own "
        "removable field is what let one tick schedule GPSVersionID for removal"
    )
    assert coordinates.derived is True
    assert "png.exif.0x0131" in keys, "Software is technical and stays"


def test_png_text_keywords_are_classified_and_only_personal_is_offered(
    tmp_path: Path,
) -> None:
    found = _report(samples.personal_png(tmp_path / "classify.png"))

    title = found.find("png.text.tEXt.Title")
    comment = found.find("png.text.tEXt.Comment")
    author = found.find("png.text.tEXt.Author")
    copyright_notice = found.find("png.text.tEXt.Copyright")

    assert title is not None and title.category is MetadataCategory.DESCRIPTIVE
    assert title.removable is False
    assert comment is not None and comment.category is MetadataCategory.PERSONAL
    assert comment.removable is True
    # An author is personal data, but stripping a credit is the user's call.
    assert author is not None and author.category is MetadataCategory.RIGHTS
    assert author.removable is True
    assert copyright_notice is not None
    assert copyright_notice.category is MetadataCategory.RIGHTS
    assert copyright_notice.removable is False, "copyright is kept by policy"


def test_png_unknown_chunks_are_shown_and_never_removable(tmp_path: Path) -> None:
    found = _report(samples.personal_png(tmp_path / "unknown.png"))

    chunk = found.find("png.chunk.prVt")
    assert chunk is not None, "a private chunk must be reported, not silently dropped"
    assert chunk.category is MetadataCategory.UNKNOWN
    assert chunk.removable is False


def test_every_field_says_what_it_is_and_whether_it_can_go(tmp_path: Path) -> None:
    for name, maker in (
        ("a.png", samples.personal_png),
        ("a.wav", samples.personal_wav),
        ("a.flac", samples.personal_flac),
    ):
        found = _report(maker(tmp_path / name))
        assert found.fields, name
        for item in found.fields:
            assert item.key and item.label, (name, item)
            assert item.category.value in CATEGORY_ORDER, (name, item.key)
            assert isinstance(item.removable, bool)
            assert item.container


def test_counts_match_the_fields(tmp_path: Path) -> None:
    found = _report(samples.personal_png(tmp_path / "counts.png"))
    counts = found.counts
    counted = [item for item in found.fields if not item.derived]
    assert counts.total == len(counted), "derived views are not entries of their own"
    assert len(counted) < len(found.fields), "the fixture has a derived view to exclude"
    assert counts.personal == len([item for item in counted if item.sensitive])
    assert counts.removable == len([item for item in counted if item.removable])
    assert sum(counts.of(category) for category in MetadataCategory) == counts.total


def test_a_corrupt_chunk_crc_is_reported_and_flagged(tmp_path: Path) -> None:
    data = bytearray(samples.png_bytes(text={"Comment": "x"}))
    # Flip a byte inside the first tEXt payload: the chunk still parses, the CRC
    # no longer matches, and the inspector has to say so.
    offset = data.find(b"tEXt") + 6
    data[offset] ^= 0xFF
    broken = tmp_path / "broken-crc.png"
    broken.write_bytes(bytes(data))

    found = _report(broken)

    assert found.checksum_ok is False
    assert any("CRC" in warning for warning in found.warnings)


def test_a_huge_text_chunk_is_truncated_for_display_and_says_so(
    tmp_path: Path,
) -> None:
    found = _report(
        samples.personal_png(
            tmp_path / "huge.png", text={"Comment": "A" * 20_000}
        )
    )

    comment = found.find("png.text.tEXt.Comment")
    assert comment is not None
    assert comment.truncated is True
    assert len(comment.value) <= 400


def test_invalid_utf8_in_an_itxt_chunk_becomes_a_warning(tmp_path: Path) -> None:
    """``iTXt`` declares UTF-8, so latin-1 bytes inside it are a defect worth naming."""
    payload = b"Comment\x00\x00\x00en\x00\x00" + b"caf\xe9 au lait"
    found = _report(
        samples.personal_png(tmp_path / "latin1.png", extra=((b"iTXt", payload),))
    )

    assert any("UTF-8" in warning for warning in found.warnings)
    comment = found.find("png.text.iTXt.Comment")
    assert comment is not None, "the replacement characters must still be shown"


def test_a_truncated_png_is_reported_with_a_warning(tmp_path: Path) -> None:
    """A cut file is refused by the cleaner, but the inspector may still explain it."""
    good = samples.png_bytes()
    cut = tmp_path / "cut.png"
    cut.write_bytes(good[: len(good) // 3])

    try:
        found = _report(cut)
    except PhantomError:
        return  # refusing outright is a correct answer too
    assert found.warnings, "a truncated container must be flagged, not silently accepted"


# ---------------------------------------------------------------------------
# WAV
# ---------------------------------------------------------------------------
def test_wav_reports_info_bext_and_unknown_chunks(tmp_path: Path) -> None:
    found = _report(samples.personal_wav(tmp_path / "take.wav"))

    keys = {item.key for item in found.fields}
    assert {"wav.info.ICMT", "wav.info.IART", "wav.info.INAM"} <= keys
    assert "wav.bext.originator" in keys
    assert "wav.bext.reserved" in keys
    assert "wav.chunk.junk" in keys

    junk = found.find("wav.chunk.junk")
    assert junk is not None and junk.removable is False
    comment = found.find("wav.info.ICMT")
    assert comment is not None and comment.category is MetadataCategory.PERSONAL


def test_wav_technical_parameters_are_read_from_fmt_and_data(tmp_path: Path) -> None:
    found = _report(samples.personal_wav(tmp_path / "fmt.wav"))
    technical = found.technical

    assert technical["channels"] == "1"
    assert technical["sample_rate"] == str(samples.SAMPLE_RATE)
    assert technical["bits_per_sample"] == "16"
    assert technical["frames"] == str(samples.FRAMES)
    assert float(technical["duration_seconds"]) == pytest.approx(
        samples.FRAMES / samples.SAMPLE_RATE, abs=0.001
    )


def test_a_wav_whose_riff_says_avi_is_refused(tmp_path: Path) -> None:
    data = bytearray(samples.wav_bytes(info={"ICMT": "x"}))
    data[8:12] = b"AVI "
    wrong = tmp_path / "form-type.wav"
    wrong.write_bytes(bytes(data))

    with pytest.raises(UnsupportedFormat):
        _report(wrong)


# ---------------------------------------------------------------------------
# FLAC
# ---------------------------------------------------------------------------
def test_flac_reports_vorbis_comments_and_streaminfo(tmp_path: Path) -> None:
    found = _report(samples.personal_flac(tmp_path / "track.flac"))

    keys = {item.key for item in found.fields}
    assert {"flac.vorbis.COMMENT", "flac.vorbis.DATE", "flac.vorbis.TITLE"} <= keys
    assert "flac.vendor" in keys
    technical = found.technical
    assert technical["sample_rate"] == str(samples.SAMPLE_RATE)
    assert technical["total_samples"] == str(samples.FRAMES)


def test_flac_pictures_are_reported_as_descriptive_content(tmp_path: Path) -> None:
    path = samples.flac_file(
        tmp_path / "art.flac", {"TITLE": "with cover"}, picture=True
    )
    found = _report(path)

    picture = next((f for f in found.fields if f.key.startswith("flac.picture.")), None)
    assert picture is not None
    assert picture.category is MetadataCategory.DESCRIPTIVE
    assert picture.removable is True, "a cover is removable, but it is not personal"


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------
def test_no_manifest_is_reported_as_no_manifest(tmp_path: Path) -> None:
    found = _report(samples.personal_png(tmp_path / "plain.png"))

    assert found.provenance.state is ProvenanceState.NONE
    assert found.provenance.detected is False
    text = meta_report.inspection_text(found)
    assert "provenance: none" in text
    assert "is not proof" in text


def test_a_signed_png_reports_an_unverified_signature(tmp_path: Path) -> None:
    found = _report(samples.signed_png(tmp_path / "signed.png"))

    provenance = found.provenance
    assert provenance.detected is True
    assert provenance.signed is True
    assert provenance.state is ProvenanceState.SIGNATURE_UNVERIFIED
    assert provenance.manifests, "the manifest label should be readable"
    details = dict(provenance.details)
    assert details["signature"] == "present, not verified"
    assert "does not verify" in details["limits"]


def test_a_signed_wav_is_detected_too(tmp_path: Path) -> None:
    found = _report(samples.signed_wav(tmp_path / "signed.wav"))
    assert found.provenance.detected is True


def test_the_report_never_claims_the_file_has_no_traces(tmp_path: Path) -> None:
    found = _report(samples.personal_png(tmp_path / "traces.png"))
    text = meta_report.inspection_text(found).lower()
    for claim in ("no traces", "no metadata at all", "clean file", "anonymous"):
        assert claim not in text


# ---------------------------------------------------------------------------
# robustness
# ---------------------------------------------------------------------------
def test_a_file_over_the_inspector_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from phantom.metadata import inspector as inspector_module

    monkeypatch.setattr(inspector_module, "MAX_INSPECT_BYTES", 32)
    path = samples.personal_png(tmp_path / "big.png")

    with pytest.raises(MetadataError, match="larger"):
        _report(path)


def test_cancellation_stops_before_the_parse(tmp_path: Path) -> None:
    cancel = threading.Event()
    cancel.set()
    path = samples.personal_png(tmp_path / "cancel.png")

    with pytest.raises(OperationCancelled):
        inspector.inspect_path(str(path), cancel=cancel)


def test_unicode_paths_and_long_names_are_accepted(tmp_path: Path) -> None:
    name = "щоденник-подорожі-" + "довга-назва-" * 6 + "фото.png"
    path = samples.personal_png(tmp_path / name)

    found = _report(path)

    assert found.path.endswith(".png")
    assert found.find("png.text.tEXt.Comment") is not None


def test_reports_do_not_carry_raw_personal_values_into_log_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = samples.personal_png(tmp_path / "logged.png")
    with caplog.at_level("DEBUG", logger="phantom.metadata"):
        found = _report(path)
    assert found.find("png.gps.coordinates") is not None
    assert "50.45" not in caplog.text
    assert "Ada Lovelace" not in caplog.text
