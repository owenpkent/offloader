"""The installer may only replace files it can identify exactly."""

from __future__ import annotations

import json

import pytest

from offloader.installation import (
    LOCK_NAME,
    MANIFEST_NAME,
    InstallationBusyError,
    InstallationError,
    install,
    launch,
    uninstall,
)
from offloader.installation_lock import installation_lock


def _payload(root, text: str = "first"):
    payload = root / "payload"
    (payload / "runtime").mkdir(parents=True)
    (payload / "Offloader.exe").write_text(text, encoding="utf-8")
    (payload / "runtime" / "support.dll").write_text("support", encoding="utf-8")
    (payload / "offloader-maintenance.exe").write_text("helper", encoding="utf-8")
    return payload


class _SimulatedStop(BaseException):
    """A process stop that intentionally bypasses the maintenance exception handler."""


def test_install_upgrade_and_uninstall_preserve_unowned_file(tmp_path):
    target = tmp_path / "installed"
    first = _payload(tmp_path / "one")
    install(first, target)
    extra = target / "readme-from-user.txt"
    extra.write_text("keep me", encoding="utf-8")
    second = _payload(tmp_path / "two", "second")

    install(second, target)

    assert (target / "Offloader.exe").read_text(encoding="utf-8") == "second"
    assert extra.read_text(encoding="utf-8") == "keep me"
    uninstall(target)
    assert extra.read_text(encoding="utf-8") == "keep me"
    assert not (target / MANIFEST_NAME).exists()
    assert (target / LOCK_NAME).exists()


def test_install_refuses_unowned_collision_without_changing_existing_file(tmp_path):
    target = tmp_path / "installed"
    target.mkdir()
    existing = target / "Offloader.exe"
    existing.write_text("not ours", encoding="utf-8")

    with pytest.raises(InstallationError, match="nonempty unowned"):
        install(_payload(tmp_path / "payload"), target)

    assert existing.read_text(encoding="utf-8") == "not ours"


def test_install_refuses_malformed_manifest_and_keeps_files(tmp_path):
    target = tmp_path / "installed"
    target.mkdir()
    manifest = target / MANIFEST_NAME
    manifest.write_text('{"format": 1, "files": {"../outside": {}}}', encoding="utf-8")

    with pytest.raises(InstallationError):
        install(_payload(tmp_path / "payload"), target)

    assert manifest.exists()


def test_payload_cannot_claim_installer_manifest(tmp_path):
    payload = _payload(tmp_path / "payload")
    (payload / MANIFEST_NAME).write_text("not payload-owned", encoding="utf-8")

    with pytest.raises(InstallationError, match="reserved"):
        install(payload, tmp_path / "installed")


def test_failed_upgrade_restores_previous_inventory(tmp_path, monkeypatch):
    target = tmp_path / "installed"
    install(_payload(tmp_path / "first", "old"), target)
    replacement = _payload(tmp_path / "second", "new")
    import offloader.installation as installation

    real_replace = installation.os.replace

    def fail_promotion(source, destination):
        if str(source).endswith("Offloader.exe") and ".offloader-stage-" in str(source):
            raise OSError("simulated disk failure")
        return real_replace(source, destination)

    monkeypatch.setattr(installation.os, "replace", fail_promotion)
    with pytest.raises(OSError, match="simulated disk failure"):
        install(replacement, target)

    assert (target / "Offloader.exe").read_text(encoding="utf-8") == "old"
    assert not (target / ".offloader-installing").exists()
    assert json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))["format"] == 1


def test_active_application_lock_blocks_maintenance(tmp_path):
    target = tmp_path / "installed"
    payload = _payload(tmp_path / "payload")
    install(payload, target)

    with installation_lock(target):
        with pytest.raises(InstallationBusyError):
            install(payload, target)


def test_uninstall_refuses_changed_managed_file(tmp_path):
    target = tmp_path / "installed"
    install(_payload(tmp_path / "payload"), target)
    app = target / "Offloader.exe"
    app.write_text("changed", encoding="utf-8")

    with pytest.raises(InstallationError, match="changed"):
        uninstall(target)

    assert app.read_text(encoding="utf-8") == "changed"


def test_launch_only_uses_the_verified_installed_desktop_executable(tmp_path, monkeypatch):
    target = tmp_path / "installed"
    install(_payload(tmp_path / "payload"), target)
    import offloader.windows_launch as windows_launch

    launched = []
    monkeypatch.setattr(windows_launch, "launch_as_desktop_user", launched.append)

    launch(target)

    assert launched == [target / "Offloader.exe"]


def test_recovery_reconciles_stop_after_old_file_move_before_journal_save(tmp_path, monkeypatch):
    target = tmp_path / "installed"
    first = _payload(tmp_path / "first", "old")
    second = _payload(tmp_path / "second", "new")
    install(first, target)
    import offloader.installation as installation

    real_replace = installation.os.replace

    def stop_after_backup(source, destination):
        result = real_replace(source, destination)
        if str(source).endswith("Offloader.exe") and ".offloader-backup-" in str(destination):
            raise _SimulatedStop()
        return result

    monkeypatch.setattr(installation.os, "replace", stop_after_backup)
    with pytest.raises(_SimulatedStop):
        install(second, target)
    monkeypatch.setattr(installation.os, "replace", real_replace)

    install(first, target)

    assert (target / "Offloader.exe").read_text(encoding="utf-8") == "old"
    assert not (target / ".offloader-installing").exists()


def test_recovery_rolls_forward_stop_after_manifest_write(tmp_path, monkeypatch):
    target = tmp_path / "installed"
    first = _payload(tmp_path / "first", "old")
    second = _payload(tmp_path / "second", "new")
    install(first, target)
    import offloader.installation as installation

    real_write = installation._write_json_atomic
    old_digest = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))["files"][
        "Offloader.exe"
    ]["sha256"]

    def stop_after_manifest(path, data):
        real_write(path, data)
        if path.name == MANIFEST_NAME and data["files"]["Offloader.exe"]["sha256"] != old_digest:
            raise _SimulatedStop()

    monkeypatch.setattr(installation, "_write_json_atomic", stop_after_manifest)
    with pytest.raises(_SimulatedStop):
        install(second, target)
    monkeypatch.setattr(installation, "_write_json_atomic", real_write)

    install(second, target)

    assert (target / "Offloader.exe").read_text(encoding="utf-8") == "new"
    assert not (target / ".offloader-installing").exists()


def test_recovery_retries_after_stop_during_rollback(tmp_path, monkeypatch):
    target = tmp_path / "installed"
    first = _payload(tmp_path / "first", "old")
    second = _payload(tmp_path / "second", "new")
    install(first, target)
    import offloader.installation as installation

    real_replace = installation.os.replace
    state = {"promotion_failed": False}

    def stop_during_rollback(source, destination):
        source_text = str(source)
        destination_text = str(destination)
        if ".offloader-stage-" in source_text and source_text.endswith("Offloader.exe"):
            state["promotion_failed"] = True
            raise OSError("simulated promotion failure")
        result = real_replace(source, destination)
        if (state["promotion_failed"] and ".offloader-backup-" in source_text
                and source_text.endswith("Offloader.exe") and destination_text.endswith("Offloader.exe")):
            raise _SimulatedStop()
        return result

    monkeypatch.setattr(installation.os, "replace", stop_during_rollback)
    with pytest.raises(_SimulatedStop):
        install(second, target)
    monkeypatch.setattr(installation.os, "replace", real_replace)

    install(first, target)

    assert (target / "Offloader.exe").read_text(encoding="utf-8") == "old"
    assert not (target / ".offloader-installing").exists()


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_committed_cleanup_failure_recovers_without_touching_unowned_file(
        tmp_path, monkeypatch, operation):
    target = tmp_path / "installed"
    first = _payload(tmp_path / "first", "old")
    second = _payload(tmp_path / "second", "new")
    install(first, target)
    keep = target / "keep.txt"
    keep.write_text("user data", encoding="utf-8")
    import offloader.installation as installation

    real_cleanup = installation._remove_owned_tree

    def fail_backup_cleanup(root, allowed):
        if ".offloader-backup-" in root.name or ".offloader-uninstall-" in root.name:
            raise OSError("simulated cleanup failure")
        return real_cleanup(root, allowed)

    monkeypatch.setattr(installation, "_remove_owned_tree", fail_backup_cleanup)
    with pytest.raises(OSError, match="cleanup"):
        if operation == "install":
            install(second, target)
        else:
            uninstall(target)
    monkeypatch.setattr(installation, "_remove_owned_tree", real_cleanup)

    if operation == "install":
        install(second, target)
        assert (target / "Offloader.exe").read_text(encoding="utf-8") == "new"
    else:
        with pytest.raises(InstallationError, match="no Offloader installation manifest"):
            uninstall(target)
        assert not (target / "Offloader.exe").exists()
    assert keep.read_text(encoding="utf-8") == "user data"
    assert not (target / ".offloader-installing").exists()
