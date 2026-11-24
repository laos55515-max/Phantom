"""The metadata tab's side of the bridge: inspect, plan, clean — and the refusals.

The desktop shell reaches the cleaner only through these three commands, so the
contract is pinned here the same way the hide/extract contract is: the report
shape the interface renders, the safe default selection, the preview the user
confirms, the untouched source, and the typed errors the UI explains instead of
showing a traceback.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile
from PIL import Image

import metadata_samples as samples
from phantom.metadata import cleaner, inspector

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"


def _session(requests: list[dict], timeout: float = 180.0) -> list[dict]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "phantom.bridge"],
        input="\n".join(json.dumps(request) for request in requests) + "\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert len(responses) == len(requests), proc.stderr
    return responses


def _call(request: dict) -> dict:
    return _session([request])[0]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pixels(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.array(image)


def _samples(path: Path) -> np.ndarray:
    data, _rate = soundfile.read(str(path), dtype="int32", always_2d=True)
    return np.ascontiguousarray(data, dtype=np.int32)


def _keys(fields: list[dict]) -> set[str]:
    return {item["key"] for item in fields}


# --------------------------------------------------------------------------- inspect
def test_inspect_returns_the_png_report_grouped_for_the_interface(tmp_path: Path) -> None:
    source = samples.rich_exif_png(tmp_path / "gps.png")
    response = _call({"cmd": "metadata_inspect", "path": str(source)})

    assert response["ok"] is True
    report = response["report"]
    assert report["format"] == "png"
    assert report["sha256"] == _sha(source)
    entries = [item for item in report["fields"] if not item["derived"]]
    assert report["counts"]["total"] == len(entries), "derived views are not entries"
    # Category order is the model's, so the interface renders groups as they come.
    ranks = [item["category_rank"] for item in report["fields"]]
    assert ranks == sorted(ranks)
    categories = {item["category"] for item in report["fields"]}
    assert {"personal", "technical", "rights"} <= categories

    gps = next(item for item in report["fields"] if item["key"] == "png.gps.0x0002")
    assert gps["category"] == "personal"
    assert gps["removable"] is True
    orientation = next(item for item in report["fields"] if item["key"] == "png.exif.0x0112")
    assert orientation["category"] == "technical"
    assert orientation["removable"] is False
    assert orientation["protected"] is True
    assert report["technical_summary"]["resolution"] == "48x32"
    assert report["checksum_ok"] is True
    assert isinstance(report["chunks"], list) and report["chunks"]

    found = inspector.inspect_path(str(source))
    assert set(response["default_selection"]) == cleaner.default_selection(found)
    assert "png.gps.0x0002" in response["default_selection"]
    assert "png.exif.0x0112" not in response["default_selection"], "orientation is technical"

    plan = response["plan"]
    assert plan["allowed"] is True
    assert plan["reason"] == ""
    assert _keys(plan["remove"]) == {
        key for key in response["default_selection"] if key in found.removable_keys
    }
    assert plan["keep"] and plan["blocked"] == []


def test_plan_says_what_goes_and_what_a_protected_selection_blocks(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "plan.png")
    response = _call(
        {
            "cmd": "metadata_plan",
            "path": str(source),
            "selection": ["png.text.tEXt.Comment", "png.text.tEXt.Copyright"],
        }
    )

    assert response["ok"] is True
    plan = response["plan"]
    assert _keys(plan["remove"]) == {"png.text.tEXt.Comment"}
    assert _keys(plan["blocked"]) == {"png.text.tEXt.Copyright"}
    assert "png.text.tEXt.Copyright" in _keys(plan["keep"])
    blocked = plan["blocked"][0]
    assert blocked["removable"] is False
    assert blocked["reason"], "a blocked field must say why it is kept"


# --------------------------------------------------------------------------- cleaning
def test_cleaning_a_png_keeps_the_source_bytes_and_the_pixels(tmp_path: Path) -> None:
    source = samples.rich_exif_png(tmp_path / "gps.png")
    output = tmp_path / "gps-clean.png"
    before = _sha(source)
    inspected = _call({"cmd": "metadata_inspect", "path": str(source)})

    response = _call(
        {
            "cmd": "metadata_clean",
            "path": str(source),
            "output_path": str(output),
            "selection": inspected["default_selection"],
        }
    )

    assert response["ok"] is True, response
    assert response["verified"] is True
    assert response["checksum_ok"] is True
    assert response["source_sha256"] == before
    assert response["output_sha256"] == _sha(output)
    assert response["output_sha256"] != before, "removing metadata changes the file hash"
    assert response["content"]["identical"] is True
    assert response["content"]["checked"] is True
    assert response["content"]["source"] == response["content"]["output"]
    assert _sha(source) == before, "the original must not change"
    assert np.array_equal(_pixels(source), _pixels(output))
    assert response["output"] == str(output)
    assert response["report_after"]["path"] == str(output)

    after = _call({"cmd": "metadata_inspect", "path": str(output)})["report"]
    keys = _keys(after["fields"])
    assert "png.gps.0x0002" not in keys
    assert "png.gps.0x0004" not in keys
    assert "png.text.tEXt.Comment" not in keys
    assert "png.exif.0x0112" in keys, "orientation is technical and must stay"
    assert "png.exif.0x8298" in keys, "copyright is rights data and is not removed by default"
    assert "png.exif.0x013b" in keys, "attribution is offered but not preselected"


def test_cleaning_refuses_to_write_over_the_source(tmp_path: Path) -> None:
    source = samples.rich_exif_png(tmp_path / "gps.png")
    before = _sha(source)

    response = _call(
        {
            "cmd": "metadata_clean",
            "path": str(source),
            "output_path": str(source),
            "selection": ["png.gps.0x0002"],
        }
    )

    assert response["ok"] is False
    error = response["error"]
    assert error["code"] == "source_overwrite"
    assert "original" in error["message"]
    assert error["params"]["output"] == str(source)
    assert _sha(source) == before
    assert not any(path.name.startswith(".") for path in tmp_path.iterdir())


def test_cleaning_refuses_an_existing_destination_without_overwrite(tmp_path: Path) -> None:
    source = samples.rich_exif_png(tmp_path / "gps.png")
    output = tmp_path / "already-there.png"
    output.write_bytes(b"someone else's file")
    before = _sha(output)

    response = _call(
        {
            "cmd": "metadata_clean",
            "path": str(source),
            "output_path": str(output),
            "selection": ["png.gps.0x0002"],
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "metadata"
    assert _sha(output) == before


def test_a_signed_png_is_inspected_but_refused_with_a_reason(tmp_path: Path) -> None:
    source = samples.signed_png(tmp_path / "signed.png")
    output = tmp_path / "signed-clean.png"
    before = _sha(source)

    inspected = _call({"cmd": "metadata_inspect", "path": str(source)})
    assert inspected["ok"] is True
    assert inspected["report"]["provenance"]["state"] == "unverified"
    assert inspected["report"]["provenance"]["detected"] is True
    assert inspected["plan"]["allowed"] is False
    assert inspected["plan"]["reason"] == "signed_media"
    assert inspected["plan"]["remove"] == []

    response = _call(
        {
            "cmd": "metadata_clean",
            "path": str(source),
            "output_path": str(output),
            "selection": inspected["default_selection"],
        }
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "signed_media"
    assert response["error"]["params"]["state"] == "unverified"
    assert not output.exists()
    assert _sha(source) == before


def test_wav_tags_are_removed_and_every_sample_survives(tmp_path: Path) -> None:
    source = samples.personal_wav(tmp_path / "take.wav")
    output = tmp_path / "take-clean.wav"
    before = _sha(source)
    inspected = _call({"cmd": "metadata_inspect", "path": str(source)})
    assert inspected["report"]["format"] == "wav"
    assert inspected["default_selection"]

    response = _call(
        {
            "cmd": "metadata_clean",
            "path": str(source),
            "output_path": str(output),
            "selection": inspected["default_selection"],
        }
    )

    assert response["ok"] is True, response
    assert response["verified"] is True
    assert response["content"]["identical"] is True
    assert _sha(source) == before
    assert np.array_equal(_samples(source), _samples(output))

    after = _call({"cmd": "metadata_inspect", "path": str(output)})["report"]
    keys = _keys(after["fields"])
    assert "wav.info.ICMT" not in keys
    assert "wav.bext.description" not in keys
    assert "wav.info.IART" in keys, "attribution is offered but not preselected"
    assert "wav.info.ICOP" in keys, "copyright is rights data and stays"
    assert "wav.info.INAM" in keys, "the title is descriptive and stays"


def test_flac_tags_are_removed_and_every_sample_survives(tmp_path: Path) -> None:
    source = samples.personal_flac(tmp_path / "session.flac")
    output = tmp_path / "session-clean.flac"
    before = _sha(source)
    inspected = _call({"cmd": "metadata_inspect", "path": str(source)})
    assert inspected["report"]["format"] == "flac"

    response = _call(
        {
            "cmd": "metadata_clean",
            "path": str(source),
            "output_path": str(output),
            "selection": inspected["default_selection"],
        }
    )

    assert response["ok"] is True, response
    assert response["content"]["identical"] is True
    assert _sha(source) == before
    assert np.array_equal(_samples(source), _samples(output))

    after = _call({"cmd": "metadata_inspect", "path": str(output)})["report"]
    keys = _keys(after["fields"])
    assert "flac.vorbis.COMMENT" not in keys
    assert "flac.vorbis.DATE" not in keys
    assert "flac.vorbis.ARTIST" in keys, "attribution is offered but not preselected"
    assert "flac.vorbis.TITLE" in keys, "the title is descriptive and stays"


# --------------------------------------------------------------------------- refusals
def test_unsupported_files_are_refused_by_contents_not_by_name(tmp_path: Path) -> None:
    disguised = tmp_path / "photo.png"
    disguised.write_bytes(b"this is not a PNG at all\n")
    response = _call({"cmd": "metadata_inspect", "path": str(disguised)})

    assert response["ok"] is False
    assert response["error"]["code"] == "unsupported_format"


def test_bad_requests_are_typed_errors_not_tracebacks(tmp_path: Path) -> None:
    source = samples.rich_exif_png(tmp_path / "gps.png")
    responses = _session(
        [
            {"cmd": "metadata_plan", "path": str(source), "selection": "png.gps.0x0002"},
            {
                "cmd": "metadata_clean",
                "path": str(source),
                "output_path": str(tmp_path / "out.png"),
                "selection": ["png.exif.0x0112"],
            },
            {
                "cmd": "metadata_clean",
                "path": str(source),
                "selection": ["png.gps.0x0002"],
            },
        ]
    )
    assert [response["ok"] for response in responses] == [False, False, False]
    assert responses[0]["error"]["code"] == "invalid_input"
    assert responses[1]["error"]["code"] == "metadata"
    assert "no removable field" in responses[1]["error"]["message"]
    assert responses[2]["error"]["code"] == "invalid_input"


def test_stderr_never_carries_metadata_values(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "secret.png")  # Comment: "… wifi hunter2"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "phantom.bridge"],
        input=json.dumps({"cmd": "metadata_inspect", "path": str(source)}) + "\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert "hunter2" not in proc.stderr
    assert "hunter2" in proc.stdout  # the report is the answer, and it goes to stdout
    assert proc.stderr.count("phantom-bridge cmd=") == 1
