"""Launch policy tests use a fake Win32 boundary and never create a process."""

from __future__ import annotations

from pathlib import Path

import pytest

from offloader import windows_launch


class FakeWindowsApi:
    def __init__(self, *, shell_pid: int = 44, same_session: bool = True, elevated: bool = False,
                 error_at: str | None = None) -> None:
        self.shell_pid = shell_pid
        self.same_session = same_session
        self.elevated = elevated
        self.error_at = error_at
        self.calls: list[object] = []
        self.closed: list[object] = []
        self.destroyed: list[object] = []

    def _raise(self, name: str) -> None:
        if self.error_at == name:
            raise OSError(name)

    def desktop_shell_process_id(self) -> int:
        self.calls.append("shell")
        self._raise("shell")
        if not self.shell_pid:
            raise OSError("no shell")
        return self.shell_pid

    def is_current_session(self, process_id: int) -> bool:
        self.calls.append(("session", process_id))
        self._raise("session")
        return self.same_session

    def open_process_query(self, process_id: int) -> int:
        self.calls.append(("open_process", process_id))
        self._raise("open_process")
        return 101

    def open_process_token(self, process: int) -> int:
        self.calls.append(("open_token", process))
        self._raise("open_token")
        return 102

    def token_is_elevated(self, token: int) -> bool:
        self.calls.append(("elevated", token))
        self._raise("elevated")
        return self.elevated

    def duplicate_primary_token(self, token: int) -> int:
        self.calls.append(("duplicate", token))
        self._raise("duplicate")
        return 103

    def create_environment_block(self, token: int) -> int:
        self.calls.append(("environment", token))
        self._raise("environment")
        return 106

    def destroy_environment_block(self, environment: int) -> None:
        self.destroyed.append(environment)

    def create_process_with_token(
        self, token: int, executable: Path, command_line: str, cwd: Path, environment: int
    ):
        self.calls.append(("create", token, executable, command_line, cwd, environment))
        self._raise("create")
        return 104, 105

    def close_handle(self, handle: int) -> None:
        self.closed.append(handle)


@pytest.fixture
def desktop_target(tmp_path: Path) -> Path:
    target = tmp_path / "Offloader App.exe"
    target.write_bytes(b"fixture")
    return target


def configure_windows(monkeypatch, api: FakeWindowsApi) -> None:
    monkeypatch.setattr(windows_launch.sys, "platform", "win32")
    monkeypatch.setattr(windows_launch, "_windows_api", lambda: api)


def test_launch_uses_unelevated_shell_token_and_closes_every_handle(
    monkeypatch, desktop_target: Path
):
    api = FakeWindowsApi()
    configure_windows(monkeypatch, api)

    windows_launch.launch_as_desktop_user(desktop_target)

    assert api.calls[-1] == (
        "create", 103, desktop_target, '"' + str(desktop_target) + '"', desktop_target.parent, 106,
    )
    assert api.closed == [105, 104, 103, 102, 101]
    assert api.destroyed == [106]


@pytest.mark.parametrize("api", [FakeWindowsApi(shell_pid=0), FakeWindowsApi(same_session=False)])
def test_no_shell_or_other_session_fails_without_process_open(monkeypatch, desktop_target: Path, api):
    configure_windows(monkeypatch, api)
    with pytest.raises(OSError):
        windows_launch.launch_as_desktop_user(desktop_target)
    assert not any(call for call in api.calls if isinstance(call, tuple) and call[0] == "open_process")
    assert api.closed == []


def test_elevated_shell_fails_without_duplicate_or_fallback(monkeypatch, desktop_target: Path):
    api = FakeWindowsApi(elevated=True)
    configure_windows(monkeypatch, api)
    with pytest.raises(OSError, match="elevated"):
        windows_launch.launch_as_desktop_user(desktop_target)
    assert not any(call for call in api.calls if isinstance(call, tuple) and call[0] in {"duplicate", "create"})
    assert api.closed == [102, 101]
    assert api.destroyed == []


def test_create_failure_closes_source_and_duplicate_token(monkeypatch, desktop_target: Path):
    api = FakeWindowsApi(error_at="create")
    configure_windows(monkeypatch, api)
    with pytest.raises(OSError, match="create"):
        windows_launch.launch_as_desktop_user(desktop_target)
    assert api.closed == [103, 102, 101]
    assert api.destroyed == [106]


def test_environment_failure_closes_tokens_without_creating_process(monkeypatch, desktop_target: Path):
    api = FakeWindowsApi(error_at="environment")
    configure_windows(monkeypatch, api)
    with pytest.raises(OSError, match="environment"):
        windows_launch.launch_as_desktop_user(desktop_target)
    assert not any(call for call in api.calls if isinstance(call, tuple) and call[0] == "create")
    assert api.closed == [103, 102, 101]
    assert api.destroyed == []


def test_requires_existing_absolute_executable_without_calling_windows(monkeypatch, tmp_path: Path):
    api = FakeWindowsApi()
    configure_windows(monkeypatch, api)
    with pytest.raises(OSError, match="existing absolute"):
        windows_launch.launch_as_desktop_user(tmp_path / "missing.exe")
    assert api.calls == []
