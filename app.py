"""PHANTOM launcher kept for backwards compatibility.

The application now lives in the ``phantom`` package (``src/phantom``).
Run either ``python app.py``, ``python -m phantom`` or the installed
``phantom`` console script — all three start the same GUI.
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from phantom.ui import launch  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(launch())
