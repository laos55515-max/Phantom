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

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class TextMetrics:
    """One measurement of one string, taken once.

    The interface used to ask four different questions — how many characters, how
    many UTF-8 bytes, is any line long enough to slow Tk's word wrap, can this text
    be stored at all — and every one of them walked the whole string.  On a 12 MiB
    payload that is more than the editor can afford per keystroke, so all four are
    measured in one pass, stored, and reused until the text really changes.
    """

    chars: int
    utf8_bytes: int
    longest_line: int
    unencodable: int | None

    @property
    def empty(self) -> bool:
        return self.chars == 0

    @property
    def storable(self) -> bool:
        """Whether this text has an exact UTF-8 form (no lone surrogates)."""
        return self.unencodable is None


def measure_text(text: str, *, scan_lines: bool = True) -> TextMetrics:
    """Measure ``text`` in one walk: characters, UTF-8 bytes, longest line, validity.

    ``scan_lines=False`` skips the line walk and reports ``longest_line=-1`` — for a
    text short enough that no line can reach the wrap threshold, walking millions of
    lines to prove it would be the cost this function exists to avoid.
    """
    longest = -1
    if scan_lines:
        longest = 0
        start = 0
        while True:
            found = text.find("\n", start)
            if found < 0:
                longest = max(longest, len(text) - start)
                break
            longest = max(longest, found - start)
            start = found + 1
    try:
        size = len(text.encode("utf-8"))
        unencodable: int | None = None
    except UnicodeEncodeError as exc:
        # The byte counter must not make the field unusable: count what it would
        # take with the replacement form, and let the interface say separately that
        # the text cannot be stored (see `require_exact`).
        size = utf8_size(text)
        unencodable = exc.start
    return TextMetrics(
        chars=len(text),
        utf8_bytes=size,
        longest_line=longest,
        unencodable=unencodable,
    )
