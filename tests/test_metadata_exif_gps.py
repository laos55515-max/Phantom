"""The EXIF/GPS dossier that made cleaning fail, end to end.

Reported from a real macOS run of 2.1.0: a PNG with a position, two dates, make,
model, owner and body serial numbers was cleaned and ended with

    Ошибка обработки метаданных: png.gps.0x0000

The plan had offered the derived "GPS coordinates" row as one removable field and
translated it into *every* GPS tag, GPSVersionID included; the last check refused
to publish a file that had lost a field the plan promised to keep, so nothing was
published and the user was told a tag number.

These tests pin the policy from both ends — what the inspector reports and what the
cleaner actually writes — and they deliberately do not weaken that last check: the
point is that the plan and the rewrite now agree, not that the guard was relaxed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import metadata_samples as samples
from phantom.core.errors import ErrorCode, MetadataConflictError, MetadataError
from phantom.metadata import cleaner, inspector
from phantom.metadata import png as png_module
from phantom.metadata.integrity import png_checksums_ok
from phantom.metadata.models import MetadataCategory

#: The tags the reported file carried, by the key the interface uses.
PERSONAL = (
    "png.exif.0x010f",  # Make
    "png.exif.0x0110",  # Model
    "png.exif.0x0132",  # DateTime
    "png.exif.0x9003",  # DateTimeOriginal
    "png.exif.0xa430",  # CameraOwnerName
    "png.exif.0xa431",  # BodySerialNumber
    "png.gps.0x0001",  # GPSLatitudeRef
    "png.gps.0x0002",  # GPSLatitude
    "png.gps.0x0003",  # GPSLongitudeRef
    "png.gps.0x0004",  # GPSLongitude
    "png.text.tEXt.Comment",
)
PROTECTED = (
    "png.exif.0x0112",  # Orientation: technical
    "png.exif.0x0131",  # Software: technical
    "png.exif.0x8298",  # Copyright: rights
    "png.gps.0x0000",  # GPSVersionID: technical
    "png.exif.0xc7a1",  # no name in any table: unknown
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pixels(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.array(image)


@pytest.fixture
def dossier(tmp_path: Path) -> Path:
    return samples.rich_exif_png(tmp_path / "dossier.png")


# ---------------------------------------------------------------------------
# 1. detection
# ---------------------------------------------------------------------------
def test_every_reported_tag_is_found_with_its_policy(dossier: Path) -> None:
    found = inspector.inspect_path(str(dossier))
    keys = {item.key for item in found.fields}

    for key in PERSONAL:
        item = found.find(key)
        assert item is not None, f"{key} was not reported"
        assert item.removable is True, f"{key} must be removable"
        assert item.sensitive is True, f"{key} is personal"
    for key in PROTECTED:
        item = found.find(key)
        assert item is not None, f"{key} was not reported"
        assert item.removable is False, f"{key} must be protected"
        assert item.reason, f"{key} must say why it is kept"

    assert found.find("png.gps.0x0000").label == "GPSVersionID"
    assert found.find("png.exif.0xc7a1").category is MetadataCategory.UNKNOWN
    assert keys >= set(PERSONAL) | set(PROTECTED)


def test_the_position_is_one_derived_view_and_not_an_entry(dossier: Path) -> None:
    found = inspector.inspect_path(str(dossier))

    coordinates = found.find("png.gps.coordinates")
    assert coordinates is not None
    assert coordinates.derived is True
    assert coordinates.removable is False, (
        "the view must not be selectable: that is what let one tick schedule "
        "GPSVersionID for removal"
    )
    latitude, longitude = (part.strip() for part in coordinates.value.split(","))
    assert float(latitude) == pytest.approx(50.45, abs=0.01)
    assert float(longitude) == pytest.approx(30.52, abs=0.01)

    # No double counting: the view is not one more personal field.
    counted = [item for item in found.fields if not item.derived]
    assert found.counts.total == len(counted)
    assert found.counts.personal == len(
        [item for item in counted if item.category is MetadataCategory.PERSONAL]
    )
    assert found.counts.personal == len(PERSONAL)


def test_the_preselection_is_the_personal_fields_and_nothing_else(dossier: Path) -> None:
    found = inspector.inspect_path(str(dossier))

    selection = cleaner.default_selection(found)

    assert set(PERSONAL) - {"png.text.tEXt.Comment"} <= selection
    assert "png.text.tEXt.Comment" in selection
    for key in PROTECTED:
        assert key not in selection, f"{key} must never be preselected"
    assert "png.gps.coordinates" not in selection


# ---------------------------------------------------------------------------
# 2/3/4. selective removal, plan == result, protected fields kept
# ---------------------------------------------------------------------------
def test_cleaning_removes_personal_data_and_keeps_the_policy(dossier: Path, tmp_path: Path) -> None:
    output = tmp_path / "dossier-clean.png"
    before = _sha(dossier)
    found = inspector.inspect_path(str(dossier))

    outcome = cleaner.clean(str(dossier), str(output), cleaner.default_selection(found))

    assert outcome.verified is True
    assert _sha(dossier) == before, "the source is only ever read"

    after = inspector.inspect_path(str(output))
    keys = {item.key for item in after.fields}
    for key in PERSONAL:
        assert key not in keys, f"{key} should be gone"
    for key in PROTECTED:
        assert key in keys, f"{key} should still be there"
    assert "png.gps.coordinates" not in keys, "the view goes with the tags behind it"
    assert after.counts.personal == 0
    assert after.counts.unknown == 1


def test_the_plan_says_exactly_what_the_rewrite_did(dossier: Path, tmp_path: Path) -> None:
    found = inspector.inspect_path(str(dossier))
    selection = cleaner.default_selection(found)

    decision = cleaner.plan(found, selection)
    outcome = cleaner.clean(str(dossier), str(tmp_path / "plan.png"), selection)

    after = inspector.inspect_path(str(outcome.output))
    assert decision.allowed is True
    assert decision.blocked == ()
    assert decision.remove_keys == frozenset(item.key for item in outcome.removed)
    assert {item.key for item in decision.remove} == {item.key for item in outcome.removed}
    # Everything the plan promised to keep is still in the file, and nothing else is.
    kept = {item.key for item in decision.keep}
    assert kept == {item.key for item in after.fields if not item.derived}
    assert len([item for item in after.fields if not item.derived]) == len(decision.keep)


def test_gps_version_id_and_the_other_protected_tags_survive(dossier: Path, tmp_path: Path) -> None:
    found = inspector.inspect_path(str(dossier))
    outcome = cleaner.clean(str(dossier), str(tmp_path / "kept.png"), cleaner.default_selection(found))

    after = inspector.inspect_path(str(outcome.output))

    version = after.find("png.gps.0x0000")
    assert version is not None, "GPSVersionID is technical: it must not be removed"
    assert version.value == found.find("png.gps.0x0000").value
    assert after.find("png.gps.0x0001") is None, "the reference tag is personal"
    assert after.find("png.gps.0x0002") is None
    for key in ("png.exif.0x0112", "png.exif.0x0131", "png.exif.0x8298", "png.exif.0xc7a1"):
        assert after.find(key) is not None, f"{key} must survive"


# ---------------------------------------------------------------------------
# 5/6/7. pixels, CRCs, source
# ---------------------------------------------------------------------------
def test_the_pixels_are_unchanged(dossier: Path, tmp_path: Path) -> None:
    found = inspector.inspect_path(str(dossier))
    outcome = cleaner.clean(str(dossier), str(tmp_path / "pixels.png"), cleaner.default_selection(found))

    assert np.array_equal(_pixels(dossier), _pixels(Path(outcome.output)))
    assert outcome.comparison.identical is True
    assert outcome.comparison.checked is True


def test_every_png_chunk_still_has_a_valid_crc(dossier: Path, tmp_path: Path) -> None:
    found = inspector.inspect_path(str(dossier))
    outcome = cleaner.clean(str(dossier), str(tmp_path / "crc.png"), cleaner.default_selection(found))

    cleaned = Path(outcome.output).read_bytes()

    assert png_checksums_ok(cleaned) is True
    assert outcome.checksum_ok is True
    assert inspector.inspect_path(str(Path(outcome.output))).checksum_ok is True


def test_the_source_file_is_never_modified(dossier: Path, tmp_path: Path) -> None:
    before = _sha(dossier)
    found = inspector.inspect_path(str(dossier))

    outcome = cleaner.clean(str(dossier), str(tmp_path / "source.png"), cleaner.default_selection(found))

    assert _sha(dossier) == before
    assert _sha(Path(outcome.output)) != before
    assert outcome.source_sha256 == before


# ---------------------------------------------------------------------------
# 8. nothing is published when the guard fires
# ---------------------------------------------------------------------------
def test_a_rewrite_that_loses_a_protected_field_publishes_nothing(
    dossier: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "guarded.png"
    before = _sha(dossier)
    original = png_module.rewrite_png

    def careless(data: bytes, removal: object) -> bytes:
        # What the bug did: take the version tag out together with the position.
        everything = frozenset({"png.gps.0x0000", "png.gps.0x0001", "png.gps.0x0002"})
        return original(data, png_module.removal_from_keys(everything))

    monkeypatch.setattr(png_module, "rewrite_png", careless)

    with pytest.raises(MetadataConflictError) as caught:
        cleaner.clean(
            str(dossier),
            str(output),
            frozenset({"png.gps.0x0002"}),
        )

    assert caught.value.code is ErrorCode.METADATA_CONFLICT
    assert "GPSVersionID" in str(caught.value.params.get("fields", "")), (
        "the sentence names the field by label, not by tag number"
    )
    assert not output.exists(), "no cleaned copy is published"
    assert _sha(dossier) == before


# ---------------------------------------------------------------------------
# 9/10. partial selections and unknown tags
# ---------------------------------------------------------------------------
def test_a_partial_gps_selection_keeps_the_other_axis(dossier: Path, tmp_path: Path) -> None:
    output = tmp_path / "latitude-only.png"

    outcome = cleaner.clean(
        str(dossier),
        str(output),
        frozenset({"png.gps.0x0002", "png.gps.0x0001"}),
    )

    after = inspector.inspect_path(str(Path(outcome.output)))
    assert after.find("png.gps.0x0002") is None, "latitude was selected"
    assert after.find("png.gps.0x0001") is None, "so was its reference"
    assert after.find("png.gps.0x0004") is not None, "longitude was not selected"
    assert after.find("png.gps.0x0003") is not None
    assert after.find("png.gps.0x0000") is not None
    assert [item.label for item in outcome.removed] == ["GPSLatitude", "GPSLatitudeRef"]


def test_an_unknown_tag_is_listed_and_never_removed(dossier: Path, tmp_path: Path) -> None:
    found = inspector.inspect_path(str(dossier))
    unknown = found.find("png.exif.0xc7a1")
    assert unknown is not None
    assert unknown.category is MetadataCategory.UNKNOWN
    assert unknown.removable is False

    # Asking for it directly is refused by the plan, not acted on by the rewriter.
    decision = cleaner.plan(found, frozenset({"png.exif.0xc7a1"}))
    assert decision.remove == ()
    assert [item.key for item in decision.blocked] == ["png.exif.0xc7a1"]
    assert png_module.removal_from_keys(frozenset({"png.exif.0xc7a1"})).exif_tags == frozenset(
        {0xC7A1}
    ), "the rewriter maps it, the plan is what refuses it"

    outcome = cleaner.clean(
        str(dossier),
        str(tmp_path / "unknown.png"),
        cleaner.default_selection(found),
    )
    assert inspector.inspect_path(str(outcome.output)).find("png.exif.0xc7a1") is not None


# ---------------------------------------------------------------------------
# 11. cleaning twice
# ---------------------------------------------------------------------------
def test_cleaning_the_cleaned_copy_is_safe(dossier: Path, tmp_path: Path) -> None:
    found = inspector.inspect_path(str(dossier))
    first = cleaner.clean(str(dossier), str(tmp_path / "first.png"), cleaner.default_selection(found))

    again = inspector.inspect_path(str(first.output))
    assert again.counts.personal == 0
    assert cleaner.default_selection(again) == frozenset()

    # The attribution fields are still offered, so a second pass is a real pass.
    second = cleaner.clean(
        str(first.output),
        str(tmp_path / "second.png"),
        frozenset({"png.exif.0x013b"}),
    )
    assert second.verified is True
    assert inspector.inspect_path(str(second.output)).find("png.exif.0x013b") is None
    assert inspector.inspect_path(str(second.output)).counts.personal == 0

    # And a third pass with nothing selected is refused instead of writing a copy.
    with pytest.raises(MetadataError):
        cleaner.clean(str(second.output), str(tmp_path / "third.png"), frozenset())


def test_nothing_the_plan_offers_can_lose_a_protected_field(dossier: Path, tmp_path: Path) -> None:
    """Every removable field, on its own, must clean or be refused — never fail.

    This is the property the bug broke: a field offered by the plan has to be
    something the rewrite can actually do without touching a protected field.
    """
    found = inspector.inspect_path(str(dossier))
    offered = sorted(found.removable_keys)

    assert offered, "the fixture must offer something"
    for index, key in enumerate(offered):
        decision = cleaner.plan(found, frozenset({key}))
        if not decision.remove:
            continue
        output = tmp_path / f"single-{index}.png"
        outcome = cleaner.clean(str(dossier), str(output), frozenset({key}))
        after = inspector.inspect_path(str(outcome.output))
        for protected in PROTECTED:
            assert after.find(protected) is not None, f"{protected} was lost when removing {key}"
        assert outcome.verified is True


# ---------------------------------------------------------------------------
# the same defect in the other structured blocks
# ---------------------------------------------------------------------------
def test_a_mixed_xmp_packet_is_protected_instead_of_gutted(tmp_path: Path) -> None:
    """One tick used to drop the whole packet, copyright line included."""
    path = samples.xmp_png(
        tmp_path / "mixed.png",
        {
            "dc:title": "Holiday",
            "dc:rights": "(c) 2024 Ada Lovelace",
            "photoshop:City": "Lubny",
        },
    )
    found = inspector.inspect_path(str(path))

    packet = found.find("png.xmp")
    assert packet is not None
    assert packet.removable is False, "the packet also carries the copyright line"
    assert "dc:rights" in packet.reason or "XMP dc:rights" in packet.reason
    assert cleaner.default_selection(found) == frozenset(), (
        "nothing in this packet can be removed without losing a protected property"
    )
    decision = cleaner.plan(found, frozenset({"png.xmp"}))
    assert decision.remove == ()
    assert [item.key for item in decision.blocked] == ["png.xmp"]

    with pytest.raises(MetadataError):
        cleaner.clean(str(path), str(tmp_path / "mixed-clean.png"), frozenset({"png.xmp"}))
    assert not (tmp_path / "mixed-clean.png").exists()

    # The properties are still readable: refusing to remove is not refusing to look.
    assert found.find("png.xmp.photoshop:City") is not None
    assert found.find("png.xmp.dc:rights") is not None
    assert found.counts.total == 1, "the properties are views, not extra entries"


def test_an_xmp_packet_with_only_personal_properties_is_removed(tmp_path: Path) -> None:
    path = samples.xmp_png(
        tmp_path / "personal.png",
        {"photoshop:City": "Lubny", "photoshop:Country": "UA"},
    )
    found = inspector.inspect_path(str(path))
    output = tmp_path / "personal-clean.png"

    decision = cleaner.plan(found, frozenset({"png.xmp"}))
    outcome = cleaner.clean(str(path), str(output), frozenset({"png.xmp"}))

    assert [item.key for item in decision.remove] == ["png.xmp"]
    assert outcome.verified is True
    assert inspector.inspect_path(str(output)).fields == (), "the packet left as a block"
    assert inspector.inspect_path(str(output)).counts.total == 0


@pytest.mark.parametrize("builder", [samples.personal_wav, samples.personal_flac])
def test_the_other_formats_never_offer_a_field_that_costs_a_protected_one(
    builder: Callable[[Path], Path], tmp_path: Path
) -> None:
    """The same property as for PNG: what the plan offers, the rewrite can do.

    WAV and FLAC do not carry the derived views PNG does, so this is a guard
    against a future block-level field (an iXML chunk, a picture block) growing
    the same defect rather than a bug found today.
    """
    source = builder(tmp_path / "probe")
    found = inspector.inspect_path(str(source))
    protected = [item.key for item in found.fields if not item.removable and not item.derived]

    assert found.removable_keys, "the fixture must offer something"
    for index, key in enumerate(sorted(found.removable_keys)):
        decision = cleaner.plan(found, frozenset({key}))
        if not decision.remove:
            continue
        output = tmp_path / f"single-{index}.{found.fmt.value}"
        outcome = cleaner.clean(str(source), str(output), frozenset({key}))
        after = inspector.inspect_path(str(outcome.output))
        for kept in protected:
            assert after.find(kept) is not None, f"{kept} was lost when removing {key}"
        assert outcome.verified is True
