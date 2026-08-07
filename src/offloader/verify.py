"""Re-verify an offloaded tree against its MHL manifest.

This is the check that should stand between a card and the format button, and
the one to run again months later when the question is whether an archive has
rotted. Nothing here writes to the media it inspects.

An MHL is a chain-of-custody document: it records what each file hashed to at
the moment it was copied. Re-hashing later and comparing is the only way to
detect corruption that happened *after* the offload — bit rot, a failing drive,
a bad cable on the way to the archive.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator
from xml.etree import ElementTree as ET

from .hashers import ALGORITHMS, hash_file
from .integrity import evict_from_cache


class EntryResult(str, Enum):
    OK = "ok"
    MISMATCH = "mismatch"       # the file changed since it was recorded
    MISSING = "missing"         # the file is gone
    UNREADABLE = "unreadable"   # present but cannot be read
    NO_CHECKSUM = "no-checksum"  # the manifest recorded none


@dataclass
class FileVerdict:
    path: Path
    result: EntryResult
    expected: str | None = None
    actual: str | None = None
    expected_size: int | None = None
    actual_size: int | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.result is EntryResult.OK

    def describe(self) -> str:
        if self.result is EntryResult.OK:
            return f"ok        {self.path}"
        if self.result is EntryResult.MISSING:
            return f"MISSING   {self.path}"
        if self.result is EntryResult.UNREADABLE:
            return f"UNREADABLE {self.path}  ({self.detail})"
        if self.result is EntryResult.NO_CHECKSUM:
            return f"no hash   {self.path}"
        return (f"MISMATCH  {self.path}\n"
                f"            expected {self.expected}\n"
                f"            actual   {self.actual}")


@dataclass
class VerifyReport:
    manifest: Path
    algorithm: str
    verdicts: list[FileVerdict] = field(default_factory=list)
    #: Files present on disk that the manifest does not mention.
    unlisted: list[Path] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return len(self.verdicts)

    @property
    def failures(self) -> list[FileVerdict]:
        return [v for v in self.verdicts if not v.ok]

    @property
    def passed(self) -> bool:
        return bool(self.verdicts) and not self.failures

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for verdict in self.verdicts:
            tally[verdict.result.value] = tally.get(verdict.result.value, 0) + 1
        return tally

    def summary(self) -> str:
        if not self.verdicts:
            return "manifest listed no files with checksums"
        if self.passed:
            return f"all {self.checked} files match the manifest"
        parts = [f"{count} {name}" for name, count in sorted(self.counts().items())]
        return f"{self.checked} checked: " + ", ".join(parts)


#: MHL element name -> our algorithm key.
_TAG_TO_ALGORITHM = {
    algorithm.mhl_tag: key
    for key, algorithm in ALGORITHMS.items() if algorithm.mhl_tag
}


def find_manifests(root: Path) -> list[Path]:
    """Every MHL under `root`, newest last."""
    found = sorted(Path(root).rglob("*.mhl"), key=lambda p: p.stat().st_mtime)
    return found


def _entries(manifest: Path) -> Iterator[tuple[Path, str | None, str | None, int | None]]:
    """(path, algorithm key, expected digest, expected size) per manifest entry."""
    base = manifest.parent
    root = ET.parse(manifest).getroot()

    for node in root.iter("hash"):
        relative = node.findtext("file")
        if not relative:
            continue
        size_text = node.findtext("size")
        try:
            size = int(size_text) if size_text else None
        except ValueError:
            size = None

        algorithm_key = digest = None
        for tag, key in _TAG_TO_ALGORITHM.items():
            value = node.findtext(tag)
            if value:
                algorithm_key, digest = key, value.strip()
                break
        yield (base / relative), algorithm_key, digest, size


def verify_manifest(
    manifest: Path,
    *,
    progress: Callable[[int, int, Path], None] | None = None,
    bypass_cache: bool = True,
    find_unlisted: bool = True,
) -> VerifyReport:
    """Re-hash everything an MHL describes and compare.

    `bypass_cache` evicts each file before reading it, so a freshly written tree
    is read off the device rather than out of memory.
    """
    manifest = Path(manifest)
    entries = list(_entries(manifest))
    report = VerifyReport(manifest=manifest,
                          algorithm=next((e[1] for e in entries if e[1]), "unknown"))

    for index, (path, algorithm_key, expected, expected_size) in enumerate(entries):
        if progress:
            progress(index, len(entries), path)

        if algorithm_key is None or not expected:
            report.verdicts.append(FileVerdict(path, EntryResult.NO_CHECKSUM))
            continue
        if not path.exists():
            report.verdicts.append(
                FileVerdict(path, EntryResult.MISSING, expected=expected,
                            expected_size=expected_size))
            continue

        try:
            actual_size = path.stat().st_size
            if bypass_cache:
                evict_from_cache(path)
            actual = hash_file(path, algorithm_key)
        except OSError as exc:
            report.verdicts.append(
                FileVerdict(path, EntryResult.UNREADABLE, expected=expected,
                            detail=str(exc)))
            continue

        matched = actual == expected
        report.verdicts.append(FileVerdict(
            path,
            EntryResult.OK if matched else EntryResult.MISMATCH,
            expected=expected, actual=actual,
            expected_size=expected_size, actual_size=actual_size,
        ))

    if find_unlisted:
        listed = {p.resolve() for p, _, _, _ in entries}
        for candidate in _described_root(manifest, listed).rglob("*"):
            if not candidate.is_file() or candidate.suffix.lower() == ".mhl":
                continue
            if candidate.resolve() not in listed:
                report.unlisted.append(candidate)

    return report


def _described_root(manifest: Path, listed: set[Path]) -> Path:
    """The directory a manifest actually describes.

    Reports live in `<name>_Reports/` while the media sits alongside, so
    scanning the manifest's own folder would find nothing. Use the common
    ancestor of the files it lists instead.
    """
    if not listed:
        return manifest.parent
    try:
        return Path(os.path.commonpath([str(p) for p in listed]))
    except ValueError:
        return manifest.parent


def verify_tree(root: Path, **options) -> list[VerifyReport]:
    """Verify every MHL found under `root`."""
    return [verify_manifest(manifest, **options)
            for manifest in find_manifests(root)]
