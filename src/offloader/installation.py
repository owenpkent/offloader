"""Safe, inventory-backed lifecycle operations for a Windows installation.

This module deliberately knows nothing about NSIS.  The installer extracts a
complete, signed payload and asks the standalone maintenance executable to use
these operations.  Keeping ownership checks here makes upgrade and uninstall
safe even when invoked without the wizard.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from .installation_lock import InstallationBusyError, installation_lock

MANIFEST_NAME = ".offloader-install.json"
MARKER_NAME = ".offloader-installing"
LOCK_NAME = ".offloader-install.lock"
FORMAT_VERSION = 1
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
RESERVED_ROOT_NAMES = {LOCK_NAME, MANIFEST_NAME, MARKER_NAME, "Uninstall.exe"}
BUILD_METADATA_NAME = ".offloader-build.json"
IGNORED_PAYLOAD_NAMES = {LOCK_NAME, BUILD_METADATA_NAME}
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
    "LPT6", "LPT7", "LPT8", "LPT9",
}


class InstallationError(RuntimeError):
    """An installation target or its contents are unsafe to change."""


@dataclass(frozen=True)
class Inventory:
    """The exact application files that the lifecycle is allowed to manage."""

    files: dict[str, dict[str, Any]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _raise_walk_error(error: OSError) -> None:
    raise error


def _path_exists_or_link(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _is_reparse(path: Path) -> bool:
    """Treat Windows junctions and symlinks as unsafe path indirections."""
    try:
        info = path.lstat()
    except OSError:
        raise
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _safe_relative(value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InstallationError("inventory contains an invalid path")
    candidate = PurePath(value)
    raw_parts = value.split("/")
    if (candidate.is_absolute() or any(part in ("", ".", "..") for part in raw_parts)
            or any(part[-1:] in (" ", ".") for part in raw_parts)):
        raise InstallationError(f"inventory path is unsafe: {value!r}")
    # A Windows manifest must remain safe when inspected on a non-Windows host.
    if ":" in value or "\\" in value or any(
            any(char in '<>:"\\|?*' or ord(char) < 32 for char in part)
            or part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
            for part in raw_parts):
        raise InstallationError(f"inventory path is unsafe: {value!r}")
    return Path(*raw_parts)


def _path_under(root: Path, relative: str) -> Path:
    return root / _safe_relative(relative)


def _assert_contained_path(root: Path, path: Path) -> None:
    """Ensure an existing child path cannot escape through a reparse ancestor."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise InstallationError("path escapes its installation root") from exc
    current = root
    if _is_reparse(current):
        raise InstallationError("installation root is a reparse point")
    for part in relative.parts:
        current /= part
        if _path_exists_or_link(current) and _is_reparse(current):
            raise InstallationError(f"reparse point is not allowed in path: {current}")


def _validate_tree_root(path: Path, *, allow_missing: bool = False) -> None:
    """Reject a reparse point at the root or any existing ancestor."""
    absolute = path.absolute()
    if not absolute.is_absolute():
        raise InstallationError("installation target must be absolute")
    probe = absolute
    missing: list[Path] = []
    while not probe.exists() and not probe.is_symlink():
        missing.append(probe)
        if probe.parent == probe:
            break
        probe = probe.parent
    if not probe.exists() and not allow_missing:
        raise InstallationError(f"path does not exist: {path}")
    chain = [probe]
    while chain[-1].parent != chain[-1]:
        chain.append(chain[-1].parent)
    for item in reversed(chain):
        if _path_exists_or_link(item) and _is_reparse(item):
            raise InstallationError(f"reparse point is not allowed in path: {item}")


def _validate_payload(payload: Path) -> Inventory:
    payload = payload.absolute()
    if not payload.is_dir():
        raise InstallationError("payload must be a directory")
    _validate_tree_root(payload)
    files: dict[str, dict[str, Any]] = {}
    casefolded: set[str] = set()
    for current, dirs, names in os.walk(payload, followlinks=False, onerror=_raise_walk_error):
        current_path = Path(current)
        for directory in dirs:
            if _is_reparse(current_path / directory):
                raise InstallationError("payload contains a symlink or junction")
        for name in names:
            item = current_path / name
            if _is_reparse(item) or not item.is_file():
                raise InstallationError("payload contains an unsafe non-file")
            relative = item.relative_to(payload).as_posix()
            _safe_relative(relative)
            first = relative.split("/", 1)[0]
            if relative.casefold() in {name.casefold() for name in IGNORED_PAYLOAD_NAMES}:
                continue
            if (first.casefold() in {name.casefold() for name in RESERVED_ROOT_NAMES - {"Uninstall.exe"}}
                    or first.casefold().startswith(".offloader-")):
                raise InstallationError(f"payload uses a reserved installation path: {relative}")
            folded = relative.casefold()
            if folded in casefolded:
                raise InstallationError("payload has case-colliding file names")
            casefolded.add(folded)
            files[relative] = {"sha256": _sha256(item), "size": item.stat().st_size}
    if not files:
        raise InstallationError("payload contains no files")
    return Inventory(files)


def _inventory_data(inventory: Inventory) -> dict[str, Any]:
    return {"format": FORMAT_VERSION, "files": inventory.files}


def _parse_inventory(data: Any) -> Inventory:
    if not isinstance(data, dict) or data.get("format") != FORMAT_VERSION:
        raise InstallationError("installation manifest has an unsupported format")
    records = data.get("files")
    if not isinstance(records, dict) or not records:
        raise InstallationError("installation manifest has no files")
    parsed: dict[str, dict[str, Any]] = {}
    casefolded: set[str] = set()
    for relative, record in records.items():
        _safe_relative(relative)
        first = relative.split("/", 1)[0]
        if (first.casefold() in {name.casefold() for name in RESERVED_ROOT_NAMES - {"Uninstall.exe"}}
                or first.casefold().startswith(".offloader-")):
            raise InstallationError("installation manifest claims a reserved path")
        if not isinstance(record, dict):
            raise InstallationError("installation manifest has an invalid record")
        digest = record.get("sha256")
        size = record.get("size")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest.lower())
                or not isinstance(size, int) or size < 0):
            raise InstallationError("installation manifest has an invalid file hash")
        folded = relative.casefold()
        if folded in casefolded:
            raise InstallationError("installation manifest has case-colliding files")
        casefolded.add(folded)
        parsed[relative] = {"sha256": digest.lower(), "size": size}
    return Inventory(parsed)


def _read_json(path: Path) -> Any:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise InstallationError(f"{path.name} is too large")
        return json.loads(raw.decode("utf-8"))
    except InstallationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallationError(f"could not read {path.name}") from exc


def _read_manifest(target: Path) -> Inventory | None:
    path = target / MANIFEST_NAME
    if not _path_exists_or_link(path):
        return None
    if _is_reparse(path) or not path.is_file():
        raise InstallationError("installation manifest is unsafe")
    return _parse_inventory(_read_json(path))


def _write_json_atomic(path: Path, data: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _assert_file_matches(path: Path, record: dict[str, Any], *, root: Path | None = None) -> None:
    if root is not None:
        _assert_contained_path(root, path)
    if _is_reparse(path) or not path.is_file() or path.stat().st_size != record["size"]:
        raise InstallationError(f"managed file was changed or is unsafe: {path}")
    if _sha256(path) != record["sha256"]:
        raise InstallationError(f"managed file was changed: {path}")


def _validate_owned_files(target: Path, inventory: Inventory) -> None:
    for relative, record in inventory.files.items():
        path = _path_under(target, relative)
        _assert_contained_path(target, path)
        _assert_file_matches(path, record, root=target)


def _target_is_root(target: Path) -> bool:
    return target == Path(target.anchor)


def _assert_target_preflight(payload: Path, target: Path) -> None:
    if not target.is_absolute():
        raise InstallationError("installation target must be absolute")
    if _target_is_root(target):
        raise InstallationError("installation target cannot be a drive root")
    if os.name == "nt" and str(target).startswith(("\\\\", "\\\\?\\", "\\\\.\\")):
        raise InstallationError("installation target must be on a local drive")
    _validate_tree_root(target, allow_missing=True)
    payload_resolved = payload.resolve(strict=True)
    target_resolved = target.resolve(strict=False)
    try:
        target_resolved.relative_to(payload_resolved)
    except ValueError:
        pass
    else:
        raise InstallationError("installation target cannot be inside the payload")
    try:
        payload_resolved.relative_to(target_resolved)
    except ValueError:
        pass
    else:
        raise InstallationError("payload cannot be inside the installation target")


def _allowed_root_entries(inventory: Inventory | None) -> set[str]:
    entries = {LOCK_NAME, MANIFEST_NAME, MARKER_NAME}
    if inventory is not None:
        entries.update(path.split("/", 1)[0] for path in inventory.files)
    return entries


def _assert_fresh_target(target: Path) -> None:
    if not target.exists():
        return
    manifest = _read_manifest(target)
    if manifest is not None:
        return
    entries = {item.name for item in target.iterdir()}
    if entries - {LOCK_NAME}:
        raise InstallationError("refusing to install into a nonempty unowned directory")


def _assert_destination_collisions(target: Path, old: Inventory | None,
                                   new: Inventory) -> None:
    old_files = old.files if old else {}
    for relative in new.files:
        destination = _path_under(target, relative)
        _assert_contained_path(target, destination)
        for parent in destination.parents:
            if parent == target.parent:
                break
            if parent.exists() and (not parent.is_dir() or _is_reparse(parent)):
                raise InstallationError(f"unowned path blocks installation: {parent}")
            if parent == target:
                break
        if _path_exists_or_link(destination) and relative not in old_files:
            raise InstallationError(f"unowned file collides with installation: {destination}")
        if _path_exists_or_link(destination) and destination.is_dir():
            raise InstallationError(f"directory collides with application file: {destination}")
    if old is None:
        for item in target.iterdir():
            if item.name not in _allowed_root_entries(old):
                raise InstallationError(f"unowned path exists in installation: {item}")


def _make_transaction(target: Path, old: Inventory | None, new: Inventory, stage: str) -> dict[str, Any]:
    token = uuid.uuid4().hex
    backup = f".offloader-backup-{token}"
    marker = {
        "format": FORMAT_VERSION,
        "stage": stage,
        "backup": backup,
        "old": _inventory_data(old) if old else None,
        "new": _inventory_data(new),
        "moved_old": [],
        "promoted": [],
        "created_dirs": [],
        "moving_old": None,
        "moving_promoted": None,
        "phase": "mutating",
    }
    _write_json_atomic(target / MARKER_NAME, marker)
    return marker


def _transaction_path(target: Path, value: Any, prefix: str) -> Path:
    if not isinstance(value, str) or not re.fullmatch(rf"{re.escape(prefix)}[0-9a-f]{{32}}", value):
        raise InstallationError("installation recovery marker is unsafe")
    path = target / value
    if _is_reparse(path) if _path_exists_or_link(path) else False:
        raise InstallationError("installation recovery directory is unsafe")
    return path


def _read_transaction(target: Path) -> tuple[dict[str, Any], Inventory | None, Inventory]:
    marker = target / MARKER_NAME
    if _is_reparse(marker) or not marker.is_file():
        raise InstallationError("installation recovery marker is unsafe")
    data = _read_json(marker)
    if not isinstance(data, dict) or data.get("format") != FORMAT_VERSION:
        raise InstallationError("installation recovery marker is invalid")
    old_data = data.get("old")
    old = _parse_inventory(old_data) if old_data is not None else None
    new = _parse_inventory(data.get("new"))
    if data.get("phase") not in ("mutating", "committing", "committed"):
        raise InstallationError("installation recovery marker is invalid")
    data.setdefault("created_dirs", [])
    for key in ("moved_old", "promoted", "created_dirs"):
        values = data.get(key)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise InstallationError("installation recovery marker is invalid")
        if len(values) != len(set(values)):
            raise InstallationError("installation recovery marker is invalid")
    for value in data["created_dirs"]:
        # Rejected here rather than at rmdir time: a marker naming `..` or an
        # absolute path has no business being acted on at all.
        _safe_relative(value)
    if any(value not in (old.files if old else {}) for value in data["moved_old"]):
        raise InstallationError("installation recovery marker is invalid")
    if any(value not in new.files for value in data["promoted"]):
        raise InstallationError("installation recovery marker is invalid")
    for key, records in (("moving_old", old.files if old else {}),
                         ("moving_promoted", new.files)):
        value = data.get(key)
        if value is not None and (not isinstance(value, str) or value not in records):
            raise InstallationError("installation recovery marker is invalid")
    _transaction_path(target, data.get("stage"), ".offloader-stage-")
    _transaction_path(target, data.get("backup"), ".offloader-backup-")
    return data, old, new


def _save_transaction(target: Path, transaction: dict[str, Any]) -> None:
    _write_json_atomic(target / MARKER_NAME, transaction)


def _remove_owned_tree(root: Path, allowed: Iterable[str]) -> None:
    """Remove a temporary tree only when every file is one we created."""
    allowed_set = set(allowed)
    if not root.exists():
        return
    if _is_reparse(root) or not root.is_dir():
        raise InstallationError("transaction directory is unsafe")
    found: set[str] = set()
    directories: list[Path] = []
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False,
                                        onerror=_raise_walk_error):
        current_path = Path(current)
        directories.append(current_path)
        for directory in dirs:
            if _is_reparse(current_path / directory):
                raise InstallationError("transaction directory is unsafe")
        for name in names:
            item = current_path / name
            if _is_reparse(item) or not item.is_file():
                raise InstallationError("transaction directory is unsafe")
            found.add(item.relative_to(root).as_posix())
    if not found.issubset(allowed_set):
        raise InstallationError("transaction directory contains unowned files")
    for relative in found:
        (root / relative).unlink()
    for directory in reversed(directories):
        directory.rmdir()


def _create_parents(target: Path, destination: Path,
                    transaction: dict[str, Any]) -> None:
    """Create ``destination``'s parents under ``target``, recording the new ones.

    A first install of a frozen bundle creates `_internal` on the way to
    promoting its files. Creating those with ``parents=True`` and forgetting
    them meant a rolled-back install left the target holding an empty
    `_internal`, which `_assert_fresh_target` reads as a nonempty unowned
    directory: a transient failure could not be retried through the installer
    even though recovery reported no outstanding transaction. Recording them
    lets rollback take back exactly what it made, and nothing else.

    Recorded before they exist, not after, so a crash in between leaves the
    marker naming a directory that is not there rather than a directory
    nothing knows about. Pruning tolerates the first and cannot fix the second.
    """
    try:
        relative = destination.parent.relative_to(target)
    except ValueError as exc:
        raise InstallationError("path escapes its installation root") from exc
    created = transaction["created_dirs"]
    fresh: list[str] = []
    current = target
    for part in relative.parts:
        current /= part
        if not _path_exists_or_link(current):
            value = current.relative_to(target).as_posix()
            if value not in created:
                fresh.append(value)
    if fresh:
        created.extend(fresh)
        _save_transaction(target, transaction)
    destination.parent.mkdir(parents=True, exist_ok=True)


def _prune_created_dirs(target: Path, transaction: dict[str, Any]) -> None:
    """Remove the directories this transaction created, deepest first.

    Only while still empty, and never the installation root: anything that
    arrived in one of them belongs to somebody else, and a directory that
    already existed was never ours to remove.
    """
    created = transaction.get("created_dirs") or []
    for relative in sorted(created, key=lambda value: (-value.count("/"), value)):
        path = _path_under(target, relative)
        if _path_exists_or_link(path):
            _assert_contained_path(target, path)
            if _is_reparse(path) or not path.is_dir():
                raise InstallationError(f"unowned path replaced a created directory: {path}")
            if any(path.iterdir()):
                continue
            path.rmdir()
        transaction["created_dirs"].remove(relative)
        _save_transaction(target, transaction)


def _recover(target: Path) -> None:
    marker = target / MARKER_NAME
    if not marker.exists():
        return
    data = _read_json(marker)
    if isinstance(data, dict) and data.get("action") == "uninstall":
        _recover_uninstall(target, data)
        return
    transaction, old, new = _read_transaction(target)
    stage = _transaction_path(target, transaction["stage"], ".offloader-stage-")
    backup = _transaction_path(target, transaction["backup"], ".offloader-backup-")
    _reconcile_install_intents(target, transaction, old, new, stage, backup)
    if transaction["phase"] == "committing":
        manifest = _read_manifest(target)
        if manifest == new:
            transaction["phase"] = "committed"
            _save_transaction(target, transaction)
        elif manifest is None or (old is not None and manifest == old):
            # The manifest write is what commits an update, and it is atomic:
            # either the new inventory landed or the previous one is still
            # there. Both mean it did not commit, so both roll back. Treating
            # the surviving old manifest as a mismatch instead wedged the
            # installation -- every later install or uninstall recovers first,
            # and recovery raised.
            transaction["phase"] = "mutating"
            _save_transaction(target, transaction)
        else:
            raise InstallationError("installation manifest does not match a committing update")
    if transaction["phase"] == "committed":
        _validate_owned_files(target, new)
        _remove_owned_tree(stage, new.files)
        _remove_owned_tree(backup, old.files if old else ())
        marker.unlink()
        return
    for relative in tuple(transaction["promoted"]):
        destination = _path_under(target, relative)
        if _path_exists_or_link(destination):
            _assert_file_matches(destination, new.files[relative], root=target)
            destination.unlink()
        transaction["promoted"].remove(relative)
        _save_transaction(target, transaction)
    if old is not None:
        for relative in tuple(transaction["moved_old"]):
            source = _path_under(backup, relative)
            destination = _path_under(target, relative)
            _assert_contained_path(target, destination)
            if _path_exists_or_link(source):
                _assert_file_matches(source, old.files[relative], root=backup)
                if _path_exists_or_link(destination):
                    raise InstallationError("cannot safely recover an occupied application path")
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
            elif _path_exists_or_link(destination):
                _assert_file_matches(destination, old.files[relative], root=target)
            else:
                raise InstallationError("cannot safely recover a missing application file")
            transaction["moved_old"].remove(relative)
            _save_transaction(target, transaction)
        _write_json_atomic(target / MANIFEST_NAME, _inventory_data(old))
    else:
        manifest = target / MANIFEST_NAME
        if manifest.exists():
            raise InstallationError("cannot safely recover an unexpected manifest")
    _remove_owned_tree(stage, new.files)
    _remove_owned_tree(backup, old.files if old else ())
    # Last, because restoring an upgrade's old files refills the directories it
    # promoted into, and those are not empty and not ours to remove.
    _prune_created_dirs(target, transaction)
    marker.unlink()


def _reconcile_install_intents(target: Path, transaction: dict[str, Any], old: Inventory | None,
                               new: Inventory, stage: Path, backup: Path) -> None:
    """Resolve the tiny crash interval between a recorded intent and os.replace."""
    moving_old = transaction["moving_old"]
    if moving_old is not None:
        record = old.files[moving_old] if old else None
        source = _path_under(target, moving_old)
        destination = _path_under(backup, moving_old)
        if source.exists() and not destination.exists():
            _assert_file_matches(source, record, root=target)
        elif not source.exists() and destination.exists():
            _assert_file_matches(destination, record, root=backup)
            transaction["moved_old"].append(moving_old)
        else:
            raise InstallationError("cannot safely reconcile an interrupted backup move")
        transaction["moving_old"] = None
        _save_transaction(target, transaction)
    moving_new = transaction["moving_promoted"]
    if moving_new is not None:
        source = _path_under(stage, moving_new)
        destination = _path_under(target, moving_new)
        if source.exists() and not destination.exists():
            _assert_file_matches(source, new.files[moving_new], root=stage)
        elif not source.exists() and destination.exists():
            _assert_file_matches(destination, new.files[moving_new], root=target)
            transaction["promoted"].append(moving_new)
        else:
            raise InstallationError("cannot safely reconcile an interrupted promotion")
        transaction["moving_promoted"] = None
        _save_transaction(target, transaction)


def _recover_uninstall(target: Path, transaction: Any) -> None:
    """Restore an interrupted uninstall before any new maintenance operation."""
    if not isinstance(transaction, dict) or transaction.get("format") != FORMAT_VERSION:
        raise InstallationError("installation recovery marker is invalid")
    old = _parse_inventory(transaction.get("old"))
    moved = transaction.get("moved_old")
    if (not isinstance(moved, list) or any(not isinstance(item, str) for item in moved)
            or len(moved) != len(set(moved)) or any(item not in old.files for item in moved)):
        raise InstallationError("installation recovery marker is invalid")
    backup = _transaction_path(target, transaction.get("backup"), ".offloader-uninstall-")
    phase = transaction.get("phase")
    if phase not in ("mutating", "committed"):
        raise InstallationError("installation recovery marker is invalid")
    moving = transaction.get("moving_old")
    if moving is not None and (not isinstance(moving, str) or moving not in old.files):
        raise InstallationError("installation recovery marker is invalid")
    if moving is not None:
        source = _path_under(target, moving)
        destination = _path_under(backup, moving)
        if source.exists() and not destination.exists():
            _assert_file_matches(source, old.files[moving], root=target)
        elif not source.exists() and destination.exists():
            _assert_file_matches(destination, old.files[moving], root=backup)
            moved.append(moving)
        else:
            raise InstallationError("cannot safely reconcile an interrupted uninstall")
        transaction["moving_old"] = None
        _write_json_atomic(target / MARKER_NAME, transaction)
    if phase == "committed":
        if (target / MANIFEST_NAME).exists():
            raise InstallationError("completed uninstall still has an installation manifest")
        _remove_owned_tree(backup, old.files)
        (target / MARKER_NAME).unlink()
        return
    for relative in tuple(moved):
        source = _path_under(backup, relative)
        destination = _path_under(target, relative)
        _assert_contained_path(target, destination)
        if _path_exists_or_link(source):
            _assert_file_matches(source, old.files[relative], root=backup)
            if _path_exists_or_link(destination):
                raise InstallationError("cannot safely recover an occupied application path")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
        elif _path_exists_or_link(destination):
            _assert_file_matches(destination, old.files[relative], root=target)
        else:
            raise InstallationError("cannot safely recover a missing application file")
        moved.remove(relative)
        _write_json_atomic(target / MARKER_NAME, transaction)
    _write_json_atomic(target / MANIFEST_NAME, _inventory_data(old))
    _remove_owned_tree(backup, old.files)
    (target / MARKER_NAME).unlink()


def _copy_to_stage(payload: Path, stage: Path, inventory: Inventory) -> None:
    stage.mkdir()
    try:
        for relative, record in inventory.files.items():
            source = _path_under(payload, relative)
            destination = _path_under(stage, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            _assert_file_matches(destination, record, root=stage)
    except Exception:
        _remove_owned_tree(stage, inventory.files)
        raise


def install(payload: Path, target: Path) -> None:
    """Install ``payload`` into ``target`` without touching unowned files."""
    payload = Path(payload).absolute()
    requested_target = Path(target)
    if not requested_target.is_absolute():
        raise InstallationError("installation target must be absolute")
    target = requested_target.absolute()
    new = _validate_payload(payload)
    _assert_target_preflight(payload, target)
    try:
        target.mkdir(parents=True, exist_ok=True)
        with installation_lock(target, exclusive=True):
            _recover(target)
            _assert_fresh_target(target)
            old = _read_manifest(target)
            if old is not None:
                _validate_owned_files(target, old)
            _assert_destination_collisions(target, old, new)
            stage = target / f".offloader-stage-{uuid.uuid4().hex}"
            transaction = _make_transaction(target, old, new, stage.name)
            backup = target / transaction["backup"]
            try:
                _copy_to_stage(payload, stage, new)
                backup.mkdir()
                if old is not None:
                    for relative in old.files:
                        source = _path_under(target, relative)
                        destination = _path_under(backup, relative)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        _assert_contained_path(target, source)
                        _assert_contained_path(backup, destination)
                        transaction["moving_old"] = relative
                        _save_transaction(target, transaction)
                        os.replace(source, destination)
                        transaction["moved_old"].append(relative)
                        transaction["moving_old"] = None
                        _save_transaction(target, transaction)
                for relative in new.files:
                    source = _path_under(stage, relative)
                    destination = _path_under(target, relative)
                    _create_parents(target, destination, transaction)
                    _assert_contained_path(stage, source)
                    _assert_contained_path(target, destination)
                    transaction["moving_promoted"] = relative
                    _save_transaction(target, transaction)
                    os.replace(source, destination)
                    transaction["promoted"].append(relative)
                    transaction["moving_promoted"] = None
                    _save_transaction(target, transaction)
                transaction["phase"] = "committing"
                _save_transaction(target, transaction)
                _write_json_atomic(target / MANIFEST_NAME, _inventory_data(new))
                transaction["phase"] = "committed"
                _save_transaction(target, transaction)
                _remove_owned_tree(stage, new.files)
                _remove_owned_tree(backup, old.files if old else ())
                (target / MARKER_NAME).unlink()
            except Exception:
                if transaction.get("phase") != "committed":
                    _recover(target)
                raise
    except InstallationBusyError:
        raise


def uninstall(target: Path) -> None:
    """Remove only exact, unchanged files listed in the installation manifest."""
    requested_target = Path(target)
    if not requested_target.is_absolute():
        raise InstallationError("installation target must be an absolute non-root directory")
    target = requested_target.absolute()
    if _target_is_root(target):
        raise InstallationError("installation target must be an absolute non-root directory")
    _validate_tree_root(target)
    try:
        with installation_lock(target, exclusive=True):
            _recover(target)
            inventory = _read_manifest(target)
            if inventory is None:
                raise InstallationError("no Offloader installation manifest found")
            _validate_owned_files(target, inventory)
            backup = target / f".offloader-uninstall-{uuid.uuid4().hex}"
            backup.mkdir()
            marker = {
                "format": FORMAT_VERSION,
                "action": "uninstall",
                "backup": backup.name,
                "old": _inventory_data(inventory),
                "moved_old": [],
                "moving_old": None,
                "phase": "mutating",
            }
            _write_json_atomic(target / MARKER_NAME, marker)
            try:
                for relative in inventory.files:
                    source = _path_under(target, relative)
                    destination = _path_under(backup, relative)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _assert_contained_path(target, source)
                    _assert_contained_path(backup, destination)
                    marker["moving_old"] = relative
                    _write_json_atomic(target / MARKER_NAME, marker)
                    os.replace(source, destination)
                    marker["moved_old"].append(relative)
                    marker["moving_old"] = None
                    _write_json_atomic(target / MARKER_NAME, marker)
                manifest = target / MANIFEST_NAME
                manifest.unlink()
                marker["phase"] = "committed"
                _write_json_atomic(target / MARKER_NAME, marker)
                _remove_owned_tree(backup, inventory.files)
                (target / MARKER_NAME).unlink()
                # Only remove empty directories created by this application.
                for relative in sorted(inventory.files, key=lambda value: value.count("/"), reverse=True):
                    directory = _path_under(target, relative).parent
                    while directory != target:
                        try:
                            directory.rmdir()
                        except OSError:
                            break
                        directory = directory.parent
            except Exception:
                if marker.get("phase") != "committed":
                    _recover(target)
                raise
    except InstallationBusyError:
        raise


def launch(target: Path) -> None:
    """Launch the exact installed GUI under the interactive desktop user."""
    requested_target = Path(target)
    if not requested_target.is_absolute() or _target_is_root(requested_target):
        raise InstallationError("installation target must be an absolute non-root directory")
    target = requested_target.absolute()
    _validate_tree_root(target)
    with installation_lock(target):
        if _path_exists_or_link(target / MARKER_NAME):
            raise InstallationError("installation is incomplete; re-run Setup to recover it")
        inventory = _read_manifest(target)
        if inventory is None or "Offloader.exe" not in inventory.files:
            raise InstallationError("no complete Offloader desktop installation was found")
        executable = target / "Offloader.exe"
        _assert_file_matches(executable, inventory.files["Offloader.exe"], root=target)
        from .windows_launch import launch_as_desktop_user

        launch_as_desktop_user(executable)
