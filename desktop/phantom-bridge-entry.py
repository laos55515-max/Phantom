"""Entry point for the frozen PHANTOM bridge sidecar (Tauri desktop shell).

PyInstaller analyzes this file instead of ``python -m phantom.bridge`` so the
import graph stays explicit.  See ``desktop/README.md``.
"""

from phantom.bridge import main

if __name__ == "__main__":
    raise SystemExit(main())
