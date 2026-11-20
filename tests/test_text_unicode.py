"""Invalid Unicode: refused exactly, never silently rewritten.

The defect these tests pin down is a crash, not a corruption: a Python string can
hold a *lone surrogate*

    >>> Payload.text("\\ud800")
    UnicodeEncodeError: 'utf-8' codec can't encode character '\\ud800' ...

Such a code point has no UTF-8 form.  It reaches the application from a clipboard,
from a file name the operating system could not decode, or from a tool that spliced
UTF-16 halves together — and it used to surface as a bare ``UnicodeEncodeError``
from a constructor: the worker could only turn that into "internal error", the
character counter raised on every keystroke, and the property test
``test_text_roundtrip_is_exact`` failed whenever hypothesis generated one.

The policy, and the reason for it:

* text that cannot be encoded **exactly** is refused with
  :class:`phantom.core.errors.InvalidUnicode` *before* anything is read, compressed
  or written — there is no output file to leave behind;
* it is never replaced (``errors="replace"``) or re-encoded as WTF-8: silently
  changing what the user typed is worse than saying "I cannot store this";
* nothing about the stored format changes, so every file written earlier — including
  one whose *extracted* text is displayed with replacement characters — keeps
  working exactly as before.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests import metadata_samples as samples

from phantom.core import api, kdf
from phantom.core import files as fsutil
from phantom.core import text as text_mod
from phantom.core.errors import InvalidPassword, InvalidUnicode, PhantomError
from phantom.core.payload import Payload

#: One code point from each of the writing systems the report asks about, plus the
#: shapes that usually break encoders: an emoji outside the BMP, a combining
#: sequence, a right-to-left run and a multi-line block.
VALID_TEXTS = {
    "empty": "",
    "ascii": "hello",
    "cyrillic": "Привіт, світе — тест",
    "greek": "Καλημέρα κόσμε",
    "chinese": "你好，世界",
    "japanese": "こんにちは世界",
    "arabic": "مرحبا بالعالم",
    "hebrew": "שלום עולם",
    "emoji": "🙂👩‍🚀🏳️‍🌈",
    "combining": "e\u0301a\u0300o\u0308 (éàö composed)",
    "multiline": "line one\nline two\r\nline three\twith tab",
    "nul_and_controls": "a\x00b\x07c",
    "zero_width": "a\u200bb\u200dc",
    "max_bmp": "\U0010ffff",
}

INVALID_TEXTS = {
    "lone_high": "\ud800",
    "lone_low": "\udfff",
    "embedded": "safe \ud834 text",
    "two_halves": "\ud83d\ude00",  # a valid pair, written as two surrogates
}


def _carrier(tmp_path: Path, name: str = "carrier.png") -> Path:
    return samples.png_file(tmp_path / name)


# ---------------------------------------------------------------------------
# the crash itself
# ---------------------------------------------------------------------------
def test_the_reported_crash_is_now_a_typed_error() -> None:
    with pytest.raises(InvalidUnicode) as caught:
        Payload.text("\ud800")

    error = caught.value
    assert error.code.value == "invalid_unicode"
    assert error.i18n_key == "err.invalid_unicode"
    assert error.params == {"codepoint": "U+D800", "position": 1}
    assert "U+D800" in (error.detail or "")


def test_the_api_refuses_before_it_touches_anything(tmp_path: Path) -> None:
    """No output, no staged sibling, and the carrier keeps its bytes."""
    carrier = _carrier(tmp_path)
    before = carrier.read_bytes()
    output = tmp_path / "out.png"

    with pytest.raises(InvalidUnicode):
        api.embed_payload(
            str(carrier),
            str(output),
            Payload.text("boom \ud800"),
            password="Property-passphrase-2026",
        )

    assert carrier.read_bytes() == before
    assert not output.exists()
    assert not list(tmp_path.glob("*.stego.png")), "no published copy either"


# ---------------------------------------------------------------------------
# the valid half: everything that *can* be stored must round-trip byte for byte
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("name", "value"), sorted(VALID_TEXTS.items()))
def test_valid_text_roundtrips_exactly(name: str, value: str, tmp_path: Path) -> None:
    data = Payload.text(value).data
    assert data.decode("utf-8") == value, f"{name}: encoding must be reversible"
    # Nothing is normalised: NFC/NFD stay as typed, so a composing sequence and its
    # precomposed form remain different texts, as they must.
    assert Payload.text(value).data == value.encode("utf-8")


@pytest.mark.parametrize(("name", "value"), sorted(VALID_TEXTS.items()))
def test_valid_text_survives_a_full_roundtrip(name: str, value: str, tmp_path: Path) -> None:
    """The end-to-end promise, on a carrier large enough for every case."""
    carrier = tmp_path / f"{name}.png"
    carrier.write_bytes(samples.png_bytes(pixels=samples.texture(160, 120)))
    output = tmp_path / f"{name}-out.png"
    password = "Property-passphrase-2026"

    payload = Payload.text(value)
    capacity = api.capacity_of(str(carrier))
    if len(payload.data) > capacity.user_bytes(""):
        pytest.skip(f"{name}: carrier too small for this test sample")

    outcome = api.embed_payload(str(carrier), str(output), payload, password=password)
    result = api.extract_from_path(str(output), password)

    assert outcome.verified is True
    assert result.payload.data == value.encode("utf-8")
    assert result.payload.data.decode("utf-8") == value


# ---------------------------------------------------------------------------
# the invalid half: refused, and measurable
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("name", "value"), sorted(INVALID_TEXTS.items()))
def test_invalid_text_is_refused_with_the_position(name: str, value: str) -> None:
    with pytest.raises(InvalidUnicode) as caught:
        Payload.text(value)

    error = caught.value
    index = text_mod.first_unencodable(value)
    assert index is not None
    assert error.params["position"] == index + 1
    assert error.params["codepoint"] == f"U+{ord(value[index]):04X}"


def test_the_refusal_is_a_phantom_error_the_ui_can_show() -> None:
    """``_show_error`` only knows ``PhantomError``; anything else becomes internal."""
    with pytest.raises(PhantomError):
        Payload.text("\ud800")
    with pytest.raises(PhantomError):
        Payload.file("photo\ud800.png", b"data")


def test_a_file_name_with_no_utf8_form_is_refused_not_crashed() -> None:
    """Linux hands over undecodable file names with surrogate escapes."""
    with pytest.raises(InvalidUnicode):
        Payload.file("tab\u0000le\udcff.png", b"data")


def test_a_stored_name_is_still_sanitised_the_same_way() -> None:
    """A hostile name from a container still goes through the old sanitiser."""
    assert fsutil.sanitise_filename("../../etc/passwd") == "passwd"
    assert fsutil.sanitise_filename("a\udcffb.png") == "a_b.png"


# ---------------------------------------------------------------------------
# passwords are secrets: refused, never normalised
# ---------------------------------------------------------------------------
def test_a_password_with_a_surrogate_is_a_password_error() -> None:
    with pytest.raises(InvalidPassword) as caught:
        kdf.validate_password("Property-passphrase-2026\ud800")
    assert caught.value.params["codepoint"] == "U+D800"


def test_the_kdf_never_sees_an_unencodable_password(tmp_path: Path) -> None:
    carrier = _carrier(tmp_path)
    with pytest.raises(InvalidPassword):
        api.embed_payload(
            str(carrier),
            str(tmp_path / "out.png"),
            Payload.text("hello"),
            password="Property-passphrase-2026\ud800",
        )
    assert not (tmp_path / "out.png").exists()


# ---------------------------------------------------------------------------
# counters never raise, and never lie about being able to store the text
# ---------------------------------------------------------------------------
def test_the_size_helper_counts_without_raising() -> None:
    assert text_mod.utf8_size("abc") == 3
    assert text_mod.utf8_size("😀") == 4
    assert text_mod.utf8_size("Привіт") == 12
    # An unencodable code point is counted as the three bytes it is shown as.
    assert text_mod.utf8_size("\ud800") == 3
    assert text_mod.utf8_size("a\ud800b") == 5


def test_require_exact_returns_the_same_bytes_as_encode() -> None:
    for value in VALID_TEXTS.values():
        assert text_mod.require_exact(value) == value.encode("utf-8")
