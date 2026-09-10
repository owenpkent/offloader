# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller definition for the unsigned Offloader Windows directory."""

from __future__ import annotations

import re
import runpy
from importlib.metadata import version as distribution_version
from pathlib import Path

from PyInstaller.utils.hooks import copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)


REPO = Path(SPECPATH).resolve().parents[1]
SRC = REPO / "src"
windows_version = runpy.run_path(str(REPO / "build/windows/versioning.py"))["windows_version"]


def project_version() -> str:
    version_file = SRC / "offloader" / "_version.py"
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']',
        version_file.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise RuntimeError(f"could not read __version__ from {version_file}")
    return match.group(1)


def version_resource(version: str, filename: str, description: str) -> VSVersionInfo:
    numbers = windows_version(version)
    prerelease = bool(re.search(r"(?:a|b|rc)\d+$", version))
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=numbers,
            prodvers=numbers,
            mask=0x3F,
            flags=0x2 if prerelease else 0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([
                StringTable("040904B0", [
                    StringStruct("CompanyName", "Offloader contributors"),
                    StringStruct("FileDescription", description),
                    StringStruct("FileVersion", version),
                    StringStruct("InternalName", Path(filename).stem),
                    StringStruct("LegalCopyright", "Copyright (c) Owen Kent"),
                    StringStruct("OriginalFilename", filename),
                    StringStruct("ProductName", "Offloader"),
                    StringStruct("ProductVersion", version),
                ])
            ]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )


VERSION = project_version()
if distribution_version("offloader") != VERSION:
    raise RuntimeError(
        "installed offloader metadata does not match source version; reinstall the project"
    )
COMMON = dict(
    pathex=[str(SRC)],
    datas=copy_metadata("offloader") + [(str(REPO / "LICENSE"), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["opentimelineio", "otio_fcp_adapter"],
    noarchive=False,
    optimize=0,
)

cli_analysis = Analysis([str(REPO / "build/windows/cli_entry.py")], **COMMON)
gui_analysis = Analysis([str(REPO / "build/windows/gui_entry.py")], **COMMON)

cli_pyz = PYZ(cli_analysis.pure)
gui_pyz = PYZ(gui_analysis.pure)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="offloader-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    version=version_resource(VERSION, "offloader-cli.exe", "Offloader command line"),
)
gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="Offloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    version=version_resource(VERSION, "Offloader.exe", "Offloader desktop application"),
)

COLLECT(
    gui_exe,
    cli_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    cli_analysis.binaries,
    cli_analysis.datas,
    strip=False,
    upx=False,
    name="Offloader",
)
