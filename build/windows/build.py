"""Build the unsigned Windows onedir bundle from any working directory."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SPEC = HERE / "offloader.spec"
DIST = REPO / "dist" / "windows"
WORK = REPO / ".pyinstaller" / "windows"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clean",
        action="store_true",
        help="discard PyInstaller's work cache before building",
    )
    args = parser.parse_args()

    if sys.platform != "win32":
        parser.error("the Windows bundle must be built on Windows")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
    ]
    if args.clean:
        command.append("--clean")
    command.append(str(SPEC))
    return subprocess.run(command, cwd=REPO, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
