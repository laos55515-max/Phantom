"""CustomTkinter interface.  Importing this package is Tk-free by design."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from phantom.ui.app import PhantomApp

__all__ = ["PhantomApp", "launch"]


def __getattr__(name: str) -> Any:
    if name == "PhantomApp":
        from phantom.ui.app import PhantomApp  # noqa: PLC0415 - Tk stays lazy

        return PhantomApp
    raise AttributeError(name)


def launch() -> int:
    """Start the GUI (imported lazily so tests never need a display)."""
    from phantom.ui.app import main  # noqa: PLC0415 - Tk stays lazy

    return main()
