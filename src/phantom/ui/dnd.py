"""Drag-and-drop with mandatory graceful degradation (D14).

Parsing of the Tcl drop payload is a pure function so it is unit-testable on a
headless machine.  The provider tries to import ``tkinterdnd2``; when that
fails (or the Tk build lacks it), the app keeps working, hides the DnD hints and
records the reason in the log — it never crashes and never pretends to support
dropping.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from urllib.parse import unquote, urlparse

LOGGER: Final = logging.getLogger("phantom.dnd")


def _split_tcl_list(data: str) -> list[str]:
    """Parse the ``%D`` Tcl list: braces, backslash escapes and whitespace."""
    tokens: list[str] = []
    current: list[str] = []
    depth = 0
    index = 0
    length = len(data)
    while index < length:
        char = data[index]
        if char == "\\" and index + 1 < length:
            current.append(data[index + 1])
            index += 2
            continue
        if char == "{":
            depth += 1
            index += 1
            continue
        if char == "}":
            depth = max(0, depth - 1)
            index += 1
            continue
        if char.isspace() and depth == 0:
            if current:
                tokens.append("".join(current))
                current = []
            index += 1
            continue
        current.append(char)
        index += 1
    if current:
        tokens.append("".join(current))
    return tokens


def paths_from_uri(token: str) -> str | None:
    """Decode a ``file://`` URI; leave plain paths untouched."""
    if token.startswith("file://"):
        parsed = urlparse(token)
        path = unquote(parsed.path)
        if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return path or None
    return token or None


def parse_drop_paths(raw: str) -> list[str]:
    """Turn a Tk/Tcl drop payload into an ordered list of local paths.

    Handles: one path, several space-separated paths wrapped in braces, paths
    with spaces, newline-separated payloads (Windows) and ``file://`` URIs.
    """
    if not raw:
        return []
    candidates: list[str] = []
    for raw_chunk in raw.replace("\r", "\n").split("\n"):
        chunk = raw_chunk.strip()
        if not chunk:
            continue
        if chunk.startswith("{") or chunk.startswith("file://") or " " in chunk:
            tokens = _split_tcl_list(chunk)
            if not tokens:
                tokens = [chunk]
        else:
            tokens = [chunk]
        for token in tokens:
            decoded = paths_from_uri(token)
            # An empty Tcl list arrives as ``{}`` or whitespace; drop those.
            if decoded and decoded.strip("{} \t"):
                candidates.append(decoded)
    seen: set[str] = set()
    unique: list[str] = []
    for path in candidates:
        normalized = os.path.normpath(path)
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


@dataclass(slots=True)
class DropTarget:
    """A widget registered as a drop zone with its accepted extensions."""

    widget: object
    on_drop: Callable[[list[str]], None]
    extensions: tuple[str, ...]
    on_enter: Callable[[], None] | None = None
    on_leave: Callable[[], None] | None = None


class DndProvider:
    """Optional ``tkinterdnd2`` glue with explicit unavailability reporting."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self._dnd_module: object | None = None
        #: A registration that failed means drops do not work, whatever the module
        #: said: ``tkinterdnd2`` imports fine on Tcl/Tk 9 and its Tcl side does not
        #: load, so every ``drop_target_register`` raises "invalid command name".
        self._registration_failed = False
        #: ``PHANTOM_DND=0`` turns drag-and-drop off before anything is imported.
        #: It is the switch for a machine where the Tk build cannot survive the
        #: tkdnd library, and what the crash diagnosis uses to tell "the window"
        #: apart from "the window plus tkdnd" (tools/diagnose_tk_crash.py).
        if os.environ.get("PHANTOM_DND", "").strip().lower() in {"0", "off", "no", "false"}:
            self.reason = "drag-and-drop disabled by PHANTOM_DND"
            LOGGER.info("drag-and-drop disabled by PHANTOM_DND")
            return
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD  # noqa: PLC0415

            self._dnd_module = TkinterDnD
            self._dnd_constants = DND_FILES
        except Exception as exc:  # noqa: BLE001 - ImportError or TclError from a broken Tk build
            self.reason = f"tkinterdnd2 unavailable: {exc}"
            LOGGER.warning("drag-and-drop disabled: %s", exc)

    @property
    def available(self) -> bool:
        return self._dnd_module is not None

    @property
    def usable(self) -> bool:
        """Drops really work: the module loaded *and* the registration succeeded.

        ``available`` alone is a lie on Tcl/Tk 9, where ``tkinterdnd2`` imports and
        then fails to create its Tcl commands.  The window shows its drop hints from
        ``usable``, and file selection by button never depends on any of this.
        """
        return self._dnd_module is not None and not self._registration_failed

    def make_ctk_root(self) -> object:
        """Create a CustomTkinter root that also accepts drops (or a plain one)."""
        import customtkinter as ctk  # noqa: PLC0415

        if self._dnd_module is None:
            return ctk.CTk()
        try:
            from tkinterdnd2 import TkinterDnD  # noqa: PLC0415

            class _DndRoot(ctk.CTk, TkinterDnD.DnDWrapper):  # type: ignore[misc]
                def __init__(self) -> None:
                    super().__init__()
                    self.TkdndVersion = TkinterDnD._require(self)

            return _DndRoot()
        except Exception as exc:  # noqa: BLE001 - pragma: no cover, depends on Tk build
            self.reason = f"TkDnD root failed: {exc}"
            LOGGER.warning("falling back to a plain CTk root: %s", exc)
            self._dnd_module = None
            self._registration_failed = True
            return ctk.CTk()

    def register(self, target: DropTarget) -> bool:
        if self._dnd_module is None:
            return False
        try:
            from tkinterdnd2 import DND_FILES  # noqa: PLC0415

            widget = target.widget
            widget.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
            widget.dnd_bind(  # type: ignore[attr-defined]
                "<<Drop>>",
                lambda event: target.on_drop(parse_drop_paths(getattr(event, "data", ""))),
            )
            if target.on_enter is not None:
                widget.dnd_bind("<<DropEnter>>", lambda _event: target.on_enter())  # type: ignore[attr-defined]
            if target.on_leave is not None:
                widget.dnd_bind("<<DropLeave>>", lambda _event: target.on_leave())  # type: ignore[attr-defined]
            return True
        except Exception as exc:  # noqa: BLE001 - pragma: no cover, depends on Tk build
            self.reason = f"drop registration failed: {exc}"
            self._registration_failed = True
            LOGGER.warning("cannot register drop target: %s", exc)
            return False


def filter_by_extension(paths: list[str], extensions: tuple[str, ...]) -> list[str]:
    return [p for p in paths if os.path.splitext(p)[1].lower() in extensions]
