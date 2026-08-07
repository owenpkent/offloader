"""Making a verification read actually touch the disk.

Reading a file back immediately after writing it usually reads it out of the
page cache, which compares memory against memory and proves nothing about what
landed on the platter. Microsoft's own guidance is explicit about this: if you
read the data back it "may end up being read out of the disk cache, in which
case you're not actually verifying physical media."

Opening a handle with `FILE_FLAG_NO_BUFFERING` evicts that file from the cache
as a side effect, so a normal buffered read afterwards has to go to the device.
That is cheaper and far less error-prone than doing the whole verification pass
with unbuffered, sector-aligned I/O.

This is best-effort and honest about its limits: a drive or RAID controller with
its own volatile cache can still serve the read from there. Eviction removes the
operating system from the equation, not the entire storage stack.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

_WINDOWS = platform.system() == "Windows"

# CreateFileW constants
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_FLAG_NO_BUFFERING = 0x20000000
_INVALID_HANDLE = -1


def _evict_windows(path: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.CreateFileW(
        str(path), _GENERIC_READ, _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None, _OPEN_EXISTING, _FILE_FLAG_NO_BUFFERING, None,
    )
    if not handle or handle == _INVALID_HANDLE or handle == 2 ** 64 - 1:
        return False
    kernel32.CloseHandle(handle)
    return True


def _evict_posix(path: Path) -> bool:
    if not hasattr(os, "posix_fadvise"):
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        return True
    finally:
        os.close(fd)


def evict_from_cache(path: Path) -> bool:
    """Drop `path` from the operating system's page cache.

    Returns whether the eviction was actually performed, so callers can report
    honestly rather than claim a stronger guarantee than they delivered.
    """
    try:
        return _evict_windows(path) if _WINDOWS else _evict_posix(path)
    except (OSError, AttributeError, ValueError):
        return False


def flush_volume(path: Path) -> bool:
    """Ask the volume holding `path` to flush its write buffers.

    Requires elevation on Windows and is not available everywhere, so failure is
    expected and non-fatal.
    """
    if not _WINDOWS:
        try:
            os.sync()
            return True
        except (AttributeError, OSError):
            return False

    import ctypes
    from ctypes import wintypes

    drive = Path(path).anchor.rstrip("\\/")
    if not drive:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        handle = kernel32.CreateFileW(
            f"\\\\.\\{drive}", 0x40000000,  # GENERIC_WRITE
            _FILE_SHARE_READ | _FILE_SHARE_WRITE, None, _OPEN_EXISTING, 0, None,
        )
        if not handle or handle == _INVALID_HANDLE or handle == 2 ** 64 - 1:
            return False
        try:
            return bool(kernel32.FlushFileBuffers(handle))
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return False
