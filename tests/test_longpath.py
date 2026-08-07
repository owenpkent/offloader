"""Windows paths past MAX_PATH.

A caveat worth stating: on a machine with `LongPathsEnabled = 1` in the
registry, Python handles long paths natively and these code paths are belt and
braces. That key defaults to 0, so most Windows machines do need the `\\\\?\\`
prefix — but it means the end-to-end tests here pass either way, and only the
transformation tests actually prove the prefix is built correctly.
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path

import pytest

from offloader import engine, longpath
from offloader.models import VerificationMode

WINDOWS = platform.system() == "Windows"
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows path semantics")


# ------------------------------------------------------------- transformation


def test_short_paths_are_left_alone():
    assert longpath.extended("C:/short/path.mov") == "C:/short/path.mov"
    assert not longpath.needs_extended("C:/short/path.mov")


@windows_only
def test_a_long_local_path_gets_the_prefix():
    long_path = "C:\\" + "\\".join("segment" + "x" * 20 for _ in range(12))
    assert longpath.needs_extended(long_path)

    converted = longpath.extended(long_path)
    assert converted.startswith("\\\\?\\")
    assert converted.endswith(long_path.split("\\", 1)[1])


@windows_only
def test_a_long_unc_path_uses_the_unc_form():
    """\\\\server\\share becomes \\\\?\\UNC\\server\\share — not \\\\?\\\\\\server."""
    long_unc = "\\\\nas\\archive\\" + "\\".join("f" * 30 for _ in range(10))
    assert longpath.needs_extended(long_unc)

    converted = longpath.extended(long_unc)
    assert converted.startswith("\\\\?\\UNC\\nas\\archive")
    assert not converted.startswith("\\\\?\\\\\\")


@windows_only
def test_an_already_prefixed_path_is_not_prefixed_twice():
    prefixed = "\\\\?\\C:\\" + "\\".join("x" * 30 for _ in range(10))
    assert longpath.extended(prefixed) == prefixed


@windows_only
def test_dot_segments_are_normalised_before_prefixing():
    """The prefix disables normalisation, so `..` must be resolved first or it
    reaches the filesystem verbatim and the open fails."""
    messy = "C:\\base\\" + "\\".join("y" * 30 for _ in range(10)) + "\\..\\final.mov"
    converted = longpath.extended(messy)
    assert ".." not in converted
    assert "/" not in converted
    assert converted.endswith("final.mov")


@windows_only
def test_forward_slashes_are_converted():
    messy = "C:/base/" + "/".join("z" * 30 for _ in range(10)) + "/clip.mov"
    converted = longpath.extended(messy)
    assert "/" not in converted


def test_display_form_strips_the_prefix_again():
    assert longpath.shorten_for_display("\\\\?\\C:\\x\\y.mov") == "C:\\x\\y.mov"
    assert longpath.shorten_for_display("\\\\?\\UNC\\nas\\share\\y.mov") \
        == "\\\\nas\\share\\y.mov"
    assert longpath.shorten_for_display("C:\\plain.mov") == "C:\\plain.mov"


def test_non_windows_is_a_no_op(monkeypatch):
    monkeypatch.setattr(longpath, "_WINDOWS", False)
    long_path = "/" + "/".join("segment" * 8 for _ in range(6))
    assert longpath.extended(long_path) == long_path
    assert not longpath.needs_extended(long_path)


# ------------------------------------------------------------- the helpers


def test_helpers_round_trip_a_deep_tree(tmp_path: Path):
    deep = tmp_path
    while len(str(deep)) < longpath.THRESHOLD + 40:
        deep = deep / ("deep" + "d" * 24)
    longpath.makedirs(deep)

    target = deep / "clip.mov"
    payload = b"IRREPLACEABLE " * 500
    with longpath.open_binary(target, "wb") as handle:
        handle.write(payload)

    assert longpath.exists(target)
    assert longpath.stat(target).st_size == len(payload)
    with longpath.open_binary(target, "rb") as handle:
        assert handle.read() == payload

    moved = deep / "clip.final.mov"
    longpath.replace(target, moved)
    assert longpath.exists(moved) and not longpath.exists(target)

    longpath.unlink(moved)
    assert not longpath.exists(moved)
    shutil.rmtree(tmp_path / os.listdir(tmp_path)[0], ignore_errors=True)


def test_unlink_of_a_missing_file_is_quiet(tmp_path: Path):
    longpath.unlink(tmp_path / "never-existed.mov")
    with pytest.raises(FileNotFoundError):
        longpath.unlink(tmp_path / "never-existed.mov", missing_ok=False)


def test_exists_is_false_rather_than_raising(tmp_path: Path):
    assert not longpath.exists(tmp_path / "nope")
    assert longpath.exists(tmp_path)


def test_hash_file_reaches_a_deep_path(tmp_path: Path):
    from offloader import hashers

    deep = tmp_path
    while len(str(deep)) < longpath.THRESHOLD + 40:
        deep = deep / ("deep" + "e" * 24)
    longpath.makedirs(deep)
    target = deep / "clip.bin"
    with longpath.open_binary(target, "wb") as handle:
        handle.write(b"data" * 1000)

    assert hashers.hash_file(target, "xxh3-64")
    shutil.rmtree(tmp_path / os.listdir(tmp_path)[0], ignore_errors=True)


# ------------------------------------------------------------- end to end


def test_offload_to_a_destination_past_max_path(tmp_path: Path):
    """Passes on a machine with LongPathsEnabled regardless, so this guards
    against regression rather than proving the prefix is load-bearing."""
    card = tmp_path / "card"
    card.mkdir()
    payload = b"IRREPLACEABLE FOOTAGE " * 500
    (card / "A001_C001.mov").write_bytes(payload)

    # Build to length rather than assuming: the temp root's own length varies.
    parts = ("2026-08-07_Feature_Production_Dailies_Delivery",
             "Camera_A_Blackmagic_PYXIS_6K_Reel_A001",
             "Day_01_Interior_Kitchen_Scene_01_Setup_03")
    deep = tmp_path / "dest"
    index = 0
    while len(str(deep / "A001_C001.mov")) <= 280:
        deep = deep / parts[index % len(parts)]
        index += 1
    assert len(str(deep / "A001_C001.mov")) > 260

    job = engine.run(card, engine.OffloadOptions(
        destinations=[deep], verification=VerificationMode.FULL,
        thumbnail_count=0, extra_probe=False))

    assert job.final_status == "Verified"
    landed = deep / "A001_C001.mov"
    assert longpath.exists(landed)
    with longpath.open_binary(landed, "rb") as handle:
        assert handle.read() == payload
    shutil.rmtree(tmp_path / "dest", ignore_errors=True)


@windows_only
def test_probe_reports_whether_the_filesystem_can_hold_long_paths(tmp_path: Path):
    supported, detail = longpath.probe_support(tmp_path)
    assert isinstance(supported, bool)
    if not supported:
        assert detail
    # The probe must clean up whatever it created.
    assert not any("longpath" in name for name in os.listdir(tmp_path))
