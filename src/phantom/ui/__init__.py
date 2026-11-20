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
    """Start the GUI (imported lazily so tests never need a display).

    With ``PHANTOM_STARTUP_DEBUG=1`` the clock starts here, before
    CustomTkinter and numpy are imported, so the first stage of the report is
    the import cost the owner's beachball was hiding.
    """
    import logging  # noqa: PLC0415 - keep the package import cheap
    import os  # noqa: PLC0415

    from phantom.ui.startup import debug_enabled, trace_from_env  # noqa: PLC0415

    trace = trace_from_env()
    if debug_enabled():
        # Timings must be printable before ``app`` (and its logging setup) loads.
        logging.basicConfig(
            level=os.environ.get("PHANTOM_LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    trace.mark("launch")
    from phantom.ui.app import main  # noqa: PLC0415 - Tk stays lazy

    trace.mark("import:gui")
    return main(trace=trace)
