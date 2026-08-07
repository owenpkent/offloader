"""ASC MHL v2.0 — the format ARRI and the ASC recommend.

Classic MHL 1.1 records one flat list of checksums. ASC MHL records a *history*:
a numbered series of manifests in an `ascmhl/` folder at the root of the managed
data, tied together by a chain file that identifies each manifest by its C4 ID.
Each generation labels every hash `original`, `verified` or `failed`, so a
delivery carries the evidence of where in the chain a file went wrong rather
than only that it did.

Everything here was validated against the reference implementation's own worked
example (`ascmitc/mhl`, scenario 02): file hashes, both directory hash variants,
the root hash, and the C4 in the chain file all reproduce byte for byte.
"""

from __future__ import annotations

import datetime as _dt
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from . import PRODUCT_NAME, __version__
from .hashers import ALGORITHMS, c4_of_bytes, get_algorithm, new_hasher
from .models import FileStatus, Job

ASCMHL_DIRNAME = "ascmhl"
CHAIN_FILENAME = "ascmhl_chain.xml"
NAMESPACE = "urn:ASC:MHL:v2.0"
DIRECTORY_NAMESPACE = "urn:ASC:MHL:DIRECTORY:v2.0"
VERSION = "2.0"

#: The spec's ProcessType values.
PROCESS_TRANSFER = "transfer"    # data was copied, archived or restored
PROCESS_IN_PLACE = "in-place"    # a manifest taken over data that did not move
PROCESS_FLATTEN = "flatten"

#: HashFormatType action values.
ACTION_ORIGINAL = "original"
ACTION_VERIFIED = "verified"
ACTION_FAILED = "failed"

_MANIFEST_PATTERN = re.compile(r"^(\d{4,})_.*\.mhl$", re.IGNORECASE)


def _utc(when: _dt.datetime | float) -> _dt.datetime:
    if isinstance(when, (int, float)):
        when = _dt.datetime.fromtimestamp(when, tz=_dt.timezone.utc)
    if when.tzinfo is None:
        when = when.astimezone()
    return when.astimezone(_dt.timezone.utc)


def _iso(when: _dt.datetime | float) -> str:
    return _utc(when).replace(microsecond=0).isoformat()


def manifest_filename(sequence: int, folder_name: str,
                      when: _dt.datetime) -> str:
    """`0001_A002R2EC_2020-01-07_080228Z.mhl`, per section 6.3."""
    moment = _utc(when)
    return (f"{sequence:04d}_{folder_name}_{moment:%Y-%m-%d}_"
            f"{moment:%H%M%S}Z.mhl")


# ------------------------------------------------------------------ hashing


def hash_of_hashes(digests: list[str], algorithm_key: str) -> str:
    """Appendix G: sort the hashes lexicographically, write their bytes into a
    fresh generator, digest."""
    hasher = new_hasher(algorithm_key)
    for digest in sorted(digests):
        hasher.update(bytes.fromhex(digest))
    return hasher.hexdigest()


def _structure_entry(name: str, digest: str, algorithm_key: str) -> str:
    """A child's encoded name concatenated with its encoded hash, hashed."""
    hasher = new_hasher(algorithm_key)
    hasher.update(name.encode("utf-8") + bytes.fromhex(digest))
    return hasher.hexdigest()


@dataclass
class _Node:
    """One directory in the managed data set."""

    name: str
    #: name -> (digest, action). The action is kept because a `failed` hash
    #: must still be *recorded* — that is the evidence of where the chain broke
    #: — while being excluded from any directory hash computed over it.
    files: dict[str, tuple[str, str]] = field(default_factory=dict)
    directories: dict[str, _Node] = field(default_factory=dict)

    def child(self, name: str) -> _Node:
        return self.directories.setdefault(name, _Node(name))


def _build_tree(entries: list[tuple[Path, str, str]]) -> _Node:
    root = _Node("")
    for relative, digest, action in entries:
        node = root
        for part in relative.parts[:-1]:
            node = node.child(part)
        node.files[relative.name] = (digest, action)
    return root


def _directory_hashes(node: _Node, algorithm_key: str) -> tuple[str, str]:
    """(content, structure) for a directory, computed bottom-up.

    Only hashes that stand as evidence contribute: a `failed` hash means the
    file is not what it was, so folding it in would produce a directory hash
    that certifies a known-bad tree.
    """
    content_inputs: list[str] = []
    structure_inputs: list[str] = []

    for name in sorted(node.directories):
        child_content, child_structure = _directory_hashes(
            node.directories[name], algorithm_key)
        content_inputs.append(child_content)
        structure_inputs.append(
            _structure_entry(name, child_structure, algorithm_key))

    for name in sorted(node.files):
        digest, action = node.files[name]
        if action == ACTION_FAILED:
            continue
        content_inputs.append(digest)
        structure_inputs.append(_structure_entry(name, digest, algorithm_key))

    return (hash_of_hashes(content_inputs, algorithm_key),
            hash_of_hashes(structure_inputs, algorithm_key))


# ------------------------------------------------------------------ history


def ascmhl_dir(root: Path) -> Path:
    return Path(root) / ASCMHL_DIRNAME


def existing_manifests(root: Path) -> list[Path]:
    """Manifests already in this history, in sequence order."""
    directory = ascmhl_dir(root)
    if not directory.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for candidate in directory.iterdir():
        match = _MANIFEST_PATTERN.match(candidate.name)
        if match and candidate.is_file():
            found.append((int(match.group(1)), candidate))
    return [path for _, path in sorted(found)]


def next_sequence(root: Path) -> int:
    manifests = existing_manifests(root)
    if not manifests:
        return 1
    last = _MANIFEST_PATTERN.match(manifests[-1].name)
    return int(last.group(1)) + 1


def read_manifest_hashes(manifest: Path) -> dict[str, dict[str, str]]:
    """{relative path: {algorithm tag: digest}} from an existing manifest.

    Only hashes labelled `original` or `verified` are returned: the spec is
    explicit that a `failed` hash may not be used for verification.
    """
    result: dict[str, dict[str, str]] = {}
    try:
        root = ET.parse(manifest).getroot()
    except (ET.ParseError, OSError):
        return result

    for node in root.iter(f"{{{NAMESPACE}}}hash"):
        path_element = node.find(f"{{{NAMESPACE}}}path")
        if path_element is None or not path_element.text:
            continue
        digests: dict[str, str] = {}
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag == "path" or not child.text:
                continue
            if child.get("action", ACTION_ORIGINAL) == ACTION_FAILED:
                continue
            digests[tag] = child.text.strip()
        if digests:
            result[path_element.text.strip()] = digests
    return result


def _previous_hashes(root: Path) -> dict[str, dict[str, str]]:
    manifests = existing_manifests(root)
    return read_manifest_hashes(manifests[-1]) if manifests else {}


# ------------------------------------------------------------------ writing


def _indent_and_write(element: ET.Element, path: Path) -> None:
    # ElementTree writes the declaration with single quotes; the reference
    # implementation and the spec's examples use double. Match them, so a
    # manifest can be diffed against another tool's output directly.
    ET.indent(element, space="  ")
    declaration = '<?xml version="1.0" encoding="UTF-8"?>\n'
    body = ET.tostring(element, encoding="unicode")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((declaration + body + "\n").encode("utf-8"))


def write_chain(root: Path) -> Path:
    """Rewrite the chain file from the manifests present, C4-identifying each."""
    directory = ascmhl_dir(root)
    chain = ET.Element("ascmhldirectory", {"xmlns": DIRECTORY_NAMESPACE})

    for manifest in existing_manifests(root):
        sequence = int(_MANIFEST_PATTERN.match(manifest.name).group(1))
        node = ET.SubElement(chain, "hashlist", {"sequencenr": str(sequence)})
        ET.SubElement(node, "path").text = manifest.name
        ET.SubElement(node, "c4").text = c4_of_bytes(manifest.read_bytes())

    target = directory / CHAIN_FILENAME
    _indent_and_write(chain, target)
    return target


def write_manifest(job: Job, root: Path, *, destination_index: int = 0,
                   process: str = PROCESS_TRANSFER,
                   algorithm_key: str | None = None,
                   ignore_patterns: list[str] | None = None,
                   directory_hashes: bool = True,
                   when: _dt.datetime | None = None) -> Path:
    """Write one ASC MHL generation for the copy at `root`, and update the chain.

    Each file's hash is labelled by comparing it against the newest manifest
    already in this history: unseen files are `original`, matching files are
    `verified`, differing files are `failed`.
    """
    root = Path(root)
    moment = _utc(when or job.finished or job.started)
    algorithm_key = algorithm_key or _algorithm_for(job)
    algorithm = get_algorithm(algorithm_key)
    tag = algorithm.mhl_tag or "xxh64"

    previous = _previous_hashes(root)
    entries: list[tuple[Path, str, int, float, str]] = []   # rel, digest, size, mtime, action

    for entry in job.files:
        if destination_index >= len(entry.destinations):
            continue
        destination = entry.destinations[destination_index]
        digest = destination.checksum or entry.checksum
        if not digest:
            continue
        try:
            relative = Path(destination.path).relative_to(root)
        except ValueError:
            continue

        key = relative.as_posix()
        if destination.status is FileStatus.FAILED:
            action = ACTION_FAILED
        elif key in previous:
            action = (ACTION_VERIFIED if previous[key].get(tag) == digest
                      else ACTION_FAILED)
        else:
            action = ACTION_ORIGINAL
        entries.append((relative, digest, entry.size, entry.modified, action))

    manifest = ET.Element("hashlist", {"version": VERSION, "xmlns": NAMESPACE})

    creator = ET.SubElement(manifest, "creatorinfo")
    ET.SubElement(creator, "creationdate").text = _iso(moment)
    ET.SubElement(creator, "hostname").text = platform.node()
    ET.SubElement(creator, "tool", {"version": __version__}).text = PRODUCT_NAME

    info = ET.SubElement(manifest, "processinfo")
    ET.SubElement(info, "process").text = process

    # Every entry is recorded, including failed ones — that is the evidence of
    # where the chain broke. `_directory_hashes` is what excludes them.
    tree = _build_tree([(rel, digest, action)
                        for rel, digest, _size, _mtime, action in entries]
                       ) if entries else None

    if directory_hashes and tree is not None:
        content, structure = _directory_hashes(tree, algorithm_key)
        root_hash = ET.SubElement(info, "roothash")
        _hash_pair(root_hash, tag, content, structure, moment)

    patterns = ignore_patterns if ignore_patterns is not None else [ASCMHL_DIRNAME]
    if patterns:
        ignore = ET.SubElement(info, "ignore")
        for pattern in patterns:
            ET.SubElement(ignore, "pattern").text = pattern

    hashes = ET.SubElement(manifest, "hashes")
    lookup = {rel.as_posix(): (digest, size, modified, action)
              for rel, digest, size, modified, action in entries}
    if tree is not None:
        _emit(hashes, tree, Path("."), lookup, tag, algorithm_key, moment,
              directory_hashes, root)

    target = ascmhl_dir(root) / manifest_filename(
        next_sequence(root), root.name or "root", moment)
    _indent_and_write(manifest, target)
    write_chain(root)
    return target


def _hash_pair(parent: ET.Element, tag: str, content: str, structure: str,
               moment: _dt.datetime) -> None:
    content_element = ET.SubElement(parent, "content")
    ET.SubElement(content_element, tag, {"hashdate": _iso(moment)}).text = content
    structure_element = ET.SubElement(parent, "structure")
    ET.SubElement(structure_element, tag,
                  {"hashdate": _iso(moment)}).text = structure


def _emit(parent: ET.Element, node: _Node, prefix: Path,
          lookup: dict, tag: str, algorithm_key: str,
          moment: _dt.datetime, directory_hashes: bool,
          directory_root: Path | None = None) -> None:
    """Depth-first in sorted order: a directory's contents, then its own hash.

    Matches the ordering the reference implementation produces.
    """
    for name in sorted(node.directories):
        child = node.directories[name]
        child_prefix = Path(name) if prefix == Path(".") else prefix / name
        _emit(parent, child, child_prefix, lookup, tag, algorithm_key, moment,
              directory_hashes, directory_root)
        if directory_hashes:
            content, structure = _directory_hashes(child, algorithm_key)
            element = ET.SubElement(parent, "directoryhash")
            attributes = {}
            if directory_root is not None:
                try:
                    attributes["lastmodificationdate"] = _iso(
                        (directory_root / child_prefix).stat().st_mtime)
                except OSError:
                    pass
            ET.SubElement(element, "path",
                          attributes).text = child_prefix.as_posix()
            _hash_pair(element, tag, content, structure, moment)

    for name in sorted(node.files):
        relative = (Path(name) if prefix == Path(".") else prefix / name).as_posix()
        digest, size, modified, action = lookup[relative]
        element = ET.SubElement(parent, "hash")
        ET.SubElement(element, "path", {
            "size": str(size),
            "lastmodificationdate": _iso(modified),
        }).text = relative
        ET.SubElement(element, tag, {
            "action": action,
            "hashdate": _iso(moment),
        }).text = digest


def _algorithm_for(job: Job) -> str:
    for key, algorithm in ALGORITHMS.items():
        if algorithm.label == job.hash_label and algorithm.mhl_tag:
            return key
    return "xxh64"


def write_ascmhl(job: Job, path: Path, *, destination_index: int = 0,
                 **options) -> Path:
    """Report-writer entry point.

    `path` is the conventional report location; ASC MHL ignores it and writes
    into `ascmhl/` at the root of the copy, which is where the format requires
    a history to live.
    """
    roots = job.destination_roots or [job.source_root]
    index = min(destination_index, len(roots) - 1)
    # The writer interface is shared with the PDF, which takes logo/footer.
    # Accept and drop anything that does not apply here.
    accepted = {"process", "algorithm_key", "ignore_patterns",
                "directory_hashes", "when"}
    return write_manifest(
        job, roots[index], destination_index=index,
        **{k: v for k, v in options.items() if k in accepted},
    )
