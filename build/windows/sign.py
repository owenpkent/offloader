"""Sign and verify Windows release files with the OK Studio certificate.

This module deliberately has no build-side effects on import.  ``sign_file`` is
the only operation which may open a hardware-token prompt; callers can use
``inspect_file`` to decide which files are eligible before invoking it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_CERT_THUMBPRINT = "FC22B5221318F3F3F6B3EB2D969D7F99091557BF"
EXPECTED_CERTIFICATE_NAME = "OK Studio Inc."
CODE_SIGNING_EKU = "1.3.6.1.5.5.7.3.3"
TIMESTAMP_URL = "http://timestamp.digicert.com"

# DLL/PYD coverage may retain signatures from these suppliers.  This is an
# identity allowlist, rather than a broad "valid signature" exception.
KNOWN_VENDOR_PUBLISHERS = frozenset(
    {
        ("Microsoft Corporation", "Microsoft Corporation"),
        ("Microsoft Windows Software Compatibility Publisher", "Microsoft Corporation"),
        ("Python Software Foundation", "Python Software Foundation"),
        ("The QT Company Oy", "The QT Company Oy"),
    }
)


class SigningError(RuntimeError):
    """A signing prerequisite or signature verification requirement failed."""


@dataclass(frozen=True)
class SigningConfig:
    """Public signing inputs discovered during :func:`preflight`."""

    signtool: Path
    certificate_thumbprint: str
    certificate_subject: str
    timestamp_url: str = TIMESTAMP_URL

    def to_dict(self) -> dict[str, str]:
        """Return an inventory-safe representation without private-key data."""
        return {
            "signtool": str(self.signtool),
            "certificate_thumbprint": self.certificate_thumbprint,
            "certificate_subject": self.certificate_subject,
            "timestamp_url": self.timestamp_url,
        }


def _completed(command: list[str], *, interactive: bool = False) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "text": True,
        "capture_output": True,
        "check": False,
        "timeout": 120,
    }
    # Token middleware may show its own dialog while signing.  Keep all
    # discovery and verification processes hidden so release scripts do not
    # steal focus.
    if not interactive and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(command, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SigningError(f"could not run {command[0]}: {error}") from error


def _normal_thumbprint(value: str) -> str:
    thumbprint = value.replace(" ", "").upper()
    if not re.fullmatch(r"[0-9A-F]{40}", thumbprint):
        raise SigningError("certificate thumbprint must be exactly 40 hexadecimal characters")
    return thumbprint


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell_json(script: str) -> dict[str, Any]:
    result = _completed(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
    if result.returncode != 0:
        raise SigningError(f"PowerShell inspection failed: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise SigningError("PowerShell inspection returned invalid JSON") from error
    if not isinstance(data, dict):
        raise SigningError("PowerShell inspection returned an unexpected result")
    return data


def _certificate_metadata(thumbprint: str) -> dict[str, Any]:
    store_path = f"Cert:\\CurrentUser\\My\\{thumbprint}"
    script = (
        f"$cert = Get-Item -LiteralPath {_powershell_quote(store_path)} "
        "-ErrorAction SilentlyContinue; "
        "$result = if ($null -eq $cert) { [pscustomobject]@{ Found = $false } } else { "
        "[pscustomobject]@{ Found = $true; Subject = $cert.Subject; "
        "Thumbprint = $cert.Thumbprint; NotBefore = $cert.NotBefore.ToUniversalTime().ToString('o'); "
        "NotAfter = $cert.NotAfter.ToUniversalTime().ToString('o'); HasPrivateKey = $cert.HasPrivateKey; "
        "Ekus = @($cert.EnhancedKeyUsageList | ForEach-Object { $_.ObjectId }) "
        "} }; $result | ConvertTo-Json -Compress"
    )
    return _powershell_json(script)


def discover_signtool() -> Path:
    """Find a deterministic x64 Windows SDK SignTool, honoring an explicit override."""
    override = os.environ.get("OFFLOADER_SIGNTOOL")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise SigningError(f"OFFLOADER_SIGNTOOL is not a file: {candidate}")
        return candidate.resolve()

    roots = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\\Program Files (x86)")),
        Path(os.environ.get("ProgramFiles", r"C:\\Program Files")),
    ]
    candidates: list[Path] = []
    for root in roots:
        kit = root / "Windows Kits" / "10" / "bin"
        if kit.is_dir():
            candidates.extend(path for path in kit.glob("*/x64/signtool.exe") if path.is_file())
        legacy = root / "Windows Kits" / "8.1" / "bin" / "x64" / "signtool.exe"
        if legacy.is_file():
            candidates.append(legacy)
    if not candidates:
        raise SigningError("Windows SDK SignTool x64 was not found; set OFFLOADER_SIGNTOOL")

    def sort_key(path: Path) -> tuple[tuple[int, ...], str]:
        parts = tuple(int(part) for part in re.findall(r"\d+", path.parent.parent.name))
        return parts, str(path).casefold()

    return max(candidates, key=sort_key).resolve()


def verify_config() -> SigningConfig:
    """Return the signer identity and SignTool required for offline verification.

    This intentionally does not require the certificate store, private key, or
    an unexpired certificate. A valid RFC 3161 timestamp must remain
    verifiable after the signing certificate expires and on a clean machine.
    """
    thumbprint = _normal_thumbprint(os.environ.get("OFFLOADER_SIGN_CERT_SHA1", DEFAULT_CERT_THUMBPRINT))
    return SigningConfig(
        discover_signtool(),
        thumbprint,
        f"CN={EXPECTED_CERTIFICATE_NAME}, O={EXPECTED_CERTIFICATE_NAME}",
    )


def _certificate_time(metadata: dict[str, Any], field: str) -> datetime:
    try:
        value = datetime.fromisoformat(str(metadata[field]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as error:
        raise SigningError(f"selected certificate has an unreadable {field} date") from error
    if value.tzinfo is None or value.utcoffset() is None:
        raise SigningError(f"selected certificate has a timezone-free {field} date")
    return value.astimezone(timezone.utc)


def preflight() -> SigningConfig:
    """Require a current, code-signing capable local certificate before signing."""
    config = verify_config()
    thumbprint = config.certificate_thumbprint
    metadata = _certificate_metadata(thumbprint)
    if metadata.get("Found") is not True:
        raise SigningError(f"code-signing certificate {thumbprint} was not found in CurrentUser\\My")
    actual_thumbprint = _normal_thumbprint(str(metadata.get("Thumbprint", "")))
    subject = str(metadata.get("Subject", ""))
    if actual_thumbprint != thumbprint:
        raise SigningError("certificate store returned a different certificate thumbprint")
    attributes = _subject_attributes(subject)
    if attributes.get("CN") != EXPECTED_CERTIFICATE_NAME or attributes.get("O") != EXPECTED_CERTIFICATE_NAME:
        raise SigningError("selected certificate is not the OK Studio Inc. code-signing certificate")
    if metadata.get("HasPrivateKey") is not True:
        raise SigningError("selected certificate has no associated private key")
    ekus = metadata.get("Ekus")
    if not isinstance(ekus, list) or CODE_SIGNING_EKU not in ekus:
        raise SigningError("selected certificate is not authorized for code signing")
    now = datetime.now(timezone.utc)
    if _certificate_time(metadata, "NotBefore") > now:
        raise SigningError("selected certificate is not valid yet")
    if _certificate_time(metadata, "NotAfter") <= now:
        raise SigningError("selected certificate has expired")
    return SigningConfig(config.signtool, thumbprint, subject)


def _subject_attributes(subject: str) -> dict[str, str]:
    return {
        name.upper(): value.strip()
        for name, value in re.findall(r"(?:^|,)\s*([A-Za-z][A-Za-z0-9.]*)=([^,]+)", subject)
    }


def _file(path: Path | str) -> Path:
    candidate = Path(path).absolute()
    if not candidate.is_file():
        raise SigningError(f"file does not exist: {candidate}")
    current = candidate
    while True:
        if _is_reparse_point(current):
            raise SigningError(f"path crosses a symlink or junction: {current}")
        parent = current.parent
        if parent == current:
            break
        current = parent
    return candidate


def _is_reparse_point(path: Path) -> bool:
    """Check junctions as well as symlinks, which ``Path.is_symlink`` misses."""
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SigningError(f"could not inspect path component {path}: {error}") from error
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attributes & reparse_point)


def write_record(record: dict[str, object], path: Path | str) -> Path:
    """Atomically write a verified inventory record to a normal filesystem path."""
    target = Path(path).absolute()
    parent = target.parent
    if not parent.is_dir():
        raise SigningError(f"record directory does not exist: {parent}")
    current = parent
    while True:
        if _is_reparse_point(current):
            raise SigningError(f"record path crosses a symlink or junction: {current}")
        ancestor = current.parent
        if ancestor == current:
            break
        current = ancestor
    if target.exists() and _is_reparse_point(target):
        raise SigningError(f"record path is a symlink or junction: {target}")
    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{target.name}.", suffix=".tmp", dir=parent, delete=False
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
    except OSError as error:
        raise SigningError(f"could not write signature record {target}: {error}") from error
    finally:
        if temporary_name is not None:
            temporary = Path(temporary_name)
            if temporary.exists():
                temporary.unlink()
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_file(path: Path | str) -> dict[str, object]:
    """Return Authenticode metadata without treating an unsigned file as an error.

    Build orchestration should use this before signing.  Only a ``NotSigned``
    status is eligible for :func:`sign_file`; valid vendor signatures should be
    retained and checked with ``verify_file(..., allow_vendor=True)``.
    """
    target = _file(path)
    script = (
        f"$sig = Get-AuthenticodeSignature -LiteralPath {_powershell_quote(str(target))}; "
        "[pscustomobject]@{ Status = [string]$sig.Status; StatusMessage = [string]$sig.StatusMessage; "
        "SignerSubject = if ($null -eq $sig.SignerCertificate) { $null } else { $sig.SignerCertificate.Subject }; "
        "SignerThumbprint = if ($null -eq $sig.SignerCertificate) { $null } else { $sig.SignerCertificate.Thumbprint }; "
        "TimestampSubject = if ($null -eq $sig.TimeStamperCertificate) { $null } else { $sig.TimeStamperCertificate.Subject }; "
        "TimestampThumbprint = if ($null -eq $sig.TimeStamperCertificate) { $null } else { $sig.TimeStamperCertificate.Thumbprint }; "
        "FileVersion = [string](Get-Item -LiteralPath "
        f"{_powershell_quote(str(target))}).VersionInfo.FileVersion }} | ConvertTo-Json -Compress"
    )
    metadata = _powershell_json(script)
    return {
        "path": str(target),
        "sha256": _sha256(target),
        "signature_status": str(metadata.get("Status", "")),
        "signature_status_message": str(metadata.get("StatusMessage", "")),
        "signer_subject": metadata.get("SignerSubject"),
        "signer_thumbprint": metadata.get("SignerThumbprint"),
        "timestamp_subject": metadata.get("TimestampSubject"),
        "timestamp_thumbprint": metadata.get("TimestampThumbprint"),
        "timestamp_present": bool(metadata.get("TimestampThumbprint")),
        "file_version": metadata.get("FileVersion") or None,
    }


def _require_success(result: subprocess.CompletedProcess[str], operation: str) -> None:
    if result.returncode == 0:
        return
    output = f"{result.stdout}\n{result.stderr}".casefold()
    if "cancel" in output or "abort" in output:
        raise SigningError(f"{operation} was cancelled")
    raise SigningError(f"{operation} failed with SignTool exit code {result.returncode}: {result.stderr.strip()}")


def _is_own_signer(record: dict[str, object], config: SigningConfig) -> bool:
    subject = record.get("signer_subject")
    thumbprint = record.get("signer_thumbprint")
    return (
        isinstance(subject, str)
        and _subject_attributes(subject).get("CN") == EXPECTED_CERTIFICATE_NAME
        and _subject_attributes(subject).get("O") == EXPECTED_CERTIFICATE_NAME
        and isinstance(thumbprint, str)
        and _normal_thumbprint(thumbprint) == config.certificate_thumbprint
    )


def _is_known_vendor(record: dict[str, object]) -> bool:
    subject = record.get("signer_subject")
    if not isinstance(subject, str):
        return False
    attributes = _subject_attributes(subject)
    return (attributes.get("CN"), attributes.get("O")) in KNOWN_VENDOR_PUBLISHERS


def _verify(path: Path | str, config: SigningConfig, expected_version: str | None, allow_vendor: bool) -> dict[str, object]:
    target = _file(path)
    # /tw turns a missing timestamp into a SignTool warning.  Warnings have a
    # non-zero exit code and are deliberately fatal here.
    result = _completed(
        [str(config.signtool), "verify", "/pa", "/all", "/tw", str(target)]
    )
    _require_success(result, "signature verification")
    record = inspect_file(target)
    if record["signature_status"] != "Valid":
        raise SigningError(f"Authenticode status is {record['signature_status']!r}, not Valid")
    if not record["timestamp_present"]:
        raise SigningError("signature has no RFC 3161 timestamp")
    own_signer = _is_own_signer(record, config)
    vendor_signed = _is_known_vendor(record)
    if not own_signer and not (allow_vendor and vendor_signed):
        raise SigningError("signature signer is not the selected OK Studio certificate")
    if expected_version is not None and record["file_version"] != expected_version:
        raise SigningError(
            f"file version {record['file_version']!r} does not match {expected_version!r}"
        )
    record["vendor_signed"] = vendor_signed and not own_signer
    return record


def verify_file(
    path: Path | str, expected_version: str | None = None, allow_vendor: bool = False
) -> dict[str, object]:
    """Cryptographically verify a signed file and return a serializable record.

    ``allow_vendor`` is intended only for DLL/PYD dependency coverage.  It does
    not allow arbitrary trusted publishers, only ``KNOWN_VENDOR_PUBLISHERS``.
    """
    return _verify(path, verify_config(), expected_version, allow_vendor)


def sign_file(path: Path | str) -> dict[str, object]:
    """Sign a confirmed-unsigned file and return its verified inventory record."""
    target = _file(path)
    before = inspect_file(target)
    if before["signature_status"] != "NotSigned":
        raise SigningError(
            f"refusing to replace existing signature with status {before['signature_status']!r}"
        )
    config = preflight()
    result = _completed(
        [
            str(config.signtool),
            "sign",
            "/sha1",
            config.certificate_thumbprint,
            "/fd",
            "SHA256",
            "/tr",
            config.timestamp_url,
            "/td",
            "SHA256",
            str(target),
        ],
        interactive=True,
    )
    _require_success(result, "signing")
    return _verify(target, config, None, False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    for name in ("inspect", "sign", "verify"):
        command = commands.add_parser(name)
        command.add_argument("path", type=Path)
        command.add_argument("--version")
        if name == "sign":
            command.add_argument("--record", type=Path)
        if name == "verify":
            command.add_argument("--allow-vendor", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            result: dict[str, object] = preflight().to_dict()
        elif args.command == "inspect":
            result = inspect_file(args.path)
        elif args.command == "sign":
            result = sign_file(args.path)
            if args.version is not None and result["file_version"] != args.version:
                raise SigningError("signed file version does not match --version")
            if args.record is not None:
                write_record(result, args.record)
        else:
            result = verify_file(args.path, args.version, args.allow_vendor)
    except SigningError as error:
        print(f"signing error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
