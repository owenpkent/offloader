"""Identity and tamper-evident records for Windows build artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def sbom_names(version: str) -> list[str]:
    """The bill-of-materials files a release carries, in upload order."""
    return [
        f"Offloader-{version}-sbom.cyclonedx.json",
        f"Offloader-{version}-third-party-notices.txt",
        f"Offloader-{version}-requirements.txt",
    ]


def release_assets(version: str) -> list[str]:
    """Every file that has to be attached to a published release.

    One list, because three places have to agree about it: the build
    checksums these, its own verification refuses anything it did not expect,
    and the generated release instructions upload them. They had drifted --
    the bill-of-materials files entered `SHA256SUMS.txt` while the upload
    command still named only the installer, the checksums and the inventory,
    so following the instructions published checksums for assets that were not
    there.
    """
    return [
        f"Offloader-Setup-{version}.exe",
        "SHA256SUMS.txt",
        f"Offloader-{version}-inventory.json",
        *sbom_names(version),
    ]
_SOURCE_SUFFIXES = {".py", ".spec", ".nsi", ".nsh", ".ico", ".bmp"}
_RECORD_NAME = ".offloader-build.json"
_MAX_RECORD_BYTES = 4 * 1024 * 1024


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if os.name == "nt":
        try:
            return bool(metadata.st_file_attributes & 0x400)
        except OSError:
            return False
    return metadata.st_mode & 0o170000 == 0o120000


def _reject_path_links(path: Path) -> None:
    current = path.absolute()
    for item in (current, *current.parents):
        if _is_reparse(item):
            raise RuntimeError(f"reparse point is not allowed in artifact path: {item}")


def _files(
    root: Path, *, suffixes: set[str] | None = None, skip_pycache: bool = False
) -> list[tuple[Path, str]]:
    _reject_path_links(root)
    if not root.is_dir():
        raise RuntimeError(f"artifact root is not a directory: {root}")
    found: list[tuple[Path, str]] = []
    def onerror(error: OSError) -> None:
        raise RuntimeError(f"could not walk artifact tree: {error}") from error

    for current, directories, names in os.walk(
        root, onerror=onerror, followlinks=False
    ):
        current_path = Path(current)
        _reject_path_links(current_path)
        kept_directories: list[str] = []
        for name in directories:
            child = current_path / name
            if skip_pycache and name == "__pycache__":
                continue
            if _is_reparse(child):
                raise RuntimeError(f"reparse point is not allowed in artifact tree: {child}")
            kept_directories.append(name)
        directories[:] = kept_directories
        for name in names:
            child = current_path / name
            if _is_reparse(child) or not child.is_file():
                raise RuntimeError(f"unsafe or non-file artifact input: {child}")
            if suffixes is None or child.suffix.lower() in suffixes:
                found.append((child, child.relative_to(root).as_posix()))
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_files(repo: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for directory, suffixes in (
        (repo / "src", {".py"}),
        (repo / "build" / "windows", _SOURCE_SUFFIXES),
    ):
        files.extend(
            (path, path.relative_to(repo).as_posix())
            for path, _ in _files(directory, suffixes=suffixes, skip_pycache=True)
        )
    for name in ("requirements-build.txt", "pyproject.toml", "LICENSE"):
        path = repo / name
        _reject_path_links(path)
        if not path.is_file() or _is_reparse(path):
            raise RuntimeError(f"required source file is missing or unsafe: {path}")
        files.append((path, name))
    return sorted(files, key=lambda item: item[1].casefold())


def source_identity(repo: Path) -> dict[str, str]:
    """Return version, source commit, and deterministic source digest."""
    repo = Path(repo)
    version_path = repo / "src" / "offloader" / "_version.py"
    _reject_path_links(version_path)
    match = _VERSION_RE.search(version_path.read_text(encoding="utf-8"))
    if match is None:
        raise RuntimeError(f"could not read release version from {version_path}")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("could not determine source commit") from exc
    commit = result.stdout.strip()
    if not commit:
        raise RuntimeError("git returned an empty source commit")
    digest = hashlib.sha256()
    for path, relative in _source_files(repo):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(path).encode("ascii"))
        digest.update(b"\0")
    return {"version": match.group(1), "source_commit": commit, "source_digest": digest.hexdigest()}


def bundle_inventory(bundle: Path) -> dict[str, str]:
    """Hash every safe bundle file, excluding the build record itself."""
    bundle = Path(bundle)
    pairs = _files(bundle)
    names: dict[str, str] = {}
    folded_names: set[str] = set()
    for path, relative in pairs:
        key = relative.replace("\\", "/")
        folded = key.casefold()
        if folded in folded_names:
            raise RuntimeError(f"case-insensitive bundle name collision: {key}")
        folded_names.add(folded)
        if folded == _RECORD_NAME.casefold():
            continue
        names[key] = _sha256(path)
    required = {"offloader.exe", "offloader-maintenance.exe", "offloader-cli.exe", ".offloader-install.lock"}
    folded = {name.casefold() for name in names}
    missing = sorted(name for name in required if name.casefold() not in folded)
    if not any(name.casefold().startswith("_internal/python") and name.casefold().endswith(".dll") for name in names):
        missing.append("_internal/python312.dll")
    if missing:
        raise RuntimeError(f"bundle is missing required files: {', '.join(missing)}")
    return dict(sorted(names.items(), key=lambda item: item[0].casefold()))


def write_build_record(bundle: Path, identity: dict[str, str]) -> dict[str, Any]:
    """Write the versioned build record atomically and return its contents."""
    bundle = Path(bundle)
    files = bundle_inventory(bundle)
    record: dict[str, Any] = {"schema": 1, **identity, "files": files}
    target = bundle / _RECORD_NAME
    _reject_path_links(bundle)
    fd, temporary_name = tempfile.mkstemp(prefix=".offloader-build-", suffix=".tmp", dir=bundle)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return record


def validate_build_record(bundle: Path, identity: dict[str, str]) -> dict[str, Any]:
    """Validate a build record and the complete current bundle contents."""
    bundle = Path(bundle)
    target = bundle / _RECORD_NAME
    _reject_path_links(bundle)
    try:
        if _is_reparse(target) or target.stat().st_size > _MAX_RECORD_BYTES:
            raise RuntimeError("invalid build record path or size")
        raw = target.read_bytes()
        if len(raw) > _MAX_RECORD_BYTES:
            raise RuntimeError("build record exceeds size limit")
        record = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("could not read build record") from exc
    if not isinstance(record, dict) or record.get("schema") != 1:
        raise RuntimeError("unsupported or malformed build record schema")
    if set(record) != {"schema", *identity, "files"}:
        raise RuntimeError("build record identity fields differ")
    for key, value in identity.items():
        if record.get(key) != value:
            raise RuntimeError(f"build record identity mismatch: {key}")
    recorded = record.get("files")
    if not isinstance(recorded, dict) or any(
        not isinstance(name, str) or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
        for name, digest in recorded.items()
    ):
        raise RuntimeError("malformed build record file inventory")
    actual = bundle_inventory(bundle)
    if set(recorded) != set(actual):
        raise RuntimeError("bundle file set differs from build record")
    if any(recorded[name].lower() != actual[name].lower() for name in actual):
        raise RuntimeError("bundle file hash differs from build record")
    return record
