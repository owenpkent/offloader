"""The release pipeline cannot bypass signing failures or reuse stale bytes."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BUILD = Path(__file__).resolve().parents[1] / "build/windows"


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("windows_pipeline", BUILD / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.sys, "platform", "win32")
    module.DIST = tmp_path
    (tmp_path / "Offloader").mkdir()
    calls = []
    identity = {"version": "0.1.0", "source_commit": "a" * 40, "source_digest": "b" * 64}
    artifacts = SimpleNamespace(
        source_identity=lambda _: identity,
        validate_build_record=lambda *args: calls.append("validate"),
        write_build_record=lambda *args: calls.append("record"),
    )
    def build_installer(*args, **kwargs):
        calls.append(("installer", kwargs))
        (tmp_path / ".offloader-uninstaller-signature.json").write_text("{}")

    installer = SimpleNamespace(
        find_makensis=lambda: calls.append("nsis"),
        build_installer=build_installer,
    )
    sign = SimpleNamespace(
        preflight=lambda: calls.append("preflight"),
        sign_file=lambda *args: calls.append("sign installer"),
        verify_file=lambda *args, **kwargs: {"verified": True},
    )
    for name, value in (("artifacts", artifacts), ("installer", installer), ("sign", sign)):
        monkeypatch.setitem(sys.modules, name, value)
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **k: "")
    monkeypatch.setattr(module, "run", lambda cmd: calls.append("smoke" if "smoke.py" in str(cmd) else "freeze"))
    monkeypatch.setattr(module, "check_versions", lambda *a: calls.append("versions"))
    monkeypatch.setattr(module, "check_signatures", lambda *a, **k: calls.append("sign bundle") or [])
    monkeypatch.setattr(module, "save_outputs", lambda *a, **k: calls.append(("outputs", k)))
    monkeypatch.setattr(module, "validate_outputs", lambda *a: calls.append("validate outputs"))
    return module, calls, artifacts, sign


def test_unsigned_build_never_accesses_signing_key(pipeline):
    module, calls, _, _ = pipeline
    assert module.main(["--no-sign"]) == 0
    assert "preflight" not in calls
    assert "sign bundle" not in calls
    assert "sign installer" not in calls
    assert ("installer", {"sign_command": None}) in calls
    assert "smoke" in calls
    assert not (module.DIST / ".offloader-build-incomplete").exists()


def test_signed_pipeline_orders_signing_before_assembly(pipeline):
    module, calls, _, _ = pipeline
    assert module.main([]) == 0
    installer_index = next(i for i, value in enumerate(calls) if isinstance(value, tuple) and value[0] == "installer")
    assert calls.index("sign bundle") < installer_index < calls.index("sign installer")
    assert calls.index("sign installer") < calls.index("smoke")


def test_signing_failure_leaves_candidate_incomplete(pipeline):
    module, calls, _, sign = pipeline

    def fail(*args):
        raise RuntimeError("token cancelled")

    sign.sign_file = fail
    assert module.main([]) == 1
    assert not any(isinstance(value, tuple) and value[0] == "outputs" for value in calls)
    assert (module.DIST / ".offloader-build-incomplete").exists()


def test_stale_skip_build_fails_before_mutation(pipeline):
    module, calls, artifacts, _ = pipeline

    def fail(*args):
        raise RuntimeError("source mismatch")

    artifacts.validate_build_record = fail
    assert module.main(["--skip-build", "--no-sign"]) == 1
    assert "freeze" not in calls
    assert not (module.DIST / ".offloader-build-incomplete").exists()


def test_verify_only_does_not_sign_rebuild_or_publish(pipeline):
    module, calls, _, _ = pipeline
    assert module.main(["--verify-only"]) == 0
    assert "validate" in calls
    assert "preflight" not in calls
    assert "freeze" not in calls
    assert "record" not in calls
    assert "sign installer" not in calls


def test_signed_build_refuses_dirty_sources(pipeline, monkeypatch):
    module, calls, _, _ = pipeline
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **k: " M src/offloader/cli.py")
    assert module.main([]) == 1
    assert "preflight" not in calls
    assert "freeze" not in calls


def test_sources_changed_during_build_block_outputs(pipeline):
    module, calls, artifacts, _ = pipeline
    identities = iter([{"version": "0.1.0"}, {"version": "0.1.1"}])
    artifacts.source_identity = lambda _: next(identities)
    assert module.main(["--no-sign"]) == 1
    assert not any(isinstance(value, tuple) and value[0] == "outputs" for value in calls)


def test_failed_smoke_check_blocks_outputs(pipeline, monkeypatch):
    module, calls, _, _ = pipeline

    def run(cmd):
        if "smoke.py" in str(cmd):
            raise RuntimeError("frozen artifact is broken")

    monkeypatch.setattr(module, "run", run)
    assert module.main(["--no-sign"]) == 1
    assert not any(isinstance(value, tuple) and value[0] == "outputs" for value in calls)
    assert (module.DIST / ".offloader-build-incomplete").exists()


@pytest.mark.parametrize("args", [
    ["--verify-only", "--no-sign"], ["--verify-only", "--skip-build"],
    ["--skip-build", "--clean"],
])
def test_conflicting_modes_fail(pipeline, args):
    with pytest.raises(SystemExit):
        pipeline[0].main(args)
