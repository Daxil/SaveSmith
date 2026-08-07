# PyInstaller build description.
#
# One console binary with the plugins and the risk database inside it, so a
# person can be handed a single file and use it without Python, uv, or any
# installation at all.
#
# Build:  uv run pyinstaller savesmith.spec
# Result: dist/savesmith  (dist\savesmith.exe on Windows)

a = Analysis(
    ["savesmith/__main__.py"],
    pathex=[],
    binaries=[],
    # Shipped data. savesmith/resources.py finds these through sys._MEIPASS
    # when frozen, which is the whole reason that module exists.
    datas=[("plugins", "plugins")],
    hiddenimports=[
        # Operations register themselves by being imported, and PyInstaller
        # cannot see an import that happens for its side effect alone.
        "savesmith.core.ops.binary",
        "savesmith.core.ops.bnd4",
        "savesmith.core.ops.checksum",
        "savesmith.core.ops.compress",
        "savesmith.core.ops.crypto",
        "savesmith.core.ops.fromsoft",
        "savesmith.core.ops.gvas",
        "savesmith.core.ops.lzstring",
        "savesmith.core.ops.packed",
        "savesmith.core.ops.structured",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here needs a window, and dragging in Tk would triple the size.
    excludes=["tkinter", "test", "unittest", "pydoc_data"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="savesmith",
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
