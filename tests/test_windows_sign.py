"""Mocked coverage for the release signing helper, without a hardware token."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BUILD = Path(__file__).resolve().parents[1] / "build/windows"


@pytest.fixture
def signing_module():
    spec = importlib.util.spec_from_file_location("offloader_windows_sign_test", BUILD / "sign.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def result(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def config(module):
    return module.SigningConfig(Path("C:/sdk/x64/signtool.exe"), module.DEFAULT_CERT_THUMBPRINT,
                                "CN=OK Studio Inc., O=OK Studio Inc.")


def metadata(*, status="Valid", subject="CN=OK Studio Inc., O=OK Studio Inc.", thumbprint=None,
             timestamp="CN=DigiCert Timestamp", version="0.1.0"):
    return {
        "Status": status,
        "StatusMessage": "ok",
        "SignerSubject": subject,
        "SignerThumbprint": thumbprint or "FC22B5221318F3F3F6B3EB2D969D7F99091557BF",
        "TimestampSubject": timestamp,
        "TimestampThumbprint": "A" * 40 if timestamp else None,
        "FileVersion": version,
    }


def test_preflight_uses_exact_default_thumbprint_and_checks_private_key(signing_module, monkeypatch):
    module = signing_module
    monkeypatch.setattr(module, "discover_signtool", lambda: Path("C:/sdk/signtool.exe"))
    monkeypatch.setattr(module, "_certificate_metadata", lambda thumb: {
        "Found": True, "Thumbprint": thumb, "Subject": "CN=OK Studio Inc., O=OK Studio Inc.",
        "NotBefore": "2025-12-31T00:00:00+00:00", "NotAfter": "2026-12-31T00:00:00+00:00",
        "HasPrivateKey": True, "Ekus": [module.CODE_SIGNING_EKU],
    })
    assert module.preflight().certificate_thumbprint == module.DEFAULT_CERT_THUMBPRINT


def test_preflight_rejects_missing_code_signing_eku(signing_module, monkeypatch):
    module = signing_module
    monkeypatch.setattr(module, "verify_config", lambda: config(module))
    monkeypatch.setattr(module, "_certificate_metadata", lambda thumb: {
        "Found": True, "Thumbprint": thumb, "Subject": "CN=OK Studio Inc., O=OK Studio Inc.",
        "NotBefore": "2025-12-31T00:00:00+00:00", "NotAfter": "2026-12-31T00:00:00+00:00",
        "HasPrivateKey": True, "Ekus": [],
    })
    with pytest.raises(module.SigningError, match="code signing"):
        module.preflight()


def test_preflight_rejects_not_yet_valid_certificate(signing_module, monkeypatch):
    module = signing_module
    monkeypatch.setattr(module, "verify_config", lambda: config(module))
    monkeypatch.setattr(module, "_certificate_metadata", lambda thumb: {
        "Found": True, "Thumbprint": thumb, "Subject": "CN=OK Studio Inc., O=OK Studio Inc.",
        "NotBefore": "2099-12-31T00:00:00+00:00", "NotAfter": "2100-12-31T00:00:00+00:00",
        "HasPrivateKey": True, "Ekus": [module.CODE_SIGNING_EKU],
    })
    with pytest.raises(module.SigningError, match="not valid yet"):
        module.preflight()


def test_sign_rejects_existing_vendor_signature_before_prompt(signing_module, monkeypatch, tmp_path):
    target = tmp_path / "vendor.dll"
    target.write_bytes(b"vendor")
    monkeypatch.setattr(module := signing_module, "inspect_file", lambda path: {"signature_status": "Valid"})
    monkeypatch.setattr(module, "preflight", lambda: pytest.fail("token should not be queried"))
    with pytest.raises(module.SigningError, match="refusing"):
        module.sign_file(target)


def test_sign_uses_sha256_rfc3161_and_verifies_afterward(signing_module, monkeypatch, tmp_path):
    module = signing_module
    target = tmp_path / "Offloader.exe"
    target.write_bytes(b"unsigned")
    commands = []
    monkeypatch.setattr(module, "inspect_file", lambda path: {"signature_status": "NotSigned"})
    monkeypatch.setattr(module, "preflight", lambda: config(module))
    monkeypatch.setattr(module, "_completed", lambda command, interactive=False: (commands.append((command, interactive)) or result()))
    expected = {"signature_status": "Valid", "timestamp_present": True}
    monkeypatch.setattr(module, "_verify", lambda path, cfg, version, allow_vendor: expected)
    assert module.sign_file(target) is expected
    command, interactive = commands[0]
    assert interactive is True
    assert command[1:] == ["sign", "/sha1", module.DEFAULT_CERT_THUMBPRINT, "/fd", "SHA256", "/tr",
                           module.TIMESTAMP_URL, "/td", "SHA256", str(target.resolve())]


def test_verify_requires_signtool_success_and_timestamp(signing_module, monkeypatch, tmp_path):
    module = signing_module
    target = tmp_path / "Offloader.exe"
    target.write_bytes(b"signed")
    monkeypatch.setattr(module, "verify_config", lambda: config(module))
    monkeypatch.setattr(module, "_completed", lambda *args, **kwargs: result(1, stderr="warning: no timestamp"))
    with pytest.raises(module.SigningError, match="exit code 1"):
        module.verify_file(target)

    monkeypatch.setattr(module, "_completed", lambda *args, **kwargs: result())
    monkeypatch.setattr(module, "inspect_file", lambda path: {
        "signature_status": "Valid", "timestamp_present": False, "signer_subject": "CN=OK Studio Inc., O=OK Studio Inc.",
        "signer_thumbprint": module.DEFAULT_CERT_THUMBPRINT, "file_version": "0.1.0",
    })
    with pytest.raises(module.SigningError, match="timestamp"):
        module.verify_file(target)


def test_verify_rejects_wrong_signer_and_version(signing_module, monkeypatch, tmp_path):
    module = signing_module
    target = tmp_path / "Offloader.exe"
    target.write_bytes(b"signed")
    monkeypatch.setattr(module, "verify_config", lambda: config(module))
    monkeypatch.setattr(module, "_completed", lambda *args, **kwargs: result())
    wrong = {"signature_status": "Valid", "timestamp_present": True, "signer_subject": "CN=Other, O=Other",
             "signer_thumbprint": "B" * 40, "file_version": "0.1.0"}
    monkeypatch.setattr(module, "inspect_file", lambda path: wrong)
    with pytest.raises(module.SigningError, match="signer"):
        module.verify_file(target)
    wrong["signer_subject"] = "CN=OK Studio Inc., O=OK Studio Inc."
    wrong["signer_thumbprint"] = module.DEFAULT_CERT_THUMBPRINT
    with pytest.raises(module.SigningError, match="version"):
        module.verify_file(target, expected_version="0.2.0")


def test_verify_vendor_only_allows_explicit_cn_and_organization(signing_module, monkeypatch, tmp_path):
    module = signing_module
    target = tmp_path / "Qt6Core.dll"
    target.write_bytes(b"signed")
    monkeypatch.setattr(module, "verify_config", lambda: config(module))
    monkeypatch.setattr(module, "_completed", lambda *args, **kwargs: result())
    vendor = {"signature_status": "Valid", "timestamp_present": True,
              "signer_subject": "CN=The QT Company Oy, O=The QT Company Oy", "signer_thumbprint": "C" * 40,
              "file_version": "0.1.0"}
    monkeypatch.setattr(module, "inspect_file", lambda path: vendor.copy())
    with pytest.raises(module.SigningError, match="signer"):
        module.verify_file(target)
    assert module.verify_file(target, allow_vendor=True)["vendor_signed"] is True
    vendor["signer_subject"] = "CN=The QT Company Oy, O=An Impostor"
    with pytest.raises(module.SigningError, match="signer"):
        module.verify_file(target, allow_vendor=True)


def test_inspection_returns_raw_notsigned_inventory(signing_module, monkeypatch, tmp_path):
    module = signing_module
    target = tmp_path / "plain.exe"
    target.write_bytes(b"plain")
    monkeypatch.setattr(module, "_powershell_json", lambda script: metadata(status="NotSigned", subject=None, thumbprint=None, timestamp=None))
    record = module.inspect_file(target)
    assert record["signature_status"] == "NotSigned"
    assert record["timestamp_present"] is False
    assert len(record["sha256"]) == 64


def test_cancelled_signing_is_a_failure(signing_module, monkeypatch, tmp_path):
    module = signing_module
    target = tmp_path / "Offloader.exe"
    target.write_bytes(b"unsigned")
    monkeypatch.setattr(module, "inspect_file", lambda path: {"signature_status": "NotSigned"})
    monkeypatch.setattr(module, "preflight", lambda: config(module))
    monkeypatch.setattr(module, "_completed", lambda *args, **kwargs: result(1, stderr="operation cancelled"))
    with pytest.raises(module.SigningError, match="cancelled"):
        module.sign_file(target)


def test_sign_rejects_missing_path_without_checking_for_a_token(signing_module, monkeypatch, tmp_path):
    module = signing_module
    monkeypatch.setattr(module, "preflight", lambda: pytest.fail("token should not be queried"))
    with pytest.raises(module.SigningError, match="does not exist"):
        module.sign_file(tmp_path / "missing.exe")


def test_verify_uses_config_without_reading_certificate_store(signing_module, monkeypatch, tmp_path):
    module = signing_module
    target = tmp_path / "signed.exe"
    target.write_bytes(b"signed")
    monkeypatch.setattr(module, "verify_config", lambda: config(module))
    monkeypatch.setattr(module, "preflight", lambda: pytest.fail("verification must not need a key"))
    monkeypatch.setattr(module, "_completed", lambda *args, **kwargs: result())
    monkeypatch.setattr(module, "inspect_file", lambda path: {
        "signature_status": "Valid", "timestamp_present": True,
        "signer_subject": "CN=OK Studio Inc., O=OK Studio Inc.",
        "signer_thumbprint": module.DEFAULT_CERT_THUMBPRINT, "file_version": "0.1.0",
    })
    assert module.verify_file(target)["vendor_signed"] is False


def test_file_rejects_reparse_point_ancestor(signing_module, monkeypatch, tmp_path):
    module = signing_module
    target = tmp_path / "bundle" / "Offloader.exe"
    target.parent.mkdir()
    target.write_bytes(b"binary")
    monkeypatch.setattr(module, "_is_reparse_point", lambda path: path == target.parent)
    with pytest.raises(module.SigningError, match="symlink or junction"):
        module._file(target)


def test_write_record_is_json_and_replaces_existing_file(signing_module, tmp_path):
    record_path = tmp_path / "uninstaller-signature.json"
    record_path.write_text("old", encoding="utf-8")
    signing_module.write_record({"path": "Uninstall.exe", "signature_status": "Valid"}, record_path)
    assert json.loads(record_path.read_text(encoding="utf-8")) == {
        "path": "Uninstall.exe", "signature_status": "Valid",
    }


def test_invalid_signtool_override_fails_closed(signing_module, monkeypatch, tmp_path):
    module = signing_module
    monkeypatch.setenv("OFFLOADER_SIGNTOOL", str(tmp_path / "missing-signtool.exe"))
    with pytest.raises(module.SigningError, match="not a file"):
        module.discover_signtool()


def test_powershell_inspection_parses_only_json(signing_module, monkeypatch):
    module = signing_module
    monkeypatch.setattr(module, "_completed", lambda *args, **kwargs: result(stdout=json.dumps({"Status": "Valid"})))
    assert module._powershell_json("test")["Status"] == "Valid"
    monkeypatch.setattr(module, "_completed", lambda *args, **kwargs: result(stdout="not json"))
    with pytest.raises(module.SigningError, match="invalid JSON"):
        module._powershell_json("test")
