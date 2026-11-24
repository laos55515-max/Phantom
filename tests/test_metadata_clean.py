"""The cleaner: selective removal, verification, atomic publish, and refusals."""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from pathlib import Path

import numpy as np
import pytest
import soundfile
from PIL import Image

import metadata_samples as samples
from phantom.core import files as fsutil
from phantom.core.errors import (
    ErrorCode,
    MetadataConflictError,
    MetadataError,
    OperationCancelled,
    OutputExistsError,
    PhantomError,
    SignedMediaError,
    SourceOverwriteError,
    UnsupportedFormat,
)
from phantom.metadata import cleaner, inspector


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _staged_siblings(directory: Path) -> list[str]:
    """Leftovers a crashed publish would leave: the hidden staged siblings."""
    return sorted(
        path.name
        for path in directory.iterdir()
        if path.name.startswith(".") and ".metadata" in path.name
    )


def _pixels(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.array(image)


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------
def test_default_selection_is_personal_and_removable_only(tmp_path: Path) -> None:
    found = inspector.inspect_path(str(samples.personal_png(tmp_path / "plan.png")))

    selection = cleaner.default_selection(found)

    assert "png.gps.0x0002" in selection  # GPSLatitude
    assert "png.gps.0x0004" in selection  # GPSLongitude
    assert "png.exif.0xa431" in selection
    assert "png.text.tEXt.Comment" in selection
    assert "png.text.tEXt.Copyright" not in selection
    assert "png.text.tEXt.Title" not in selection
    assert "png.chunk.prVt" not in selection, "an unknown chunk is never preselected"


def test_attribution_is_offered_but_not_preselected(tmp_path: Path) -> None:
    found = inspector.inspect_path(str(samples.personal_png(tmp_path / "author.png")))

    author = found.find("png.text.tEXt.Author")
    assert author is not None and author.removable is True
    assert "png.text.tEXt.Author" not in cleaner.default_selection(found)


def test_the_plan_says_what_leaves_and_what_stays(tmp_path: Path) -> None:
    found = inspector.inspect_path(str(samples.personal_wav(tmp_path / "plan.wav")))
    selection = frozenset({"wav.info.ICMT", "wav.info.ICOP"})

    decision = cleaner.plan(found, selection)

    assert decision.allowed is True
    removed = {item.key for item in decision.remove}
    blocked = {item.key for item in decision.blocked}
    assert removed == {"wav.info.ICMT"}
    assert blocked == {"wav.info.ICOP"}, "copyright is requested but protected"
    assert any(item.key == "wav.info.INAM" for item in decision.keep)


def test_a_signed_file_blocks_the_plan(tmp_path: Path) -> None:
    found = inspector.inspect_path(str(samples.signed_png(tmp_path / "signed.png")))

    decision = cleaner.plan(found, found.removable_keys)

    assert decision.allowed is False
    assert decision.reason == "signed_media"
    assert decision.remove == ()
    assert decision.keep == found.fields


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------
def test_cleaning_a_png_removes_personal_fields_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    source = samples.personal_png(tmp_path / "photo.png")
    before = _sha(source)
    output = tmp_path / "photo-clean.png"

    found = inspector.inspect_path(str(source))
    outcome = cleaner.clean(str(source), str(output), cleaner.default_selection(found))

    assert outcome.verified is True
    assert outcome.comparison.identical is True
    assert outcome.comparison.checked is True
    assert outcome.checksum_ok is True
    assert _sha(source) == before, "the original must not change"

    after = inspector.inspect_path(str(output))
    keys = {item.key for item in after.fields}
    assert "png.gps.coordinates" not in keys, "the view goes with the tags behind it"
    assert "png.exif.0x0131" in keys, "Software is technical and stays"
    assert "png.exif.0xa431" not in keys
    assert "png.text.tEXt.Comment" not in keys
    for kept in ("png.text.tEXt.Title", "png.text.tEXt.Copyright", "png.chunk.prVt"):
        assert kept in keys, f"{kept} must survive"
    assert outcome.source_sha256 != outcome.output_sha256


def test_pixels_are_identical_after_cleaning(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "pixels.png")
    output = tmp_path / "pixels-clean.png"
    found = inspector.inspect_path(str(source))

    cleaner.clean(str(source), str(output), found.removable_keys)

    assert np.array_equal(_pixels(source), _pixels(output))
    assert _pixels(output).shape == _pixels(source).shape


def test_the_unknown_chunk_is_never_dropped_even_when_everything_else_goes(
    tmp_path: Path,
) -> None:
    source = samples.personal_png(tmp_path / "all.png")
    output = tmp_path / "all-clean.png"
    found = inspector.inspect_path(str(source))

    cleaner.clean(str(source), str(output), found.removable_keys)

    after = inspector.inspect_path(str(output))
    assert any(item.key == "png.chunk.prVt" for item in after.fields)


# ---------------------------------------------------------------------------
# WAV and FLAC
# ---------------------------------------------------------------------------
def test_cleaning_a_wav_keeps_every_pcm_sample(tmp_path: Path) -> None:
    source = samples.personal_wav(tmp_path / "take.wav")
    output = tmp_path / "take-clean.wav"
    found = inspector.inspect_path(str(source))

    outcome = cleaner.clean(str(source), str(output), cleaner.default_selection(found))

    assert outcome.verified is True
    source_audio, source_rate = soundfile.read(str(source), dtype="int16")
    output_audio, output_rate = soundfile.read(str(output), dtype="int16")
    assert source_rate == output_rate
    assert np.array_equal(source_audio, output_audio)

    after = inspector.inspect_path(str(output))
    keys = {item.key for item in after.fields}
    assert "wav.info.ICMT" not in keys
    assert "wav.bext.originator" not in keys
    assert {"wav.info.INAM", "wav.info.ICOP", "wav.chunk.junk"} <= keys


def test_cleaning_a_flac_keeps_every_pcm_sample(tmp_path: Path) -> None:
    source = samples.personal_flac(tmp_path / "track.flac")
    output = tmp_path / "track-clean.flac"
    found = inspector.inspect_path(str(source))

    outcome = cleaner.clean(str(source), str(output), cleaner.default_selection(found))

    assert outcome.verified is True
    source_audio, source_rate = soundfile.read(str(source), dtype="int16")
    output_audio, output_rate = soundfile.read(str(output), dtype="int16")
    assert source_rate == output_rate
    assert np.array_equal(source_audio, output_audio)

    after = inspector.inspect_path(str(output))
    keys = {item.key for item in after.fields}
    assert "flac.vorbis.COMMENT" not in keys
    assert "flac.vorbis.DATE" not in keys
    assert {"flac.vorbis.TITLE", "flac.vorbis.COPYRIGHT", "flac.vendor"} <= keys


def test_a_cover_picture_can_be_removed_when_it_is_asked_for(tmp_path: Path) -> None:
    source = samples.flac_file(tmp_path / "art.flac", {"TITLE": "cover"}, picture=True)
    output = tmp_path / "art-clean.flac"
    found = inspector.inspect_path(str(source))
    picture = next(item for item in found.fields if item.key.startswith("flac.picture."))

    cleaner.clean(str(source), str(output), frozenset({picture.key}))

    after = inspector.inspect_path(str(output))
    assert not [item for item in after.fields if item.key.startswith("flac.picture.")]
    with soundfile.SoundFile(str(output)) as handle:
        assert handle.frames == samples.FRAMES


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------
def test_a_signed_file_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    source = samples.signed_png(tmp_path / "signed.png")
    output = tmp_path / "signed-clean.png"

    with pytest.raises(SignedMediaError):
        cleaner.clean(str(source), str(output), frozenset({"png.text.tEXt.Comment"}))

    assert not output.exists()
    assert _staged_siblings(tmp_path) == []


def test_a_signed_wav_is_refused_too(tmp_path: Path) -> None:
    source = samples.signed_wav(tmp_path / "signed.wav")

    with pytest.raises(SignedMediaError):
        cleaner.clean(str(source), str(tmp_path / "out.wav"), frozenset({"wav.info.ICMT"}))


def test_the_output_may_not_be_the_source(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "same.png")
    before = _sha(source)

    with pytest.raises(SourceOverwriteError):
        cleaner.clean(str(source), str(source), frozenset({"png.text.tEXt.Comment"}))
    assert _sha(source) == before


def test_the_source_cannot_be_reached_through_a_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = samples.personal_png(tmp_path / "rel.png")
    before = _sha(source)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SourceOverwriteError):
        cleaner.clean(str(source), "./rel.png", frozenset({"png.text.tEXt.Comment"}))

    assert _sha(source) == before


def test_an_existing_output_needs_explicit_permission(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "exists.png")
    output = tmp_path / "exists-clean.png"
    output.write_bytes(b"do not lose me")

    with pytest.raises(OutputExistsError, match="already exists"):
        cleaner.clean(str(source), str(output), frozenset({"png.text.tEXt.Comment"}))
    assert output.read_bytes() == b"do not lose me"

    outcome = cleaner.clean(
        str(source),
        str(output),
        frozenset({"png.text.tEXt.Comment"}),
        overwrite=True,
    )
    assert outcome.verified is True
    assert output.stat().st_size > 20


def test_a_wrong_extension_cannot_make_the_destination_the_source(tmp_path: Path) -> None:
    """Reproduced: photo.png cleaned into "photo.wav" used to rewrite photo.png."""
    source = samples.personal_png(tmp_path / "photo.png")
    before = _sha(source)

    with pytest.raises(SourceOverwriteError) as caught:
        cleaner.clean(
            str(source),
            str(tmp_path / "photo.wav"),
            frozenset({"png.text.tEXt.Comment"}),
        )

    assert caught.value.detail == str(tmp_path / "photo.png")
    assert _sha(source) == before, "the source must not change by a single byte"
    assert not (tmp_path / "photo.wav").exists()
    assert _staged_siblings(tmp_path) == []


def test_a_wrong_extension_cannot_replace_an_existing_file_of_the_real_format(
    tmp_path: Path,
) -> None:
    """Reproduced: "existing.wav" for a PNG used to overwrite existing.png."""
    source = samples.personal_png(tmp_path / "photo.png")
    existing = tmp_path / "existing.png"
    existing.write_bytes(b"someone else's pixels")
    before = _sha(existing)

    with pytest.raises(OutputExistsError) as caught:
        cleaner.clean(
            str(source),
            str(tmp_path / "existing.wav"),
            frozenset({"png.text.tEXt.Comment"}),
        )

    assert caught.value.detail == str(existing)
    assert _sha(existing) == before
    assert not (tmp_path / "existing.wav").exists()
    assert _staged_siblings(tmp_path) == []


def test_the_source_cannot_be_reached_through_a_symlink_with_another_extension(
    tmp_path: Path,
) -> None:
    source = samples.personal_png(tmp_path / "photo.png")
    link = tmp_path / "alias.png"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privilege
        pytest.skip("symlinks are unavailable here")
    before = _sha(source)

    with pytest.raises(SourceOverwriteError) as caught:
        cleaner.clean(
            str(source),
            str(tmp_path / "alias.wav"),
            frozenset({"png.text.tEXt.Comment"}),
        )

    assert caught.value.detail == str(link)
    assert _sha(source) == before
    assert link.is_symlink(), "the link itself must survive too"


def test_overwrite_does_not_cover_a_path_the_caller_never_named(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "photo.png")
    existing = tmp_path / "existing.png"
    existing.write_bytes(b"someone else's pixels")

    with pytest.raises(OutputExistsError):
        cleaner.clean(
            str(source),
            str(tmp_path / "existing.wav"),
            frozenset({"png.text.tEXt.Comment"}),
            overwrite=True,
        )

    assert existing.read_bytes() == b"someone else's pixels"


def test_output_path_resolves_the_extension_before_any_check(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "photo.png")

    assert cleaner.output_path(str(source), str(tmp_path / "copy")) == str(tmp_path / "copy.png")
    assert cleaner.output_path(str(source), str(tmp_path / "copy.WAV")) == str(tmp_path / "copy.png")
    assert cleaner.output_path(str(source), str(tmp_path / "copy.PNG")) == str(tmp_path / "copy.PNG")

    with pytest.raises(SourceOverwriteError):
        cleaner.output_path(str(source), str(tmp_path / "photo.wav"))


def test_cleaning_nothing_is_an_error(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "none.png")

    with pytest.raises(MetadataError, match="no removable field"):
        cleaner.clean(str(source), str(tmp_path / "none-clean.png"), frozenset())
    with pytest.raises(MetadataError, match="no removable field"):
        cleaner.clean(
            str(source),
            str(tmp_path / "none-clean.png"),
            frozenset({"png.text.tEXt.Copyright"}),
        )


def test_an_unsupported_file_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "notes.png"
    source.write_bytes(b"just text, honestly\n" * 4)

    with pytest.raises(UnsupportedFormat):
        cleaner.clean(str(source), str(tmp_path / "out.png"), frozenset({"x"}))


# ---------------------------------------------------------------------------
# atomicity and cancellation
# ---------------------------------------------------------------------------
def test_a_successful_run_leaves_no_staged_file(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "atomic.png")
    output = tmp_path / "atomic-clean.png"
    found = inspector.inspect_path(str(source))

    cleaner.clean(str(source), str(output), found.removable_keys)

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "atomic-clean.png",
        "atomic.png",
    ]
    assert _staged_siblings(tmp_path) == []


def test_cancelling_before_the_read_writes_nothing(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "cancel.png")
    output = tmp_path / "cancel-clean.png"
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(OperationCancelled):
        cleaner.clean(
            str(source),
            str(output),
            frozenset({"png.text.tEXt.Comment"}),
            cancel=cancel,
        )

    assert not output.exists()
    assert _staged_siblings(tmp_path) == []


def test_cancelling_after_the_rewrite_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window between "bytes ready" and "renamed into place" honours cancel."""
    from phantom.metadata import png as png_module

    source = samples.personal_png(tmp_path / "late.png")
    output = tmp_path / "late-clean.png"
    cancel = threading.Event()
    original = png_module.rewrite_png

    def rewrite_then_cancel(data: bytes, removal: object) -> bytes:
        cleaned = original(data, removal)  # type: ignore[arg-type]
        cancel.set()
        return cleaned

    monkeypatch.setattr(png_module, "rewrite_png", rewrite_then_cancel)

    with pytest.raises(OperationCancelled):
        cleaner.clean(
            str(source),
            str(output),
            frozenset({"png.text.tEXt.Comment"}),
            cancel=cancel,
        )

    assert not output.exists(), "a cancelled run must not publish"
    assert _staged_siblings(tmp_path) == []


def test_a_rewrite_that_changes_pixels_is_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from phantom.metadata import png as png_module

    source = samples.personal_png(tmp_path / "bad.png")
    output = tmp_path / "bad-clean.png"
    original = png_module.rewrite_png

    def lossy_rewrite(data: bytes, removal: object) -> bytes:
        cleaned = original(data, removal)  # type: ignore[arg-type]
        return cleaned[:-1] + bytes([cleaned[-1] ^ 0x01])

    monkeypatch.setattr(png_module, "rewrite_png", lossy_rewrite)

    with pytest.raises(MetadataError):
        cleaner.clean(str(source), str(output), frozenset({"png.text.tEXt.Comment"}))

    assert not output.exists()
    assert _staged_siblings(tmp_path) == []


def test_a_rewrite_that_drops_a_protected_field_is_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from phantom.metadata import png as png_module

    source = samples.personal_png(tmp_path / "lost.png")
    output = tmp_path / "lost-clean.png"

    original = png_module.rewrite_png

    def overzealous(data: bytes, removal: object) -> bytes:
        # Remove everything, including the copyright line the user kept.
        everything = frozenset(
            {
                "png.text.tEXt.Comment",
                "png.text.tEXt.Copyright",
                "png.text.tEXt.Title",
                "png.text.tEXt.Author",
                "png.text.tEXt.Software",
                "png.exif.0x8298",
                "png.exif.0x013b",
                "png.exif.0x0131",
            }
        )
        return original(data, png_module.removal_from_keys(everything))

    monkeypatch.setattr(png_module, "rewrite_png", overzealous)

    with pytest.raises(MetadataConflictError) as caught:
        cleaner.clean(str(source), str(output), frozenset({"png.text.tEXt.Comment"}))

    # Its own error code, its own sentence, and the field named the way the user
    # saw it — "metadata error: png.exif.0x8298" told them nothing.
    assert caught.value.code is ErrorCode.METADATA_CONFLICT
    assert caught.value.params.get("fields"), "the message names the field by label"
    assert "0x" not in str(caught.value.params["fields"])
    assert not output.exists(), "nothing is published when the guard fires"


def test_the_output_extension_follows_the_real_format(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "extension.png")
    requested = tmp_path / "extension-copy.wav"

    outcome = cleaner.clean(
        str(source),
        str(requested),
        frozenset({"png.text.tEXt.Comment"}),
    )

    assert outcome.output == str(tmp_path / "extension-copy.png")
    assert Path(outcome.output).exists()
    assert not requested.exists()


def test_a_read_only_directory_is_reported_not_ignored(tmp_path: Path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("running as root: directory permissions do not apply")
    source = samples.personal_png(tmp_path / "ro.png")
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o500)
    try:
        with pytest.raises(PhantomError):
            cleaner.clean(
                str(source),
                str(locked / "out.png"),
                frozenset({"png.text.tEXt.Comment"}),
            )
    finally:
        os.chmod(locked, 0o700)


def test_a_long_unicode_output_name_works(tmp_path: Path) -> None:
    source = samples.personal_flac(tmp_path / "довга.flac")
    name = "очищена-" + "дуже-" * 12 + "версія.flac"
    output = tmp_path / name

    outcome = cleaner.clean(
        str(source),
        str(output),
        frozenset({"flac.vorbis.COMMENT"}),
    )

    assert Path(outcome.output).exists()
    assert os.path.basename(outcome.output) == name


def test_exclusive_publish_never_replaces_an_existing_file(tmp_path: Path) -> None:
    staged = tmp_path / "staged.bin"
    staged.write_bytes(b"the verified copy")
    target = tmp_path / "target.bin"
    target.write_bytes(b"someone else's file")

    with pytest.raises(FileExistsError):
        fsutil.publish_staged_exclusive(str(staged), str(target))

    assert target.read_bytes() == b"someone else's file"
    assert staged.read_bytes() == b"the verified copy", "the caller still owns the staged copy"

    target.unlink()
    fsutil.publish_staged_exclusive(str(staged), str(target))

    assert target.read_bytes() == b"the verified copy"
    assert not staged.exists()


def test_two_cleans_publishing_to_one_path_have_exactly_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two verified copies racing for the same free name: one success, one refusal.

    The barrier is inside the publication primitive, after both cleans have
    passed every earlier check and verified their rewrite — the exact moment the
    old unconditional rename let both of them replace the file, and the loser
    still looked successful.
    """
    first = tmp_path / "first.png"
    first.write_bytes(
        samples.png_bytes(text={"Comment": "first"}, pixels=samples.texture(seed=1))
    )
    second = tmp_path / "second.png"
    second.write_bytes(
        samples.png_bytes(text={"Comment": "second"}, pixels=samples.texture(seed=2))
    )
    originals = {first: _sha(first), second: _sha(second)}
    target = tmp_path / "shared.png"
    barrier = threading.Barrier(2, timeout=30)
    publish = fsutil.publish_staged_exclusive

    def synchronized(staged: str, destination: str) -> None:
        barrier.wait()  # both cleans are one call away from publishing
        publish(staged, destination)

    monkeypatch.setattr(fsutil, "publish_staged_exclusive", synchronized)

    outcomes: list[cleaner.CleaningOutcome] = []
    refusals: list[OutputExistsError] = []
    unexpected: list[Exception] = []

    def clean_one(source: Path) -> None:
        try:
            outcomes.append(
                cleaner.clean(
                    str(source),
                    str(target),
                    frozenset({"png.text.tEXt.Comment"}),
                )
            )
        except OutputExistsError as exc:
            refusals.append(exc)
        except Exception as exc:  # noqa: BLE001 - collected to report the real cause
            unexpected.append(exc)

    threads = [
        threading.Thread(target=clean_one, args=(first,)),
        threading.Thread(target=clean_one, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert unexpected == []
    assert len(outcomes) == 1, f"expected exactly one success, got {len(outcomes)}"
    assert len(refusals) == 1, f"expected exactly one output_exists, got {len(refusals)}"

    winner = outcomes[0]
    assert winner.output == str(target)
    assert _sha(target) == winner.output_sha256, "the published bytes are the winner's"
    assert np.array_equal(_pixels(target), _pixels(Path(winner.source)))
    assert refusals[0].detail == str(target)
    assert refusals[0].params["path"] == str(target)
    for source, digest in originals.items():
        assert _sha(source) == digest, "neither source may change"
    assert _staged_siblings(tmp_path) == []


def test_a_second_pass_finds_nothing_personal_left(tmp_path: Path) -> None:
    """The default selection is complete: running it twice changes nothing more."""
    source = samples.personal_png(tmp_path / "twice.png")
    first = tmp_path / "twice-clean.png"
    found = inspector.inspect_path(str(source))
    cleaner.clean(str(source), str(first), cleaner.default_selection(found))

    remaining = inspector.inspect_path(str(first))
    leftover = [
        item.key
        for item in remaining.fields
        if item.sensitive and item.removable
    ]

    assert leftover == []
    assert np.array_equal(_pixels(source), _pixels(first))


def test_the_library_refuses_to_publish_over_a_directory(tmp_path: Path) -> None:
    source = samples.personal_png(tmp_path / "dir.png")
    target = tmp_path / "occupied"
    target.mkdir()

    with pytest.raises(OutputExistsError):
        cleaner.clean(str(source), str(target), frozenset({"png.text.tEXt.Comment"}))


def test_a_full_disk_is_reported_as_a_metadata_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = samples.personal_png(tmp_path / "disk.png")
    output = tmp_path / "disk-clean.png"

    def no_space(*_args: object, **_kwargs: object) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "disk_usage", no_space, raising=False)
    monkeypatch.setattr(os, "fsync", no_space, raising=False)

    with pytest.raises(MetadataError):
        cleaner.clean(str(source), str(output), frozenset({"png.text.tEXt.Comment"}))

    assert not output.exists()
