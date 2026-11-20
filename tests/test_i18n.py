"""Locales: parity, placeholders, fallback and live switching (D14, R3)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from phantom.core import constants as C
from phantom.core.errors import ERROR_CODES
from phantom.core.progress import Stage
from phantom.i18n import I18n, LocaleError, available_languages, load_locale, placeholders

LOCALES = Path(__file__).resolve().parent.parent / "src" / "phantom" / "locales"


def test_three_locales_present() -> None:
    assert set(available_languages(LOCALES)) >= {"uk", "ru", "en"}


@pytest.mark.parametrize("code", ["uk", "ru", "en"])
def test_locale_is_flat_string_map(code: str) -> None:
    data = load_locale(code, LOCALES)
    assert data
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in data.items())


def test_all_locales_have_identical_keys() -> None:
    reference = set(load_locale("en", LOCALES))
    for code in (c for c in available_languages(LOCALES) if c != "en"):
        data = load_locale(code, LOCALES)
        missing = reference - set(data)
        extra = set(data) - reference
        assert not missing, f"{code} lacks keys: {sorted(missing)}"
        assert not extra, f"{code} has unknown keys: {sorted(extra)}"


def test_all_locales_have_identical_placeholders() -> None:
    reference = load_locale("en", LOCALES)
    for code in (c for c in available_languages(LOCALES) if c != "en"):
        data = load_locale(code, LOCALES)
        for key, template in reference.items():
            assert placeholders(template) == placeholders(data[key]), (
                f"{code}:{key} placeholder mismatch"
            )


def test_every_error_code_is_localised() -> None:
    for code in ERROR_CODES:
        for locale in ("uk", "ru", "en"):
            assert f"err.{code.value}" in load_locale(locale, LOCALES), (
                f"{locale} is missing err.{code.value}"
            )


def test_the_metadata_conflict_message_says_what_happened() -> None:
    """A bare tag number is not an error message.

    The user saw "Ошибка обработки метаданных: png.gps.0x0000".  The sentence now
    names the field by label, says the source was not touched and that no copy was
    written, and calls the failure what it is: a bug in the policy.
    """
    for locale in ("uk", "ru", "en"):
        text = load_locale(locale, LOCALES)["err.metadata_conflict"]
        rendered = text.format(fields="GPSVersionID")
        assert "GPSVersionID" in rendered
        assert "{fields}" not in rendered
        assert "0x" not in rendered
        assert re.search(r"\b(file|файл)\b", rendered, re.IGNORECASE), (
            f"{locale}: the message must say what happened to the file"
        )


def test_every_stage_is_localised() -> None:
    for stage in Stage:
        for locale in ("uk", "ru", "en"):
            assert f"stage.{stage.value}" in load_locale(locale, LOCALES)


def test_every_notice_constant_is_localised() -> None:
    from phantom.audio.common import (
        NOTICE_AUDIO_SILENCE,
        NOTICE_AUDIO_TAGS,
        NOTICE_AUDIO_TAGS_LOST,
    )
    from phantom.stego import image as image_module

    notices = {
        NOTICE_AUDIO_SILENCE,
        NOTICE_AUDIO_TAGS,
        NOTICE_AUDIO_TAGS_LOST,
        image_module.NOTICE_PALETTE,
        image_module.NOTICE_GRAYSCALE,
        image_module.NOTICE_BITMAP,
        image_module.NOTICE_TRANSPARENCY_KEPT,
    }
    english = load_locale("en", LOCALES)
    for notice in notices:
        assert notice in english, f"missing locale key {notice}"


def test_translator_formats_placeholders() -> None:
    i18n = I18n("en", locales_dir=LOCALES)
    text = i18n.t("lbl_capacity_bits", size=1000, kb="1.0", bits=8000)
    assert "1000" in text and "8000" in text and "{" not in text


def test_missing_placeholder_is_left_visible() -> None:
    i18n = I18n("en", locales_dir=LOCALES)
    text = i18n.t("lbl_capacity_bits", size=5)
    assert "{kb}" in text and "{bits}" in text


def test_fallback_to_default_language(tmp_path: Path) -> None:
    (tmp_path / "en.json").write_text(json.dumps({"only_in_en": "Hello {name}"}), encoding="utf-8")
    (tmp_path / "xx.json").write_text(json.dumps({"other": "Other"}), encoding="utf-8")
    i18n = I18n("xx", locales_dir=tmp_path)
    assert i18n.t("only_in_en", name="World") == "Hello World"
    assert i18n.t("unknown_key") == "unknown_key"


def test_unknown_language_falls_back() -> None:
    i18n = I18n("zz", locales_dir=LOCALES)
    assert i18n.language == "en"


def test_set_language_reports_change() -> None:
    i18n = I18n("en", locales_dir=LOCALES)
    assert i18n.set_language("uk") is True
    assert i18n.language == "uk"
    assert i18n.set_language("uk") is False
    assert i18n.set_language("de") is False


def test_language_names_are_native() -> None:
    i18n = I18n("en", locales_dir=LOCALES)
    assert i18n.language_name("uk") == "Українська"
    assert i18n.language_name("en") == "English"


def test_missing_locale_file_raises(tmp_path: Path) -> None:
    with pytest.raises(LocaleError):
        load_locale("zz", tmp_path)


def test_broken_locale_file_raises(tmp_path: Path) -> None:
    (tmp_path / "en.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LocaleError):
        I18n("en", locales_dir=tmp_path)


def test_missing_keys_diagnostics() -> None:
    i18n = I18n("en", locales_dir=LOCALES)
    assert list(i18n.missing_keys("en")) == []
    assert dict(i18n.placeholder_mismatches("uk")) == {}


def test_security_numbers_come_from_constants() -> None:
    """D1 regression: the hint must quote the real KDF parameters."""
    profile = C.default_kdf_profile()
    expected = C.kdf_summary(profile)
    for code in ("uk", "ru", "en"):
        hint = load_locale(code, LOCALES)["lbl_pwd_hint"]
        assert "{kdf}" in hint
        rendered = hint.format(min_len=C.MIN_PASSWORD_LENGTH, kdf=expected)
        assert expected in rendered
        assert "200 000" not in rendered and "200,000" not in rendered
