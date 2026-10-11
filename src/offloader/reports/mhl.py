"""Media Hash List (MHL 1.1) output.

MHL is the interchange format post houses use to re-verify a delivery, so the
paths written here are relative to the MHL's own directory — that is what makes
the file portable with the media it describes.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import os
import platform
from pathlib import Path
from xml.etree import ElementTree as ET

from .. import PRODUCT_NAME, __version__
from ..models import Job
from ..util import xml_safe

_MHL_VERSION = "1.1"

#: XML 1.0 forbids most C0 control characters outright: they cannot even be
#: written as character references. ElementTree emits them raw, producing a
#: document no parser will read. One stray control byte in one filename would
#: therefore strand the verification of the *entire* delivery.
#:
#: The filter lives in util now, so the ASC MHL writer and the CSV writer get
#: the same guarantee instead of each having to rediscover it.
_xml_safe = xml_safe


def _iso(value: float | _dt.datetime) -> str:
    dt = value if isinstance(value, _dt.datetime) else _dt.datetime.fromtimestamp(value)
    return dt.astimezone().replace(microsecond=0).isoformat()


def _relative(target: Path, base: Path) -> str:
    """Path to `target` as seen from the MHL's own directory.

    Must stay relative even when the media sits beside or above the manifest
    rather than beneath it — reports live in `<name>_Reports/`, so the clips are
    typically one level up. `Path.relative_to` cannot express that and would
    silently fall back to an absolute path, which pins the manifest to one
    machine and one drive letter and stops it travelling with the media.

    Computed lexically when both paths are absolute. `os.path.relpath` goes
    through `abspath`, which on Windows trims a trailing dot or space from a
    name: `clip.mov.` would be recorded as `clip.mov`, and verify would then
    check the wrong file.
    """
    try:
        return Path(_lexical_relpath(str(target), str(base))).as_posix()
    except ValueError:
        # Different drives on Windows: no relative path exists.
        return Path(target).as_posix()


def _lexical_relpath(target: str, base: str) -> str:
    if not (os.path.isabs(target) and os.path.isabs(base)):
        return os.path.relpath(target, base)
    target, base = os.path.normpath(target), os.path.normpath(base)
    target_drive, target_rest = os.path.splitdrive(target)
    base_drive, base_rest = os.path.splitdrive(base)
    if os.path.normcase(target_drive) != os.path.normcase(base_drive):
        raise ValueError("paths are on different drives")
    target_parts = [part for part in target_rest.split(os.sep) if part]
    base_parts = [part for part in base_rest.split(os.sep) if part]
    common = 0
    while (common < min(len(target_parts), len(base_parts))
           and os.path.normcase(target_parts[common])
           == os.path.normcase(base_parts[common])):
        common += 1
    parts = [os.pardir] * (len(base_parts) - common) + target_parts[common:]
    return os.path.join(*parts) if parts else os.curdir


def write_mhl(job: Job, path: Path, *, destination_index: int = 0, **_options) -> Path:
    """Write an MHL describing one destination copy of the job.

    A separate MHL belongs with each destination, so `destination_index` picks
    which copy this file documents.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tag = _resolve_tag(job.hash_label)

    base = path.parent
    root = ET.Element("hashlist", {"version": _MHL_VERSION})

    info = ET.SubElement(root, "creatorinfo")
    ET.SubElement(info, "name").text = _xml_safe(job.name)
    ET.SubElement(info, "username").text = _xml_safe(_safe_user())
    ET.SubElement(info, "hostname").text = _xml_safe(platform.node())
    ET.SubElement(info, "tool", {"version": __version__}).text = PRODUCT_NAME
    ET.SubElement(info, "startdate").text = _iso(job.started)
    ET.SubElement(info, "finishdate").text = _iso(job.finished or job.started)

    # Every file the job scanned for this destination is listed, including the
    # ones that failed: a manifest that omits a failed copy reads as complete,
    # and `verify` would then pass a destination that is missing a clip. An
    # entry with no checksum element is how this file says "nothing to check
    # against", and `verify` fails it rather than counting it as a match.
    for row in job.manifest_rows(destination_index):
        node = ET.SubElement(root, "hash")
        ET.SubElement(node, "file").text = _xml_safe(_relative(row.path, base))
        if row.size is not None:
            ET.SubElement(node, "size").text = str(row.size)
        if row.modified is not None:
            ET.SubElement(node, "lastmodificationdate").text = _iso(row.modified)
        if row.checksum:
            ET.SubElement(node, tag).text = _xml_safe(row.checksum)
            ET.SubElement(node, "hashdate").text = _iso(job.finished or job.started)

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="UTF-8", xml_declaration=True)
    return path


def next_free_path(path: Path) -> Path:
    """`path`, or `JobReport-2.mhl`, `-3` and so on: the first that is free.

    A manifest is the record of what one run checked. Overwriting it with a
    later run's, which may cover fewer files (a `--skip-existing` rerun lists
    only what it copied), would discard the only evidence for the rest.
    """
    path = Path(path)
    if not path.exists():
        return path
    number = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{number}{path.suffix}")
        if not candidate.exists():
            return candidate
        number += 1


def _resolve_tag(hash_label: str) -> str:
    """Map a report-facing algorithm name to its MHL element name."""
    from ..hashers import ALGORITHMS

    for algorithm in ALGORITHMS.values():
        if algorithm.label == hash_label and algorithm.mhl_tag:
            return algorithm.mhl_tag
    return "xxh3"


def _safe_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - no login name in some containers
        return "unknown"
