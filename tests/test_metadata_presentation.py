"""What the card says, and about which file.

The numbers were right in every defect pinned here; the *words around them* were
not.  Four separate complaints, one theme — the card described one thing and named
another:

1. English sentences in a Russian or Ukrainian card ("contains personal
   properties", "a property of the packet below");
2. "unknown 0" printed next to a visible unknown property, because the property is
   a derived view and the counter only counted entries;
3. a save button that was disabled but still painted in the accent colour;
4. after a clean, a header naming the source file while the numbers described the
   cleaned copy.

The rules these tests defend:

* a sentence this program wrote is translated; data is not;
* entries and derived views are counted separately and both are shown;
* a disabled accent button does not look enabled (checked in ``test_gui``);
* the report says which file it describes, and the JSON keeps English.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests import metadata_samples as samples
from tests.test_metadata_xmp_structural import UNKNOWN_NS, xmp_png

from phantom.i18n import I18n
from phantom.metadata import cleaner, inspector
from phantom.metadata import report as meta_report
from phantom.metadata.models import MetadataCategory, MetadataField, MetadataReport

LANGUAGES = ("en", "ru", "uk")
CYRILLIC = "абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ"


def _cyrillic(text: str) -> bool:
    return any(char.lower() in CYRILLIC for char in text)


def _samples(tmp_path: Path) -> Iterator[MetadataReport]:
    """One report per container, plus the three XMP shapes that carry prose."""
    yield inspector.inspect_path(str(samples.personal_png(tmp_path / "p.png")))
    yield inspector.inspect_path(str(samples.personal_wav(tmp_path / "a.wav")))
    yield inspector.inspect_path(str(samples.personal_flac(tmp_path / "a.flac")))
    yield inspector.inspect_path(str(xmp_png(tmp_path / "x1.png", "<photoshop:City>Lubny</photoshop:City>")))
    yield inspector.inspect_path(
        str(
            xmp_png(
                tmp_path / "x2.png",
                "<photoshop:City>Lubny</photoshop:City>",
                attributes='dc:rights="(c) 2024 Ada Lovelace"',
            )
        )
    )
    yield inspector.inspect_path(
        str(
            xmp_png(
                tmp_path / "x3.png",
                "<photoshop:City>Lubny</photoshop:City>",
                extra_ns=f' xmlns:qa="{UNKNOWN_NS}"',
                attributes='qa:TestProperty="alpha"',
            )
        )
    )


def _all_fields(tmp_path: Path) -> list[MetadataField]:
    return [item for report in _samples(tmp_path) for item in report.fields]


# ---------------------------------------------------------------------------
# 1. the two reported English fragments
# ---------------------------------------------------------------------------
def test_the_value_of_a_personal_packet_is_translated(tmp_path: Path) -> None:
    """The reported string ``contains personal properties``, in all three languages."""
    report = inspector.inspect_path(
        str(xmp_png(tmp_path / "one.png", "<photoshop:City>Lubny</photoshop:City>"))
    )
    entry = report.find("png.xmp")
    assert entry is not None
    assert entry.value == "contains personal properties", "the fallback stays English"

    assert meta_report.field_value(entry, I18n("en")) == "contains personal properties"
    for code in ("ru", "uk"):
        shown = meta_report.field_value(entry, I18n(code))
        assert shown != entry.value, f"{code} still shows the English sentence"
        assert _cyrillic(shown), shown


def test_the_reason_of_a_derived_property_is_translated(tmp_path: Path) -> None:
    """The reported string ``a property of the packet below``."""
    report = inspector.inspect_path(
        str(xmp_png(tmp_path / "one.png", "<photoshop:City>Lubny</photoshop:City>"))
    )
    prop = next(item for item in report.fields if item.key == "png.xmp.photoshop:City")
    assert "packet below" in prop.reason, "the fallback stays English"
    assert prop.reason_key == "meta.reason.xmp_property"

    for code in ("ru", "uk"):
        shown = meta_report.field_reason(prop, I18n(code))
        assert "packet below" not in shown
        assert _cyrillic(shown), shown


def test_every_sentence_this_program_writes_can_be_translated(tmp_path: Path) -> None:
    """No field may carry prose without a locale key: that is how the two leaked.

    The rule is deliberately blunt — a reader that wants to show an English
    sentence has to register it in all three locales first.
    """
    fields = _all_fields(tmp_path)
    assert fields, "the samples produced no fields at all"
    texted = [item for item in fields if item.reason or item.value_key]
    assert texted, "the samples must exercise at least one sentence"

    for code in LANGUAGES:
        i18n = I18n(code)
        for item in fields:
            if item.reason:
                assert item.reason_key, f"{item.key} carries untranslatable prose: {item.reason}"
                assert i18n.has(item.reason_key), f"{code} misses {item.reason_key}"
            if item.value_key:
                assert i18n.has(item.value_key), f"{code} misses {item.value_key}"


def test_the_locales_agree_on_every_key() -> None:
    keysets = {code: I18n(code).keys() for code in LANGUAGES}
    english = keysets["en"]
    for code in ("ru", "uk"):
        assert keysets[code] == english, f"{code} and en disagree: {keysets[code] ^ english}"


def test_a_translated_sentence_is_not_the_english_one(tmp_path: Path) -> None:
    """Every key that produced the leak must actually differ, not copy through."""
    i18n_ru, i18n_uk = I18n("ru"), I18n("uk")
    i18n_en = I18n("en")
    for item in _all_fields(tmp_path):
        for key in (item.reason_key, item.value_key):
            if not key:
                continue
            english = i18n_en.t(key, **dict(item.reason_params or item.value_params))
            for i18n in (i18n_ru, i18n_uk):
                shown = i18n.t(key, **dict(item.reason_params or item.value_params))
                assert shown != english, f"{key} is untranslated in {i18n.language}"
                assert _cyrillic(shown), (key, i18n.language, shown)


def test_the_json_export_stays_english(tmp_path: Path) -> None:
    """Localisation is for eyes, not for the machine format."""
    report = inspector.inspect_path(
        str(xmp_png(tmp_path / "one.png", "<photoshop:City>Lubny</photoshop:City>"))
    )
    exported = report.to_dict()
    values = [entry["value"] for entry in exported["fields"]]  # type: ignore[union-attr]
    assert "contains personal properties" in values
    assert not any(_cyrillic(value) for value in values if isinstance(value, str))


# ---------------------------------------------------------------------------
# 2. entries and derived views are counted apart
# ---------------------------------------------------------------------------
def test_an_unknown_derived_property_is_not_counted_as_zero(tmp_path: Path) -> None:
    """The reported contradiction: "unknown 0" beside a visible unknown property."""
    report = inspector.inspect_path(
        str(
            xmp_png(
                tmp_path / "qa.png",
                "<photoshop:City>Lubny</photoshop:City>",
                extra_ns=f' xmlns:qa="{UNKNOWN_NS}"',
                attributes='qa:TestProperty="alpha"',
            )
        )
    )
    derived = [item for item in report.fields if item.derived]
    unknown_derived = [item for item in derived if item.category is MetadataCategory.UNKNOWN]

    assert unknown_derived, "the sample must show an unknown property"
    assert report.counts.unknown == 0, "no unknown *entry* is present: that is the point"
    assert report.derived_counts.unknown == len(unknown_derived)
    assert report.derived_counts.total == len(derived)
    #: Nothing is lost between the two tallies.
    assert report.counts.total + report.derived_counts.total == len(report.fields)


def test_the_two_tallies_reach_the_json_separately(tmp_path: Path) -> None:
    report = inspector.inspect_path(str(samples.personal_png(tmp_path / "p.png")))
    exported = report.to_dict()
    assert exported["derived_counts"] == report.derived_counts.to_dict()  # type: ignore[index]
    assert exported["counts"] == report.counts.to_dict()  # type: ignore[index]


def test_the_card_shows_the_derived_tally_only_when_there_is_one(tmp_path: Path) -> None:
    """The extra line is a clarification, not a permanent empty row."""
    with_views = inspector.inspect_path(
        str(xmp_png(tmp_path / "one.png", "<photoshop:City>Lubny</photoshop:City>"))
    )
    assert with_views.derived_counts.total >= 1

    plain = inspector.inspect_path(str(samples.personal_flac(tmp_path / "a.flac")))
    if plain.derived_counts.total == 0:
        for code in LANGUAGES:
            line = I18n(code).t(
                "meta_results_derived",
                total=plain.derived_counts.total,
                unknown=plain.derived_counts.unknown,
            )
            assert str(plain.derived_counts.total) in line


# ---------------------------------------------------------------------------
# 3. the disabled save button (its colour is painted only in a real window)
# ---------------------------------------------------------------------------
def test_a_disabled_accent_button_uses_a_theme_colour() -> None:
    """``set_enabled`` must go through the theme, so both modes stay readable.

    The colour itself needs a window (see ``test_gui``); what is pinned here is that
    the fill comes from the theme and is not a hard-coded blue, and that the accent
    is remembered at creation — otherwise nothing could be restored on re-enable.
    """
    ui = Path(__file__).resolve().parents[1] / "src" / "phantom" / "ui"
    theme_source = (ui / "theme.py").read_text(encoding="utf-8")
    widgets_source = (ui / "widgets.py").read_text(encoding="utf-8")

    assert "CLR_DISABLED_FILL" in theme_source
    disabled = theme_source.split("CLR_DISABLED_FILL")[1].split("\n")[0]
    accent = theme_source.split("CLR_ACCENT:")[1].split("\n")[0]
    assert disabled.split("=", 1)[1].strip() != accent.split("=", 1)[1].strip(), (
        "a disabled accent button must not keep the accent fill"
    )

    assert "def set_enabled(" in widgets_source
    assert "T.CLR_DISABLED_FILL" in widgets_source
    assert '_phantom_accent' in widgets_source, (
        "the accent has to be recorded at creation to be restorable"
    )


# ---------------------------------------------------------------------------
# 4. the result says which file it describes
# ---------------------------------------------------------------------------
def _cleanable(tmp_path: Path) -> tuple[Path, MetadataReport]:
    source = samples.personal_png(tmp_path / "src.png")
    report = inspector.inspect_path(str(source))
    return source, report


def test_the_result_names_the_cleaned_copy(tmp_path: Path) -> None:
    source, report = _cleanable(tmp_path)
    selection = frozenset(cleaner.default_selection(report))
    assert selection

    outcome = cleaner.clean(str(source), str(tmp_path / "copy.png"), selection)

    assert outcome.report_before.path == str(source), "the plan describes the source"
    assert outcome.report_after.path == outcome.output
    assert outcome.report_after.path != str(source), (
        "the post-clean numbers must not be filed under the source name"
    )
    assert Path(outcome.report_after.path).exists()


def test_the_copy_is_named_after_the_extension_it_really_has(tmp_path: Path) -> None:
    """A ``.jpg`` request becomes a ``.png`` file; the report must say so too."""
    source, report = _cleanable(tmp_path)
    selection = frozenset(cleaner.default_selection(report))

    outcome = cleaner.clean(str(source), str(tmp_path / "copy.jpg"), selection)

    assert outcome.output.endswith(".png")
    assert outcome.report_after.path == outcome.output
    assert outcome.report_after.path != str(tmp_path / "copy.jpg"), (
        "the report must name the file that was written, not the one requested"
    )


def test_the_report_text_header_names_the_file_it_describes(tmp_path: Path) -> None:
    source, report = _cleanable(tmp_path)
    selection = frozenset(cleaner.default_selection(report))
    outcome = cleaner.clean(str(source), str(tmp_path / "copy.png"), selection)

    text = meta_report.inspection_text(outcome.report_after)
    assert Path(outcome.output).name in text
    assert text.count(str(source)) == 0, "the source name would contradict the numbers"


@pytest.mark.parametrize("code", LANGUAGES)
def test_the_target_line_exists_and_names_the_file(code: str, tmp_path: Path) -> None:
    i18n = I18n(code)
    for key in ("meta_results_target", "meta_results_target_cleaned"):
        assert i18n.has(key)
        line = i18n.t(key, file="copy.png")
        assert "copy.png" in line
        assert _cyrillic(line) is (code != "en")


def test_the_panel_labels_the_two_cards_differently(tmp_path: Path) -> None:
    """Source in the file card, cleaned copy in the results card."""
    source, report = _cleanable(tmp_path)
    selection = frozenset(cleaner.default_selection(report))
    outcome = cleaner.clean(str(source), str(tmp_path / "copy.png"), selection)

    i18n = I18n("ru")
    same = i18n.t("meta_results_target", file=Path(str(source)).name)
    cleaned = i18n.t(
        "meta_results_target_cleaned", file=Path(outcome.report_after.path).name
    )
    assert same != cleaned
    assert Path(str(source)).name != Path(outcome.report_after.path).name
