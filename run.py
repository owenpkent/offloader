#!/usr/bin/env python3
r"""Run Offloader straight from this checkout, with no install step.

    python run.py                          # launch the desktop app
    python run.py info                     # anything the CLI takes
    python run.py offload --source F:\ --dest D:\video\A006 --name A006
    python run.py --help

`pip install -e .` gives you the `offloader` and `offloader-gui` commands and
stays the right way to use this day to day. This script is for when that has
not happened: a fresh clone, a branch checked out beside an older install, or a
machine where the console scripts are not on PATH.

It puts `src/` at the *front* of the import path, so it always runs the code
sitting next to it. That is the part that earns its keep -- with an editable
install both paths resolve to the same tree and this changes nothing, but with
a regular `pip install offloader` on the machine, `import offloader` would
otherwise find site-packages and quietly run a different version than the one
you are reading.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"


def prefer_local_source() -> None:
    """Make this checkout win over any installed copy of the package."""
    if not SRC.is_dir():
        return
    path = str(SRC)
    # Remove-then-insert rather than a bare insert: re-entry (or a parent
    # process that already exported PYTHONPATH) must not leave the entry
    # sitting behind site-packages, which is the whole failure this avoids.
    while path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)


def main(argv: list[str] | None = None) -> int:
    prefer_local_source()
    argv = list(sys.argv[1:] if argv is None else argv)

    # No arguments means the desktop app -- what someone double-clicking this
    # file wants. Arguments mean the CLI, forwarded untouched so that every
    # flag, subcommand and exit code behaves exactly as `offloader` does.
    if not argv:
        from offloader.gui.app import main as gui_main
        return gui_main([sys.argv[0]])

    from offloader.cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
