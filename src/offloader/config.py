"""Where persistent state lives.

Rolled by hand rather than pulling in platformdirs — it is one function and the
dependency budget for a tool people install on set should stay small.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any

APP_DIR_NAME = "Offloader"


def config_dir() -> Path:
    """Per-user configuration directory, created on demand."""
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif system == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    target = base / APP_DIR_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def config_file(name: str) -> Path:
    """A path for `name` inside the config directory.

    `name` is coerced into the directory rather than joined onto it. pathlib's
    `/` discards the left operand entirely when the right one is absolute, so
    a plain join would put `config_file("D:/x.json")` at the root of D:.
    """
    parts = [part for part in Path(name).parts
             if part not in ("", ".", "..") and not Path(part).anchor]
    if not parts:
        raise ValueError(f"not a usable config file name: {name!r}")
    return config_dir().joinpath(*parts)


def read_json(path: Path, default: Any) -> Any:
    """Load JSON, falling back to `default` on anything unreadable.

    Config corruption must never stop someone offloading a card, so a bad file
    is treated as an empty one. That includes a file that is not valid UTF-8:
    `read_text` raises UnicodeDecodeError, which is a ValueError rather than an
    OSError, and json.JSONDecodeError is a ValueError too.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically, so a crash mid-save cannot truncate the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)
