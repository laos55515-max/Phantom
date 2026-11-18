"""XMP is read structurally, and a packet that cannot be read is never removed.

The defect these tests pin down: the inspector used to look for the *text*
``<dc:rights>`` inside the packet.  RDF/XML can write the same property as an
attribute of ``rdf:Description``, inside a ``rdf:Bag``/``Seq``/``Alt``, in a
nested node, or under any prefix the file likes — none of which the text search
saw.  A packet with ``dc:rights="(c) 2024 …"`` on the ``rdf:Description`` and a
``photoshop:City`` element therefore looked like "personal properties only", was
offered as removable, and was deleted in full — copyright line included — while
the outcome still said ``verified: True``.

Every test here is about one rule: **a property survives unless the reader has
proved it is personal and nothing else is in the packet.**  "I did not see it"
is never a reason to delete something.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest
from tests import metadata_samples as samples

from phantom import i18n as i18n_mod
from phantom.core.errors import MetadataError
from phantom.metadata import cleaner, inspector, xmp
from phantom.metadata import report as meta_report
from phantom.metadata.models import MetadataCategory

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DC = "http://purl.org/dc/elements/1.1/"
XMP_NS = "http://ns.adobe.com/xap/1.0/"
PHOTOSHOP = "http://ns.adobe.com/photoshop/1.0/"
UNKNOWN_NS = "http://example.com/qa/1.0/"

_HEAD = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    f'<rdf:RDF xmlns:rdf="{RDF}">'
)
_TAIL = "</rdf:RDF></x:xmpmeta><?xpacket end=\"w\"?>"


def packet(description: str) -> str:
    """A complete XMP packet around one ``rdf:Description`` body."""
    return f"{_HEAD}{description}{_TAIL}"


def description(body: str = "", *, attributes: str = "", extra_ns: str = "") -> str:
    """``<rdf:Description>`` with the usual namespaces declared on it."""
    return (
        f'<rdf:Description rdf:about="" xmlns:dc="{DC}" xmlns:xmp="{XMP_NS}"'
        f' xmlns:photoshop="{PHOTOSHOP}"{extra_ns} {attributes}>{body}</rdf:Description>'
    )


def itxt_payload(keyword: str, text: str) -> bytes:
    """One iTXt chunk payload: keyword, no compression, no language, the text."""
    return (
        keyword.encode("latin-1")
        + b"\x00\x00\x00"  # keyword, compression flag, compression method
        + b"\x00"  # language tag
        + b"\x00"  # translated keyword
        + text.encode("utf-8")
    )


def xmp_png(path: Path, body: str = "", **kwargs: str) -> Path:
    """A PNG whose only metadata is one XMP packet built from ``body``."""
    return samples.png_file(path, text={"XML:com.adobe.xmp": packet(description(body, **kwargs))})


def two_packet_png(path: Path, first: str, second: str) -> Path:
    """A PNG with two XMP packets: PIL writes the first, the second is appended."""
    return samples.png_file(
        path,
        text={"XML:com.adobe.xmp": first},
        extra=((b"iTXt", itxt_payload("XML:com.adobe.xmp", second)),),
    )


# ---------------------------------------------------------------------------
# the reported scenario, exactly
# ---------------------------------------------------------------------------
#: The packet of the reported case: the copyright line is an *attribute* and a
#: vendor property lives in a namespace PHANTOM knows nothing about.
CASE_ATTRIBUTE_RIGHTS = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
    f'<rdf:RDF xmlns:rdf="{RDF}">'
    '<rdf:Description rdf:about=""'
    f' xmlns:dc="{DC}"'
    f' xmlns:photoshop="{PHOTOSHOP}"'
    ' xmlns:qa="http://example.com/qa/1.0/"'
    ' dc:rights="(c) 2024 Ada Lovelace"'
    ' qa:TestProperty="alpha">'
    "<photoshop:City>Lubny</photoshop:City>"
    "</rdf:Description>"
    "</rdf:RDF></x:xmpmeta>"
    '<?xpacket end="w"?>'
)


def test_case_attribute_rights_is_never_deleted(tmp_path: Path) -> None:
    """The reported file, built the way the report described it, line by line."""
    path = samples.png_file(
        tmp_path / "case.png", text={"XML:com.adobe.xmp": CASE_ATTRIBUTE_RIGHTS}
    )
    before = path.read_bytes()

    found = inspector.inspect_path(str(path))
    entry = found.find("png.xmp")

    assert entry is not None
    assert entry.removable is False, "an attribute-borne copyright line blocks the packet"
    assert "qa:TestProperty" in entry.reason or "dc:rights" in entry.reason
    assert cleaner.default_selection(found) == frozenset()

    decision = cleaner.plan(found, frozenset({"png.xmp"}))
    assert decision.remove == ()
    assert [item.key for item in decision.blocked] == ["png.xmp"]

    with pytest.raises(MetadataError):
        cleaner.clean(str(path), str(tmp_path / "clean.png"), frozenset({"png.xmp"}))
    assert not (tmp_path / "clean.png").exists(), "a refused clean publishes nothing"
    assert path.read_bytes() == before, "the source is only ever read"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == hashlib.sha256(before).hexdigest()

    # Refusing to remove is not refusing to look: every property is readable.
    assert found.find("png.xmp.dc:rights") is not None
    assert found.find("png.xmp.qa:TestProperty") is not None
    assert found.find("png.xmp.photoshop:City") is not None
    assert found.counts.total == 1, "the properties are views, not extra entries"


def test_the_attribute_copy_of_the_copyright_line_is_shown_verbatim(tmp_path: Path) -> None:
    found = inspector.inspect_path(
        str(
            samples.png_file(
                tmp_path / "case.png", text={"XML:com.adobe.xmp": CASE_ATTRIBUTE_RIGHTS}
            )
        )
    )
    rights = found.find("png.xmp.dc:rights")

    assert rights is not None
    assert rights.value == "(c) 2024 Ada Lovelace"
    assert rights.derived is True and rights.removable is False
    assert rights.category is MetadataCategory.RIGHTS


# ---------------------------------------------------------------------------
# the shapes the text search could not see
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("name", "body", "attributes", "extra_ns"),
    [
        ("element", "<dc:rights>(c) 2024</dc:rights>", "", ""),
        ("attribute", "", 'dc:rights="(c) 2024"', ""),
        (
            "alternate prefix",
            "",
            "",
            f' xmlns:d="{DC}" d:rights="(c) 2024"',
        ),
        (
            "alt container",
            '<dc:rights><rdf:Alt><rdf:li xml:lang="x-default">(c) 2024</rdf:li>'
            "</rdf:Alt></dc:rights>",
            "",
            "",
        ),
        (
            "seq container",
            "<dc:creator><rdf:Seq><rdf:li>Ada</rdf:li></rdf:Seq></dc:creator>",
            "",
            "",
        ),
        (
            "bag container",
            "<dc:subject><rdf:Bag><rdf:li>holiday</rdf:li></rdf:Bag></dc:subject>",
            "",
            "",
        ),
        (
            "nested node",
            '<photoshop:City rdf:parseType="Resource">'
            "<photoshop:Credit>Ada</photoshop:Credit></photoshop:City>",
            "",
            "",
        ),
        (
            "unknown namespace",
            "",
            "",
            f' xmlns:qa="{UNKNOWN_NS}" qa:TestProperty="alpha"',
        ),
        ("unknown local name", "<dc:format>image/png</dc:format>", "", ""),
        ("quiet rights", "<xmpRights:Marked>True</xmpRights:Marked>", "", ""),
    ],
)
def test_every_shape_of_property_blocks_the_packet(
    name: str, body: str, attributes: str, extra_ns: str, tmp_path: Path
) -> None:
    """Each spelling of a protected property is enough to keep the whole packet."""
    ns = extra_ns
    if name == "quiet rights":
        ns = ' xmlns:xmpRights="http://ns.adobe.com/xap/1.0/rights/"'
    path = xmp_png(
        tmp_path / f"{name}.png",
        body + "<photoshop:City>Lubny</photoshop:City>",
        attributes=attributes,
        extra_ns=ns,
    )
    found = inspector.inspect_path(str(path))
    entry = found.find("png.xmp")

    assert entry is not None
    assert entry.removable is False, f"{name}: the packet holds more than personal data"
    assert entry.reason, f"{name}: a protected packet has to say why it is kept"
    assert cleaner.default_selection(found) == frozenset()


def test_a_personal_property_written_as_an_attribute_is_still_personal(tmp_path: Path) -> None:
    """The reader must not trade one blind spot for another: attributes count."""
    path = samples.png_file(
        tmp_path / "attr.png",
        text={
            "XML:com.adobe.xmp": packet(
                description(attributes='photoshop:City="Lubny" photoshop:Country="UA"')
            )
        },
    )
    found = inspector.inspect_path(str(path))

    assert found.find("png.xmp.photoshop:City") is not None
    assert found.find("png.xmp").removable is True
    outcome = cleaner.clean(
        str(path), str(tmp_path / "attr-clean.png"), frozenset({"png.xmp"})
    )
    assert outcome.verified is True
    assert inspector.inspect_path(str(outcome.output)).fields == ()


def test_a_container_of_personal_values_does_not_hide_them(tmp_path: Path) -> None:
    """A Bag is a value, not a shield: personal data inside one is still personal."""
    path = xmp_png(
        tmp_path / "bag.png",
        '<photoshop:City><rdf:Bag><rdf:li>Lubny</rdf:li></rdf:Bag></photoshop:City>',
    )
    found = inspector.inspect_path(str(path))

    assert found.find("png.xmp.photoshop:City") is not None
    assert found.find("png.xmp").removable is True


# ---------------------------------------------------------------------------
# packets the reader refuses: fail closed, never fail open
# ---------------------------------------------------------------------------
def _truncated() -> str:
    return packet(description("<photoshop:City"))[: -len(_TAIL)]


def _with_doctype() -> str:
    return (
        '<?xml version="1.0"?>'
        '<!DOCTYPE x [<!ENTITY boom "aaaaaaaaaa">]>'
        + packet(description("<photoshop:City>Lubny</photoshop:City>"))
    )


def _entity_expansion() -> str:
    """The classic billion-laughs shape, small enough to run in a test."""
    return (
        '<?xml version="1.0"?>'
        '<!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
        + packet(description("<photoshop:City>&b;</photoshop:City>"))
    )


def _deeply_nested() -> str:
    depth = xmp.MAX_DEPTH + 8
    opening = '<photoshop:City rdf:parseType="Resource">' * depth
    return packet(description(opening + "</photoshop:City>" * depth))


def _many_elements() -> str:
    items = "".join(f"<photoshop:City>c{index}</photoshop:City>" for index in range(6_000))
    return packet(description(items))


def _oversized() -> str:
    return packet(
        description("<photoshop:City>" + "x" * (xmp.MAX_PACKET_BYTES + 16) + "</photoshop:City>")
    )


@pytest.mark.parametrize(
    ("name", "builder", "key"),
    [
        ("truncated", _truncated, "meta.xmp.unreadable.malformed"),
        ("doctype", _with_doctype, "meta.xmp.unreadable.doctype"),
        ("entities", _entity_expansion, "meta.xmp.unreadable.doctype"),
        ("deep", _deeply_nested, "meta.xmp.unreadable.deep"),
        ("many", _many_elements, "meta.xmp.unreadable.many"),
        ("oversized", _oversized, "meta.xmp.unreadable.large"),
    ],
)
def test_a_packet_that_cannot_be_read_is_protected(
    name: str, builder: Callable[[], str], key: str, tmp_path: Path
) -> None:
    """Not understood is not the same as safe: the packet stays and says why."""
    text = builder()
    path = samples.png_file(tmp_path / f"{name}.png", text={"XML:com.adobe.xmp": text})
    before = path.read_bytes()

    found = inspector.inspect_path(str(path))
    entry = found.find("png.xmp")

    assert entry is not None
    assert entry.removable is False
    assert entry.reason_key == key, f"{name}: the reason has to name the real problem"
    assert entry.reason, "the English sentence stays for the JSON report"
    assert cleaner.default_selection(found) == frozenset()

    with pytest.raises(MetadataError):
        cleaner.clean(str(path), str(tmp_path / f"{name}-clean.png"), frozenset({"png.xmp"}))
    assert not (tmp_path / f"{name}-clean.png").exists()
    assert path.read_bytes() == before


def test_a_doctype_is_refused_before_it_reaches_the_parser() -> None:
    """The refusal is textual, so no entity is ever expanded and nothing is fetched."""
    analysis = xmp.analyse(_with_doctype())

    assert analysis.ok is False
    assert analysis.problem_key == "meta.xmp.unreadable.doctype"
    assert analysis.removable is False


def test_a_doctype_in_one_packet_protects_the_others() -> None:
    """One unreadable packet is enough: the file's XMP is not partially trusted."""
    good = packet(description("<photoshop:City>Lubny</photoshop:City>"))
    analysis = xmp.analyse(good, _with_doctype())

    assert analysis.ok is False
    assert analysis.removable is False


# ---------------------------------------------------------------------------
# more than one packet in one file
# ---------------------------------------------------------------------------
def test_two_packets_are_read_together(tmp_path: Path) -> None:
    personal_only = packet(description("<photoshop:City>Lubny</photoshop:City>"))
    with_rights = packet(description(attributes='dc:rights="(c) 2024"'))
    path = two_packet_png(tmp_path / "two.png", personal_only, with_rights)

    found = inspector.inspect_path(str(path))
    entry = found.find("png.xmp")

    assert entry is not None
    assert entry.removable is False, "the second packet carries a copyright line"
    assert "2 packets" in entry.value
    assert found.find("png.xmp.dc:rights") is not None
    assert found.find("png.xmp.photoshop:City") is not None
    with pytest.raises(MetadataError):
        cleaner.clean(str(path), str(tmp_path / "two-clean.png"), frozenset({"png.xmp"}))
    assert not (tmp_path / "two-clean.png").exists()


def test_two_personal_packets_are_still_removable(tmp_path: Path) -> None:
    first = packet(description("<photoshop:City>Lubny</photoshop:City>"))
    second = packet(description("<photoshop:Country>UA</photoshop:Country>"))
    path = two_packet_png(tmp_path / "two.png", first, second)

    found = inspector.inspect_path(str(path))
    assert found.find("png.xmp").removable is True

    outcome = cleaner.clean(str(path), str(tmp_path / "two-clean.png"), frozenset({"png.xmp"}))
    assert outcome.verified is True
    after = inspector.inspect_path(str(outcome.output))
    assert after.fields == (), "both packets left as blocks"
    assert after.counts.total == 0


# ---------------------------------------------------------------------------
# the pixels are never part of the bargain
# ---------------------------------------------------------------------------
def test_removing_a_personal_packet_leaves_the_pixels_identical(tmp_path: Path) -> None:
    path = xmp_png(
        tmp_path / "pixels.png",
        "<photoshop:City>Lubny</photoshop:City><xmp:CreateDate>2024-05-01</xmp:CreateDate>",
    )
    outcome = cleaner.clean(str(path), str(tmp_path / "pixels-clean.png"), frozenset({"png.xmp"}))

    assert outcome.verified is True
    assert outcome.comparison.identical is True
    assert outcome.comparison.left == outcome.comparison.right


# ---------------------------------------------------------------------------
# the request itself cannot widen into "delete the packet"
# ---------------------------------------------------------------------------
def test_only_the_packet_key_asks_for_the_packet(tmp_path: Path) -> None:
    """A property row is a view; naming it must never delete the packet behind it."""
    from phantom.metadata import png as png_mod

    assert png_mod.removal_from_keys(frozenset({"png.xmp"})).drop_xmp is True
    for view in ("png.xmp.dc:rights", "png.xmp.qa:TestProperty", "png.xmp.photoshop:City"):
        assert png_mod.removal_from_keys(frozenset({view})).drop_xmp is False, view

    # End to end: a selection that only names views removes nothing and writes nothing.
    path = xmp_png(tmp_path / "views.png", "<photoshop:City>Lubny</photoshop:City>")
    before = path.read_bytes()
    with pytest.raises(MetadataError):
        cleaner.clean(
            str(path),
            str(tmp_path / "views-clean.png"),
            frozenset({"png.xmp.photoshop:City"}),
        )
    assert not (tmp_path / "views-clean.png").exists()
    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# what the user is told
# ---------------------------------------------------------------------------
def test_the_reason_is_localised_but_the_packet_text_is_not(tmp_path: Path) -> None:
    """A Ukrainian card gets a Ukrainian reason; labels and values stay data."""
    path = samples.png_file(
        tmp_path / "case.png", text={"XML:com.adobe.xmp": CASE_ATTRIBUTE_RIGHTS}
    )
    found = inspector.inspect_path(str(path))
    entry = found.find("png.xmp")
    assert entry is not None

    ukrainian = i18n_mod.I18n("uk")
    english = i18n_mod.I18n("en")
    params = dict(entry.reason_params)

    assert entry.reason_key == "meta.reason.xmp_unknown"
    assert "qa:TestProperty" in meta_report.field_reason(entry, ukrainian)
    assert "qa:TestProperty" in meta_report.field_reason(entry, english)
    assert meta_report.field_reason(entry, ukrainian) == ukrainian.t(entry.reason_key, **params)
    assert meta_report.field_reason(entry, ukrainian) != meta_report.field_reason(entry, english)
    # Without a translator the English sentence is the answer (JSON, logs).
    assert meta_report.field_reason(entry, None) == entry.reason
    # The property names inside the reason are data and are never translated.
    assert "qa:TestProperty" in entry.value or "qa:TestProperty" in entry.reason


def test_the_report_prints_the_localised_reason(tmp_path: Path) -> None:
    path = samples.png_file(
        tmp_path / "case.png", text={"XML:com.adobe.xmp": CASE_ATTRIBUTE_RIGHTS}
    )
    found = inspector.inspect_path(str(path))
    ru = i18n_mod.I18n("ru")

    text = meta_report.inspection_text(found, ru)

    assert "qa:TestProperty" in text
    assert "XMP" in text
    assert "не знает" in text or "неизвестные" in text, text
