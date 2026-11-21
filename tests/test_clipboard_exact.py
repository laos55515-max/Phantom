"""The clipboard has to carry the bytes it was given, and say when it does not.

Measured in CI on Aqua: Tk's own clipboard path re-encodes what it is given, and
every emoji in a 37 800-character payload arrived in another application as four
Latin-1 characters (``😀`` → ``ðŸ˜€``).  The TXT export was exact the whole time —
these tests keep both halves true: the OS tool is fed UTF-8 bytes, the read-back
decides what the footer claims, and the export stays byte for byte.
"""

from __future__ import annotations

import subprocess

import pytest

from phantom.core import constants as C
from phantom.ui import clipboard as clip

EMOJI = "строка результата 😀\n👩‍👩‍👧‍👦 🇺🇦 e\u0301"


class _Done:
    """What ``subprocess.run`` returns, without running anything."""

    def __init__(self, returncode: int = 0, stdout: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


class _Recorder:
    """A runner that remembers the command and the bytes it was handed."""

    def __init__(self, returncode: int = 0, stdout: bytes = b"", raises: bool = False) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.raises = raises
        self.calls: list[tuple[list[str], bytes]] = []

    def __call__(self, command: list[str], **kwargs: object) -> _Done:
        if self.raises:
            raise FileNotFoundError("no such tool")
        payload = kwargs.get("input") or b""
        self.calls.append((list(command), bytes(payload)))  # type: ignore[arg-type]
        return _Done(self.returncode, self.stdout)


class _FakeRoot:
    """The four Tk clipboard calls the window makes, and nothing else."""

    def __init__(self, clipboard: str = "") -> None:
        self.clipboard = clipboard
        self.after_calls: list[int] = []

    def clipboard_clear(self) -> None:
        self.clipboard = ""

    def clipboard_append(self, value: str) -> None:
        self.clipboard += value

    def clipboard_get(self) -> str:
        if not self.clipboard:
            raise RuntimeError("clipboard has no text")
        return self.clipboard

    def after(self, milliseconds: int, _callback: object) -> str:
        self.after_calls.append(milliseconds)
        return "token"

    def after_cancel(self, _token: object) -> None:
        return None


def _window(clipboard: str = "") -> object:
    """A stand-in for :class:`PhantomApp` with its real clipboard methods bound in.

    ``phantom.ui.app`` imports tkinter, so the window-level tests ask for it here
    and skip on a machine that has none — the module-level tests above still run.
    """
    pytest.importorskip("tkinter")
    from phantom.ui.app import PhantomApp  # noqa: PLC0415 - needs tkinter

    class _Window:
        _copy_to_clipboard = PhantomApp._copy_to_clipboard
        _verify_clipboard = PhantomApp._verify_clipboard
        _clipboard_holds = PhantomApp._clipboard_holds
        _report_copied = PhantomApp._report_copied

        def __init__(self) -> None:
            self.root = _FakeRoot(clipboard)
            self._clipboard_value: str | None = None
            self._clipboard_exact = False
            self._clipboard_token: str | None = None
            self.status: list[tuple[str, str]] = []

        def _set_status_key(self, key: str, *, severity: str = "info", **_kw: object) -> None:
            self.status.append((key, severity))

    return _Window()


# -- the platform tools -------------------------------------------------------
@pytest.mark.parametrize(
    ("platform", "write", "read"),
    [
        ("darwin", ("pbcopy",), ("pbpaste",)),
        ("win32", ("clip",), None),
        # Wayland's tools are preferred where they exist, and they are in the set of
        # "installed" names above; the X11 fallbacks are covered on their own below.
        ("linux", ("wl-copy",), ("wl-paste", "--no-newline")),
    ],
)
def test_the_system_tool_is_chosen_per_platform(
    platform: str, write: tuple[str, ...] | None, read: tuple[str, ...] | None
) -> None:
    # "Everything this platform ships is installed": Darwin pbcopy/pbpaste, Windows
    # clip, and no X11 tool on a machine that answers for none of them.
    installed = {"pbcopy", "pbpaste", "clip", "wl-copy", "wl-paste"}
    which = lambda name: f"/usr/bin/{name}" if name in installed else None  # noqa: E731
    assert clip.native_write_command(platform, which) == write
    assert clip.native_read_command(platform, which) == read
    # And the same platform with nothing installed: the answer is "ask Tk".
    nothing = lambda _name: None  # noqa: E731
    assert clip.native_write_command(platform, nothing) is None
    assert clip.native_read_command(platform, nothing) is None


def test_linux_uses_the_tool_that_is_installed() -> None:
    which = lambda name: "/usr/bin/xclip" if name == "xclip" else None  # noqa: E731
    assert clip.native_write_command("linux", which) == ("xclip", "-selection", "clipboard", "-i")
    assert clip.native_read_command("linux", which) == ("xclip", "-selection", "clipboard", "-o")


# -- writing ------------------------------------------------------------------
def test_the_writer_hands_over_utf8_bytes_and_never_a_string() -> None:
    """The regression: the bytes on the pasteboard are the bytes of the text."""
    runner = _Recorder()
    assert clip.write_clipboard_exact(EMOJI, runner=runner, platform="darwin", which=lambda _n: "x")
    command, payload = runner.calls[0]
    assert command == ["pbcopy"]
    assert payload == EMOJI.encode("utf-8")
    assert payload.decode("utf-8") == EMOJI
    assert b"\xf0\x9f\x98\x80" in payload, "the emoji did not survive as UTF-8"


def test_a_missing_tool_is_not_an_error() -> None:
    runner = _Recorder()
    assert (
        clip.write_clipboard_exact(EMOJI, runner=runner, platform="darwin", which=lambda _n: None)
        is False
    )
    assert runner.calls == []


def test_a_tool_that_fails_is_reported_not_raised() -> None:
    runner = _Recorder(raises=True)
    assert (
        clip.write_clipboard_exact(EMOJI, runner=runner, platform="darwin", which=lambda _n: "x")
        is False
    )
    assert (
        clip.write_clipboard_exact(
            EMOJI, runner=_Recorder(returncode=1), platform="darwin", which=lambda _n: "x"
        )
        is False
    )


def test_empty_text_is_never_written() -> None:
    runner = _Recorder()
    assert (
        clip.write_clipboard_exact("", runner=runner, platform="darwin", which=lambda _n: "x")
        is False
    )
    assert runner.calls == []


# -- reading ------------------------------------------------------------------
def test_the_reader_decodes_utf8_and_normalises_line_endings() -> None:
    runner = _Recorder(stdout="a\r\nb 😀".encode())
    assert (
        clip.read_clipboard_exact(runner=runner, platform="darwin", which=lambda _n: "x")
        == "a\nb 😀"
    )


def test_bytes_that_are_not_utf8_are_refused_not_guessed() -> None:
    """A guess is how a copied string comes back different from the copy."""
    runner = _Recorder(stdout=b"\xff\xfe broken")
    assert clip.read_clipboard_exact(runner=runner, platform="darwin", which=lambda _n: "x") is None


def test_an_empty_or_failed_read_means_ask_tk() -> None:
    which = lambda _n: "x"  # noqa: E731
    assert clip.read_clipboard_exact(runner=_Recorder(stdout=b""), platform="darwin", which=which) is None
    assert (
        clip.read_clipboard_exact(
            runner=_Recorder(returncode=1, stdout=b"x"), platform="darwin", which=which
        )
        is None
    )
    assert clip.read_clipboard_exact(runner=_Recorder(raises=True), platform="darwin", which=which) is None


def test_a_round_trip_through_the_tool_is_byte_exact() -> None:
    """Write, then read back exactly what was written: the whole point."""
    stored: dict[str, bytes] = {}

    def writer(command: list[str], **kwargs: object) -> _Done:
        stored["bytes"] = bytes(kwargs.get("input") or b"")  # type: ignore[arg-type]
        return _Done()

    def reader(command: list[str], **_kwargs: object) -> _Done:
        return _Done(stdout=stored["bytes"])

    which = lambda _n: "x"  # noqa: E731
    assert clip.write_clipboard_exact(EMOJI, runner=writer, platform="darwin", which=which)
    assert clip.read_clipboard_exact(runner=reader, platform="darwin", which=which) == EMOJI


# -- what the window does with it --------------------------------------------
def test_the_window_writes_through_the_tool_and_not_through_tk(monkeypatch) -> None:
    """Tk would re-encode it: when the tool answered, Tk must not touch the board."""
    written: dict[str, str] = {}

    def exact(value: str, **_kwargs: object) -> bool:
        written["value"] = value
        return True

    monkeypatch.setattr(clip, "write_clipboard_exact", exact)
    monkeypatch.setattr(clip, "read_clipboard_exact", lambda **_k: written.get("value"))
    window = _window(clipboard="previous owner's text")
    window._copy_to_clipboard(EMOJI)
    assert window._clipboard_value == EMOJI
    assert window._clipboard_exact is True
    assert window.root.clipboard == "previous owner's text", "Tk also wrote to the board"


def test_a_read_back_that_differs_is_reported_as_not_exact(monkeypatch) -> None:
    monkeypatch.setattr(clip, "write_clipboard_exact", lambda *_a, **_k: True)
    # The pasteboard hands back mojibake: exactly what the old Tk path produced.
    monkeypatch.setattr(clip, "read_clipboard_exact", lambda **_k: "ðŸ˜€")
    window = _window()
    window._copy_to_clipboard("😀")
    assert window._clipboard_value == "😀"
    assert window._clipboard_exact is False
    window._report_copied("😀")
    assert window.status == [("hint_clipboard_exact", "warn")]


def test_without_the_tool_the_window_falls_back_to_tk_and_says_so(monkeypatch) -> None:
    monkeypatch.setattr(clip, "write_clipboard_exact", lambda *_a, **_k: False)
    monkeypatch.setattr(clip, "read_clipboard_exact", lambda **_k: None)
    window = _window()
    window._copy_to_clipboard("plain ascii")
    assert window.root.clipboard == "plain ascii"
    assert window._clipboard_exact is True
    window._report_copied("plain ascii")
    assert window.status == [("status_copied", "ok")]


def test_the_auto_clear_is_scheduled_for_an_exact_copy(monkeypatch) -> None:
    monkeypatch.setattr(clip, "write_clipboard_exact", lambda *_a, **_k: True)
    monkeypatch.setattr(clip, "read_clipboard_exact", lambda **_k: "short")
    window = _window()
    window._copy_to_clipboard("short")
    assert window.root.after_calls == [int(C.CLIPBOARD_CLEAR_SECONDS * 1000)]


def test_the_holds_check_uses_the_exact_reader(monkeypatch) -> None:
    monkeypatch.setattr(clip, "read_clipboard_exact", lambda **_k: EMOJI)
    monkeypatch.setattr(clip, "read_clipboard", lambda *_a, **_k: "mangled")
    window = _window()
    assert window._clipboard_holds(EMOJI) is True
    assert window._clipboard_holds("something else") is False


def test_a_copy_too_large_to_read_back_is_not_verified(monkeypatch) -> None:
    """Reading 15 MiB back on the GUI thread is the cost the cap exists for."""
    calls: list[str] = []
    monkeypatch.setattr(clip, "write_clipboard_exact", lambda *_a, **_k: True)
    monkeypatch.setattr(
        clip,
        "read_clipboard_exact",
        lambda **_k: calls.append("read") or "x",
    )
    window = _window()
    window._copy_to_clipboard("x" * (C.CLIPBOARD_AUTO_CLEAR_MAX_CHARS + 1))
    assert calls == []
    assert window._clipboard_exact is True
    assert window.root.after_calls == []


def test_the_export_stays_byte_exact_for_the_same_text() -> None:
    """The TXT export and the clipboard carry the same bytes — no second encoder."""
    payload = EMOJI.encode("utf-8")
    assert payload.decode("utf-8") == EMOJI
    assert b"\xef\xbf\xbd" not in payload, "a replacement character got into the payload"


def test_subprocess_is_the_only_thing_that_runs() -> None:
    """No shell, no temporary file: the command is an argument vector."""
    runner = _Recorder()
    clip.write_clipboard_exact("x", runner=runner, platform="darwin", which=lambda _n: "x")
    command, _payload = runner.calls[0]
    assert isinstance(command, list)
    assert subprocess.list2cmdline(command).count('"') % 2 == 0
