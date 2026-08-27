# -*- mode: python ; coding: utf-8 -*-
# #WF-1/#WF-2: имя exe = wf-recorder-v<версия> (версия из единого _version.py).
import os
import importlib.util as _il
_s = _il.spec_from_file_location("_version", os.path.join(SPECPATH, "_version.py"))
_v = _il.module_from_spec(_s)
_s.loader.exec_module(_v)
_EXE_NAME = "wf-recorder-v" + _v.__version__


a = Analysis(
    ['wf_recorder_app.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=['wf_pull_client', '_version'],
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
    a.binaries,
    a.datas,
    [],
    name=_EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
