# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification file for building Windows standalone desktop executable.

Usage:
    pyinstaller dist_build/build_windows.spec --clean --noconfirm
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Collect data files
datas = [
    (str(PROJECT_ROOT / ".cache"), ".cache"),
    (str(PROJECT_ROOT / ".env.example"), "."),
]
datas += collect_data_files("playwright_stealth")

# Collect hidden imports
hiddenimports = [
    "playwright",
    "playwright.sync_api",
    "playwright_stealth",
    "loguru",
    "pandas",
    "openpyxl",
    "dotenv",
    "requests",
    "core",
    "config",
    "scraper",
    "services",
    "ui_bridge",
    "exporters",
]
hiddenimports += collect_submodules("openpyxl")
hiddenimports += collect_submodules("pandas")

a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter.test", "unittest"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="hotels_scraper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # Set to False if bundling purely windowed desktop UI
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="hotels_scraper",
)

