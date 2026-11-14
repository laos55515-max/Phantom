"""PHANTOM — offline payload hiding in PNG images and WAV/FLAC audio.

Public API lives in :mod:`phantom.core.api`; the GUI lives in
:mod:`phantom.ui.app`.  Importing this package must never require a display.
"""

from __future__ import annotations

from phantom.core.constants import APP_NAME, APP_VERSION

__all__ = ["APP_NAME", "APP_VERSION", "__version__"]
__version__ = APP_VERSION
