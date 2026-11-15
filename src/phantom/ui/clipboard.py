"""Clipboard operations for the text widgets, without importing Tk (D14).

Two macOS problems have nothing to do with encryption and everything to do with
the widget layer: ``Cmd+V`` inside a CustomTkinter field can be swallowed by the
Tk class bindings, and Tk offers paste/copy only through a native menu.  This
module implements copy, paste, cut, select-all and clipboard access against the
*widget protocol* (``get``, ``insert``, ``delete``, ``clipboard_get`` …) so the
behaviour is unit-testable on a display-less build and is shared by the buttons,
the keyboard shortcuts and the right-click menu.

Nothing here touches the crypto, audio or stego layers: the only state that
changes is the text inside a widget or the OS clipboard.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

LOGGER: Final = logging.getLogger("phantom.clipboard")

#: A clipboard tool that hangs should not hold up a GUI callback.
CLIPBOARD_TOOL_TIMEOUT: Final = 2.0


@dataclass(slots=True, frozen=True)
class TextBuffer:
    """One logical text field seen through two widgets.

    ``inner`` is the Tk widget that owns the selection and receives events;
    ``public`` is the CustomTkinter wrapper.  Writes must go through the wrapper:
    ``CTkEntry`` keeps its placeholder as *real text* inside the inner entry and
    hides it behind a flag, so writing to the inner widget directly leaves the
    field reading back as empty.
    """

    inner: Any
    public: Any | None = None

    @property
    def editor(self) -> Any:
        """The widget whose ``insert``/``delete``/``get`` we should call."""
        return self.public if self.public is not None else self.inner

    def text(self) -> str:
        return all_text(self)

    def insert(self, value: str) -> bool:
        return insert_text(self, value)

    def delete_selection(self) -> bool:
        return delete_selection(self)

    def selected(self) -> str | None:
        return selected_text(self)

    def select_all(self) -> bool:
        return select_all(self)


def as_buffer(widget: Any) -> TextBuffer:
    """Accept either a :class:`TextBuffer` or a bare widget."""
    if isinstance(widget, TextBuffer):
        return widget
    return TextBuffer(inner=widget)


def _editor(widget: Any) -> Any:
    return as_buffer(widget).editor


def _inner(widget: Any) -> Any:
    return as_buffer(widget).inner


def is_multiline(widget: Any) -> bool:
    """True for a Text-like widget (selection tags, own vertical scrolling)."""
    return callable(getattr(widget, "tag_add", None)) and callable(
        getattr(widget, "yview", None)
    )


def read_clipboard(widget: Any) -> str | None:
    """Return the clipboard text, or ``None`` when it holds no text."""
    getter = getattr(widget, "clipboard_get", None)
    if not callable(getter):
        return None
    try:
        value = getter()
    except Exception as exc:  # noqa: BLE001 - empty clipboard, image, or no owner
        LOGGER.debug("clipboard read failed: %s", exc)
        return None
    if not isinstance(value, str):
        return None
    return value.replace("\r\n", "\n")


def write_clipboard(widget: Any, text: str) -> bool:
    """Put ``text`` on the clipboard; ``False`` when Tk refuses."""
    if not isinstance(text, str) or not text:
        return False
    clear = getattr(widget, "clipboard_clear", None)
    append = getattr(widget, "clipboard_append", None)
    if not callable(clear) or not callable(append):
        return False
    try:
        clear()
        append(text)
    except Exception as exc:  # noqa: BLE001 - Tk dependent, never fatal for the app
        LOGGER.warning("clipboard write failed: %s", exc)
        return False
    return True


def _entry_selection(widget: Any) -> tuple[int, int] | None:
    """Selection bounds of a single-line entry, or ``None`` without selection."""
    index = getattr(widget, "index", None)
    if not callable(index):
        return None
    try:
        first = int(index("sel.first"))
        last = int(index("sel.last"))
    except Exception as exc:  # noqa: BLE001 - TclError when nothing is selected
        LOGGER.debug("entry selection unavailable: %s", exc)
        return None
    if last <= first:
        return None
    return first, last


def selected_text(widget: Any) -> str | None:
    """The selected text, or ``None`` when the selection is empty."""
    inner = _inner(widget)
    if is_multiline(inner):
        try:
            value = inner.get("sel.first", "sel.last")
        except Exception as exc:  # noqa: BLE001 - TclError when nothing is selected
            LOGGER.debug("text selection unavailable: %s", exc)
            return None
        return value if isinstance(value, str) and value else None
    bounds = _entry_selection(inner)
    if bounds is None:
        return None
    try:
        value = _editor(widget).get()
    except Exception as exc:  # noqa: BLE001 - the widget may be destroyed
        LOGGER.debug("entry read failed: %s", exc)
        return None
    if not isinstance(value, str):
        return None
    first, last = bounds
    chunk = value[first:last]
    return chunk or None


def delete_selection(widget: Any) -> bool:
    """Delete the current selection; ``False`` when there is none."""
    inner = _inner(widget)
    if is_multiline(inner):
        try:
            inner.delete("sel.first", "sel.last")
        except Exception as exc:  # noqa: BLE001 - TclError without a selection
            LOGGER.debug("text selection delete skipped: %s", exc)
            return False
        return True
    bounds = _entry_selection(inner)
    if bounds is None:
        return False
    try:
        _editor(widget).delete(*bounds)
    except Exception as exc:  # noqa: BLE001 - the widget may be read-only
        LOGGER.debug("entry selection delete skipped: %s", exc)
        return False
    return True


def select_all(widget: Any) -> bool:
    """Select the whole content so a following copy/cut has something to work on."""
    inner = _inner(widget)
    if is_multiline(inner):
        try:
            inner.tag_add("sel", "1.0", "end-1c")
            inner.mark_set("insert", "1.0")
        except Exception as exc:  # noqa: BLE001 - Tk dependent
            LOGGER.warning("select-all failed: %s", exc)
            return False
        return True
    try:
        _editor(widget).selection_range(0, "end")
        _editor(widget).icursor("end")
    except Exception as exc:  # noqa: BLE001 - Tk dependent
        LOGGER.warning("select-all failed: %s", exc)
        return False
    return True


def insert_text(widget: Any, text: str) -> bool:
    """Insert ``text`` at the caret; ``False`` for a read-only widget."""
    insert = getattr(_editor(widget), "insert", None)
    if not callable(insert) or not text:
        return False
    try:
        insert("insert", text)
    except Exception as exc:  # noqa: BLE001 - disabled Text/Entry raises TclError
        LOGGER.debug("insert refused: %s", exc)
        return False
    return True


def normalise_paste(widget: Any, text: str) -> str:
    """Trim the form-feed/newline noise a clipboard round-trip adds."""
    if is_multiline(_inner(widget)):
        if text.endswith("\n"):
            return text[:-1]
        return text
    # A single-line field cannot hold a newline; password managers add one.
    return text.strip("\r\n")


def paste(widget: Any) -> str | None:
    """Paste the clipboard at the caret, replacing a selection."""
    clipboard = read_clipboard(_inner(widget))
    if not clipboard:
        return None
    text = normalise_paste(widget, clipboard)
    if not text:
        return None
    delete_selection(widget)
    if not insert_text(widget, text):
        return None
    return text


def copy(widget: Any) -> str | None:
    """Copy the selection; returns what was copied, or ``None``."""
    text = selected_text(widget)
    if text is None:
        return None
    return text if write_clipboard(_inner(widget), text) else None


def cut(widget: Any) -> str | None:
    """Copy the selection and remove it from the widget."""
    text = selected_text(widget)
    if text is None:
        return None
    if not write_clipboard(_inner(widget), text):
        return None
    delete_selection(widget)
    return text


def all_text(widget: Any) -> str:
    """Whole content of the widget (empty string when unreadable)."""
    inner = _inner(widget)
    try:
        value = (
            inner.get("1.0", "end-1c") if is_multiline(inner) else _editor(widget).get()
        )
    except Exception as exc:  # noqa: BLE001 - the widget may be destroyed
        LOGGER.debug("widget read failed: %s", exc)
        return ""
    return value if isinstance(value, str) else ""


@dataclass(slots=True, frozen=True)
class ClipboardActions:
    """Bound copy/paste/cut/select-all for one widget (used by menus)."""

    widget: Any

    def paste(self) -> bool:
        return paste(self.widget) is not None

    def copy(self) -> bool:
        return copy(self.widget) is not None

    def cut(self) -> bool:
        return cut(self.widget) is not None

    def select_all(self) -> bool:
        return select_all(self.widget)

    def text(self) -> str:
        return all_text(self.widget)



def clear_command(
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...] | None:
    """This system's own way of emptying the clipboard, or ``None``.

    macOS and Windows ship one; on Linux the answer depends on the session, so
    Wayland is asked first and X11 tools are the fallback.
    """
    system = sys.platform if platform is None else platform
    if system == "darwin":
        return ("pbcopy",)
    if system.startswith("win"):
        return ("clip",)
    for candidate in (
        ("wl-copy", "--clear"),
        ("xclip", "-selection", "clipboard", "-i", "/dev/null"),
        ("xsel", "--clipboard", "--delete"),
    ):
        if which(candidate[0]):
            return candidate
    return None


def clear_clipboard(
    *,
    runner: Callable[..., Any] = subprocess.run,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    timeout: float = CLIPBOARD_TOOL_TIMEOUT,
) -> bool:
    """Empty the OS clipboard through a native tool; ``False`` when there is none.

    Tk's ``clipboard_clear`` only reaches the system clipboard while Tk owns the
    selection, and on macOS the text outlives the window, so the native call is
    the part that really removes a copied password.  No shell is involved: the
    command is an argument vector and the tool gets an empty stdin.
    """
    command = clear_command(platform, which)
    if command is None:
        LOGGER.debug("no native clipboard tool on this system")
        return False
    try:
        runner(
            list(command),
            input=b"",
            timeout=timeout,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001 - a missing tool must not break the app
        LOGGER.warning("native clipboard clear failed: %s", exc)
        return False
    return True
