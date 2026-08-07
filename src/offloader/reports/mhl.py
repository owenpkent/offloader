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

_MHL_VERSION = "1.1"

#: XML 1.0 forbids most C0 control characters outright — they cannot even be
#: written as character references. ElementTree emits them raw, producing a
#: document no parser will read. One stray control byte in one filename would
#: therefore strand the verification of the *entire* delivery, so it is
#: replaced with U+FFFD rather than passed through.
#: U+FFFD REPLACEMENT CHARACTER.
_REPLACEMENT = chr(0xFFFD)


def _is_xml_char(code: int) -> bool:
    """The XML 1.0 Char production."""
    return (
        code in (0x09, 0x0A, 0x0D)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def _xml_safe(text: object) -> str:
    """Text ElementTree can serialise and a parser can read back."""
    return "".join(
        character if _is_xml_char(ord(character)) else _REPLACEMENT
        for character in str(text)
    )


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
    """
    try:
        return Path(os.path.relpath(target, base)).as_posix()
    except ValueError:
        # Different drives on Windows: no relative path exists.
        return Path(target).as_posix()


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

    for entry in job.files:
        if destination_index < len(entry.destinations):
            destination = entry.destinations[destination_index]
            file_path, checksum = destination.path, destination.checksum or entry.checksum
        else:
            file_path, checksum = entry.source, entry.checksum
        if not checksum:
            continue

        node = ET.SubElement(root, "hash")
        ET.SubElement(node, "file").text = _xml_safe(_relative(file_path, base))
        ET.SubElement(node, "size").text = str(entry.size)
        ET.SubElement(node, "lastmodificationdate").text = _iso(entry.modified)
        ET.SubElement(node, tag).text = _xml_safe(checksum)
        ET.SubElement(node, "hashdate").text = _iso(job.finished or job.started)

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="UTF-8", xml_declaration=True)
    return path


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
