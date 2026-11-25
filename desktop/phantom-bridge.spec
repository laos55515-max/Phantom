# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Tauri desktop sidecar (``phantom-bridge``).

Build from the repository root:

    pyinstaller --noconfirm desktop/phantom-bridge.spec

Produces a single-file ``phantom-bridge`` in ``dist/`` which the Tauri build
renames to ``phantom-bridge-<target-triple>`` before bundling as
``externalBin`` (see ``desktop/README.md`` and ``.github/workflows/desktop.yml``).

The sidecar deliberately does *not* import ``phantom.ui``: the frozen build
carries only the core, the stego/audio engines and the example resources.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(".")
SRC = os.path.join(ROOT, "src")
RESOURCES = os.path.join(SRC, "phantom", "resources")

datas = [(RESOURCES, "phantom/resources")]
binaries = []
hiddenimports = (
    collect_submodules("phantom.core")
    + collect_submodules("phantom.stego")
    + collect_submodules("phantom.audio")
    # The Metadata tab reads PNG, WAV and FLAC containers through this package.
    # It was excluded while the shell had no metadata surface; the frozen bridge
    # now ships it, and the submodule collector keeps the lazy imports inside
    # ``phantom.metadata.integrity`` (Pillow, soundfile) in the graph.
    + collect_submodules("phantom.metadata")
)
# soundfile is a single module and ships its libsndfile wheel payload in the
# separate ``_soundfile_data`` package, which the default hook misses.
collected = collect_all("_soundfile_data")
datas += collected[0]
binaries += collected[1]
hiddenimports += collected[2]

a = Analysis(
    [os.path.join(ROOT, "desktop", "phantom-bridge-entry.py")],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["phantom.ui", "customtkinter", "tkinter", "pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="phantom-bridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
