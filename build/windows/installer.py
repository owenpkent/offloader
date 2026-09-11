"""Assemble an NSIS installer from a verified Windows directory bundle."""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "installer.nsi"
REQUIRED_BUNDLE_FILES = {
    "Offloader.exe",
    "offloader-cli.exe",
    "offloader-maintenance.exe",
    "LICENSE",
}


def find_makensis() -> Path:
    """Return NSIS's compiler, including its normal Windows installation path."""
    found = shutil.which("makensis")
    if found:
        return Path(found)
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        value = os.environ.get(variable)
        if value:
            candidate = Path(value) / "NSIS" / "makensis.exe"
            if candidate.is_file():
                return candidate
    raise RuntimeError("makensis.exe was not found; install NSIS 3.x to build the installer")


def _nsi_value(value: str) -> str:
    if not value or any(char in value for char in ('"', "\r", "\n", "$")):
        raise ValueError("NSIS value contains an unsafe character")
    return value


def _bundle_path(bundle: Path) -> Path:
    bundle = Path(bundle).resolve()
    if not bundle.is_dir():
        raise ValueError(f"bundle is not a directory: {bundle}")
    missing = sorted(name for name in REQUIRED_BUNDLE_FILES if not (bundle / name).is_file())
    if missing:
        raise ValueError(f"bundle is missing required files: {', '.join(missing)}")
    return bundle


def _command_for_uninstaller(sign_command: list[str] | None) -> str:
    if sign_command is None:
        return ""
    if not sign_command or any(not isinstance(part, str) or not part for part in sign_command):
        raise ValueError("sign command must contain executable arguments")
    command = f'{subprocess.list2cmdline(sign_command)} "%1"'
    if any(character in command for character in ("'", "\r", "\n", "$")):
        raise ValueError("sign command contains an unsafe NSIS character")
    return command


def _render(bundle: Path, output: Path, version: str, sign_command: list[str] | None) -> str:
    version_parts = runpy.run_path(str(HERE / "versioning.py"))["windows_version"](version)
    values = {
        "@BUNDLE_DIR@": _nsi_value(str(bundle)),
        "@OUTPUT_FILE@": _nsi_value(str(output)),
        "@PRODUCT_VERSION@": _nsi_value(version),
        "@WINDOWS_VERSION@": ".".join(str(part) for part in version_parts),
        "@UNINSTALL_FINALIZE@": (
            f"!uninstfinalize '{_command_for_uninstaller(sign_command)}' = 0"
            if sign_command is not None else ""
        ),
    }
    source = TEMPLATE.read_text(encoding="utf-8")
    for token, value in values.items():
        source = source.replace(token, value)
    if any(token in source for token in values):
        raise RuntimeError("installer template contains an unexpanded token")
    return source


def build_installer(bundle: Path, output: Path, version: str, *,
                    sign_command: list[str] | None = None) -> Path:
    """Compile ``bundle`` into ``output`` and return the resulting setup path.

    ``sign_command`` signs the generated uninstaller during NSIS compilation.
    The release builder must also sign the resulting setup executable afterwards.
    """
    bundle = _bundle_path(bundle)
    output = Path(output).resolve()
    if output.suffix.lower() != ".exe":
        raise ValueError("installer output must be an .exe file")
    output.parent.mkdir(parents=True, exist_ok=True)
    script = _render(bundle, output, version, sign_command)
    compiler = find_makensis()
    with tempfile.TemporaryDirectory(prefix="offloader-nsis-") as temporary:
        nsi = Path(temporary) / "offloader-installer.nsi"
        nsi.write_text(script, encoding="utf-8", newline="\n")
        result = subprocess.run(
            [str(compiler), "/V2", "/WX", "/INPUTCHARSET", "UTF8", "/NOCONFIG", str(nsi)],
            check=False,
        )
    if result.returncode:
        raise RuntimeError(f"makensis failed with exit code {result.returncode}")
    if not output.is_file():
        raise RuntimeError("makensis completed without creating the installer")
    return output
