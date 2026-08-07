"""Windows paths longer than MAX_PATH.

Windows caps a path at 260 characters unless the caller opts out with the
`\\\\?\\` prefix. Camera-original trees hit that easily: a dated project folder,
a reel, a camera letter, a `Proxy` subdirectory and a long clip name add up, and
a destination on a NAS share adds more. Without this the copy fails with
"The system cannot find the path specified" on a path that plainly exists.

The prefix has sharp edges — it turns off all path normalisation, so the path
must already be absolute, backslash-separated and free of `.` and `..` — which
is why it is applied through these helpers rather than sprinkled at call sites.

It is applied only when a path is close to the limit. Below that the ordinary
form is used, so the common case behaves exactly as it always did and the
extended form is not a new variable in every code path.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import BinaryIO

_WINDOWS = platform.system() == "Windows"

#: Windows' documented limit is 260 including the NUL. Convert below that so a
#: path that grows slightly during the operation is still covered.
THRESHOLD = 240

_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def needs_extended(path: os.PathLike | str) -> bool:
    return _WINDOWS and len(str(path)) >= THRESHOLD


def extended(path: os.PathLike | str) -> str:
    """The form of `path` that Windows will accept at any length.

    A no-op off Windows, on short paths, and on paths already prefixed.
    """
    text = str(path)
    if not _WINDOWS or text.startswith(_PREFIX) or not needs_extended(text):
        return text

    # The prefix disables normalisation, so normalise first or `..` and `/`
    # would be passed to the filesystem verbatim.
    absolute = os.path.abspath(text)
    if absolute.startswith(_PREFIX):
        return absolute
    if absolute.startswith("\\\\"):          # UNC: \\server\share -> \\?\UNC\server\share
        return _UNC_PREFIX + absolute[2:]
    return _PREFIX + absolute


def open_binary(path: os.PathLike | str, mode: str = "rb") -> BinaryIO:
    """`open` that works at any path length.

    Goes through the builtin so tests that patch `builtins.open` still see it.
    """
    return open(extended(path), mode)


def makedirs(path: os.PathLike | str, exist_ok: bool = True) -> None:
    os.makedirs(extended(path), exist_ok=exist_ok)


def replace(source: os.PathLike | str, target: os.PathLike | str) -> None:
    os.replace(extended(source), extended(target))


def unlink(path: os.PathLike | str, missing_ok: bool = True) -> None:
    try:
        os.remove(extended(path))
    except FileNotFoundError:
        if not missing_ok:
            raise


def stat(path: os.PathLike | str) -> os.stat_result:
    return os.stat(extended(path))


def exists(path: os.PathLike | str) -> bool:
    try:
        os.stat(extended(path))
        return True
    except (OSError, ValueError):
        return False


def shorten_for_display(path: os.PathLike | str) -> str:
    """Strip the prefix again for anything a human reads."""
    text = str(path)
    if text.startswith(_UNC_PREFIX):
        return "\\\\" + text[len(_UNC_PREFIX):]
    if text.startswith(_PREFIX):
        return text[len(_PREFIX):]
    return text


def probe_support(directory: Path) -> tuple[bool, str]:
    """Can this filesystem actually hold a path past the limit?

    Returns (supported, detail). exFAT and NTFS can; some network filesystems
    silently cannot, and it is better to find out before an offload than
    during one.
    """
    directory = Path(directory)
    deep = directory
    try:
        while len(str(deep)) < THRESHOLD + 60:
            deep = deep / ("longpath" + "x" * 20)
        makedirs(deep)
        probe = deep / "probe.tmp"
        with open_binary(probe, "wb") as handle:
            handle.write(b"probe")
        unlink(probe)
    except OSError as exc:
        return False, str(exc)
    finally:
        # Unwind whatever was created, deepest first.
        current = deep
        while current != directory:
            try:
                os.rmdir(extended(current))
            except OSError:
                break
            current = current.parent
    return True, ""


def os_long_paths_enabled() -> bool | None:
    r"""Whether Windows itself accepts paths past MAX_PATH.

    None off Windows or when the setting cannot be read. The key defaults to 0,
    and when it is off the `\\?\` prefix is the only thing making a deep
    destination work at all.
    """
    if not _WINDOWS:
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return bool(value)
    except (OSError, ImportError, ValueError):
        return None
