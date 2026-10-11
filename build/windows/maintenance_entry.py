"""Frozen entry point for safe Offloader installation maintenance."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from offloader.installation import (
    InstallationBusyError,
    InstallationError,
    install,
    launch,
    uninstall,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install or remove an Offloader bundle safely.")
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install")
    install_parser.add_argument("--payload", type=Path, required=True)
    install_parser.add_argument("--target", type=Path, required=True)
    uninstall_parser = commands.add_parser("uninstall")
    uninstall_parser.add_argument("--target", type=Path, required=True)
    launch_parser = commands.add_parser("launch")
    launch_parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            install(args.payload, args.target)
        elif args.command == "uninstall":
            uninstall(args.target)
        else:
            launch(args.target)
    except InstallationBusyError:
        print("Offloader is running. Finish or close it before changing the installation.", file=sys.stderr)
        return 3
    except (InstallationError, OSError) as exc:
        print(f"Offloader maintenance failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
