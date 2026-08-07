"""Media Hash List (MHL 1.1) output.

MHL is the interchange format post houses use to re-verify a delivery, so the
paths written here are relative to the MHL's own directory — that is what makes
the file portable with the media it describes.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import platform
from pathlib import Path
from xml.etree import ElementTree as ET

from .. import PRODUCT_NAME, __version__
from ..models import Job

_MHL_VERSION = "1.1"


def _iso(value: float | _dt.datetime) -> str:
    dt = value if isinstance(value, _dt.datetime) else _dt.datetime.fromtimestamp(value)
    return dt.astimezone().replace(microsecond=0).isoformat()


def _relative(target: Path, base: Path) -> str:
    try:
        return target.relative_to(base).as_posix()
    except ValueError:
        return target.as_posix()


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
    ET.SubElement(info, "name").text = job.name
    ET.SubElement(info, "username").text = _safe_user()
    ET.SubElement(info, "hostname").text = platform.node()
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
        ET.SubElement(node, "file").text = _relative(file_path, base)
        ET.SubElement(node, "size").text = str(entry.size)
        ET.SubElement(node, "lastmodificationdate").text = _iso(entry.modified)
        ET.SubElement(node, tag).text = checksum
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
