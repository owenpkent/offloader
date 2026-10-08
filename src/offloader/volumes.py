"""Mounted volume discovery, for the drive panel and card detection.

Deliberately free of any Qt import so it can be unit-tested headlessly and
reused by the CLI.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import string
from dataclasses import dataclass
from pathlib import Path

#: Directory names that only camera firmware creates. Kept deliberately narrow:
#: broader guesses like "misc" or a vendor name match ordinary NAS folders and
#: flag a fileserver as a card.
CAMERA_MARKERS = {
    "dcim", "private", "avchd", "bdmv", "xdroot", "m4root", "clip",
    "brawcontents", "pana_grp",
}

#: Camera originals. Some cameras — Blackmagic among them — write clips
#: straight to the root with no marker directory at all, so a volume holding
#: several of these is treated as a card even without one.
CAMERA_EXTENSIONS = {
    ".braw", ".r3d", ".mxf", ".mov", ".mp4", ".ari", ".arx", ".crm", ".cine",
    ".dng", ".mts", ".m2ts", ".insv", ".avi", ".wav",
}

MIN_MEDIA_FILES = 3
#: Cap the root listing so a NAS with thousands of entries cannot stall the
#: drive panel's refresh.
SCAN_LIMIT = 400

_WINDOWS_DRIVE_TYPES = {
    0: "unknown", 1: "no-root", 2: "removable", 3: "fixed",
    4: "network", 5: "optical", 6: "ramdisk",
}


@dataclass
class Volume:
    root: Path
    label: str
    filesystem: str
    total_bytes: int
    free_bytes: int
    drive_type: str = "fixed"
    #: Computed once when the volume is listed — the check touches the disk,
    #: and the drive panel refreshes on a timer.
    is_camera_card: bool = False

    @property
    def used_bytes(self) -> int:
        return max(0, self.total_bytes - self.free_bytes)

    @property
    def percent_used(self) -> float:
        return (self.used_bytes / self.total_bytes * 100.0) if self.total_bytes else 0.0

    @property
    def removable(self) -> bool:
        return self.drive_type in ("removable", "optical")

    @property
    def display_name(self) -> str:
        return self.label or str(self.root)

    @property
    def is_system(self) -> bool:
        return self.root == system_root()


def system_root() -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("SystemDrive", "C:") + "\\")
    return Path("/")


def detect_camera_card(root: Path, drive_type: str) -> bool:
    """Whether a volume root looks like camera media rather than a disk.

    Two signals: a marker directory the camera wrote, or a root full of camera
    originals. Removability is deliberately not required — a reader in a
    Thunderbolt dock usually reports as a fixed disk.
    """
    if drive_type in ("network", "optical", "ramdisk", "no-root", "unknown"):
        return False
    # Compare resolved: macOS surfaces the boot volume a second time as
    # /Volumes/Macintosh HD, a firmlink to /. Without resolving, that copy
    # slipped past this guard and was badged as a card, because macOS has a
    # /private directory and "private" is an AVCHD marker.
    try:
        if Path(root).resolve() == system_root().resolve():
            return False
    except OSError:
        if Path(root) == system_root():
            return False

    directories: set[str] = set()
    media_files = 0
    try:
        for count, child in enumerate(Path(root).iterdir()):
            if count >= SCAN_LIMIT:
                break
            name = child.name
            if name.startswith(("$", ".", "#")) or name == "System Volume Information":
                continue
            try:
                if child.is_dir():
                    directories.add(name.casefold())
                elif child.suffix.lower() in CAMERA_EXTENSIONS:
                    media_files += 1
            except OSError:
                continue
    except OSError:
        return False

    if directories & CAMERA_MARKERS:
        return True
    return media_files >= MIN_MEDIA_FILES


def _usage(root: Path) -> tuple[int, int]:
    try:
        usage = shutil.disk_usage(root)
        return usage.total, usage.free
    except OSError:
        return 0, 0


def _windows_volumes() -> list[Volume]:
    import ctypes

    kernel32 = ctypes.windll.kernel32
    # Stop Windows popping "insert a disk" dialogs for empty card readers.
    previous = kernel32.SetErrorMode(0x0001 | 0x0002)
    found: list[Volume] = []
    try:
        bitmask = kernel32.GetLogicalDrives()
        for index, letter in enumerate(string.ascii_uppercase):
            if not bitmask & (1 << index):
                continue
            root = f"{letter}:\\"
            drive_type = _WINDOWS_DRIVE_TYPES.get(kernel32.GetDriveTypeW(root), "unknown")
            if drive_type in ("no-root", "unknown"):
                continue

            label_buffer = ctypes.create_unicode_buffer(261)
            fs_buffer = ctypes.create_unicode_buffer(261)
            ok = kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(root), label_buffer, 261,
                None, None, None, fs_buffer, 261,
            )
            if not ok and drive_type == "optical":
                continue    # empty drive

            total, free = _usage(Path(root))
            if total == 0 and drive_type != "removable":
                continue
            found.append(Volume(
                root=Path(root),
                label=label_buffer.value,
                filesystem=fs_buffer.value,
                total_bytes=total,
                free_bytes=free,
                drive_type=drive_type,
                is_camera_card=detect_camera_card(Path(root), drive_type),
            ))
    finally:
        kernel32.SetErrorMode(previous)
    return found


def _posix_volumes() -> list[Volume]:
    candidates: list[Path] = [Path("/")]
    for parent in (Path("/Volumes"), Path("/media"), Path("/mnt"),
                   Path("/run/media") / (Path.home().name)):
        try:
            candidates.extend(child for child in parent.iterdir() if child.is_dir())
        except OSError:
            continue

    found: list[Volume] = []
    seen: set[Path] = set()
    for root in candidates:
        if root in seen:
            continue
        seen.add(root)
        total, free = _usage(root)
        if total == 0:
            continue
        drive_type = "fixed" if root == Path("/") else "removable"
        found.append(Volume(
            root=root,
            label=root.name or str(root),
            filesystem="",
            total_bytes=total,
            free_bytes=free,
            drive_type=drive_type,
            is_camera_card=detect_camera_card(root, drive_type),
        ))
    return found


#: Linux filesystems that are not storage: kernel interfaces, containers, snap
#: loop images, and per-user FUSE plumbing (gvfs, portals).
_LINUX_PSEUDO_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "ramfs", "cgroup", "cgroup2",
    "overlay", "squashfs", "autofs", "securityfs", "debugfs", "tracefs", "fusectl",
    "configfs", "pstore", "bpf", "mqueue", "hugetlbfs", "binfmt_misc", "rpc_pipefs",
    "efivarfs", "nsfs", "fuse.gvfsd-fuse", "fuse.portal", "fuse.snapfuse",
    "fuse.lxcfs", "fuse.gvfs-fuse-daemon",
}
_LINUX_NETWORK_FS = {
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "ceph", "glusterfs", "afs",
    "davfs", "fuse.sshfs", "fuse.rclone",
}
_LINUX_OPTICAL_FS = {"iso9660", "udf"}
_MOUNTINFO = "/proc/self/mountinfo"

#: Mounts under these belong to the OS, not to a person plugging something in.
_LINUX_SYSTEM_PREFIXES = (
    "/boot", "/snap", "/var/snap", "/var/lib", "/usr", "/etc", "/proc", "/sys",
    "/dev", "/run",
)
#: Where udisks and friends put removable media. Exempt from the list above,
#: because /run/media sits under /run.
_LINUX_MEDIA_PREFIXES = ("/media", "/run/media", "/mnt")


def _unescape_mountinfo(field: str) -> str:
    """mountinfo writes space, tab, newline and backslash as octal escapes."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _linux_device_is_removable(major_minor: str) -> bool:
    """Whether the block device behind a mount reports itself as removable.

    A partition's `removable` flag lives on its parent disk. Device-mapper
    devices (LUKS) have none, and read as not removable.
    """
    try:
        device = (Path("/sys/dev/block") / major_minor).resolve()
    except OSError:
        return False
    for candidate in (device, device.parent):
        try:
            return (candidate / "removable").read_text().strip() == "1"
        except OSError:
            continue
    return False


def _linux_volumes() -> list[Volume]:
    """Volumes from the kernel's mount table.

    Cards mount at /media/<user>/<LABEL> (or /run/media/<user>/<LABEL>), one
    level below the directory the macOS-style scan in `_posix_volumes` looks
    at, so listing children of /media finds the user's directory and never the
    card. The mount table names the real mount points, and gives the
    filesystem type as well.
    """
    with open(_MOUNTINFO, encoding="utf-8", errors="replace") as handle:
        lines = handle.read().splitlines()

    found: list[Volume] = []
    for line in lines:
        head, separator, tail = line.partition(" - ")
        left, right = head.split(), tail.split()
        if not separator or len(left) < 5 or len(right) < 2:
            continue
        major_minor, mount_point = left[2], _unescape_mountinfo(left[4])
        fstype = right[0]

        # The system root is always a candidate, even on overlayfs (containers,
        # live systems): it is filtered only by the usage check below.
        if fstype in _LINUX_PSEUDO_FS and mount_point != "/":
            continue
        removable_path = _under(mount_point, _LINUX_MEDIA_PREFIXES)
        if mount_point != "/" and not removable_path and _under(
                mount_point, _LINUX_SYSTEM_PREFIXES):
            continue

        root = Path(mount_point)
        total, free = _usage(root)
        if total == 0:
            continue

        if fstype in _LINUX_NETWORK_FS:
            drive_type = "network"
        elif fstype in _LINUX_OPTICAL_FS:
            drive_type = "optical"
        elif mount_point == "/":
            drive_type = "fixed"
        elif removable_path or _linux_device_is_removable(major_minor):
            drive_type = "removable"
        else:
            drive_type = "fixed"

        found.append(Volume(
            root=root,
            label=root.name or str(root),
            filesystem=fstype,
            total_bytes=total,
            free_bytes=free,
            drive_type=drive_type,
            is_camera_card=detect_camera_card(root, drive_type),
        ))
    return found


def list_volumes() -> list[Volume]:
    """Every mounted volume, cards first so they are easy to spot.

    Deduplicated by resolved root: macOS reaches the boot volume through both
    `/` and `/Volumes/Macintosh HD`, and listing it twice would be noise.
    """
    system = platform.system()
    try:
        if system == "Windows":
            volumes = _windows_volumes()
        elif system == "Linux":
            try:
                volumes = _linux_volumes()
            except OSError:      # no /proc (some containers): scan the usual parents
                volumes = _posix_volumes()
        else:
            volumes = _posix_volumes()
    except Exception:
        volumes = []

    seen: set[Path] = set()
    unique: list[Volume] = []
    for volume in sorted(volumes, key=lambda v: len(str(v.root))):
        try:
            key = volume.root.resolve()
        except OSError:
            key = volume.root
        if key in seen:
            continue
        seen.add(key)
        unique.append(volume)

    return sorted(unique, key=lambda v: (not v.is_camera_card, str(v.root)))


def find_volume(path: Path) -> Volume | None:
    """The volume a path sits on, for labelling a chosen source."""
    path = Path(path).resolve()
    best: Volume | None = None
    for volume in list_volumes():
        try:
            root = volume.root.resolve()
        except OSError:
            continue
        if path == root or root in path.parents:
            if best is None or len(str(root)) > len(str(best.root)):
                best = volume
    return best
