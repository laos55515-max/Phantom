"""Full-size, read-only view of an extraction/encryption result.

The result panel inside the main window is intentionally small: it sits under
the carrier card and must not push the primary action button off screen.  That
makes long text and long file paths hard to read, which is the complaint this
window answers.

``ResultWindow`` opens a top-level window at ~80 % of the screen with the same
text in a monospaced, selectable, read-only textbox, plus the three actions a
user actually needs there: copy the text, copy the path, and reveal the saved
file in the platform file manager.  ``Cmd/Ctrl + C/A`` and the right-click menu
work because the textbox goes through :func:`phantom.ui.widgets.install_text_shortcuts`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

import customtkinter as ctk

from phantom.core import files as fsutil
from phantom.ui import theme as T
from phantom.ui.layout import MIN_WINDOW, window_geometry
from phantom.ui.widgets import (
    install_text_shortcuts,
    mono_font,
    primary_button,
    sans_font,
    secondary_button,
)

LOGGER: Final = logging.getLogger("phantom.result")

class ResultWindow(ctk.CTkToplevel):  # type: ignore[misc]
    """Read-only, resizable view of a result with copy/reveal actions."""

    def __init__(
        self,
        master: Any,
        *,
        title: str,
        text: str,
        labels: Mapping[str, str],
        path: str | Path | None = None,
        copy_callback: Callable[[str], bool] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.labels = dict(labels)
        self.result_text = text
        self.path: Path | None = Path(path) if path else None
        self._copy_callback = copy_callback
        self._on_close = on_close
        self._closed = False

        self.title(title)
        self.configure(fg_color=T.CLR_BG)
        width, height, x, y = window_geometry(
            self.winfo_screenwidth(), self.winfo_screenheight()
        )
        self.requested_geometry: str = f"{width}x{height}+{x}+{y}"
        self.geometry(self.requested_geometry)
        self.minsize(*MIN_WINDOW)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self._build()
        self._bind_keys()
        self.after(60, self._focus_self)

    # -- construction ---------------------------------------------------
    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        heading = ctk.CTkLabel(
            self,
            text=self.labels.get("result_window_title", ""),
            font=sans_font(self, 14, weight="bold"),
            text_color=T.CLR_TEXT,
            anchor="w",
        )
        heading.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 4))

        meta = ctk.CTkLabel(
            self,
            text=str(self.path) if self.path is not None else "",
            font=mono_font(self, 11),
            text_color=T.CLR_MUTED,
            anchor="w",
            justify="left",
        )
        meta.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 8))
        self.path_label = meta

        self.text_box = ctk.CTkTextbox(
            self,
            font=mono_font(self, 13),
            fg_color=T.CLR_FIELD,
            border_color=T.CLR_BORDER,
            border_width=1,
            corner_radius=12,
            wrap="word",
        )
        self.text_box.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 8))
        self.text_box.insert("1.0", self.result_text)
        self.text_box.configure(state="disabled")
        self.shortcuts = install_text_shortcuts(
            self, self.text_box, labels_provider=self._menu_labels
        )

        self.feedback = ctk.CTkLabel(
            self,
            text="",
            font=sans_font(self, 11),
            text_color=T.CLR_TEXT_DIM,
            anchor="w",
        )
        self.feedback.grid(row=3, column=0, sticky="ew", padx=20)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=4, column=0, sticky="ew", padx=20, pady=(6, 18))

        self.copy_text_button = primary_button(
            row,
            self,
            self.labels.get("btn_copy_text", ""),
            self.copy_text,
            height=38,
        )
        self.copy_text_button.pack(side="left")

        self.copy_path_button = secondary_button(
            row, self, self.labels.get("btn_copy_path", ""), self.copy_path
        )
        self.copy_path_button.pack(side="left", padx=(10, 0))

        self.reveal_button = secondary_button(
            row, self, self.labels.get("btn_reveal", ""), self.reveal
        )
        self.reveal_button.pack(side="left", padx=(10, 0))

        self.close_button = secondary_button(
            row, self, self.labels.get("btn_close", ""), self.close
        )
        self.close_button.pack(side="right")

        if self.path is None:
            self.copy_path_button.configure(state="disabled")
            self.reveal_button.configure(state="disabled")
        elif not self.path.exists():
            self.reveal_button.configure(state="disabled")

    def _menu_labels(self) -> Mapping[str, str]:
        return {
            "menu_paste": self.labels.get("menu_paste", ""),
            "menu_copy": self.labels.get("menu_copy", ""),
            "menu_cut": self.labels.get("menu_cut", ""),
            "menu_select_all": self.labels.get("menu_select_all", ""),
        }

    def _bind_keys(self) -> None:
        for sequence in ("<Escape>", "<Command-w>", "<Control-w>"):
            self.bind(sequence, lambda _event: self.close())
        for sequence in ("<Command-c>", "<Control-c>"):
            self.bind(sequence, lambda _event: self.copy_text())
        for sequence in ("<Command-a>", "<Control-a>"):
            self.bind(sequence, lambda _event: _select_all(self.text_box))

    def _focus_self(self) -> None:
        if self._closed:
            return
        try:
            self.lift()
            self.focus_force()
            self.text_box.focus_set()
        except Exception as exc:  # noqa: BLE001 - the window may be gone already
            LOGGER.debug("result window focus failed: %s", exc)

    # -- actions --------------------------------------------------------
    def copy_text(self) -> bool:
        return self._copy(self.result_text, "status_copied")

    def copy_path(self) -> bool:
        if self.path is None:
            self._set_feedback("status_path_missing")
            return False
        return self._copy(str(self.path), "status_path_missing")

    def reveal(self) -> bool:
        if self.path is None:
            self._set_feedback("status_path_missing")
            return False
        if fsutil.reveal_in_file_manager(self.path):
            return self._set_feedback("status_revealed")
        return self._set_feedback("status_reveal_failed")

    def _copy(self, value: str, failure_key: str) -> bool:
        if not value:
            return self._set_feedback(failure_key)
        accepted = False
        if self._copy_callback is not None:
            try:
                accepted = bool(self._copy_callback(value))
            except Exception as exc:  # noqa: BLE001 - copying must never crash the window
                LOGGER.warning("copy callback failed: %s", exc)
                accepted = False
        else:
            try:
                self.clipboard_clear()
                self.clipboard_append(value)
            except Exception as exc:  # noqa: BLE001 - Tk dependent
                LOGGER.warning("clipboard write failed: %s", exc)
                accepted = False
            else:
                accepted = True
        return self._set_feedback("status_copied" if accepted else "status_reveal_failed")

    def _set_feedback(self, key: str) -> bool:
        message = self.labels.get(key, "")
        try:
            self.feedback.configure(text=message)
        except Exception as exc:  # noqa: BLE001 - window already destroyed
            LOGGER.debug("feedback label unavailable: %s", exc)
            return False
        return bool(message)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        callback = self._on_close
        self._on_close = None
        try:
            self.destroy()
        except Exception as exc:  # noqa: BLE001 - already destroyed
            LOGGER.debug("result window destroy failed: %s", exc)
        if callback is not None:
            callback()


def _select_all(textbox: Any) -> str:
    from phantom.ui.clipboard import select_all  # noqa: PLC0415 - tiny helper

    inner = getattr(textbox, "_textbox", textbox)
    select_all(inner)
    return "break"
