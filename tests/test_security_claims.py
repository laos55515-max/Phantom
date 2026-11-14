"""R3: every user-visible security claim is generated from the code constants."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from phantom.core import constants as C
from phantom.i18n import I18n, load_locale

REPO = Path(__file__).resolve().parent.parent
LOCALES = REPO / "src" / "phantom" / "locales"
README_FILES = [REPO / "README.md", REPO / "README.ru.md"]
# Assembled from fragments so this file can itself be scanned by
# ``test_no_stubs.py`` without tripping the same rule.
FORBIDDEN = tuple(
    left + right
    for left, right in (
        ("невид", "имо"),
        ("необнаруж", "имо"),
        ("невзлам", "ываемо"),
        ("invi", "sible"),
        ("unde", "tectable"),
        ("unbr", "eakable"),
    )
)


def _params() -> dict[str, object]:
    return {
        "cipher": "AES-256-GCM",
        "kdf": C.kdf_summary(C.default_kdf_profile()),
        "version": C.APP_VERSION,
        "min_len": C.MIN_PASSWORD_LENGTH,
        "length": C.GENERATED_PASSWORD_LENGTH,
        "seconds": C.CLIPBOARD_CLEAR_SECONDS,
    }


def test_about_text_quotes_real_constants() -> None:
    for code in ("uk", "ru", "en"):
        template = load_locale(code, LOCALES)["settings_about"]
        rendered = template.format(**_params())
        assert f"PHANTOM {C.APP_VERSION}" in rendered
        assert C.kdf_summary(C.default_kdf_profile()) in rendered
        assert "AES-256-GCM" in rendered
        for legacy_number in ("v12.1", "600k", "200 000", "200,000"):
            assert legacy_number not in rendered


def test_header_subtitle_quotes_real_constants() -> None:
    for code in ("uk", "ru", "en"):
        rendered = load_locale(code, LOCALES)["header_subtitle"].format(**_params())
        assert C.kdf_summary(C.default_kdf_profile()) in rendered
        assert "AES-256-GCM" in rendered


def test_no_locale_hardcodes_kdf_numbers() -> None:
    """A hard-coded iteration/memory count may disagree with the code (D1)."""
    allowed_numbers = {
        str(C.ARGON2_MEMORY_KIB // 1024),
        f"{C.ARGON2_MEMORY_KIB // 1024:,}",
        str(C.ARGON2_ITERATIONS),
        f"{C.PBKDF2_ITERATIONS:,}",
        str(C.PBKDF2_ITERATIONS),
        str(C.MIN_PASSWORD_LENGTH),
        str(C.GENERATED_PASSWORD_LENGTH),
        str(C.CLIPBOARD_CLEAR_SECONDS),
        str(C.HEADER_BITS),
        str(C.VERSION) if hasattr(C, "VERSION") else C.APP_VERSION,
        "256",
        "64",
        "32",
    }
    suspicious = {"600 000", "600,000", "200 000", "200,000", "600000", "200000"}
    for code in ("uk", "ru", "en"):
        data = load_locale(code, LOCALES)
        for key, value in data.items():
            for number in suspicious:
                if number in value:
                    assert number in allowed_numbers, f"{code}:{key} hardcodes {number}"


def test_no_forbidden_claim_words_in_locales() -> None:
    for code in ("uk", "ru", "en"):
        data = load_locale(code, LOCALES)
        for key, value in data.items():
            lowered = value.lower()
            for word in FORBIDDEN:
                assert word not in lowered, f"{code}:{key} claims {word!r}"


def test_locales_describe_limitations() -> None:
    for code in ("uk", "ru", "en"):
        data = load_locale(code, LOCALES)
        threat = data["settings_threat_model"].lower()
        assert "lsb" in threat
        assert "стег" in threat or "steg" in threat
        assert "document" in threat or "документ" in threat
        assert "ssd" in data["settings_wipe_warning"].lower()


@pytest.mark.parametrize("readme", README_FILES, ids=lambda p: p.name)
def test_readme_exists_and_is_constant_driven(readme: Path) -> None:
    assert readme.exists(), f"missing {readme.name}"
    text = readme.read_text(encoding="utf-8")
    assert C.kdf_summary(C.default_kdf_profile()) in text
    assert C.APP_VERSION in text
    assert "AES-256-GCM" in text
    for word in FORBIDDEN:
        assert word not in text.lower(), f"{readme.name} contains {word!r}"
    assert "photo" in text.lower() or "фото" in text.lower()
    assert "document" in text.lower() or "документ" in text.lower()
    assert "ssd" in text.lower()


def test_security_md_exists() -> None:
    security = REPO / "SECURITY.md"
    assert security.exists()
    text = security.read_text(encoding="utf-8")
    assert C.kdf_summary(C.default_kdf_profile()) in text
    assert "AES-256-GCM" in text


def test_changelog_mentions_release() -> None:
    changelog = REPO / "CHANGELOG.md"
    assert changelog.exists()
    assert C.APP_VERSION in changelog.read_text(encoding="utf-8")


def test_ui_translations_do_not_promise_perfection() -> None:
    i18n = I18n("en", locales_dir=LOCALES)
    text = i18n.t("settings_threat_model").lower()
    assert "does not protect" in text or "не захищає" in text or "не защищает" in text


def test_security_summary_helper_shape() -> None:
    summary = C.security_summary()
    assert "AES-256-GCM" in summary
    assert "Argon2id" in summary
    assert re.search(r"m=\d+ MiB", summary)
    assert f"t={C.ARGON2_ITERATIONS}" in summary
