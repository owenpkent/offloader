"""A running application must exclude maintenance across process boundaries."""

import os
import subprocess
import sys

import pytest

from offloader.installation_lock import (
    INCOMPLETE_NAME,
    LOCK_NAME,
    InstallationBusyError,
    frozen_installation_lock,
    installation_lock,
)


def attempt_lock(root, exclusive=False, crash=False):
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from offloader.installation_lock import installation_lock, InstallationBusyError\n"
        "try:\n"
        f"    with installation_lock(Path(sys.argv[1]), exclusive={exclusive!r}):\n"
        f"        {'os._exit(0)' if crash else 'pass'}\n"
        "except InstallationBusyError:\n"
        "    sys.exit(7)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(root)],
        capture_output=True, text=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode in (0, 7), result.stderr
    return result.returncode


def test_running_applications_share_lock_and_block_maintenance(tmp_path):
    with installation_lock(tmp_path, exclusive=True):
        pass
    with installation_lock(tmp_path):
        assert attempt_lock(tmp_path) == 0
        assert attempt_lock(tmp_path, exclusive=True) == 7
    assert attempt_lock(tmp_path, exclusive=True) == 0


def test_maintenance_blocks_new_launches_and_other_installers(tmp_path):
    with installation_lock(tmp_path, exclusive=True):
        assert attempt_lock(tmp_path) == 7
        assert attempt_lock(tmp_path, exclusive=True) == 7


def test_crashed_process_does_not_leave_stale_lock(tmp_path):
    assert attempt_lock(tmp_path, exclusive=True, crash=True) == 0
    assert attempt_lock(tmp_path, exclusive=True) == 0


def test_launch_requires_existing_lock(tmp_path):
    with pytest.raises(FileNotFoundError):
        with installation_lock(tmp_path):
            pytest.fail("missing installation metadata must not allow startup")


def test_lock_released_after_exception(tmp_path):
    with pytest.raises(ValueError), installation_lock(tmp_path, exclusive=True):
        raise ValueError("failed installation")
    assert attempt_lock(tmp_path, exclusive=True) == 0


def test_incomplete_installation_cannot_launch(tmp_path, monkeypatch):
    (tmp_path / LOCK_NAME).touch()
    (tmp_path / INCOMPLETE_NAME).touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Offloader.exe"))
    with pytest.raises(InstallationBusyError, match="incomplete"):
        with frozen_installation_lock():
            pytest.fail("incomplete installation launched")


def test_lock_rejects_symbolic_links(tmp_path):
    original = tmp_path / "other"
    original.touch()
    try:
        (tmp_path / LOCK_NAME).symlink_to(original)
    except OSError:
        pytest.skip("symlink creation requires Windows developer mode")
    with pytest.raises(OSError, match="reparse"):
        with installation_lock(tmp_path, exclusive=True):
            pytest.fail("linked lock accepted")
    assert original.read_bytes() == b""


@pytest.mark.skipif(os.name != "nt", reason="Windows file-sharing contract")
def test_lock_cannot_be_replaced_while_held(tmp_path):
    with installation_lock(tmp_path, exclusive=True):
        with pytest.raises(PermissionError):
            (tmp_path / LOCK_NAME).unlink()
