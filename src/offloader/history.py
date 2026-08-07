"""Offload history, and the duplicate-card protection built on it.

Re-offloading a card you already pulled is the classic on-set error: it wastes
an hour and, worse, it can overwrite good media with a reformatted card that
happens to share a name. We fingerprint the source's file listing — names and
sizes, never contents, so the check costs a directory walk — and warn when a
fingerprint comes back.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .config import config_file, read_json, write_json
from .models import Job

HISTORY_FILE = "history.json"
MAX_ENTRIES = 500


def fingerprint(paths: Sequence[Path], root: Path) -> str:
    """A stable digest of a source tree's shape.

    Two cards with the same relative paths and sizes are, for practical
    purposes, the same offload. Contents are deliberately not read: this has to
    be fast enough to run the moment a card is mounted.
    """
    digest = hashlib.sha1()
    root = Path(root)
    entries = []
    for path in paths:
        try:
            relative = Path(path).relative_to(root).as_posix()
        except ValueError:
            relative = Path(path).name
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = -1
        entries.append(f"{relative}:{size}")
    for item in sorted(entries):
        digest.update(item.encode("utf-8", "replace"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass
class HistoryEntry:
    fingerprint: str
    job_name: str
    source: str
    destinations: list[str]
    file_count: int
    total_bytes: int
    status: str
    finished: str

    @property
    def finished_at(self) -> _dt.datetime | None:
        try:
            return _dt.datetime.fromisoformat(self.finished)
        except ValueError:
            return None

    def describe(self) -> str:
        when = self.finished_at
        stamp = f"{when:%d %b %Y at %H:%M}" if when else "an earlier session"
        return (f"“{self.job_name}” — {self.file_count} files to "
                f"{', '.join(self.destinations) or 'nowhere'} on {stamp} "
                f"({self.status})")

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict) -> "HistoryEntry":
        return cls(
            fingerprint=data.get("fingerprint", ""),
            job_name=data.get("job_name", ""),
            source=data.get("source", ""),
            destinations=list(data.get("destinations", [])),
            file_count=int(data.get("file_count", 0)),
            total_bytes=int(data.get("total_bytes", 0)),
            status=data.get("status", ""),
            finished=data.get("finished", ""),
        )


class History:
    """Append-only log of completed offloads, newest first."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_file(HISTORY_FILE)
        self.entries: list[HistoryEntry] = [
            HistoryEntry.from_dict(item) for item in read_json(self.path, [])
        ]

    def save(self) -> None:
        write_json(self.path, [entry.to_dict() for entry in self.entries[:MAX_ENTRIES]])

    def record(self, job: Job, source_fingerprint: str) -> HistoryEntry:
        entry = HistoryEntry(
            fingerprint=source_fingerprint,
            job_name=job.name,
            source=str(job.source_root),
            destinations=[str(root) for root in job.destination_roots],
            file_count=job.total_files,
            total_bytes=job.total_bytes,
            status=job.final_status,
            finished=(job.finished or job.started).isoformat(timespec="seconds"),
        )
        self.entries.insert(0, entry)
        del self.entries[MAX_ENTRIES:]
        self.save()
        return entry

    def find(self, source_fingerprint: str) -> list[HistoryEntry]:
        """Previous offloads of an identical-looking source.

        Only successful ones count — a cancelled or failed attempt is a reason
        to run again, not a reason to warn.
        """
        return [
            entry for entry in self.entries
            if entry.fingerprint == source_fingerprint
            and entry.status in ("Verified", "Copied")
        ]

    def used_names(self) -> Iterable[str]:
        """Job names already taken, so auto-naming can avoid collisions."""
        return (entry.job_name for entry in self.entries)
