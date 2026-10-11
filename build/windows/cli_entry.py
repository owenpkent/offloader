"""Frozen console entry point for Offloader."""

from __future__ import annotations

import importlib.util
import sys
from contextlib import ExitStack


def _timeline_requested(argv: list[str]) -> bool:
    if not argv or "--help" in argv or "-h" in argv:
        return False
    return argv[0] == "resolve" or (
        argv[0] == "offload"
        and any(arg == "--timeline" or arg.startswith("--timeline=") for arg in argv[1:])
    )


def _main() -> int:
    # Timeline adapters are deliberately outside the first Windows bundle.
    # Detect this here so the frozen app does not suggest an unusable pip install.
    if _timeline_requested(sys.argv[1:]) and importlib.util.find_spec("opentimelineio") is None:
        print(
            "Timeline import is not included in this Windows build of Offloader.",
            file=sys.stderr,
        )
        return 4

    from offloader.cli import main as cli_main

    return cli_main()


def main() -> int:
    from offloader.installation_lock import frozen_installation_lock

    with ExitStack() as lifetime:
        try:
            lifetime.enter_context(frozen_installation_lock())
        except OSError as exc:
            print(f"Offloader cannot start: {exc}", file=sys.stderr)
            return 4
        return _main()


if __name__ == "__main__":
    raise SystemExit(main())
