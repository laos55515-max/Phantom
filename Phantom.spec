# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the package layout (``src/phantom``).

Build with::

    pyinstaller --noconfirm Phantom.spec

The launcher stays at the repository root, so the entry point is unchanged; the
package itself and the locale JSON files are added explicitly because they are
not imported until the UI starts.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(".")
SRC = os.path.join(ROOT, "src")
LOCALES = os.path.join(SRC, "phantom", "locales")

datas = []
binaries = []
hiddenimports = []

for package in ("customtkinter", "tkinterdnd2"):
    collected = collect_all(package)
    datas += collected[0]
    binaries += collected[1]
    hiddenimports += collected[2]

datas += [(os.path.join(LOCALES, name), "phantom/locales") for name in os.listdir(LOCALES)]
hiddenimports += collect_submodules("phantom")

a = Analysis(
    ["app.py"],
    pathex=[SRC],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Phantom",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["mac_logo.icns"],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Phantom",
)
app = BUNDLE(
    coll,
    name="Phantom.app",
    icon="mac_logo.icns",
    bundle_identifier=None,
)
