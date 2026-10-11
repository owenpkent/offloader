"""Install a built wheel away from the checkout and check its public entry points."""

from __future__ import annotations

import argparse
import os
import runpy
import subprocess
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="Wheel to install in a fresh environment")
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    expected = runpy.run_path(str(ROOT / "src/offloader/_version.py"))["__version__"]
    env = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(name, None)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryDirectory(prefix="offloader-wheel-") as directory:
        work = Path(directory)
        venv.EnvBuilder(with_pip=True).create(work / "venv")
        scripts = work / "venv" / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")

        def run(*command: str) -> str:
            result = subprocess.run(
                command, cwd=work, env=env, check=True, capture_output=True,
                text=True, timeout=180, creationflags=flags,
            )
            return result.stdout.strip()

        run(str(python), "-I", "-m", "pip", "install", str(wheel))
        reported = run(
            str(python), "-I", "-c",
            "from importlib.metadata import version; import offloader; "
            "print(version('offloader')); print(offloader.__version__)",
        )
        if reported.splitlines() != [expected, expected]:
            raise RuntimeError(f"Wheel version mismatch: {reported!r}; expected {expected}")
        cli = scripts / ("offloader.exe" if os.name == "nt" else "offloader")
        if run(str(cli), "--version") != f"Offloader {expected}":
            raise RuntimeError("Installed CLI version differs from the release version")
        run(str(cli), "info")
        run(str(python), "-I", "-m", "offloader", "--help")
    print(f"Wheel install, metadata, and CLI passed: {wheel.name}")


if __name__ == "__main__":
    main()
