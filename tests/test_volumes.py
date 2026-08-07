from __future__ import annotations

from pathlib import Path

from offloader import volumes


def test_list_volumes_finds_the_system_drive():
    found = volumes.list_volumes()
    assert found, "expected at least one mounted volume"
    assert any(v.root == volumes.system_root() for v in found)
    for volume in found:
        assert volume.total_bytes >= 0
        assert 0.0 <= volume.percent_used <= 100.0


def test_cards_sort_first():
    found = volumes.list_volumes()
    cards = [index for index, v in enumerate(found) if v.is_camera_card]
    others = [index for index, v in enumerate(found) if not v.is_camera_card]
    if cards and others:
        assert max(cards) < min(others)


def test_marker_directory_identifies_a_card(tmp_path: Path):
    (tmp_path / "DCIM").mkdir()
    assert volumes.detect_camera_card(tmp_path, "removable")


def test_marker_match_is_case_insensitive(tmp_path: Path):
    (tmp_path / "Private").mkdir()
    assert volumes.detect_camera_card(tmp_path, "fixed")


def test_clips_at_the_root_identify_a_card(tmp_path: Path):
    """Blackmagic and similar write straight to the root with no marker dir."""
    for index in range(3):
        (tmp_path / f"A005_C{index:03d}.braw").write_bytes(b"x")
    assert volumes.detect_camera_card(tmp_path, "fixed")


def test_a_couple_of_clips_is_not_enough(tmp_path: Path):
    for index in range(2):
        (tmp_path / f"clip{index}.mov").write_bytes(b"x")
    assert not volumes.detect_camera_card(tmp_path, "fixed")


def test_an_ordinary_folder_tree_is_not_a_card(tmp_path: Path):
    for name in ("Documents", "Projects", "Misc", "Canon", "Red"):
        (tmp_path / name).mkdir()
    assert not volumes.detect_camera_card(tmp_path, "fixed")


def test_network_and_optical_volumes_are_never_cards(tmp_path: Path):
    (tmp_path / "DCIM").mkdir()
    for drive_type in ("network", "optical", "ramdisk", "unknown"):
        assert not volumes.detect_camera_card(tmp_path, drive_type)


def test_system_volume_is_never_a_card():
    assert not volumes.detect_camera_card(volumes.system_root(), "fixed")


def test_hidden_and_system_entries_are_ignored(tmp_path: Path):
    (tmp_path / "$RECYCLE.BIN").mkdir()
    (tmp_path / "System Volume Information").mkdir()
    (tmp_path / ".Spotlight-V100").mkdir()
    assert not volumes.detect_camera_card(tmp_path, "removable")


def test_missing_path_does_not_raise(tmp_path: Path):
    assert not volumes.detect_camera_card(tmp_path / "gone", "removable")


def test_find_volume_locates_the_containing_root():
    volume = volumes.find_volume(Path.home())
    assert volume is not None
    assert volume.root == volumes.system_root() or volume.root in Path.home().parents


def test_usage_fields_are_consistent():
    for volume in volumes.list_volumes():
        assert volume.used_bytes == max(0, volume.total_bytes - volume.free_bytes)
        assert volume.display_name
