"""Exact UTF-8 encoding of user text: refused when it cannot be exact.

A Python ``str`` can hold a **lone surrogate** (``"\\ud800"``).  Such a code point is
not a character: it has no UTF-8 form, it cannot be written to a file, printed, or
stored in a payload, and ``str.encode("utf-8")`` raises ``UnicodeEncodeError``.
Surrogates reach the application from a clipboard, from a file name the operating
system could not decode (``os.listdir`` uses ``surrogateescape``), or from a tool
that spliced UTF-16 halves together.

Three policies were possible; two of them are wrong for this product:

* **replace silently** (``errors="replace"``) or re-encode through surrogatepass —
  the user's text changes without them being told, and a payload of 200 characters
  may come back as 200 different ones.  PHANTOM's promise is an exact round trip, so
  a silent lossy transform is out;
* **store whatever the C library accepts** — WTF-8 is not UTF-8: every other reader
  of that payload, including this one on extraction, has to guess, and the digest
  check would still say the bytes are intact while the text is not what was typed;
* **refuse, and say exactly what is wrong** — nothing is read, compressed, written
  or renamed, the input is left byte-for-byte as the user had it, and the message
  names the code point and where it is.  That is what this module implements.

Nothing about stored data changes: extraction still decodes with
``errors="replace"`` for display only, so every file written by an older build keeps
extracting exactly as before, and this refusal happens *before* anything is packed.
"""

from __future__ import annotations

from typing import Final

from phantom.core.errors import InvalidUnicode

#: Surrogate code points.  Only the *lone* ones matter here: a valid pair written
#: as two surrogates in a Python string (``"\ud83d\ude00"``) is not a character
#: either, and it fails the same way — ``str.encode`` is the single source of truth
#: below, so this range is documentation, not a second implementation.
SURROGATE_RANGE: Final = (0xD800, 0xDFFF)

def first_unencodable(text: str) -> int | None:
    """Index of the first code point without a UTF-8 form, or ``None``.

    ``str.encode`` is asked rather than a hand-written surrogate test: it is the
    same code the payload writer runs, so "this helper says it is fine" and "the
    writer accepts it" can never disagree.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        return exc.start
    return None


def describe_unencodable(text: str, index: int) -> str:
    """``"U+D800 at position 1"`` — the code point and a 1-based position."""
    code = ord(text[index])
    return f"U+{code:04X} at position {index + 1}"


def require_exact(text: str, *, what: str = "value") -> bytes:
    """The UTF-8 bytes of ``text``, or :class:`InvalidUnicode` naming the offender.

    ``what`` is for the log line only ("text", "file name", "password"): the message
    the user sees is a locale template with the code point and its position, because
    an English noun inside a Ukrainian dialog is the same bug in another place.
    """
    index = first_unencodable(text)
    if index is None:
        return text.encode("utf-8")
    raise InvalidUnicode(
        f"{what} contains a code point with no UTF-8 form",
        detail=describe_unencodable(text, index),
    ).with_params(codepoint=f"U+{ord(text[index]):04X}", position=index + 1)


def utf8_size(text: str) -> int:
    """UTF-8 size of ``text`` for counters and estimates; never raises.

    A counter is not an encoder: it must not make a text box unusable because one
    pasted character has no encoding.  ``surrogatepass`` is used here — and only
    here — so an unencodable code point is counted as the three bytes of the
    replacement character it is displayed as, while the interface says separately
    that the text itself cannot be stored.
    """
    return len(text.encode("utf-8", errors="surrogatepass"))
