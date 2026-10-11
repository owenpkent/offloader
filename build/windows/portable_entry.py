"""Frozen single-file graphical entry point for Offloader.

The portable executable unpacks itself to a temporary directory on each
launch. It is not an installation: no lock file, maintenance helper, or
installer manages the folder it was copied to. It therefore skips the
installation lock the bundled programs hold, which would otherwise refuse to
start without a lock file beside the executable.
"""

from __future__ import annotations

from gui_entry import run

if __name__ == "__main__":
    run(lock=False)
