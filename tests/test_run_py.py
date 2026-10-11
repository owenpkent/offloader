"""The no-install launcher at the repo root."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUN = REPO / "run.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUN), *args],
        capture_output=True, text=True, cwd=REPO, timeout=120,
    )


def test_arguments_are_forwarded_to_the_cli():
    result = _run("info")
    assert result.returncode == 0
    assert "Offloader" in result.stdout
    assert "checksums:" in result.stdout


def test_argparse_exit_codes_survive_the_hop():
    """A launcher that swallowed the CLI's exit code would break scripting."""
    assert _run("--nope").returncode == 2
    assert _run("verify", "definitely-not-a-path").returncode != 0


def test_subcommand_help_is_the_real_cli_help():
    result = _run("offload", "--help")
    assert result.returncode == 0
    assert "--proxies-first" in result.stdout
    assert "--originals-first" in result.stdout


def test_local_source_is_preferred_over_an_installed_copy():
    """src/ must land at the front, ahead of site-packages."""
    import runpy

    module = runpy.run_path(str(RUN))
    saved = list(sys.path)
    try:
        sys.path.append(str(REPO / "src"))     # a stale entry, behind everything
        module["prefer_local_source"]()
        assert sys.path[0] == str(REPO / "src")
        assert sys.path.count(str(REPO / "src")) == 1, "must not accumulate"
    finally:
        sys.path[:] = saved
