"""Structural reading of an XMP packet, and the policy that decides if it may go.

XMP is RDF/XML.  The reader used to search the packet text for ``<dc:rights>`` and
friends, which is not the same thing as reading it: RDF/XML can write a property
as an element *or* as an attribute of ``rdf:Description``, a property can be a
``rdf:Bag``/``Seq``/``Alt`` of items, a value can be a nested node, and a property
in a namespace nobody has heard of is still a property.  A packet whose copyright
line was written as an attribute therefore looked like "personal properties only",
the cleaner offered to delete it, and the whole packet — rights line, unknown
vendor properties and all — was deleted and reported as verified.

This module replaces the text search with a real, deliberately small RDF/XML
reader, and a fail-closed policy on top of it:

* only the properties listed in :data:`PERSONAL` are personal, and therefore
  removable;
* everything else — descriptive, rights, technical, provenance, and anything in a
  namespace this build does not know — is protected, so a packet that carries one
  of them is never removed as a whole;
* a packet that cannot be read *completely* (truncated XML, a DOCTYPE, a shape
  this reader does not model, too many elements) is protected as well: "I could
  not analyse it" is never "it is safe to delete".

The parser is the standard library's :mod:`xml.etree.ElementTree` driven with
limits that are checked while walking, not after: a packet is at most
:data:`MAX_PACKET_BYTES`, has at most :data:`MAX_ELEMENTS` elements and
:data:`MAX_DEPTH` levels, and a document that declares a DTD or an entity is
refused before it reaches the parser at all — that is the whole of XXE and entity
expansion, and ElementTree is never asked to resolve an external reference.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import StringIO
from typing import Final
from xml.etree import ElementTree as ET

from phantom.metadata.models import MetadataCategory

LOGGER: Final = logging.getLogger("phantom.metadata.xmp")

# ---------------------------------------------------------------------------
# limits: checked before and during the walk, never after it
# ---------------------------------------------------------------------------
#: One packet.  Real XMP is a few kilobytes; a megabyte is already absurd, and the
#: limit exists so a hostile file cannot make the inspector walk for minutes.
MAX_PACKET_BYTES: Final = 1 << 20
#: Every packet of one file together (a PNG may carry several).
MAX_TOTAL_BYTES: Final = 4 << 20
MAX_ELEMENTS: Final = 4096
MAX_ATTRIBUTES: Final = 1024
MAX_DEPTH: Final = 32
#: A property without children is shown as this many characters of its value.
MAX_VALUE: Final = 400

_RDF_NS: Final = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_XML_NS: Final = "http://www.w3.org/XML/1998/namespace"
_XMPMETA_NS: Final = "adobe:ns:meta/"
_XAPMETA_NS: Final = "http://ns.adobe.com/xap/1.0/meta/"

#: Wrappers around the RDF graph, in the two spellings Adobe ships.
_WRAPPERS: Final = frozenset({f"{{{_XMPMETA_NS}}}xmpmeta", f"{{{_XAPMETA_NS}}}xapmeta"})
_RDF_RDF: Final = f"{{{_RDF_NS}}}RDF"
_RDF_DESCRIPTION: Final = f"{{{_RDF_NS}}}Description"
_RDF_LI: Final = f"{{{_RDF_NS}}}li"
_CONTAINERS: Final = frozenset(
    {f"{{{_RDF_NS}}}{kind}" for kind in ("Bag", "Seq", "Alt")}
)
#: ``rdf:`` attributes that describe the *node*, not a property of it.
_NODE_ATTRIBUTES: Final = frozenset(
    {
        f"{{{_RDF_NS}}}about",
        f"{{{_RDF_NS}}}ID",
        f"{{{_RDF_NS}}}nodeID",
        f"{{{_RDF_NS}}}parseType",
        f"{{{_RDF_NS}}}resource",
        f"{{{_RDF_NS}}}datatype",
    }
)

#: A DTD is refused before parsing: it is the door to external entities and to
#: entity expansion, and no legitimate XMP packet has one.
_FORBIDDEN: Final = ("<!DOCTYPE", "<!ENTITY")

# ---------------------------------------------------------------------------
# the policy: which property may go, which one must stay
# ---------------------------------------------------------------------------
#: The only properties this build is willing to describe as personal.  Nothing
#: here was added for a new reason: the list is the one the inspector already
#: used, so the rewrite cannot start removing more than it did before.
PERSONAL: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("http://ns.adobe.com/xap/1.0/", "CreateDate"),
        ("http://ns.adobe.com/xap/1.0/", "ModifyDate"),
        ("http://ns.adobe.com/photoshop/1.0/", "DateCreated"),
        ("http://ns.adobe.com/photoshop/1.0/", "City"),
        ("http://ns.adobe.com/photoshop/1.0/", "Country"),
        ("http://ns.adobe.com/photoshop/1.0/", "State"),
        ("http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/", "Location"),
        ("http://ns.adobe.com/exif/1.0/", "GPSLatitude"),
        ("http://ns.adobe.com/exif/1.0/", "GPSLongitude"),
        ("http://ns.adobe.com/exif/1.0/", "GPSAltitude"),
        ("http://ns.adobe.com/exif/1.0/", "DateTimeOriginal"),
    }
)

#: Properties this build knows by name, with the category it reports for them.
#: Being in this table does not make a property removable: everything except
#: :data:`PERSONAL` is protected, it is only *named* instead of "unknown".
KNOWN: Final[dict[tuple[str, str], MetadataCategory]] = {
    ("http://purl.org/dc/elements/1.1/", "title"): MetadataCategory.DESCRIPTIVE,
    ("http://purl.org/dc/elements/1.1/", "description"): MetadataCategory.DESCRIPTIVE,
    ("http://purl.org/dc/elements/1.1/", "subject"): MetadataCategory.DESCRIPTIVE,
    ("http://purl.org/dc/elements/1.1/", "creator"): MetadataCategory.RIGHTS,
    ("http://purl.org/dc/elements/1.1/", "rights"): MetadataCategory.RIGHTS,
    ("http://purl.org/dc/elements/1.1/", "publisher"): MetadataCategory.RIGHTS,
    ("http://purl.org/dc/elements/1.1/", "contributor"): MetadataCategory.RIGHTS,
    ("http://purl.org/dc/elements/1.1/", "date"): MetadataCategory.DESCRIPTIVE,
    ("http://purl.org/dc/elements/1.1/", "identifier"): MetadataCategory.PROVENANCE,
    ("http://ns.adobe.com/xap/1.0/", "CreatorTool"): MetadataCategory.TECHNICAL,
    ("http://ns.adobe.com/xap/1.0/", "MetadataDate"): MetadataCategory.TECHNICAL,
    ("http://ns.adobe.com/xap/1.0/", "Rating"): MetadataCategory.DESCRIPTIVE,
    ("http://ns.adobe.com/xap/1.0/rights/", "Marked"): MetadataCategory.RIGHTS,
    ("http://ns.adobe.com/xap/1.0/rights/", "WebStatement"): MetadataCategory.RIGHTS,
    ("http://ns.adobe.com/xap/1.0/rights/", "UsageTerms"): MetadataCategory.RIGHTS,
    ("http://ns.adobe.com/xap/1.0/mm/", "DocumentID"): MetadataCategory.PROVENANCE,
    ("http://ns.adobe.com/xap/1.0/mm/", "InstanceID"): MetadataCategory.PROVENANCE,
    ("http://ns.adobe.com/xap/1.0/mm/", "OriginalDocumentID"): MetadataCategory.PROVENANCE,
    ("http://ns.adobe.com/xap/1.0/mm/", "History"): MetadataCategory.PROVENANCE,
    ("http://ns.adobe.com/photoshop/1.0/", "Credit"): MetadataCategory.RIGHTS,
    ("http://ns.adobe.com/photoshop/1.0/", "Source"): MetadataCategory.PROVENANCE,
    ("http://ns.adobe.com/photoshop/1.0/", "Headline"): MetadataCategory.DESCRIPTIVE,
    ("http://ns.adobe.com/photoshop/1.0/", "ColorMode"): MetadataCategory.TECHNICAL,
    ("http://ns.adobe.com/tiff/1.0/", "Make"): MetadataCategory.TECHNICAL,
    ("http://ns.adobe.com/tiff/1.0/", "Model"): MetadataCategory.TECHNICAL,
    ("http://ns.adobe.com/exif/1.0/", "DateTimeOriginal"): MetadataCategory.PERSONAL,
    ("http://ns.adobe.com/camera-raw-settings/1.0/", "Version"): MetadataCategory.TECHNICAL,
}
for _ns_uri, _ns_name in sorted(PERSONAL):
    KNOWN.setdefault((_ns_uri, _ns_name), MetadataCategory.PERSONAL)
# A property is personal only if it is in :data:`PERSONAL`.  The table above names
# properties; it must never make one removable by accident, so anything it marks
# personal without being in the list is reported as technical (i.e. protected).
for _prop_key, _prop_category in list(KNOWN.items()):
    if _prop_category is MetadataCategory.PERSONAL and _prop_key not in PERSONAL:
        KNOWN[_prop_key] = MetadataCategory.TECHNICAL

#: A prefix per namespace, so a property is named the way a person knows it.  The
#: packet's own prefixes win when they exist; this table is the fallback for a
#: packet that declares a namespace in a parent element or not at all.
CANONICAL_PREFIX: Final[dict[str, str]] = {
    "http://purl.org/dc/elements/1.1/": "dc",
    "http://ns.adobe.com/xap/1.0/": "xmp",
    "http://ns.adobe.com/xap/1.0/rights/": "xmpRights",
    "http://ns.adobe.com/xap/1.0/mm/": "xmpMM",
    "http://ns.adobe.com/photoshop/1.0/": "photoshop",
    "http://ns.adobe.com/tiff/1.0/": "tiff",
    "http://ns.adobe.com/exif/1.0/": "exif",
    "http://ns.adobe.com/exif/1.0/aux/": "aux",
    "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/": "Iptc4xmpCore",
    "http://ns.adobe.com/camera-raw-settings/1.0/": "crs",
    "http://ns.adobe.com/pdf/1.3/": "pdf",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#": "rdf",
}


# ---------------------------------------------------------------------------
# the result
# ---------------------------------------------------------------------------
class _PacketError(Exception):
    """A packet this reader will not vouch for; the key is a locale key."""

    def __init__(self, key: str, detail: str = "") -> None:
        super().__init__(f"{key}: {detail}" if detail else key)
        self.key = key
        self.detail = detail


@dataclass(frozen=True, slots=True)
class XmpProperty:
    """One property of the packet, wherever and however it was written."""

    uri: str
    local: str
    prefix: str
    value: str
    #: ``element``, ``attribute``, ``item`` or ``structure``: how the property was
    #: written.  A reader that only looked for elements missed three of the four.
    written_as: str = "element"
    #: The RDF container the value came from, when it came from one.
    container: str = ""
    #: Depth of the node the property belongs to (0 = the description itself).
    depth: int = 0

    @property
    def qualified(self) -> str:
        if self.prefix:
            return f"{self.prefix}:{self.local}"
        return self.local

    @property
    def category(self) -> MetadataCategory:
        if (self.uri, self.local) in PERSONAL:
            return MetadataCategory.PERSONAL
        return KNOWN.get((self.uri, self.local), MetadataCategory.UNKNOWN)

    @property
    def personal(self) -> bool:
        return self.category is MetadataCategory.PERSONAL

    @property
    def known(self) -> bool:
        return (self.uri, self.local) in PERSONAL or (self.uri, self.local) in KNOWN

    def label(self) -> str:
        return f"XMP {self.qualified}"


@dataclass(frozen=True, slots=True)
class XmpAnalysis:
    """What one file's XMP packets turned out to contain, and whether they may go."""

    ok: bool = True
    #: Locale key of the reason the packet cannot be removed (never for ``ok``).
    problem_key: str = ""
    problem: str = ""
    packet_count: int = 0
    properties: tuple[XmpProperty, ...] = ()

    @property
    def personal(self) -> tuple[XmpProperty, ...]:
        return tuple(item for item in self.properties if item.personal)

    @property
    def protected(self) -> tuple[XmpProperty, ...]:
        return tuple(item for item in self.properties if not item.personal)

    @property
    def unknown(self) -> tuple[XmpProperty, ...]:
        return tuple(
            item for item in self.properties if item.category is MetadataCategory.UNKNOWN
        )

    @property
    def reason_key(self) -> str:
        """The locale key the interface and the localised report show."""
        if not self.ok:
            return self.problem_key
        if self.unknown:
            return "meta.reason.xmp_unknown"
        if self.protected:
            return "meta.reason.xmp_protected"
        if not self.personal:
            return "meta.reason.xmp_no_personal"
        return ""

    @property
    def reason(self) -> str:
        """The English sentence; the JSON report and the log use it."""
        if not self.ok:
            return f"XMP cannot be read safely ({self.problem}); it is never removed"
        if self.unknown:
            names = ", ".join(item.qualified for item in self.unknown[:4])
            return (
                "the packet carries properties PHANTOM does not know "
                f"({names}); XMP is removed as a whole packet"
            )
        if self.protected:
            names = ", ".join(item.qualified for item in self.protected[:4])
            return (
                f"the packet also carries protected properties ({names}) "
                "and XMP is removed as a whole packet"
            )
        if not self.personal:
            return "no personal property recognised"
        return ""

    @property
    def reason_params(self) -> tuple[tuple[str, str], ...]:
        if not self.ok:
            return (("detail", self.problem),)
        named = self.unknown or self.protected
        if not named:
            return ()
        return (("properties", ", ".join(item.qualified for item in named[:4])),)

    @property
    def removable(self) -> bool:
        """The whole packet may go: it is understood, personal and nothing else."""
        return self.ok and bool(self.personal) and not self.protected


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def analyse(*packets: str) -> XmpAnalysis:
    """Read every XMP packet of one file; one bad packet protects all of them.

    A file may carry several packets (more than one ``iTXt`` chunk with the same
    keyword).  They are removed together, so they are judged together: if any one
    of them cannot be read safely, none of them may go.
    """
    texts = [text for text in packets if text and text.strip()]
    if not texts:
        return XmpAnalysis(packet_count=0)
    if any(len(text) > MAX_PACKET_BYTES for text in texts) or (
        sum(len(text) for text in texts) > MAX_TOTAL_BYTES
    ):
        LOGGER.debug("XMP packet refused: too large")
        return _failed("meta.xmp.unreadable.large", f"{sum(map(len, texts))} bytes")
    properties: list[XmpProperty] = []
    for text in texts:
        try:
            properties.extend(_read_packet(text))
        except _PacketError as problem:
            LOGGER.debug("XMP packet refused: %s", problem)
            return _failed(problem.key, problem.detail)
    return XmpAnalysis(packet_count=len(texts), properties=tuple(properties))


def _failed(key: str, detail: str) -> XmpAnalysis:
    return XmpAnalysis(ok=False, problem_key=key, problem=detail)


def _read_packet(text: str) -> list[XmpProperty]:
    root, prefixes = _parse(text)
    reader = _Reader(prefixes)
    return reader.read(root)


def _parse(text: str) -> tuple[ET.Element, dict[str, str]]:
    """Parse one packet, and refuse anything that could fetch or expand."""
    head = text[:MAX_PACKET_BYTES]
    for token in _FORBIDDEN:
        if token in head:
            # Refused before the parser sees it: ElementTree expands *internal*
            # entities and refuses external ones, and the simplest way to make
            # both impossible is to have no DTD at all.
            raise _PacketError("meta.xmp.unreadable.doctype", token)
    body = _strip_wrapper(head)
    prefixes: dict[str, str] = {}
    try:
        # The parser is fed a document that has already been refused a DTD, an
        # entity declaration and a size over the limit, and ElementTree never
        # resolves an external reference; that is the whole of S314's concern.
        parser = ET.iterparse(  # noqa: S314
            StringIO(body), events=("start-ns", "start")
        )
        root: ET.Element | None = None
        for event, payload in parser:
            if event == "start-ns":
                prefix, uri = payload
                prefixes.setdefault(uri, prefix)
            elif root is None:
                root = payload
    except ET.ParseError as exc:
        raise _PacketError("meta.xmp.unreadable.malformed", str(exc)[:120]) from exc
    except (ValueError, TypeError) as exc:  # pragma: no cover - expat corner cases
        raise _PacketError("meta.xmp.unreadable.malformed", str(exc)[:120]) from exc
    if root is None:
        raise _PacketError("meta.xmp.unreadable.malformed", "no element")
    return root, prefixes


def _strip_wrapper(text: str) -> str:
    """Drop the ``<?xpacket?>`` wrapper, a BOM and the padding around it.

    ElementTree would accept processing instructions, but the padding after
    ``<?xpacket end="w"?>`` is whitespace by contract and some writers add bytes
    that are not XML at all.
    """
    start = text.find("<")
    if start < 0:
        raise _PacketError("meta.xmp.unreadable.malformed", "no markup")
    return text[start:].lstrip("\ufeff \t\r\n")


class _Reader:
    """Walks one RDF graph, counting what it sees so the limits mean something."""

    def __init__(self, prefixes: dict[str, str]) -> None:
        self._prefixes = prefixes
        self._elements = 0
        self._attributes = 0

    # -- entry ---------------------------------------------------------
    def read(self, root: ET.Element) -> list[XmpProperty]:
        tag = _local(root.tag)
        if root.tag in _WRAPPERS:
            children = list(root)
            if len(children) != 1 or children[0].tag != _RDF_RDF:
                raise _PacketError("meta.xmp.unreadable.structure", f"{tag} without rdf:RDF")
            return self._read_rdf(children[0])
        if root.tag == _RDF_RDF:
            return self._read_rdf(root)
        if root.tag == _RDF_DESCRIPTION:
            # A packet may be a bare description; it is the same graph.
            return self._read_description(root, depth=0)
        raise _PacketError("meta.xmp.unreadable.structure", f"<{tag}>")

    def _count(self, node: ET.Element) -> None:
        self._elements += 1
        self._attributes += len(node.attrib)
        if self._elements > MAX_ELEMENTS:
            raise _PacketError("meta.xmp.unreadable.many", f">{MAX_ELEMENTS} elements")
        if self._attributes > MAX_ATTRIBUTES:
            raise _PacketError("meta.xmp.unreadable.many", f">{MAX_ATTRIBUTES} attributes")

    # -- the graph -----------------------------------------------------
    def _read_rdf(self, rdf: ET.Element) -> list[XmpProperty]:
        self._count(rdf)
        properties: list[XmpProperty] = []
        for child in rdf:
            if child.tag != _RDF_DESCRIPTION:
                # A typed node (``<photoshop:...>`` as a node) is legal RDF/XML and
                # this reader does not model it: fail closed instead of guessing.
                raise _PacketError("meta.xmp.unreadable.structure", f"<{_local(child.tag)}>")
            properties.extend(self._read_description(child, depth=0))
        return properties

    def _read_description(self, node: ET.Element, *, depth: int) -> list[XmpProperty]:
        if depth > MAX_DEPTH:
            raise _PacketError("meta.xmp.unreadable.deep", f">{MAX_DEPTH}")
        self._count(node)
        properties: list[XmpProperty] = []
        for name, value in node.attrib.items():
            if name in _NODE_ATTRIBUTES or name.startswith(f"{{{_XML_NS}}}"):
                continue
            properties.append(self._property(name, value, written_as="attribute", depth=depth))
        for child in node:
            properties.extend(self._read_property(child, depth=depth + 1))
        return properties

    def _read_property(self, node: ET.Element, *, depth: int) -> list[XmpProperty]:
        if depth > MAX_DEPTH:
            raise _PacketError("meta.xmp.unreadable.deep", f">{MAX_DEPTH}")
        self._count(node)
        parse_type = node.get(f"{{{_RDF_NS}}}parseType", "")
        properties: list[XmpProperty] = []
        # Attributes of a property element are properties of the *node* it
        # describes, and they are the shape a text search never sees.
        for name, value in node.attrib.items():
            if name in _NODE_ATTRIBUTES or name.startswith(f"{{{_XML_NS}}}"):
                continue
            properties.append(self._property(name, value, written_as="attribute", depth=depth))
        descriptions = [child for child in node if child.tag == _RDF_DESCRIPTION]
        if parse_type == "Resource" or descriptions:
            # An inline node.  ``rdf:parseType="Resource"`` makes the property
            # element itself the node, so its children are properties; a nested
            # ``rdf:Description`` is a node in its own right.  Both are walked, so
            # nothing inside them can disappear with the packet unnoticed.
            for child in node:
                if child.tag == _RDF_DESCRIPTION:
                    properties.extend(self._read_description(child, depth=depth + 1))
                elif parse_type == "Resource":
                    properties.extend(self._read_property(child, depth=depth + 1))
                else:
                    raise _PacketError(
                        "meta.xmp.unreadable.structure",
                        f"<{_local(child.tag)}> beside a nested rdf:Description",
                    )
            return properties
        if parse_type and parse_type != "Literal":
            # ``Collection`` and anything else this reader does not model.
            raise _PacketError("meta.xmp.unreadable.structure", f"rdf:parseType={parse_type}")
        containers = [child for child in node if child.tag in _CONTAINERS]
        if containers:
            for container in containers:
                properties.extend(self._read_container(container, node, depth=depth))
            return properties
        unknown_children = [child for child in node if child.tag not in _CONTAINERS]
        if unknown_children:
            raise _PacketError(
                "meta.xmp.unreadable.structure",
                f"<{_local(unknown_children[0].tag)}> inside <{_local(node.tag)}>",
            )
        properties.append(
            self._property(node.tag, _text(node), written_as="element", depth=depth)
        )
        return properties

    def _read_container(
        self, container: ET.Element, owner: ET.Element, *, depth: int
    ) -> list[XmpProperty]:
        self._count(container)
        kind = _local(container.tag)
        items: list[str] = []
        properties: list[XmpProperty] = []
        for item in container:
            if item.tag != _RDF_LI:
                raise _PacketError(
                    "meta.xmp.unreadable.structure", f"<{_local(item.tag)}> in {kind}"
                )
            self._count(item)
            if _is_node(item):
                # An item that is itself a node: walk it like one, so its fields
                # are counted and classified instead of vanishing inside a value.
                properties.extend(self._read_description(item, depth=depth))
                continue
            items.append(_text(item))
        properties.append(
            self._property(
                owner.tag,
                " \u00b7 ".join(part for part in items if part),
                written_as="item",
                depth=depth,
                container=kind,
            )
        )
        return properties

    # -- one property --------------------------------------------------
    def _property(
        self,
        tag: str,
        value: str,
        *,
        written_as: str,
        depth: int,
        container: str = "",
    ) -> XmpProperty:
        uri, local = _split(tag)
        prefix = self._prefixes.get(uri) or CANONICAL_PREFIX.get(uri, "")
        return XmpProperty(
            uri=uri,
            local=local,
            prefix=prefix,
            value=(value or "").strip()[:MAX_VALUE],
            written_as=written_as,
            container=container,
            depth=depth,
        )


def _is_node(element: ET.Element) -> bool:
    """Whether an ``rdf:li`` (or a property) carries a node rather than text.

    ``rdf:parseType="Resource"`` and a nested ``rdf:Description`` both mean "this
    is a resource"; ``xml:lang`` alone does not: it is a qualifier of the text.
    """
    if element.get(f"{{{_RDF_NS}}}parseType") == "Resource":
        return True
    if any(child.tag == _RDF_DESCRIPTION for child in element):
        return True
    return any(
        name not in _NODE_ATTRIBUTES and not name.startswith(f"{{{_XML_NS}}}")
        for name in element.attrib
    )


def _split(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        uri, _, local = tag[1:].partition("}")
        return uri, local
    return "", tag


def _local(tag: str) -> str:
    return _split(tag)[1]


def _text(node: ET.Element) -> str:
    return "".join(node.itertext()).strip()

