"""Small CustomTkinter building blocks and the single private-API adapter.

CustomTkinter has no public API for a few things the design needs (the canvas
behind a scrollable frame, the inner Tk widget of a textbox/entry, the font
list of the Tk root).  Every such access is isolated here so a future CTk
release only requires changes in one file (D14).
"""

from __future__ import annotations

import logging
import os
import tkinter as tk
import tkinter.font as tkfont
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import customtkinter as ctk

from phantom.ui import theme as T
from phantom.ui.clipboard import ClipboardActions, TextBuffer, paste

LOGGER: Final = logging.getLogger("phantom.widgets")

#: The ``update_idletasks`` CustomTkinter's scrollbar calls on its own canvas
#: inside ``_draw``.  Saved at import time so the context manager below can put it
#: back even if the same method is patched twice.
_CANVAS_FLUSH: Final[Any] = tk.Canvas.update_idletasks

#: The same method as every Tk widget inherits it.  CustomTkinter reaches it from
#: ``CTkBaseClass._set_appearance_mode`` through ``super()``, i.e. once per widget
#: per appearance change.
_MISC_FLUSH: Final[Any] = tk.Misc.update_idletasks

@contextmanager
def quiet_idle_flush() -> Iterator[None]:
    """Build the window without CustomTkinter flushing it once per scrollbar.

    ``CTkScrollbar._draw`` ends with ``self._canvas.update_idletasks()`` — a full
    flush of every pending layout task in the whole window, once per scrollbar
    draw, and a build causes about a hundred of them.  Each flush therefore costs
    more than the one before it, which is the quadratic part of the first frame and
    the reason the window takes longer to appear as the four tabs grow.

    The flush exists only so a scrollbar knows its own size.  During the build that
    question has one answer, and the single flush after the build gives every
    scrollbar the same one; afterwards the method is back exactly as it was.

    ``PHANTOM_QUIET_FLUSH=0`` turns the batching off — the unbatched build, where
    Tk flushes incrementally as it always did.  It exists for one question: when a
    native crash is reported inside ``update_idletasks`` on a machine this project
    cannot reach, running the same case both ways says whether the batching is
    involved before anyone edits a line.
    """

    if os.environ.get("PHANTOM_QUIET_FLUSH", "1") == "0":
        yield
        return

    def _no_flush(_self: Any, *_args: Any, **_kwargs: Any) -> None:
        return None

    canvas_type: Any = tk.Canvas
    canvas_type.update_idletasks = _no_flush
    try:
        yield
    finally:
        canvas_type.update_idletasks = _CANVAS_FLUSH


@dataclass
class _QuietState:
    """How deep the refusal is nested, and how many passes it has refused."""

    depth: int = 0
    refused: int = 0


#: One counter for the whole process: a theme switch can nest (a redraw asked for
#: from inside a redraw), and a test wants the total.
_QUIET: Final[_QuietState] = _QuietState()


def suppressed_flush_count() -> int:
    """How many display passes have been refused while a redraw was in progress."""
    return _QUIET.refused


def _flush_refused(_widget: Any, *_args: Any, **_kwargs: Any) -> None:
    _QUIET.refused += 1


@contextmanager
def quiet_redraws() -> Iterator[None]:
    """Repaint without asking Tk for pixels from inside the callback doing it.

    ``customtkinter.set_appearance_mode`` walks every registered widget
    *synchronously*, and ``CTkBaseClass._set_appearance_mode`` ends with
    ``super().update_idletasks()`` — one full display pass per widget, all of them
    inside the click handler that asked for the new mode.  A window of this size
    makes that a few hundred nested passes.

    ``update idletasks`` runs Tk's display idle handlers, and on macOS that is where
    native crashes live: Tk 9.1 on Aqua segfaults inside exactly this call when it
    comes from a button command (the script-level flush sits inside Aqua's own idle
    drawing loop, and idle tasks there hold window pointers that are freed under
    them — see the open Aqua tickets quoted in ``PhantomApp._flush_layout``).
    Nothing on screen depends on those passes: every widget has already recoloured
    itself, and Tk paints when the callback returns.  The window still owes one pass
    and runs it from a task of its own
    (:meth:`PhantomApp._schedule_layout_flush`).

    ``PHANTOM_QUIET_FLUSH=0`` turns the refusal off — the old behaviour, where a
    theme click flushes once per widget.  It exists for the machine this project
    cannot reach: run the case both ways and the difference is the answer.
    """
    if os.environ.get("PHANTOM_QUIET_FLUSH", "1") == "0":
        yield
        return

    misc_type: Any = tk.Misc
    canvas_type: Any = tk.Canvas
    _QUIET.depth += 1
    if _QUIET.depth == 1:
        misc_type.update_idletasks = _flush_refused
        canvas_type.update_idletasks = _flush_refused
    try:
        yield
    finally:
        _QUIET.depth -= 1
        if _QUIET.depth == 0:
            misc_type.update_idletasks = _MISC_FLUSH
            canvas_type.update_idletasks = _CANVAS_FLUSH


#: ``tkfont.families(root)`` costs a full enumeration of the system's fonts —
#: milliseconds on macOS, where a machine carries hundreds of them — and it used
#: to run once per widget, because every ``sans_font()``/``mono_font()`` call
#: looked the family up again.  One answer per Tk root is enough.
_FAMILIES: Final[weakref.WeakKeyDictionary[Any, frozenset[str]]] = (
    weakref.WeakKeyDictionary()
)

#: ``CTkFont`` objects are immutable once created and shared by many widgets of
#: the same root, so one object per ``(family, size, weight)`` is enough.  This
#: is what keeps a language switch and a repaint from rebuilding ~100 fonts.
_FONTS: Final[weakref.WeakKeyDictionary[Any, dict[tuple[str, int, str], Any]]] = (
    weakref.WeakKeyDictionary()
)


def reset_font_cache() -> None:
    """Forget cached families and fonts (tests build several roots)."""
    _FAMILIES.clear()
    _FONTS.clear()


class CTkAdapter:
    """The only place that reads CustomTkinter private attributes."""

    @staticmethod
    def scroll_canvas(scrollable_frame: Any) -> Any | None:
        canvas = getattr(scrollable_frame, "_parent_canvas", None)
        if canvas is None:
            LOGGER.debug("CTk scrollable frame without _parent_canvas")
        return canvas

    @staticmethod
    def inner_textbox(textbox: Any) -> Any | None:
        inner = getattr(textbox, "_textbox", None)
        if inner is None:
            LOGGER.debug("CTk textbox without _textbox attribute")
        return inner

    @staticmethod
    def inner_entry(entry: Any) -> Any | None:
        inner = getattr(entry, "_entry", None)
        if inner is None:
            LOGGER.debug("CTk entry without _entry attribute")
        return inner

    @staticmethod
    def families(root: Any) -> frozenset[str]:
        """Every font family Tk knows, enumerated once per root."""
        cached = _FAMILIES.get(root)
        if cached is not None:
            return cached
        try:
            available = frozenset(tkfont.families(root))
        except Exception as exc:  # noqa: BLE001 - pragma: no cover, Tk dependent
            LOGGER.warning("cannot list font families: %s", exc)
            return frozenset()
        _FAMILIES[root] = available
        return available

    @staticmethod
    def family(root: Any, candidates: Sequence[str], fallback: str) -> str:
        available = CTkAdapter.families(root)
        for candidate in candidates:
            if candidate in available:
                return candidate
        return fallback

    @staticmethod
    def font(
        root: Any,
        candidates: Sequence[str],
        fallback: str,
        size: int,
        weight: str = "normal",
    ) -> Any:
        """A cached ``CTkFont`` for this root, family, size and weight."""
        family = CTkAdapter.family(root, candidates, fallback)
        key = (family, int(size), str(weight))
        cache = _FONTS.setdefault(root, {})
        font = cache.get(key)
        if font is None:
            font = ctk.CTkFont(family=family, size=size, weight=weight)
            cache[key] = font
        return font


def display_font(root: Any, size: int, *, weight: str = "normal") -> ctk.CTkFont:
    return CTkAdapter.font(root, T.FONT_DISPLAY, T.FONT_DISPLAY[1], size, weight)


def sans_font(root: Any, size: int, *, weight: str = "normal") -> ctk.CTkFont:
    return CTkAdapter.font(root, T.FONT_SANS, T.FONT_SANS[1], size, weight)


def mono_font(root: Any, size: int, *, weight: str = "normal") -> ctk.CTkFont:
    return CTkAdapter.font(root, T.FONT_MONO, T.FONT_MONO[1], size, weight)


def fit_button_on_change(button: Any, *, font: Any = None, minimum: int = 0) -> Any:
    """Measure the caption now and after every translated change of it.

    Imported lazily: :mod:`phantom.ui.responsive` measures text through this
    module, so a module-level import in either direction would be a cycle.
    """
    from phantom.ui.responsive import fit_button_on_change as fit  # noqa: PLC0415

    return fit(button, font=font, minimum=minimum)


def card(master: Any, **kwargs: Any) -> ctk.CTkFrame:
    options: dict[str, Any] = {
        "fg_color": T.CLR_CARD,
        "corner_radius": 18,
        "border_width": 1,
        "border_color": T.CLR_BORDER,
    }
    options.update(kwargs)
    return ctk.CTkFrame(master, **options)


def card_title(master: Any, root: Any, text: str = "") -> ctk.CTkLabel:
    return ctk.CTkLabel(
        master,
        text=text,
        font=sans_font(root, T.FONT_SIZE_CARD_TITLE, weight="bold"),
        text_color=T.CLR_MUTED,
        anchor="w",
        height=22,
    )


def hint_label(master: Any, root: Any, text: str = "", *, wrap: int = 520) -> ctk.CTkLabel:
    """A dim paragraph; ``wrap`` is only the first guess.

    The real ``wraplength`` follows the width of the card (see
    :class:`phantom.ui.responsive.Responsive`): a fixed 520 or 760 was right for
    one window size and wrong for every other.
    """
    label = ctk.CTkLabel(
        master,
        text=text,
        font=sans_font(root, T.FONT_SIZE_HINT),
        text_color=T.CLR_TEXT_DIM,
        anchor="w",
        justify="left",
        wraplength=wrap,
    )
    label._phantom_autowrap = wrap
    return label


def value_label(master: Any, root: Any, text: str = "", *, mono: bool = False) -> ctk.CTkLabel:
    """A value line (path, hash, count): wraps instead of being cut off."""
    label = ctk.CTkLabel(
        master,
        text=text,
        font=(mono_font if mono else sans_font)(
            root, T.FONT_SIZE_MONO if mono else T.FONT_SIZE_BODY
        ),
        text_color=T.CLR_TEXT,
        anchor="w",
        justify="left",
        wraplength=760,
    )
    label._phantom_autowrap = 760
    return label


def set_enabled(button: Any, enabled: bool) -> None:
    """Enable or disable a button so that its state is visible, not just true.

    CustomTkinter greys only the caption of a disabled button; the fill keeps the
    accent colour, so a blocked "save" looked available and a click did nothing.
    Accent buttons record their fill on creation and get a muted one here.
    """
    accent = getattr(button, "_phantom_accent", None)
    if enabled:
        button.configure(state="normal")
        if accent is not None:
            button.configure(fg_color=accent)
        return
    if accent is not None:
        button.configure(fg_color=T.CLR_DISABLED_FILL)
    button.configure(state="disabled")


def primary_button(master: Any, root: Any, text: str, command: Any, **kwargs: Any) -> ctk.CTkButton:
    """The accent button, wide enough for its caption in every language.

    ``width`` in ``kwargs`` becomes a *minimum*: the measured width wins, so a
    button never has to clip its own text (see :mod:`phantom.ui.responsive`).
    """
    minimum = int(kwargs.get("width", 0) or 0)
    font = sans_font(root, T.FONT_SIZE_BODY, weight="bold")
    options: dict[str, Any] = {
        "text": text,
        "command": command,
        "height": 46,
        "corner_radius": 12,
        "font": font,
        "fg_color": T.CLR_ACCENT,
        "hover_color": T.CLR_ACCENT_HOVER,
        "text_color": T.CLR_ON_ACCENT,
    }
    options.update(kwargs)
    button = ctk.CTkButton(master, **options)
    #: Remembered so :func:`set_enabled` can restore the accent after a disable.
    setattr(button, "_phantom_accent", T.CLR_ACCENT)  # noqa: B010 - widget marker
    return fit_button_on_change(button, font=font, minimum=minimum)


def secondary_button(
    master: Any, root: Any, text: str, command: Any, **kwargs: Any
) -> ctk.CTkButton:
    minimum = int(kwargs.get("width", 0) or 0)
    font = sans_font(root, T.FONT_SIZE_HINT)
    options: dict[str, Any] = {
        "text": text,
        "command": command,
        "height": 38,
        "corner_radius": 10,
        "font": font,
        "fg_color": T.CLR_CARD_ALT,
        "hover_color": T.CLR_ACCENT_DIM,
        "text_color": T.CLR_TEXT,
        "border_width": 1,
        "border_color": T.CLR_BORDER,
    }
    options.update(kwargs)
    button = ctk.CTkButton(master, **options)
    return fit_button_on_change(button, font=font, minimum=minimum)


def separator(master: Any, *, height: int = 1) -> ctk.CTkFrame:
    return ctk.CTkFrame(master, height=height, fg_color=T.CLR_BORDER, corner_radius=0)


# ---------------------------------------------------------------------------
# text editing: clipboard shortcuts, context menu
# ---------------------------------------------------------------------------
#: ``(key, ClipboardActions method)`` pairs bound for Cmd and Ctrl.
SHORTCUT_KEYS: Final = (
    ("c", "copy"),
    ("v", "paste"),
    ("x", "cut"),
    ("a", "select_all"),
)

#: Right-click / two-finger tap on macOS, right-click elsewhere.
MENU_SEQUENCES: Final = ("<Button-2>", "<Button-3>", "<Control-Button-1>")

MENU_LABELS: Final = ("menu_paste", "menu_copy", "menu_cut", "menu_select_all")


def _appearance_color(pair: Any) -> str:
    """Pick the light or dark half of a theme tuple for the current mode."""
    if not isinstance(pair, tuple) or len(pair) < 2:
        return str(pair)
    dark = ctk.get_appearance_mode() == "Dark"
    return str(pair[1] if dark else pair[0])


def inner_text_widget(widget: Any) -> Any:
    """The Tk widget that really holds the text (CTkEntry/CTkTextbox wrapper)."""
    return CTkAdapter.inner_textbox(widget) or CTkAdapter.inner_entry(widget) or widget


def text_buffer(widget: Any) -> TextBuffer:
    """Pair the inner Tk widget with its CustomTkinter wrapper.

    Writes go through the wrapper so ``CTkEntry`` can drop its placeholder text
    first; selections and key events stay on the inner widget.
    """
    inner = inner_text_widget(widget)
    return TextBuffer(inner=inner, public=None if inner is widget else widget)


@dataclass(slots=True)
class TextShortcuts:
    """Cmd/Ctrl + C/V/X/A and a right-click menu for one text widget."""

    widget: Any
    labels_provider: Callable[[], Mapping[str, str]] | None = None
    buffer: TextBuffer | None = None
    actions: ClipboardActions = field(init=False)
    sequences: list[str] = field(default_factory=list)
    _menu: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.buffer is None:
            self.buffer = TextBuffer(inner=self.widget)
        self.actions = ClipboardActions(widget=self.buffer)

    @property
    def bound_count(self) -> int:
        return len(self.sequences)

    def copy(self, _event: Any = None) -> str:
        self.actions.copy()
        return "break"

    def paste(self, _event: Any = None) -> str:
        self.actions.paste()
        return "break"

    def cut(self, _event: Any = None) -> str:
        self.actions.cut()
        return "break"

    def select_all(self, _event: Any = None) -> str:
        self.actions.select_all()
        return "break"

    def menu_labels(self) -> dict[str, str]:
        if self.labels_provider is None:
            return {
                "menu_paste": "Paste",
                "menu_copy": "Copy",
                "menu_cut": "Cut",
                "menu_select_all": "Select all",
            }
        try:
            return dict(self.labels_provider())
        except Exception as exc:  # noqa: BLE001 - labels must never break the menu
            LOGGER.warning("context menu labels failed: %s", exc)
            return {}

    def build_menu(self) -> Any:
        """Create the context menu for this widget (Tk ``Menu``)."""
        labels = self.menu_labels()
        menu = tk.Menu(self.widget, tearoff=0)
        try:
            menu.configure(
                bg=_appearance_color(T.CLR_CARD),
                fg=_appearance_color(T.CLR_TEXT),
                activebackground=_appearance_color(T.CLR_ACCENT),
                activeforeground=_appearance_color(T.CLR_ON_ACCENT),
                borderwidth=0,
            )
        except Exception as exc:  # noqa: BLE001 - Aqua ignores most menu colours
            LOGGER.debug("menu colours not supported: %s", exc)
        commands = (
            ("menu_paste", self.paste),
            ("menu_copy", self.copy),
            ("menu_cut", self.cut),
            ("menu_select_all", self.select_all),
        )
        for index, (key, command) in enumerate(commands):
            if index == 3:
                menu.add_separator()
            menu.add_command(label=labels.get(key, key), command=command)
        return menu

    def popup(self, event: Any) -> str:
        """Show the context menu at the pointer; returns ``"break"``."""
        try:
            self._menu = self.build_menu()
            self._menu.tk_popup(
                int(getattr(event, "x_root", 0)), int(getattr(event, "y_root", 0))
            )
        except Exception as exc:  # noqa: BLE001 - no display, or Tk refuses
            LOGGER.warning("context menu failed: %s", exc)
        finally:
            menu = self._menu
            if menu is not None:
                try:
                    menu.grab_release()
                except Exception as exc:  # noqa: BLE001 - release is best effort
                    LOGGER.debug("menu grab release failed: %s", exc)
        return "break"

    def install(self) -> int:
        """Bind the shortcuts and the menu; returns the number of bindings."""
        for key, method in SHORTCUT_KEYS:
            handler = getattr(self, method)
            for sequence in (
                f"<Command-{key}>",
                f"<Control-{key}>",
                f"<Command-{key.upper()}>",
                f"<Control-{key.upper()}>",
            ):
                try:
                    self.widget.bind(sequence, handler)
                except Exception as exc:  # noqa: BLE001 - destroyed widget
                    LOGGER.debug("shortcut %s not bound: %s", sequence, exc)
                    continue
                self.sequences.append(sequence)
        for sequence in MENU_SEQUENCES:
            try:
                self.widget.bind(sequence, self.popup)
            except Exception as exc:  # noqa: BLE001 - platform dependent
                LOGGER.debug("menu binding %s not bound: %s", sequence, exc)
        return self.bound_count


def paste_into(widget: Any) -> str | None:
    """Paste the clipboard into a CTk entry/textbox, placeholder included."""
    return paste(text_buffer(widget))


def install_text_shortcuts(
    root: Any,
    widget: Any,
    *,
    labels_provider: Callable[[], Mapping[str, str]] | None = None,
) -> TextShortcuts:
    """Give an entry/textbox working Cmd/Ctrl shortcuts and a context menu.

    ``root`` is accepted for symmetry with the other helpers and for logging;
    the bindings are installed on the inner Tk widget so CustomTkinter's own
    proxies cannot swallow them.
    """
    inner = inner_text_widget(widget)
    shortcuts = TextShortcuts(
        widget=inner,
        labels_provider=labels_provider,
        buffer=TextBuffer(inner=inner, public=None if inner is widget else widget),
    )
    shortcuts.install()
    LOGGER.debug(
        "text shortcuts on %s: %d keyboard bindings, root=%r",
        type(widget).__name__,
        shortcuts.bound_count,
        root,
    )
    return shortcuts
