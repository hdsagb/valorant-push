# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成单文件、无控制台窗口的 exe。"""
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("pystray")
hiddenimports += [
    "app.vendor.val_shop_ref",
    "PIL.ImageFont",
    "PIL.ImageDraw",
]

a = Analysis(
    ["app/__main__.py"],
    pathex=["."],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ValorantPushPersonal",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
