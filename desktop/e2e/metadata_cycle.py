#!/usr/bin/env python3
"""End-to-end check of the Metadata tab through a frozen ``phantom-bridge``.

The desktop shell never imports Python: it speaks JSON lines to the packaged
sidecar.  This script therefore drives the *packaged* binary exactly as the app
does and verifies the scenario from the product brief:

1. a PNG with GPS and an orientation tag is inspected — fields grouped by
   category, the safe personal selection proposed, the plan shown;
2. the cleaned copy is written, the source is byte-identical afterwards, the
   decoded pixels are identical, GPS is gone and orientation stays;
3. writing over the source is refused with ``source_overwrite``;
4. a file with a content credential is inspected but refused with
   ``signed_media`` and no output is created;
5. WAV and FLAC with tags are inspected and cleaned; every PCM sample survives;
6. data that is not PNG, WAV or FLAC is refused by content, not by name.

Usage::

    python desktop/e2e/metadata_cycle.py --bridge dist/phantom-bridge --out report.json

Exit code 0 iff every check passes.  If the sidecar was built without
``phantom.metadata`` (the old spec excluded it), step 1 fails with an internal
error and the report says so — that is the check that the module really ships.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import struct
import subprocess
import sys
import tempfile
import zlib
from fractions import Fraction
from pathlib import Path

import numpy as np
import soundfile
from mutagen.flac import FLAC
from PIL import Image
from PIL.PngImagePlugin import PngInfo

SAMPLE_RATE = 8000
FRAMES = 400
RESULTS: list[dict[str, object]] = []


class Bridge:
    """One request per process, one JSON line back — the shell's own protocol."""

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def call(self, request: dict, timeout: float = 600.0) -> dict:
        result = subprocess.run(
            [str(self.binary)],
            input=(json.dumps(request) + "\n").encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        lines = [
            line
            for line in result.stdout.decode("utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        if not lines:
            raise RuntimeError(
                f"bridge produced no output (exit {result.returncode}); "
                f"stderr tail: {result.stderr.decode(errors='replace')[-300:]}"
            )
        return json.loads(lines[-1])


def check(name: str, condition: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(condition), "detail": detail})
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    return bool(condition)


def sha256_hex(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pixels(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        image.load()
        return np.array(image)


def samples(path: Path) -> np.ndarray:
    data, _rate = soundfile.read(str(path), dtype="int32", always_2d=True)
    return np.ascontiguousarray(data, dtype=np.int32)


def keys(fields: list[dict]) -> set[str]:
    return {item["key"] for item in fields}


# --------------------------------------------------------------------------- fixtures
def texture(width: int = 64, height: int = 48) -> np.ndarray:
    rng = np.random.default_rng(11)
    yy, xx = np.mgrid[0:height, 0:width]
    base = (np.sin(xx / 5.0) * 40 + np.cos(yy / 7.0) * 25 + 128).astype(np.int16)
    channels = [
        np.clip(base + rng.integers(-6, 7, base.shape), 0, 255),
        np.clip(base + 18 + rng.integers(-6, 7, base.shape), 0, 255),
        np.clip(base - 14 + rng.integers(-6, 7, base.shape), 0, 255),
    ]
    return np.stack(channels, axis=-1).astype(np.uint8)


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def png_with_gps(path: Path) -> Path:
    """A PNG with a camera dossier: GPS, orientation, author, comment, copyright."""
    exif = Image.Exif()
    exif[0x010F] = "PhantomCam"
    exif[0x0110] = "PhantomCam One"
    exif[0x0112] = 6  # Orientation: rotate 90° CW (technical, must survive)
    exif[0x0131] = "phantom-software 1.0"
    exif[0x013B] = "Ada Lovelace"
    exif[0x0132] = "2024:05:06 07:08:09"
    exif[0x8298] = "(c) 2024 Ada Lovelace"
    exif[0xA431] = "SN-0001-42"
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (Fraction(50), Fraction(27), Fraction(0))
    gps[3] = "E"
    gps[4] = (Fraction(30), Fraction(31), Fraction(0))
    info = PngInfo()
    info.add_text("Comment", "shot at home, kitchen wifi")
    info.add_text("Title", "Holiday")
    info.add_text("Copyright", "(c) 2024 Ada Lovelace")
    with Image.fromarray(texture(), "RGB") as image:
        image.save(path, format="PNG", pnginfo=info, exif=exif, compress_level=1)
    return path


def signed_png(path: Path) -> Path:
    """A PNG carrying a JUMBF/C2PA signature claim: never rewritten."""
    png_with_gps(path)
    blob = (
        b"jumb"
        + struct.pack(">I", 64)
        + b"c2pa"
        + b"c2pa.signature"
        + b"urn:uuid:12345678-1234-1234-1234-123456789abc"
    )
    data = path.read_bytes()
    tail = png_chunk(b"IEND", b"")
    assert data.endswith(tail), "unexpected PNG tail"
    path.write_bytes(data[: -len(tail)] + png_chunk(b"caBX", blob) + tail)
    return path


def tone(count: int = FRAMES) -> np.ndarray:
    t = np.arange(count) / SAMPLE_RATE
    return (0.3 * np.sin(2 * np.pi * (220 + 40 * t) * t)).astype(np.float32)


def riff_chunk(cid: bytes, payload: bytes) -> bytes:
    padded = payload + (b"\x00" if len(payload) % 2 else b"")
    return cid + struct.pack("<I", len(payload)) + padded


def list_info(entries: dict[str, str]) -> bytes:
    payload = b"INFO"
    for name, value in entries.items():
        payload += riff_chunk(name.encode("ascii"), value.encode("latin-1") + b"\x00")
    return riff_chunk(b"LIST", payload)


def wav_with_tags(path: Path) -> Path:
    data = (np.clip(tone(), -1.0, 1.0) * 32767).astype("<i2").tobytes()
    fmt = struct.pack("<HHIIHH", 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
    body = riff_chunk(b"fmt ", fmt) + riff_chunk(b"data", data)
    body += list_info(
        {
            "INAM": "Session 7",
            "IART": "Ada Lovelace",
            "ICMT": "recorded in my kitchen",
            "ICOP": "(c) 2024 Ada Lovelace",
        }
    )
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body)
    return path


def flac_with_tags(path: Path) -> Path:
    soundfile.write(str(path), tone(), SAMPLE_RATE, subtype="PCM_16", format="FLAC")
    audio = FLAC(str(path))
    audio["TITLE"] = "Session 7"
    audio["ARTIST"] = "Ada Lovelace"
    audio["COMMENT"] = "recorded in my kitchen"
    audio["DATE"] = "2024-05-06"
    audio["COPYRIGHT"] = "(c) 2024 Ada Lovelace"
    audio.save()
    return path


# --------------------------------------------------------------------------- scenario
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", required=True, help="path to the packaged phantom-bridge")
    parser.add_argument("--out", default=None, help="where to write the JSON report")
    args = parser.parse_args()

    binary = Path(args.bridge)
    if not binary.is_file():
        print(f"error: bridge not found: {binary}", file=sys.stderr)
        return 2
    bridge = Bridge(binary)
    version = bridge.call({"cmd": "version"})
    check(
        "bridge answers version",
        version.get("ok") is True,
        f"protocol={version.get('protocol')}",
    )

    with tempfile.TemporaryDirectory(prefix="phantom-metadata-") as tmp:
        work = Path(tmp)

        # --- 1. PNG: inspect, grouped, safe defaults -------------------------
        source = png_with_gps(work / "holiday.png")
        source_before = sha256_hex(source)
        inspected = bridge.call({"cmd": "metadata_inspect", "path": str(source)})
        check("PNG is inspected by the packaged bridge", inspected.get("ok") is True,
              str(inspected.get("error", "")))
        report = inspected.get("report", {})
        fields = report.get("fields", [])
        field_keys = keys(fields)
        check("report is grouped by category",
              [item["category_rank"] for item in fields]
              == sorted(item["category_rank"] for item in fields)
              and {"personal", "technical", "rights", "descriptive"}
              <= {item["category"] for item in fields},
              f"{len(fields)} fields")
        check("GPS position is present and personal",
              "png.gps.0x0002" in field_keys and "png.gps.0x0004" in field_keys
              and all(
                  item["removable"]
                  for item in fields
                  if item["key"] in {"png.gps.0x0002", "png.gps.0x0004"}
              ))
        orientation = next((item for item in fields if item["key"] == "png.exif.0x0112"), None)
        check("orientation is technical and protected",
              orientation is not None and orientation["category"] == "technical"
              and orientation["removable"] is False)
        default_selection = inspected.get("default_selection", [])
        check("safe default selects GPS, not orientation",
              "png.gps.0x0002" in default_selection and "png.exif.0x0112" not in default_selection,
              f"{len(default_selection)} keys selected")
        plan = inspected.get("plan", {})
        check("plan previews removals and keeps",
              plan.get("allowed") is True and len(plan.get("remove", [])) == len(default_selection)
              and len(plan.get("keep", [])) > 0,
              f"remove={len(plan.get('remove', []))} keep={len(plan.get('keep', []))}")

        # --- 2. PNG: clean, source untouched, pixels identical ---------------
        output = work / "holiday-clean.png"
        cleaned = bridge.call(
            {
                "cmd": "metadata_clean",
                "path": str(source),
                "output_path": str(output),
                "selection": default_selection,
            }
        )
        check("PNG cleanup succeeds and self-verifies",
              cleaned.get("ok") is True and cleaned.get("verified") is True,
              str(cleaned.get("error", "")))
        check("source PNG is byte-identical after cleaning",
              sha256_hex(source) == source_before == cleaned.get("source_sha256"))
        check("cleaned PNG exists and has a different file hash",
              output.is_file() and sha256_hex(output) == cleaned.get("output_sha256")
              and cleaned.get("output_sha256") != source_before)
        check(
            "decoded pixels are identical",
            output.is_file() and np.array_equal(pixels(source), pixels(output)),
        )
        check("decoded content hash matches the source",
              cleaned.get("content", {}).get("identical") is True
              and cleaned.get("content", {}).get("source")
              == cleaned.get("content", {}).get("output"))
        check("PNG chunk CRCs are valid", cleaned.get("checksum_ok") is True)
        after = bridge.call({"cmd": "metadata_inspect", "path": str(output)})
        after_keys = keys(after.get("report", {}).get("fields", []))
        check(
            "GPS is gone from the copy",
            "png.gps.0x0002" not in after_keys and "png.gps.0x0004" not in after_keys,
        )
        check("orientation survives the clean", "png.exif.0x0112" in after_keys)
        check("copyright survives the default selection", "png.exif.0x8298" in after_keys)

        # --- 3. refuse to overwrite the source -------------------------------
        before_refusal = sha256_hex(source)
        refusal = bridge.call(
            {
                "cmd": "metadata_clean",
                "path": str(source),
                "output_path": str(source),
                "selection": default_selection,
            }
        )
        error = refusal.get("error", {})
        check("writing over the source is refused",
              refusal.get("ok") is False and error.get("code") == "source_overwrite",
              error.get("message", ""))
        check("the refused source is still byte-identical", sha256_hex(source) == before_refusal)

        # --- 4. content credential: inspect, then refuse ---------------------
        signed = signed_png(work / "signed.png")
        signed_before = sha256_hex(signed)
        signed_report = bridge.call({"cmd": "metadata_inspect", "path": str(signed)})
        provenance = signed_report.get("report", {}).get("provenance", {})
        check("a credential is reported, not validated",
              signed_report.get("ok") is True and provenance.get("state") == "unverified"
              and provenance.get("detected") is True)
        check("the plan refuses a credential",
              signed_report.get("plan", {}).get("allowed") is False
              and signed_report.get("plan", {}).get("reason") == "signed_media")
        signed_output = work / "signed-clean.png"
        signed_clean = bridge.call(
            {
                "cmd": "metadata_clean",
                "path": str(signed),
                "output_path": str(signed_output),
                "selection": signed_report.get("default_selection", []),
            }
        )
        signed_error = signed_clean.get("error", {})
        check("cleaning a credential-bearing file is refused",
              signed_clean.get("ok") is False and signed_error.get("code") == "signed_media",
              signed_error.get("detail", ""))
        check("the refusal writes nothing and leaves the source alone",
              not signed_output.exists() and sha256_hex(signed) == signed_before)

        # --- 5. WAV ---------------------------------------------------------
        wav = wav_with_tags(work / "take.wav")
        wav_before = sha256_hex(wav)
        wav_report = bridge.call({"cmd": "metadata_inspect", "path": str(wav)})
        check("WAV tags are inspected",
              wav_report.get("ok") is True and wav_report.get("report", {}).get("format") == "wav"
              and "wav.info.ICMT" in keys(wav_report["report"]["fields"]))
        wav_output = work / "take-clean.wav"
        wav_clean = bridge.call(
            {
                "cmd": "metadata_clean",
                "path": str(wav),
                "output_path": str(wav_output),
                "selection": wav_report.get("default_selection", []),
            }
        )
        check(
            "WAV cleanup succeeds",
            wav_clean.get("ok") is True and wav_clean.get("verified") is True,
              str(wav_clean.get("error", "")))
        check(
            "every WAV sample survives",
            wav_output.is_file() and np.array_equal(samples(wav), samples(wav_output)),
        )
        check(
            "the WAV source is byte-identical",
            sha256_hex(wav) == wav_before == wav_clean.get("source_sha256"),
        )
        wav_after = keys(bridge.call({"cmd": "metadata_inspect", "path": str(wav_output)})
                         .get("report", {}).get("fields", []))
        check("WAV tags are gone and the title stays",
              "wav.info.ICMT" not in wav_after and "wav.info.INAM" in wav_after)

        # --- 6. FLAC --------------------------------------------------------
        flac = flac_with_tags(work / "session.flac")
        flac_before = sha256_hex(flac)
        flac_report = bridge.call({"cmd": "metadata_inspect", "path": str(flac)})
        check("FLAC tags are inspected",
              flac_report.get("ok") is True
              and flac_report.get("report", {}).get("format") == "flac"
              and "flac.vorbis.COMMENT" in keys(flac_report["report"]["fields"]))
        flac_output = work / "session-clean.flac"
        flac_clean = bridge.call(
            {
                "cmd": "metadata_clean",
                "path": str(flac),
                "output_path": str(flac_output),
                "selection": flac_report.get("default_selection", []),
            }
        )
        check(
            "FLAC cleanup succeeds",
            flac_clean.get("ok") is True and flac_clean.get("verified") is True,
              str(flac_clean.get("error", "")))
        check("every FLAC sample survives",
              flac_output.is_file() and np.array_equal(samples(flac), samples(flac_output)))
        check("the FLAC source is byte-identical",
              sha256_hex(flac) == flac_before == flac_clean.get("source_sha256"))
        flac_after = keys(bridge.call({"cmd": "metadata_inspect", "path": str(flac_output)})
                          .get("report", {}).get("fields", []))
        check("FLAC tags are gone and the title stays",
              "flac.vorbis.COMMENT" not in flac_after and "flac.vorbis.TITLE" in flac_after)

        # --- 7. resolved destinations: the two reproduced overwrite cases -----
        cases = work / "destinations"
        cases.mkdir()
        photo = png_with_gps(cases / "photo.png")
        photo_before = sha256_hex(photo)
        wrong_name = bridge.call(
            {
                "cmd": "metadata_clean",
                "path": str(photo),
                "output_path": str(cases / "photo.wav"),
                "selection": default_selection,
            }
        )
        check(
            "photo.png cleaned into photo.wav is refused",
            wrong_name.get("ok") is False
            and wrong_name.get("error", {}).get("code") == "source_overwrite",
            str(wrong_name.get("error", "")),
        )
        check(
            "the refusal names the resolved source path",
            wrong_name.get("error", {}).get("params", {}).get("output")
            == str(cases / "photo.png"),
            str(wrong_name.get("error", {}).get("params", {})),
        )
        check(
            "the source stays byte-identical and photo.wav is not created",
            sha256_hex(photo) == photo_before and not (cases / "photo.wav").exists(),
        )

        existing = cases / "existing.png"
        existing.write_bytes(b"someone else's pixels")
        existing_before = sha256_hex(existing)
        occupied = bridge.call(
            {
                "cmd": "metadata_clean",
                "path": str(photo),
                "output_path": str(cases / "existing.wav"),
                "selection": default_selection,
                "overwrite": False,
            }
        )
        check(
            "existing.wav for a PNG is refused when existing.png is there",
            occupied.get("ok") is False
            and occupied.get("error", {}).get("code") == "output_exists",
            str(occupied.get("error", "")),
        )
        check(
            "the existing PNG is untouched and nothing is written to existing.wav",
            sha256_hex(existing) == existing_before
            and existing.read_bytes() == b"someone else's pixels"
            and not (cases / "existing.wav").exists(),
        )
        resolved = bridge.call(
            {
                "cmd": "metadata_output_path",
                "path": str(photo),
                "output_path": str(cases / "copy.wav"),
            }
        )
        check(
            "the packaged bridge resolves the copy's real extension",
            resolved.get("ok") is True and resolved.get("output") == str(cases / "copy.png"),
            str(resolved.get("error", resolved.get("output"))),
        )

        # --- 7. unsupported content ------------------------------------------
        disguised = work / "photo.png"
        disguised.write_text("not a PNG, only a misleading name\n", encoding="utf-8")
        unsupported = bridge.call({"cmd": "metadata_inspect", "path": str(disguised)})
        check("format detection reads bytes, not extensions",
              unsupported.get("ok") is False
              and unsupported.get("error", {}).get("code") == "unsupported_format")

    failed = [row for row in RESULTS if not row["ok"]]
    report_data = {
        "bridge": str(binary),
        "bridge_size": binary.stat().st_size,
        "bridge_sha256": sha256_hex(binary),
        "version": version,
        "platform": platform.platform(),
        "python": sys.version,
        "passed": len(RESULTS) - len(failed),
        "failed": len(failed),
        "checks": RESULTS,
    }
    if args.out:
        Path(args.out).write_text(
            json.dumps(report_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
