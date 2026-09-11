"""Launch an installed executable with the unelevated interactive shell token."""

from __future__ import annotations

import ctypes
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20
TOKEN_PRIMARY = 1
SECURITY_IMPERSONATION = 2
LOGON_WITH_PROFILE = 1
CREATE_UNICODE_ENVIRONMENT = 0x00000400


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


class _TokenElevation(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


class _WindowsApi:
    """Small native boundary so launch policy can be tested without Win32 calls."""

    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.userenv = ctypes.WinDLL("userenv", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.user.GetShellWindow.restype = wintypes.HWND
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.ProcessIdToSessionId.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.advapi.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi.OpenProcessToken.restype = wintypes.BOOL
        self.advapi.GetTokenInformation.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.advapi.GetTokenInformation.restype = wintypes.BOOL
        self.advapi.DuplicateTokenEx.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
            wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
        ]
        self.advapi.DuplicateTokenEx.restype = wintypes.BOOL
        self.advapi.CreateProcessWithTokenW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
            wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(_StartupInfo), ctypes.POINTER(_ProcessInformation),
        ]
        self.advapi.CreateProcessWithTokenW.restype = wintypes.BOOL
        self.userenv.CreateEnvironmentBlock.argtypes = [
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL,
        ]
        self.userenv.CreateEnvironmentBlock.restype = wintypes.BOOL
        self.userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
        self.userenv.DestroyEnvironmentBlock.restype = wintypes.BOOL

    @staticmethod
    def _error() -> OSError:
        return ctypes.WinError(ctypes.get_last_error())

    def desktop_shell_process_id(self) -> int:
        window = self.user.GetShellWindow()
        if not window:
            raise OSError("Windows desktop shell is unavailable")
        process_id = wintypes.DWORD()
        if not self.user.GetWindowThreadProcessId(window, ctypes.byref(process_id)) or not process_id.value:
            raise self._error()
        return process_id.value

    def is_current_session(self, process_id: int) -> bool:
        shell_session = wintypes.DWORD()
        current_session = wintypes.DWORD()
        if not self.kernel.ProcessIdToSessionId(process_id, ctypes.byref(shell_session)):
            raise self._error()
        current_pid = self.kernel.GetCurrentProcessId()
        if not self.kernel.ProcessIdToSessionId(current_pid, ctypes.byref(current_session)):
            raise self._error()
        return shell_session.value == current_session.value

    def open_process_query(self, process_id: int) -> wintypes.HANDLE:
        handle = self.kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
        if not handle:
            raise self._error()
        return handle

    def open_process_token(self, process: wintypes.HANDLE) -> wintypes.HANDLE:
        token = wintypes.HANDLE()
        if not self.advapi.OpenProcessToken(
            process, TOKEN_QUERY | TOKEN_DUPLICATE, ctypes.byref(token)
        ):
            raise self._error()
        return token

    def token_is_elevated(self, token: wintypes.HANDLE) -> bool:
        elevation = _TokenElevation()
        size = wintypes.DWORD()
        if not self.advapi.GetTokenInformation(
            token, TOKEN_ELEVATION, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(size)
        ):
            raise self._error()
        return bool(elevation.TokenIsElevated)

    def duplicate_primary_token(self, token: wintypes.HANDLE) -> wintypes.HANDLE:
        primary = wintypes.HANDLE()
        desired_access = TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY
        if not self.advapi.DuplicateTokenEx(
            token, desired_access, None, SECURITY_IMPERSONATION, TOKEN_PRIMARY, ctypes.byref(primary)
        ):
            raise self._error()
        return primary

    def create_environment_block(self, shell_token: wintypes.HANDLE) -> ctypes.c_void_p:
        environment = ctypes.c_void_p()
        # Do not inherit variables such as APPDATA from the elevated installer.
        if not self.userenv.CreateEnvironmentBlock(ctypes.byref(environment), shell_token, False):
            raise self._error()
        return environment

    def destroy_environment_block(self, environment: ctypes.c_void_p) -> None:
        # Cleanup cannot justify retaining the desktop-user token or its profile.
        self.userenv.DestroyEnvironmentBlock(environment)

    def create_process_with_token(
        self, token: wintypes.HANDLE, executable: Path, command_line: str, cwd: Path,
        environment: ctypes.c_void_p,
    ) -> tuple[wintypes.HANDLE, wintypes.HANDLE]:
        startup = _StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        process = _ProcessInformation()
        command_buffer = ctypes.create_unicode_buffer(command_line)
        if not self.advapi.CreateProcessWithTokenW(
            token, LOGON_WITH_PROFILE, str(executable), command_buffer, CREATE_UNICODE_ENVIRONMENT,
            environment, str(cwd),
            ctypes.byref(startup), ctypes.byref(process),
        ):
            raise self._error()
        return process.hProcess, process.hThread

    def close_handle(self, handle: wintypes.HANDLE) -> None:
        if handle:
            self.kernel.CloseHandle(handle)


def _windows_api() -> _WindowsApi:
    return _WindowsApi()


def launch_as_desktop_user(executable: Path) -> None:
    """Start ``executable`` under the unelevated same-session Explorer token.

    This has no elevated or ordinary-subprocess fallback.  An installer that
    cannot prove the shell identity must leave Finish launch unavailable.
    """
    if sys.platform != "win32":
        raise OSError("desktop-user launch is only available on Windows")
    target = Path(executable)
    if not target.is_absolute() or not target.is_file():
        raise OSError("desktop-user launch requires an existing absolute executable path")
    api = _windows_api()
    process = token = primary = environment = child_process = child_thread = None
    try:
        shell_pid = api.desktop_shell_process_id()
        if not api.is_current_session(shell_pid):
            raise OSError("Windows desktop shell is not in the installer session")
        process = api.open_process_query(shell_pid)
        token = api.open_process_token(process)
        if api.token_is_elevated(token):
            raise OSError("Windows desktop shell token is elevated")
        primary = api.duplicate_primary_token(token)
        environment = api.create_environment_block(token)
        command_line = subprocess.list2cmdline([str(target)])
        child_process, child_thread = api.create_process_with_token(
            primary, target, command_line, target.parent, environment
        )
    finally:
        try:
            if environment is not None:
                api.destroy_environment_block(environment)
        finally:
            for handle in (child_thread, child_process, primary, token, process):
                if handle is not None:
                    api.close_handle(handle)
