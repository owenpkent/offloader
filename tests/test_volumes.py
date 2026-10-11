from __future__ import annotations

from pathlib import Path

import pytest

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


def test_a_firmlinked_system_volume_is_not_a_card(tmp_path: Path, monkeypatch):
    """REGRESSION, found by CI on macOS. The boot volume also appears as
    /Volumes/Macintosh HD, a firmlink to /. String comparison missed it, so the
    system-volume guard did not fire — and macOS has a /private directory,
    which is an AVCHD marker. The boot drive was badged CARD."""
    system = tmp_path / "real-root"
    (system / "private").mkdir(parents=True)
    alias = tmp_path / "Volumes" / "Macintosh HD"
    alias.parent.mkdir(parents=True)
    try:
        alias.symlink_to(system, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    monkeypatch.setattr(volumes, "system_root", lambda: system)
    assert not volumes.detect_camera_card(system, "fixed")
    assert not volumes.detect_camera_card(alias, "removable")


_MOUNTINFO_SAMPLE = r"""\
26 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
27 26 0:6 / /dev rw - devtmpfs udev rw
28 26 0:24 / /run rw - tmpfs tmpfs rw
29 26 259:1 / /boot/efi rw - vfat /dev/nvme0n1p1 rw
30 26 259:2 /var/lib/snapd/hostfs/x /var/snap/firefox/common/host-hunspell rw - ext4 /dev/nvme0n1p2 rw
31 28 0:50 / /run/user/1000/gvfs rw - fuse.gvfsd-fuse gvfsd-fuse rw
32 26 8:17 / /media/owen/A001 rw - exfat /dev/sdb1 rw
33 26 8:33 / /media/owen/BRAW\040CARD rw - vfat /dev/sdc1 rw
34 26 8:49 / /mnt/scratch rw - ext4 /dev/sdd1 rw
35 26 0:60 / /run/media/owen/SD_CARD rw - exfat /dev/sde1 rw
36 26 8:65 / /home/owen/raid rw - ext4 /dev/sdf1 rw
37 26 0:70 / /srv/nas rw - nfs4 nas:/export rw
38 26 11:0 / /media/owen/DISC rw - iso9660 /dev/sr0 ro
39 26 7:1 / /snap/core/1 ro - squashfs /dev/loop1 ro
"""


@pytest.fixture
def linux_mounts(tmp_path: Path, monkeypatch):
    table = tmp_path / "mountinfo"
    table.write_text(_MOUNTINFO_SAMPLE)
    monkeypatch.setattr(volumes, "_MOUNTINFO", str(table))
    monkeypatch.setattr(volumes, "_usage", lambda root: (1000, 400))
    monkeypatch.setattr(
        volumes, "_linux_device_is_removable", lambda major_minor: major_minor == "8:65")
    return {v.root.as_posix(): v for v in volumes._linux_volumes()}


def test_linux_finds_cards_below_the_users_media_directory(linux_mounts):
    card = linux_mounts["/media/owen/A001"]
    assert (card.label, card.filesystem, card.drive_type) == ("A001", "exfat", "removable")
    assert "/run/media/owen/SD_CARD" in linux_mounts
    assert "/media/owen" not in linux_mounts


def test_linux_decodes_escaped_mount_points(linux_mounts):
    assert linux_mounts["/media/owen/BRAW CARD"].label == "BRAW CARD"


def test_linux_skips_pseudo_and_system_mounts(linux_mounts):
    assert set(linux_mounts) == {
        "/", "/media/owen/A001", "/media/owen/BRAW CARD", "/media/owen/DISC",
        "/mnt/scratch", "/run/media/owen/SD_CARD", "/home/owen/raid", "/srv/nas",
    }


def test_linux_classifies_drive_types(linux_mounts):
    assert linux_mounts["/"].drive_type == "fixed"
    assert linux_mounts["/srv/nas"].drive_type == "network"
    assert linux_mounts["/media/owen/DISC"].drive_type == "optical"
    # Outside /media, only the device's own removable flag makes it removable.
    assert linux_mounts["/home/owen/raid"].drive_type == "removable"
    assert linux_mounts["/mnt/scratch"].drive_type == "removable"


def test_unescape_mountinfo():
    assert volumes._unescape_mountinfo(r"/a\040b\134c") == "/a b\\c"


def _mount_table(tmp_path, monkeypatch, text):
    table = tmp_path / "mountinfo"
    table.write_text(text)
    monkeypatch.setattr(volumes, "_MOUNTINFO", str(table))
    monkeypatch.setattr(volumes, "_usage", lambda root: (1000, 400))
    monkeypatch.setattr(volumes, "_linux_device_is_removable", lambda major_minor: False)
    return {v.root.as_posix(): v for v in volumes._linux_volumes()}


def test_linux_keeps_an_overlay_root_as_the_system_volume(tmp_path, monkeypatch):
    found = _mount_table(tmp_path, monkeypatch,
                         "1 0 0:50 / / rw - overlay overlay rw,lowerdir=/a\n")
    assert set(found) == {"/"}
    assert (found["/"].filesystem, found["/"].drive_type) == ("overlay", "fixed")


def test_linux_still_skips_overlay_mounts_elsewhere(tmp_path, monkeypatch):
    found = _mount_table(
        tmp_path, monkeypatch,
        "1 0 0:50 / / rw - overlay overlay rw\n"
        "2 1 0:51 / /var/lib/docker/overlay2/x/merged rw - overlay overlay rw\n"
        "3 1 0:52 / /mnt/ctr rw - overlay overlay rw\n")
    assert set(found) == {"/"}
