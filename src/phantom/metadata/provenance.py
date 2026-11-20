"""Content credentials: what PHANTOM can honestly say about a C2PA manifest.

Reading a manifest is not validating it.  A C2PA signature can only be checked
against a trust list and a certificate chain, and this build ships neither (and
never will without a verifier library), so the reader here reports what the bytes
*declare* and states plainly that the signature was not verified.  That is also
why the cleaner refuses to touch a signed file at all: rewriting the container
would break the manifest's hard binding, and a "cleaned" file that still claims a
signature it no longer satisfies is worse than no operation.
"""

from __future__ import annotations

import re
from typing import Final

from phantom.metadata.models import ProvenanceReport, ProvenanceState, Section

#: JUMBF superbox type: the box structure every C2PA manifest lives in.
JUMBF_MAGIC: Final = b"jumb"
#: Labels a C2PA manifest uses for its own boxes.
C2PA_LABELS: Final[tuple[bytes, ...]] = (
    b"c2pa",
    b"c2pa.claim",
    b"c2pa.signature",
    b"c2pa.assertions",
    b"c2pa.ingredient",
    b"http://c2pa.org",
    b"ISO 22144",
)
#: ``urn:uuid:`` labels identify one manifest inside the JUMBF store.
_MANIFEST_LABEL: Final = re.compile(rb"urn:uuid:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                                   rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

#: The sentence the interface and the reports print next to any manifest.
LOCAL_ANALYSIS_LIMITS: Final = (
    "PHANTOM reads the manifest bytes and reports what they declare. It does not "
    "verify the C2PA signature: that needs the certificate chain and the trust "
    "list, which this build does not include. A manifest is a claim, not proof "
    "of authenticity."
)


def labels_in(blob: bytes) -> list[str]:
    """The manifest labels a blob mentions, de-duplicated and in file order."""
    found: list[str] = []
    for match in _MANIFEST_LABEL.finditer(blob):
        label = match.group().decode("ascii", errors="replace")
        if label not in found:
            found.append(label)
    return found


def markers_in(blob: bytes) -> list[str]:
    """Which C2PA labels appear in a blob."""
    return [marker.decode("ascii") for marker in C2PA_LABELS if marker in blob]


def has_signature(blob: bytes) -> bool:
    """True when the blob carries a C2PA signature box."""
    return b"c2pa.signature" in blob or b"c2pa.signaturebox" in blob


def classify(section: Section) -> ProvenanceReport:
    """Turn the blocks a parser found into a provenance verdict.

    ``MANIFEST_FOUND`` means "there is a manifest and nothing that looks like a
    signature"; ``SIGNATURE_UNVERIFIED`` means "there is a signature, and this
    build cannot check it".  Neither one claims the content is authentic.
    """
    signed_by_block = bool(section.signature_blobs)
    manifests = list(section.manifests)
    if not manifests and not signed_by_block:
        return ProvenanceReport()
    labels: list[str] = []
    markers: list[str] = []
    signature = signed_by_block
    for _name, blob in manifests:
        for label in labels_in(blob):
            if label not in labels:
                labels.append(label)
        for marker in markers_in(blob):
            if marker not in markers:
                markers.append(marker)
        signature = signature or has_signature(blob) or JUMBF_MAGIC in blob[:64]
    details: list[tuple[str, str]] = [
        ("blocks", str(len(manifests))),
        ("bytes", str(sum(len(blob) for _name, blob in manifests))),
        ("labels", ", ".join(labels) if labels else "none readable"),
        ("markers", ", ".join(markers) if markers else "none readable"),
        ("signature", "present, not verified" if signature else "not found"),
        ("limits", LOCAL_ANALYSIS_LIMITS),
    ]
    if signed_by_block and not manifests:
        details.append(("signature blocks", ", ".join(section.signature_blobs)))
    state = (
        ProvenanceState.SIGNATURE_UNVERIFIED
        if signature
        else ProvenanceState.MANIFEST_FOUND
    )
    return ProvenanceReport(
        state=state,
        manifests=tuple(labels),
        signed=signature,
        details=tuple(details),
    )


def blocks_cleaning(report: ProvenanceReport) -> bool:
    """True when the file carries a credential the cleaner must not invalidate."""
    return report.signed or report.state is not ProvenanceState.NONE
