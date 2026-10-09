"""Release ordering and frozen CLI routing must not change normal command semantics."""

import runpy
from pathlib import Path

import pytest

BUILD = Path(__file__).resolve().parents[1] / "build/windows"
windows_version = runpy.run_path(str(BUILD / "versioning.py"))["windows_version"]
timeline_requested = runpy.run_path(str(BUILD / "cli_entry.py"))["_timeline_requested"]


def test_windows_release_order():
    releases = ["0.1.0a999", "0.1.0b1", "0.1.0b999", "0.1.0rc1", "0.1.0", "0.1.1a1"]
    versions = [windows_version(value) for value in releases]
    assert all(first < second for first, second in zip(versions, versions[1:], strict=False))


@pytest.mark.parametrize("version", ["0.1.0a1000", "65536.0.0", "0.1.0.dev1", "-1.0.0"])
def test_unsupported_windows_versions_fail_before_build(version):
    with pytest.raises(ValueError):
        windows_version(version)


@pytest.mark.parametrize("args", [
    ["resolve", "--help"], ["offload", "--source", "resolve"],
    ["offload", "--name", "resolve"], [],
])
def test_frozen_cli_does_not_treat_filenames_or_help_as_timeline_import(args):
    assert not timeline_requested(args)


@pytest.mark.parametrize("args", [
    ["resolve", "--timeline", "edit.xml"], ["offload", "--timeline", "edit.xml"],
    ["offload", "--timeline=edit.xml"],
])
def test_frozen_cli_explains_excluded_timeline_support(args):
    assert timeline_requested(args)
