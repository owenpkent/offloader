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
    return config_dir() / name


def read_json(path: Path, default: Any) -> Any:
    """Load JSON, falling back to `default` on anything unreadable.

    Config corruption must never stop someone offloading a card, so a bad file
    is treated as an empty one.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically, so a crash mid-save cannot truncate the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, path)
