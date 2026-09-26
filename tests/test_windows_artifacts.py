"""Artifact identity and bundle record checks."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[1] / "build/windows/artifacts.py"
SPEC = importlib.util.spec_from_file_location("windows_artifacts", MODULE)
assert SPEC and SPEC.loader
artifacts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifacts)


def _bundle(root: Path) -> Path:
    bundle = root / "Offloader"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)
    for name in ("Offloader.exe", "offloader-cli.exe", "offloader-maintenance.exe"):
        (bundle / name).write_bytes(name.encode())
    (internal / "python312.dll").write_bytes(b"python")
    (bundle / ".offloader-install.lock").write_bytes(b"")
    return bundle


def test_source_identity_is_literal_and_deterministic():
    repo = Path(__file__).parents[1]
    first = artifacts.source_identity(repo)
    second = artifacts.source_identity(repo)
    assert first == second
    assert first["version"] == "0.1.0"
    assert len(first["source_commit"]) >= 7
    assert len(first["source_digest"]) == 64


def test_build_record_round_trip_and_tamper_detection(tmp_path: Path):
    bundle = _bundle(tmp_path)
    identity = {"version": "0.1.0", "source_commit": "abc", "source_digest": "d" * 64}
    record = artifacts.write_build_record(bundle, identity)
    assert record["schema"] == 1
    assert artifacts.validate_build_record(bundle, identity) == record
    (bundle / "Offloader.exe").write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="hash"):
        artifacts.validate_build_record(bundle, identity)


def test_build_record_covers_portable_executable(tmp_path: Path):
    bundle = _bundle(tmp_path)
    portable = tmp_path / "Offloader-0.1.0-portable.exe"
    portable.write_bytes(b"portable")
    identity = {"version": "0.1.0", "source_commit": "abc", "source_digest": "d" * 64}
    record = artifacts.write_build_record(bundle, identity, portable)
    assert record["portable"]["name"] == portable.name
    assert artifacts.validate_build_record(bundle, identity, portable) == record
    # A record written for the portable build cannot be reused without it,
    # and one written without it cannot vouch for a portable file.
    with pytest.raises(RuntimeError, match="identity fields"):
        artifacts.validate_build_record(bundle, identity)
    portable.write_bytes(b"swapped")
    with pytest.raises(RuntimeError, match="portable executable hash"):
        artifacts.validate_build_record(bundle, identity, portable)
    artifacts.write_build_record(bundle, identity)
    with pytest.raises(RuntimeError, match="identity fields"):
        artifacts.validate_build_record(bundle, identity, portable)
    portable.unlink()
    with pytest.raises(RuntimeError, match="missing"):
        artifacts.write_build_record(bundle, identity, portable)


def test_record_rejects_wrong_identity_and_unexpected_file(tmp_path: Path):
    bundle = _bundle(tmp_path)
    identity = {"version": "0.1.0", "source_commit": "abc", "source_digest": "d" * 64}
    artifacts.write_build_record(bundle, identity)
    wrong = {**identity, "version": "9.9.9"}
    with pytest.raises(RuntimeError, match="identity"):
        artifacts.validate_build_record(bundle, wrong)
    (bundle / "unexpected.txt").write_bytes(b"extra")
    with pytest.raises(RuntimeError, match="file set"):
        artifacts.validate_build_record(bundle, identity)


def test_inventory_rejects_missing_required_executable(tmp_path: Path):
    bundle = _bundle(tmp_path)
    (bundle / "offloader-maintenance.exe").unlink()
    with pytest.raises(RuntimeError, match="missing"):
        artifacts.bundle_inventory(bundle)


def test_inventory_rejects_case_insensitive_duplicate(tmp_path: Path, monkeypatch):
    bundle = _bundle(tmp_path)
    original = artifacts._files
    files = original(bundle)
    files.append((bundle / "Offloader.exe", "OFFLOADER.EXE"))
    monkeypatch.setattr(artifacts, "_files", lambda root: files)
    with pytest.raises(RuntimeError, match="collision"):
        artifacts.bundle_inventory(bundle)


def test_inventory_rejects_reparse_input(tmp_path: Path):
    bundle = _bundle(tmp_path)
    link = bundle / "linked.txt"
    try:
        link.symlink_to(bundle / "Offloader.exe")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(RuntimeError, match="reparse|unsafe"):
        artifacts.bundle_inventory(bundle)


def test_inventory_surfaces_unreadable_walk(tmp_path: Path, monkeypatch):
    bundle = _bundle(tmp_path)

    def failing_walk(*args, onerror, **kwargs):
        onerror(OSError("access denied"))
        yield from ()

    monkeypatch.setattr(artifacts.os, "walk", failing_walk)
    with pytest.raises(RuntimeError, match="walk"):
        artifacts.bundle_inventory(bundle)
