"""NSIS generation stays deterministic and has the maintenance helper contract."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

BUILD = Path(__file__).resolve().parents[1] / "build" / "windows"
SPEC = importlib.util.spec_from_file_location("windows_installer", BUILD / "installer.py")
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
try:
    MAKENSIS = installer.find_makensis()
except RuntimeError:
    MAKENSIS = None


def _bundle(root: Path) -> Path:
    bundle = root / "Offloader"
    bundle.mkdir()
    for name in installer.REQUIRED_BUNDLE_FILES:
        (bundle / name).write_text(name, encoding="utf-8")
    return bundle


def test_render_uses_payload_helper_and_uninstaller_finalizer(tmp_path):
    rendered = installer._render(
        _bundle(tmp_path), tmp_path / "Offloader-Setup-1.2.3.exe", "1.2.3",
        ["python", "sign.py", "sign"],
    )

    assert 'offloader-maintenance.exe" install --payload' in rendered
    assert 'offloader-maintenance.exe" uninstall --target' in rendered
    assert "!uninstfinalize" in rendered
    assert 'VIProductVersion "1.2.3.65535"' in rendered
    assert "PreviousInstallLocation" in rendered
    assert 'PreviousInstallLocation == $INSTDIR' in rendered
    assert "InstallDirRegKey" not in rendered
    assert "${GetOptions} $CMDLINE \"/D=\" $0" in rendered
    assert "ClearErrors\n    SetOutPath" in rendered
    assert "StrCmp $PreviousInstallLocation $INSTDIR 0 uninstall_cleanup_done" in rendered
    assert "@" not in rendered


def test_build_rejects_bundle_without_maintenance_helper(tmp_path):
    bundle = _bundle(tmp_path)
    (bundle / "offloader-maintenance.exe").unlink()

    with pytest.raises(ValueError, match="offloader-maintenance.exe"):
        installer.build_installer(bundle, tmp_path / "setup.exe", "1.2.3")


def test_build_invokes_makensis_and_returns_output(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path)
    compiler = tmp_path / "makensis.exe"
    compiler.write_text("placeholder", encoding="utf-8")
    output = tmp_path / "setup.exe"
    calls = []

    class Result:
        returncode = 0

    def fake_run(command, check):
        calls.append(command)
        output.write_bytes(b"setup")
        return Result()

    monkeypatch.setattr(installer, "find_makensis", lambda: compiler)
    monkeypatch.setattr(installer.subprocess, "run", fake_run)

    assert installer.build_installer(bundle, output, "1.2.3") == output.resolve()
    assert calls[0][:2] == [str(compiler), "/V2"]
    assert "/WX" in calls[0]


@pytest.mark.skipif(MAKENSIS is None, reason="NSIS is not installed")
def test_unsigned_nsis_bundle_compiles(tmp_path):
    output = tmp_path / "Offloader-Setup-1.2.3.exe"

    result = installer.build_installer(_bundle(tmp_path), output, "1.2.3")

    assert result.is_file()
    assert result.stat().st_size > 0


@pytest.mark.skipif(MAKENSIS is None, reason="NSIS is not installed")
def test_nsis_finalizer_receives_a_quoted_uninstaller_path(tmp_path):
    bundle = _bundle(tmp_path)
    record = tmp_path / "recorded path.txt"
    script = tmp_path / "record signer.py"
    script.write_text(
        "from pathlib import Path\nimport json\nimport sys\n"
        "Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')\n"
        "raise SystemExit(0 if len(sys.argv) == 3 else 9)\n",
        encoding="utf-8",
    )

    installer.build_installer(
        bundle, tmp_path / "Offloader Setup.exe", "1.2.3",
        sign_command=[sys.executable, str(script), str(record)],
    )

    recorded = json.loads(record.read_text(encoding="utf-8"))
    assert len(recorded) == 1
    assert Path(recorded[0]).name.startswith("nst")


@pytest.mark.skipif(MAKENSIS is None, reason="NSIS is not installed")
def test_nsis_finalizer_nonzero_exit_fails_compilation(tmp_path):
    bundle = _bundle(tmp_path)
    script = tmp_path / "reject signer.py"
    script.write_text("raise SystemExit(7)\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="makensis failed"):
        installer.build_installer(
            bundle, tmp_path / "Offloader Setup.exe", "1.2.3",
            sign_command=[sys.executable, str(script)],
        )
