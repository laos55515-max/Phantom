"""Regenerate ``src/phantom/resources/example_payload.pdf``.

A tiny, fully valid one-page PDF written by hand (ASCII, computed xref table).
It must stay small: the bundled example carrier (``example_carrier.png``) only
holds ~33 KB of payload, so this file has to fit an example-sized secret.  The
contents carry no real data.
"""

from __future__ import annotations

import zlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DESTINATION = _ROOT / "src" / "phantom" / "resources" / "example_payload.pdf"

LINES = [
    ("PHANTOM - example payload", 20),
    ("", 14),
    ("This is a demo document bundled with PHANTOM.", 12),
    ("It exists so you can try hiding a real PDF file", 12),
    ("inside a picture without using any real data.", 12),
    ("", 14),
    ("Attachment: example-payload.pdf", 12),
    ("Classification: PUBLIC (demo)", 12),
]


def build_pdf() -> bytes:
    content_lines = ["BT", "/F1 1 Tf", "72 720 Td"]
    first = True
    for text, size in LINES:
        if not first:
            content_lines.append("0 -28 Td")
        first = False
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content_lines.append(f"/F1 {size} Tf ({escaped}) Tj")
    content_lines.append("ET")
    stream = "\n".join(content_lines).encode("ascii")
    stream = zlib.compress(stream)
    encoded = True

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
    )
    stream_head = f"<< /Length {len(stream)}".encode("ascii")
    if encoded:
        stream_head += b" /Filter /FlateDecode"
    stream_head += b" >>\nstream\n"
    objects.append(stream_head + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(pdf)
    count = len(objects) + 1
    pdf += f"xref\n0 {count}\n".encode("ascii")
    pdf += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        pdf += f"{offset:010d} 00000 n \n".encode("ascii")
    pdf += (
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode("ascii")
    )
    return bytes(pdf)


def main() -> None:
    data = build_pdf()
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_bytes(data)
    print(f"wrote {DESTINATION} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
