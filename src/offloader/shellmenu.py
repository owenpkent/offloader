"""The Explorer context-menu entry: right-click a card, offload it.

Everything lives under `HKEY_CURRENT_USER\\Software\\Classes`, which is the
per-user half of the same tree `HKEY_CLASSES_ROOT` presents. That choice is the
point: installing the entry needs no administrator, touches nothing another
account can see, and uninstalling is the deletion of two keys.

What it cannot do is appear in the *short* Windows 11 menu. That list is built
from packaged `IExplorerCommand` handlers — a COM object in an MSIX identity —
and a registry verb is by definition a classic one, so it shows under "Show
more options" (or Shift+F10, which opens the classic menu directly). Stated
here and in the command's own output, because a menu entry the operator cannot
find is indistinguishable from one that failed to install.
"""

from __future__ import annotations

import sys
import sysconfig
from dataclasses import dataclass
from pathlib import Path

#: Under HKCU. `Drive` is the card as Explorer presents it — the thing an
#: operator right-clicks in "This PC" — and `Directory` covers a card copied to
#: a folder, or any other tree worth offloading.
CLASSES = "Software\\Classes"
DRIVE_KEY = f"{CLASSES}\\Drive\\shell\\Offloader"
DIRECTORY_KEY = f"{CLASSES}\\Directory\\shell\\Offloader"


@dataclass(frozen=True)
class Entry:
    """One verb: where it lives, what it says, what it runs."""

    key: str
    label: str
    command: str
    icon: str

    @property
    def command_key(self) -> str:
        return f"{self.key}\\command"

    @property
    def applies_to(self) -> str:
        """The shell class this verb hangs off — "Drive" or "Directory"."""
        return self.key.split("\\")[2]


def script_dirs() -> list[Path]:
    """Every directory this interpreter might have put entry points in.

    `sysconfig` rather than a guess from `sys.executable`: a `pip install
    --user` — which is what an editable install without a virtual environment
    usually is — puts the scripts under the user scheme, nowhere near the
    interpreter. Guessing missed exactly that case.
    """
    found: list[Path] = []
    for scheme in ("nt_user", "posix_user"):
        if scheme in sysconfig.get_scheme_names():
            found.append(Path(sysconfig.get_path("scripts", scheme=scheme)))
    found.insert(0, Path(sysconfig.get_path("scripts")))
    here = Path(sys.executable).parent
    found += [here, here / "Scripts"]
    # Preserve order, drop repeats: the default scheme is often one of these.
    return list(dict.fromkeys(found))


def launcher() -> str:
    """The executable the verb should run, quoted for a registry command.

    Prefers the windowed `offloader-gui` script pip installs, so clicking the
    entry opens the app rather than flashing a console. `pythonw` with the
    module is the fallback for a checkout that was never installed.
    """
    name = "offloader-gui.exe" if sys.platform == "win32" else "offloader-gui"
    for directory in script_dirs():
        candidate = directory / name
        if candidate.exists():
            return f'"{candidate}"'
    here = Path(sys.executable).parent
    windowed = here / "pythonw.exe"
    runner = windowed if windowed.exists() else Path(sys.executable)
    return f'"{runner}" -m offloader.gui.app'


def entries(icon: str | Path, command: str | None = None) -> list[Entry]:
    """The verbs to write, as data. Pure, so the plan is testable anywhere."""
    run = command if command is not None else launcher()
    # %V is the clicked item. Unlike %1 it is also correct for a drive root,
    # which is the case this entry exists for.
    invoke = f'{run} "%V"'
    return [
        Entry(DRIVE_KEY, "Offload this card…", invoke, str(icon)),
        Entry(DIRECTORY_KEY, "Offload this folder…", invoke, str(icon)),
    ]


def _require_windows() -> None:
    if sys.platform != "win32":
        raise OSError("the Explorer context menu is a Windows feature; "
                      "nothing to install on this platform")


def install(icon: str | Path, command: str | None = None) -> list[Entry]:
    """Write the verbs. Returns what was written."""
    _require_windows()
    import winreg

    written = entries(icon, command)
    for entry in written:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, entry.key) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, entry.label)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, entry.icon)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, entry.command_key) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, entry.command)
    return written


def uninstall() -> list[str]:
    """Remove the verbs. Returns the keys that were actually there.

    Deletes the `command` subkey first: `DeleteKey` refuses a key with
    children, and a half-removed verb is worse than none.
    """
    _require_windows()
    import winreg

    removed: list[str] = []
    for entry in entries(""):
        for key in (entry.command_key, entry.key):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except FileNotFoundError:
                continue
            except OSError:
                # Left in place rather than reported gone, which is the honest
                # outcome for a key something else is holding open.
                continue
            removed.append(key)
    return removed


def installed() -> bool:
    """Whether the entry is present, by the one key that must exist."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            f"{DRIVE_KEY}\\command"):
            return True
    except OSError:
        return False
