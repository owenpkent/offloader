"""Headless smoke checks for a completed Windows bundle."""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BUNDLE = HERE.parents[1] / "dist" / "windows" / "Offloader"


REPO = HERE.parents[1]


def run(command: list[str], env: dict[str, str], cwd: Path, expected: int = 0) -> str:
    result = subprocess.run(
        command,
        env=env,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != expected:
        raise RuntimeError(
            f"expected exit {expected}, got {result.returncode}: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def source_version() -> str:
    text = (REPO / "src/offloader/_version.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if match is None:
        raise RuntimeError("could not read source version")
    return match.group(1)


def file_version(path: Path) -> str:
    size = ctypes.windll.version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise ctypes.WinError()
    buffer = ctypes.create_string_buffer(size)
    if not ctypes.windll.version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise ctypes.WinError()
    value = ctypes.c_void_p()
    length = ctypes.c_uint()
    query = r"\StringFileInfo\040904B0\FileVersion"
    if not ctypes.windll.version.VerQueryValueW(
        buffer, query, ctypes.byref(value), ctypes.byref(length)
    ):
        raise ctypes.WinError()
    return ctypes.wstring_at(value, length.value).rstrip("\0")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", nargs="?", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--portable", type=Path, help="single-file desktop executable to check")
    args = parser.parse_args()
    cli = args.bundle.resolve() / "offloader-cli.exe"
    gui = args.bundle.resolve() / "Offloader.exe"
    if not cli.is_file() or not gui.is_file():
        parser.error(f"bundle executables not found under {args.bundle}")
    if args.portable is not None and not args.portable.is_file():
        parser.error(f"portable executable not found: {args.portable}")

    with tempfile.TemporaryDirectory(prefix="offloader-smoke-") as temporary:
        root = Path(temporary)
        env = os.environ.copy()
        for name in tuple(env):
            if name.startswith("PYTHON") or name.startswith("QT_"):
                env.pop(name)
        env["APPDATA"] = str(root / "appdata")
        env["QT_QPA_PLATFORM"] = "offscreen"
        system_root = Path(env.get("SystemRoot", r"C:\Windows"))
        env["PATH"] = os.pathsep.join((str(args.bundle.resolve()), str(system_root / "System32")))

        expected_version = source_version()
        version = run([str(cli), "--version"], env, root)
        if version.strip() != f"Offloader {expected_version}":
            raise RuntimeError(f"unexpected version output: {version!r}")
        for executable in (cli, gui):
            if file_version(executable) != expected_version:
                raise RuntimeError(f"wrong FileVersion metadata on {executable}")
        run([str(cli), "info"], env, root)

        gui_env = env.copy()
        gui_env["OFFLOADER_GUI_SMOKE"] = "1"
        gui_log = root / "gui-error.txt"
        gui_env["OFFLOADER_GUI_SMOKE_LOG"] = str(gui_log)
        try:
            run([str(gui)], gui_env, root)
        except RuntimeError:
            if gui_log.exists():
                print(gui_log.read_text(encoding="utf-8"))
            raise

        if args.portable is not None:
            # Run a copy from a folder of its own, as a user would from a
            # download or USB stick. It must start without installation files
            # and must not leave anything beside itself.
            portable_dir = root / "Portable Offloader"
            portable_dir.mkdir()
            portable = portable_dir / args.portable.name
            shutil.copyfile(args.portable, portable)
            if file_version(portable) != expected_version:
                raise RuntimeError(f"wrong FileVersion metadata on {portable.name}")
            portable_env = gui_env.copy()
            portable_env["PATH"] = str(system_root / "System32")
            try:
                run([str(portable)], portable_env, root)
            except RuntimeError:
                if gui_log.exists():
                    print(gui_log.read_text(encoding="utf-8"))
                raise
            left_behind = sorted(p.name for p in portable_dir.iterdir() if p != portable)
            if left_behind:
                raise RuntimeError(f"portable executable wrote beside itself: {left_behind}")

        source = root / "source"
        source.mkdir()
        (source / "clip.bin").write_bytes(bytes(range(256)) * 32)
        destinations = [root / "copy-a", root / "copy-b"]
        run(
            [
                str(cli), "offload", "--source", str(source),
                "--dest", str(destinations[0]), "--dest", str(destinations[1]),
                "--verify", "full", "--generic",
                "--report", "pdf,csv,mhl,html,ascmhl", "--quiet",
            ],
            env,
            root,
        )
        for destination in destinations:
            if (destination / "clip.bin").read_bytes() != (source / "clip.bin").read_bytes():
                raise RuntimeError(f"Copied bytes differ at {destination}")
            names = ("JobReport.mhl",)
            if destination == destinations[0]:
                names += ("JobReport.pdf", "JobReport.csv", "JobReport.html")
            for name in names:
                if not list(destination.rglob(name)):
                    raise RuntimeError(f"Missing {name} at {destination}")
            if not (destination / "ascmhl").is_dir():
                raise RuntimeError(f"Missing ASC MHL history at {destination}")
            run(
                [str(cli), "verify", str(destination), "--allow-cache", "--quiet"],
                env,
                root,
            )

        copied = destinations[0] / "clip.bin"
        data = bytearray(copied.read_bytes())
        data[len(data) // 2] ^= 0xFF
        copied.write_bytes(data)
        run(
            [str(cli), "verify", str(destinations[0]), "--allow-cache", "--quiet"],
            env,
            root,
            expected=1,
        )

        # Exercise the shipped onefile helper without UAC, registry writes,
        # shortcuts, or changes to a real installation.
        from offloader.installation_lock import installation_lock

        helper = args.bundle.resolve() / "offloader-maintenance.exe"
        installed = root / "Installed Offloader"
        install_command = [str(helper), "install", "--payload", str(args.bundle.resolve()),
                           "--target", str(installed)]
        run(install_command, env, root)
        installed_cli = installed / "offloader-cli.exe"
        if run([str(installed_cli), "--version"], env, root).strip() != version.strip():
            raise RuntimeError("Installed CLI version differs from the bundle")
        unrelated = installed / "user-notes.txt"
        unrelated.write_text("preserve this file", encoding="utf-8")
        with installation_lock(installed):
            run(install_command, env, root, expected=3)
            run([str(helper), "uninstall", "--target", str(installed)], env, root, expected=3)
        with installation_lock(installed, exclusive=True):
            run([str(installed_cli), "--version"], env, root, expected=4)
        run(install_command, env, root)
        extracted_helper = root / "maintenance.exe"
        shutil.copyfile(installed / "offloader-maintenance.exe", extracted_helper)
        run([str(extracted_helper), "uninstall", "--target", str(installed)], env, root)
        if installed_cli.exists() or unrelated.read_text(encoding="utf-8") != "preserve this file":
            raise RuntimeError("Uninstall did not preserve the application ownership boundary")

    print(f"smoke checks passed: {args.bundle.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
