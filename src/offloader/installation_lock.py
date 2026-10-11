"""Keep installed programs and maintenance from running at the same time.

This lock protects application files only. It does not coordinate copy jobs or
their destinations. Windows releases use a shared byte-range lock opened for
reading, so a normal user needs no write access to Program Files.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = ".offloader-install.lock"
INCOMPLETE_NAME = ".offloader-installing"


class InstallationBusyError(OSError):
    """The installation is in use, being changed, or needs recovery."""


def _check_lock_path(path: Path) -> None:
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError(f"Installation lock cannot use a link or reparse point: {candidate}")


@contextmanager
def _windows_lock(path: Path, exclusive: bool) -> Iterator[None]:
    import ctypes
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.LockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(Overlapped),
    ]
    kernel.LockFileEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # No FILE_SHARE_DELETE: replacing the lock would create a second lock domain.
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None,
                                4 if exclusive else 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        overlapped = Overlapped()
        if not kernel.LockFileEx(handle, 1 | (2 if exclusive else 0), 0, 1, 0,
                                 ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            if error in (32, 33, 997):
                raise InstallationBusyError(
                    "Offloader is running or installation maintenance is in progress. "
                    "Close Offloader and its CLI, then try again."
                )
            raise ctypes.WinError(error)
        yield
    finally:
        # Closing releases the lock, including on exceptions or process exit.
        kernel.CloseHandle(handle)


@contextmanager
def installation_lock(root: Path, *, exclusive: bool = False) -> Iterator[None]:
    """Take a nonblocking lock, exclusive for maintenance, shared for programs.

    Maintenance creates the lock only after validating its target. Programs
    require the existing lock so missing installation metadata fails closed.
    The file must remain in place across upgrades and uninstall.
    """
    path = Path(root).absolute() / LOCK_NAME
    _check_lock_path(path)
    if sys.platform == "win32":
        with _windows_lock(path, exclusive):
            yield
    else:
        # Also exercise the maintenance state machine on non-Windows CI.
        import fcntl

        fd = os.open(path, os.O_RDONLY | (os.O_CREAT if exclusive else 0), 0o644)
        try:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                            | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallationBusyError("Offloader installation is in use") from exc
            yield
        finally:
            os.close(fd)


@contextmanager
def frozen_installation_lock() -> Iterator[None]:
    """Hold the shared lock for the complete lifetime of a frozen application."""
    if not getattr(sys, "frozen", False):
        yield
        return
    root = Path(sys.executable).absolute().parent
    with installation_lock(root):
        if (root / INCOMPLETE_NAME).exists():
            raise InstallationBusyError(
                "Offloader installation is incomplete. Re-run the installer to recover it."
            )
        yield
