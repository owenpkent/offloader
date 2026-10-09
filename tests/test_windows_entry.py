"""The portable desktop entry starts without installation files."""

from __future__ import annotations

import importlib.util
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BUILD = Path(__file__).resolve().parents[1] / "build/windows"


@pytest.fixture
def gui_entry(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("gui_entry", BUILD / "gui_entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # A frozen executable in a folder with no lock file, as after a download.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Offloader-portable.exe"))
    monkeypatch.setattr(module, "_main", lambda: 0)
    return module


def test_installed_entry_requires_installation_lock(gui_entry):
    with pytest.raises(FileNotFoundError):
        gui_entry.main()


def test_portable_entry_skips_installation_lock(gui_entry, tmp_path):
    assert gui_entry.main(lock=False) == 0
    assert list(tmp_path.iterdir()) == []


def test_portable_entry_runs_without_lock(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "gui_entry",
                        SimpleNamespace(run=lambda **kwargs: calls.append(kwargs)))
    runpy.run_path(str(BUILD / "portable_entry.py"), run_name="__main__")
    assert calls == [{"lock": False}]
