"""Re-verify an offloaded tree against its MHL manifest.

This is the check that should stand between a card and the format button, and
the one to run again months later when the question is whether an archive has
rotted. Nothing here writes to the media it inspects.

An MHL is a chain-of-custody document: it records what each file hashed to at
the moment it was copied. Re-hashing later and comparing is the only way to
detect corruption that happened *after* the offload — bit rot, a failing drive,
a bad cable on the way to the archive.

An ASC MHL history records more than file hashes: every directory carries a
content hash and a structure hash. Those are re-checked here too, because they
catch what no file hash can — a rename, or a file moved between folders, where
every individual file is still perfectly intact.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from xml.etree import ElementTree as ET

from .ascmhl import ASCMHL_DIRNAME
from .ascmhl import NAMESPACE as ASCMHL_NAMESPACE
from .ascmhl import directory_hashes as ascmhl_directory_hashes
from .hashers import ALGORITHMS, hash_file
from .integrity import evict_from_cache

#: Where this tool files its own paperwork, as a pattern. Only used to read
#: histories written before the writer recorded the directory itself; a current
#: manifest carries the real path, which handles `--report-dir` as this cannot.
REPORT_DIRECTORY_GLOB = "*_Reports"


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


class DirectoryResult(str, Enum):
    OK = "ok"
    RENAMED = "renamed"    # the same bytes under a different name or layout
    CHANGED = "changed"    # the content itself no longer hashes the same
    MISSING = "missing"    # nothing of the directory is left on disk


@dataclass
class DirectoryVerdict:
    """One directory's recorded hashes against what its contents hash to now.

    The two hashes answer different questions. Content covers the file hashes
    alone, so it survives a rename. Structure folds each name in with its hash,
    so it does not. Content matching while structure does not is therefore a
    precise statement: nothing was corrupted, something was renamed or moved.
    """

    path: Path
    #: Relative to the root of the managed data; `"."` is that root.
    relative: str
    result: DirectoryResult
    expected_content: str | None = None
    actual_content: str | None = None
    expected_structure: str | None = None
    actual_structure: str | None = None
    #: A file inside already failed on its own hash, which is enough to account
    #: for this. Without it a single corrupt file reads as one failure per
    #: directory between it and the root.
    explained_by_files: bool = False

    @property
    def ok(self) -> bool:
        return self.result is DirectoryResult.OK

    def describe(self) -> str:
        label = f"{self.relative}/" if self.relative != "." else "(root)"
        if self.result is DirectoryResult.OK:
            return f"ok        {label}"
        if self.result is DirectoryResult.MISSING:
            return f"MISSING   {label}"
        if self.result is DirectoryResult.RENAMED:
            return (f"RENAMED   {label}\n"
                    "            every file still hashes as recorded, so a "
                    "name changed or a file moved")
        tail = ("\n            (accounted for by the file failures above)"
                if self.explained_by_files else "")
        actual = self.actual_content or "(no files remain)"
        return (f"CHANGED   {label}\n"
                f"            expected {self.expected_content}\n"
                f"            actual   {actual}{tail}")


@dataclass
class VerifyReport:
    manifest: Path
    algorithm: str
    verdicts: list[FileVerdict] = field(default_factory=list)
    #: Files present on disk that the manifest does not mention.
    unlisted: list[Path] = field(default_factory=list)
    #: One per directory hash the manifest recorded. Only ASC MHL has these.
    directories: list[DirectoryVerdict] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return len(self.verdicts)

    @property
    def failures(self) -> list[FileVerdict]:
        return [v for v in self.verdicts if not v.ok]

    @property
    def directory_failures(self) -> list[DirectoryVerdict]:
        return [v for v in self.directories if not v.ok]

    @property
    def passed(self) -> bool:
        return (bool(self.verdicts) and not self.failures
                and not self.directory_failures)

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for verdict in self.verdicts:
            tally[verdict.result.value] = tally.get(verdict.result.value, 0) + 1
        return tally

    def summary(self) -> str:
        if not self.verdicts:
            return "manifest listed no files with checksums"
        if self.passed:
            if self.directories:
                return (f"all {self.checked} files and all "
                        f"{len(self.directories)} directory hashes match the "
                        "manifest")
            return f"all {self.checked} files match the manifest"
        parts = [f"{count} {name}" for name, count in sorted(self.counts().items())]
        line = f"{self.checked} checked: " + ", ".join(parts)
        if self.directory_failures:
            line += (f"; {len(self.directory_failures)} of "
                     f"{len(self.directories)} directory hashes differ")
        return line


#: MHL element name -> our algorithm key.
_TAG_TO_ALGORITHM = {
    algorithm.mhl_tag: key
    for key, algorithm in ALGORITHMS.items() if algorithm.mhl_tag
}


def find_manifests(root: Path) -> list[Path]:
    """Every manifest worth checking under `root`, newest last.

    An ASC MHL history holds one manifest per generation, all covering the same
    files. Only the newest is returned, so verifying a three-generation history
    hashes the media once rather than three times.
    """
    root = Path(root)
    histories: dict[Path, list[Path]] = {}
    standalone: list[Path] = []

    for candidate in root.rglob("*.mhl"):
        if candidate.parent.name == ASCMHL_DIRNAME:
            histories.setdefault(candidate.parent, []).append(candidate)
        else:
            standalone.append(candidate)

    newest = [max(generation, key=lambda p: p.name)
              for generation in histories.values()]
    return sorted(standalone + newest, key=lambda p: p.stat().st_mtime)


def _entries(manifest: Path) -> Iterator[tuple[Path, str | None, str | None, int | None]]:
    """(path, algorithm key, expected digest, expected size) per manifest entry.

    Handles both classic MHL 1.1 (`<hash><file>`) and ASC MHL v2
    (`<hash><path>`, namespaced, and living in an `ascmhl/` folder so its paths
    are relative to the folder above).
    """
    root = ET.parse(manifest).getroot()
    if root.tag == f"{{{ASCMHL_NAMESPACE}}}hashlist":
        yield from _ascmhl_entries(manifest, root)
        return

    base = manifest.parent
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
    check_directories: bool = True,
) -> VerifyReport:
    """Re-hash everything an MHL describes and compare.

    `bypass_cache` evicts each file before reading it, so a freshly written tree
    is read off the device rather than out of memory.

    `check_directories` recomputes an ASC MHL manifest's directory hashes from
    what is on disk. That means hashing the files the manifest does *not* list
    as well: a renamed file is unlisted under its new name, and hashing it is
    what turns "one file missing, one file unexpected" into the far stronger
    "these are the same bytes, the name changed".
    """
    manifest = Path(manifest)
    entries = list(_entries(manifest))
    report = VerifyReport(manifest=manifest,
                          algorithm=next((e[1] for e in entries if e[1]), "unknown"))

    # ASC MHL keeps its manifests in `ascmhl/` at the root of the managed data,
    # so the folder above is what the recorded paths are relative to.
    managed_root = manifest.parent.parent
    ascmhl_root = _ascmhl_root(manifest) if check_directories else None
    recorded: dict[str, tuple[str, str]] = {}
    if ascmhl_root is not None:
        recorded, directory_algorithm = _ascmhl_directory_hashes(ascmhl_root)
        # Recomputing is only meaningful when the directory hashes and the file
        # hashes were taken with the same algorithm.
        if not recorded or directory_algorithm != report.algorithm:
            ascmhl_root, recorded = None, {}

    #: Relative POSIX path -> what it hashes to now, feeding the directory pass.
    on_disk: dict[str, str] = {}
    #: Paths that can no longer stand as evidence, so that one bad file is not
    #: re-reported as a fresh failure for every directory above it.
    unsound: set[str] = set()
    #: Paths on disk the manifest never mentioned. A directory that gained one
    #: is *not* explained by its file failures, however many it has.
    unexpected: set[str] = set()

    def note(path: Path, digest: str | None) -> str | None:
        """Record what a file hashes to now — a `digest` of `None` meaning it
        cannot stand as evidence at all. Returns the key it was filed under."""
        if ascmhl_root is None:
            return None
        try:
            relative = path.relative_to(managed_root).as_posix()
        except ValueError:
            return None
        if digest is None:
            unsound.add(relative)
        else:
            on_disk[relative] = digest
        return relative

    for index, (path, algorithm_key, expected, expected_size) in enumerate(entries):
        if progress:
            progress(index, len(entries), path)

        if algorithm_key is None or not expected:
            # Including a hash the manifest itself disowned would contradict the
            # writer, which left it out of the directory hashes for the same
            # reason. Deliberately not noted either way.
            report.verdicts.append(FileVerdict(path, EntryResult.NO_CHECKSUM))
            continue
        if not path.exists():
            note(path, None)
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
            note(path, None)
            report.verdicts.append(
                FileVerdict(path, EntryResult.UNREADABLE, expected=expected,
                            detail=str(exc)))
            continue

        matched = actual == expected
        relative = note(path, actual)
        if not matched and relative is not None:
            unsound.add(relative)
        report.verdicts.append(FileVerdict(
            path,
            EntryResult.OK if matched else EntryResult.MISMATCH,
            expected=expected, actual=actual,
            expected_size=expected_size, actual_size=actual_size,
        ))

    # The directory pass needs the unexpected files hashed, so it scans even
    # when the caller did not ask for them to be reported.
    if find_unlisted or ascmhl_root is not None:
        listed = {p.resolve() for p, _, _, _ in entries}
        patterns = _ignore_patterns(ascmhl_root) if ascmhl_root is not None else []
        scan_root = (managed_root if ascmhl_root is not None
                     else _described_root(manifest, listed))
        # Manifests written before the report directory was recorded as ignored
        # say nothing about it, and folding the tool's own paperwork into a
        # recomputed hash reports the report as a change to the tree. Keyed off
        # the absence of any recorded pattern but the history's own folder, so
        # it stops applying the moment a manifest describes its own layout.
        legacy_paperwork = not [p for p in patterns if p != ASCMHL_DIRNAME]

        for candidate in scan_root.rglob("*"):
            if not candidate.is_file() or candidate.suffix.lower() == ".mhl":
                continue
            # The history's own bookkeeping is not managed data.
            if ASCMHL_DIRNAME in candidate.parts:
                continue
            if candidate.resolve() in listed:
                continue
            relative = candidate.relative_to(scan_root).as_posix()
            if _ignored(relative, patterns):
                continue

            if find_unlisted:
                report.unlisted.append(candidate)
            if ascmhl_root is not None:
                if legacy_paperwork and _ignored(relative, [REPORT_DIRECTORY_GLOB]):
                    continue
                unexpected.add(relative)
                try:
                    if bypass_cache:
                        evict_from_cache(candidate)
                    on_disk[relative] = hash_file(candidate, report.algorithm)
                except OSError:
                    unsound.add(relative)

    if recorded:
        report.directories = _directory_verdicts(
            managed_root, recorded, report.algorithm, on_disk,
            unsound, unexpected)

    return report


def _ascmhl_root(manifest: Path) -> ET.Element | None:
    """The parsed manifest, but only if it is an ASC MHL one.

    Classic MHL 1.1 has no concept of a directory hash, so there is nothing to
    re-check and nothing to read.
    """
    root = ET.parse(manifest).getroot()
    return root if root.tag == f"{{{ASCMHL_NAMESPACE}}}hashlist" else None


def _ascmhl_directory_hashes(
        root: ET.Element) -> tuple[dict[str, tuple[str, str]], str | None]:
    """The recorded (content, structure) per directory, and their algorithm.

    The root of the managed data is written as `<roothash>` inside
    `<processinfo>` rather than as one of the `<directoryhash>` elements, so it
    is keyed `"."` here to line up with what `ascmhl.directory_hashes` returns.
    """
    prefix = f"{{{ASCMHL_NAMESPACE}}}"
    recorded: dict[str, tuple[str, str]] = {}
    algorithm_key: str | None = None

    def pair(element: ET.Element) -> tuple[str, str] | None:
        nonlocal algorithm_key
        sides: list[str] = []
        for side in ("content", "structure"):
            holder = element.find(f"{prefix}{side}")
            if holder is None:
                return None
            for child in holder:
                name = child.tag.split("}")[-1]
                if name in _TAG_TO_ALGORITHM and child.text:
                    sides.append(child.text.strip())
                    algorithm_key = algorithm_key or _TAG_TO_ALGORITHM[name]
                    break
        return (sides[0], sides[1]) if len(sides) == 2 else None

    for element in root.iter(f"{prefix}roothash"):
        values = pair(element)
        if values:
            recorded["."] = values

    for element in root.iter(f"{prefix}directoryhash"):
        path_element = element.find(f"{prefix}path")
        if path_element is None or not path_element.text:
            continue
        values = pair(element)
        if values:
            recorded[path_element.text.strip().strip("/")] = values

    return recorded, algorithm_key


def _ignore_patterns(root: ET.Element) -> list[str]:
    """What the writer recorded as excluded from the managed data.

    Honouring it matters more here than for the unlisted list: a file the
    manifest deliberately ignored is not evidence, and folding it into a
    recomputed directory hash would make every directory above it mismatch.
    """
    prefix = f"{{{ASCMHL_NAMESPACE}}}"
    return [element.text.strip()
            for info in root.iter(f"{prefix}processinfo")
            for element in info.iter(f"{prefix}pattern")
            if element.text and element.text.strip()]


def _ignored(relative: str, patterns: list[str]) -> bool:
    """A pattern matches the whole relative path or any one component of it."""
    parts = relative.split("/")
    for pattern in patterns:
        if fnmatch.fnmatch(relative, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def _within(relative: str, directory: str) -> bool:
    return (directory == "."
            or relative == directory
            or relative.startswith(f"{directory}/"))


def _directory_verdicts(base: Path, recorded: dict[str, tuple[str, str]],
                        algorithm_key: str, on_disk: dict[str, str],
                        unsound: set[str],
                        unexpected: set[str]) -> list[DirectoryVerdict]:
    """Every recorded directory hash against the tree as it stands now."""
    computed = ascmhl_directory_hashes(
        [(Path(relative), digest) for relative, digest in on_disk.items()],
        algorithm_key)

    verdicts: list[DirectoryVerdict] = []
    for relative in sorted(recorded):
        expected_content, expected_structure = recorded[relative]
        path = base if relative == "." else base / relative
        # Only fully explained when every difference underneath was already
        # reported file by file. A file that arrived was not.
        explained = (any(_within(bad, relative) for bad in unsound)
                     and not any(_within(extra, relative) for extra in unexpected))
        found = computed.get(relative)

        if found is None:
            # Nothing hashable is left underneath. Whether the folder itself
            # survives is the difference between emptied and gone.
            verdicts.append(DirectoryVerdict(
                path, relative,
                DirectoryResult.CHANGED if path.is_dir() else DirectoryResult.MISSING,
                expected_content=expected_content,
                expected_structure=expected_structure,
                explained_by_files=explained))
            continue

        actual_content, actual_structure = found
        if actual_content != expected_content:
            result = DirectoryResult.CHANGED
        elif actual_structure != expected_structure:
            result = DirectoryResult.RENAMED
        else:
            result = DirectoryResult.OK

        verdicts.append(DirectoryVerdict(
            path, relative, result,
            expected_content=expected_content, actual_content=actual_content,
            expected_structure=expected_structure, actual_structure=actual_structure,
            explained_by_files=explained and result is not DirectoryResult.OK))
    return verdicts


def _ascmhl_entries(manifest: Path, root: ET.Element):
    """ASC MHL paths are relative to the root of the managed data, which is the
    parent of the `ascmhl` folder the manifest sits in."""
    base = manifest.parent.parent
    tag_prefix = f"{{{ASCMHL_NAMESPACE}}}"

    for node in root.iter(f"{tag_prefix}hash"):
        path_element = node.find(f"{tag_prefix}path")
        if path_element is None or not path_element.text:
            continue
        try:
            size = int(path_element.get("size", ""))
        except ValueError:
            size = None

        algorithm_key = digest = None
        for child in node:
            name = child.tag.split("}")[-1]
            if name == "path" or not child.text:
                continue
            # A hash the manifest itself marked failed is not a reference.
            if child.get("action") == "failed":
                continue
            if name in _TAG_TO_ALGORITHM:
                algorithm_key = _TAG_TO_ALGORITHM[name]
                digest = child.text.strip()
                break
        yield (base / path_element.text.strip()), algorithm_key, digest, size


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
