"""Host facts for the report header (OS version, CPU count, RAM)."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class SystemInfo:
    os_version: str
    processors: int
    system_ram: str


def _ram_bytes() -> int:
    """Total physical RAM, best-effort and without a hard psutil dependency."""
    if hasattr(os, "sysconf"):
        try:
            return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            pass
    if platform.system() == "Windows":
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except Exception:  # pragma: no cover - platform quirk
            pass
    if platform.system() == "Darwin" and shutil.which("sysctl"):
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
            )
            return int(out.stdout.strip())
        except Exception:  # pragma: no cover - platform quirk
            pass
    return 0


def _os_version() -> str:
    system = platform.system()
    if system == "Windows":
        release = platform.release()
        build = platform.version().split(".")[-1]
        return f"Windows {release} (Build {build})"
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    return f"{system} {platform.release()}"


def collect() -> SystemInfo:
    ram = _ram_bytes()
    return SystemInfo(
        os_version=_os_version(),
        processors=os.cpu_count() or 0,
        # Reported in binary GB, matching the reference report's "48 GB".
        system_ram=f"{round(ram / (1024 ** 3))} GB" if ram else "",
    )
