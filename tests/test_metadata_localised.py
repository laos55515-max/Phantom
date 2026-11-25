"""The metadata tab in ru/uk/en: the text report follows the language, JSON does not.

Two rules are pinned here:

* a user-facing text report is written in the language of the window, all of it —
  headers, plan words, verdicts, the disclaimer;
* the JSON export is a machine format: same keys, same English identifiers, no
  locale anywhere, whatever the window displays.

A third, quieter rule: every English template has a translation in all three
locales, so a new line cannot ship half-translated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import metadata_samples as samples
from phantom.i18n import I18n
from phantom.metadata import cleaner, inspector
from phantom.metadata import report as meta_report

LANGUAGES = ("en", "ru", "uk")


def _report(tmp_path: Path):
    return inspector.inspect_path(str(samples.personal_png(tmp_path / "localised.png")))


def _cyrillic(text: str) -> bool:
    return any("а" <= char.lower() <= "я" or char in "іїєґ" for char in text)


# ---------------------------------------------------------------------------
# the text report
# ---------------------------------------------------------------------------
def test_the_report_is_english_without_a_locale(tmp_path: Path) -> None:
    """The default is what the JSON schema and the existing tests speak."""
    text = meta_report.inspection_text(_report(tmp_path))

    assert text.startswith("PHANTOM — metadata report")
    assert "fields:" in text
    assert "sha256 (whole file):" in text


@pytest.mark.parametrize("language", ["ru", "uk"])
def test_the_report_is_written_in_the_windows_language(tmp_path: Path, language: str) -> None:
    found = _report(tmp_path)
    i18n = I18n(language)

    text = meta_report.inspection_text(found, i18n)

    assert _cyrillic(text), "the report stayed English"
    assert i18n.t("report.title") in text
    assert "PHANTOM — metadata report" not in text
    # Data is never translated, only the words around it.
    assert found.path in text
    assert found.sha256 in text
    for item in found.fields:
        assert item.label in text


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_disclaimer_is_the_one_the_locale_carries(tmp_path: Path, language: str) -> None:
    found = _report(tmp_path)
    i18n = I18n(language)

    text = meta_report.inspection_text(found, i18n)

    assert i18n.t("report.disclaimer") in text


def test_the_plan_preview_is_localised_too(tmp_path: Path) -> None:
    found = inspector.inspect_path(str(samples.personal_wav(tmp_path / "plan.wav")))
    decision = cleaner.plan(found, frozenset({"wav.info.ICMT"}))
    i18n = I18n("uk")

    text = meta_report.cleaning_text(decision, None, i18n)

    assert i18n.t("report.plan_title") in text
    assert i18n.t("report.plan_remove", count=1) in text
    assert "will be removed" not in text


def test_the_result_block_says_yes_in_the_windows_language(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "verdict.png")
    output = tmp_path / "verdict-clean.png"
    selection = frozenset({"png.text.tEXt.Comment"})
    found = inspector.inspect_path(str(source))
    outcome = cleaner.clean(str(source), str(output), selection)
    i18n = I18n("ru")

    text = meta_report.cleaning_text(cleaner.plan(found, selection), outcome, i18n)

    assert i18n.t("report.result_identical", state=i18n.t("report.state_yes")) in text
    assert outcome.source_sha256 in text and outcome.output_sha256 in text


def test_the_summary_line_is_localised(tmp_path: Path) -> None:
    found = _report(tmp_path)

    line = meta_report.summary_line(found, I18n("ru"))

    assert "полей" in line
    assert found.path in line
    assert "Ada Lovelace" not in line


# ---------------------------------------------------------------------------
# the JSON report
# ---------------------------------------------------------------------------
def test_the_json_report_does_not_depend_on_the_language(tmp_path: Path) -> None:
    found = _report(tmp_path)
    decision = cleaner.plan(found, frozenset({"png.text.tEXt.Comment"}))

    english = meta_report.report_json(found, decision)

    assert meta_report.report_json(found, decision) == english
    assert "PHANTOM metadata inspector" in english
    assert '"report_format": 1' in english
    assert not _cyrillic(english), "a localised word leaked into the machine format"


def test_json_report_has_no_texts_parameter_at_all() -> None:
    """The machine report must not be able to take a locale: that is the guarantee."""
    import inspect as py_inspect

    signature = py_inspect.signature(meta_report.report_json)

    assert list(signature.parameters) == ["report", "plan", "outcome"]


# ---------------------------------------------------------------------------
# locale completeness
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("language", LANGUAGES)
def test_every_report_template_exists_in_every_locale(language: str) -> None:
    i18n = I18n(language)

    missing = [key for key in meta_report.TEXTS if not i18n.has(key)]

    assert not missing, f"{language} is missing report lines: {missing}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_locale_leaves_a_report_line_empty(language: str) -> None:
    i18n = I18n(language)

    empty = [key for key in meta_report.TEXTS if not str(i18n.t(key)).strip()]

    assert not empty


def test_a_broken_locale_falls_back_to_english_instead_of_a_key(tmp_path: Path) -> None:
    """A half-translated locale must not print ``report.title`` at a user."""

    class HalfLocale:
        def has(self, key: str) -> bool:
            return True

        def t(self, key: str, **params: object) -> str:
            raise KeyError(key)

    text = meta_report.inspection_text(_report(tmp_path), HalfLocale())  # type: ignore[arg-type]

    assert text.startswith("PHANTOM — metadata report")
    assert "report." not in text


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "ru",
            "Профиль помогает правильно отображать цвета. "
            "Без него изображение может выглядеть иначе",
        ),
        (
            "uk",
            "Профіль допомагає правильно відображати кольори. "
            "Без нього зображення може виглядати інакше",
        ),
        (
            "en",
            "The profile helps display colours correctly. "
            "Without it, the image may look different",
        ),
    ],
)
def test_icc_profile_explanation_is_localised_and_does_not_claim_a_certain_change(
    tmp_path: Path, language: str, expected: str
) -> None:
    from PIL import Image, ImageCms

    path = tmp_path / "profile.png"
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (4, 4)).save(path, icc_profile=profile)
    report = inspector.inspect_path(str(path))
    field = report.find("png.chunk.iCCP")
    assert field is not None and not field.removable
    assert meta_report.field_reason(field, I18n(language)) == expected
