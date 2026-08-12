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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

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
    # Seed with the source's own name. Without it the listing is the only
    # input, so every empty card hashes to the SHA-1 of nothing and two
    # unrelated blank volumes look like the same prior offload.
    digest.update(root.name.encode("utf-8", "replace"))
    digest.update(b"\n")
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
        payload = self.__dict__.copy()
        # __dict__ is a shallow copy, so without this the caller's dict shares
        # the entry's own destinations list.
        payload["destinations"] = list(self.destinations)
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> HistoryEntry:
        """Build an entry from whatever is in the file.

        Every field is coerced rather than trusted. history.json is a
        hand-editable file that a version skew or a half-finished write can
        leave in any shape at all, and the module's whole premise is that a
        damaged history never stops someone offloading a card.
        """
        if not isinstance(data, dict):
            data = {}

        def text(key: str) -> str:
            value = data.get(key, "")
            return value if isinstance(value, str) else str(value)

        def count(key: str) -> int:
            try:
                return int(data.get(key, 0))
            except (TypeError, ValueError):
                return 0

        # A JSON string here would otherwise be exploded character by
        # character by list(), which is silent corruption rather than a crash.
        destinations = data.get("destinations", [])
        if isinstance(destinations, str) or not isinstance(destinations, Iterable):
            destinations = []

        return cls(
            fingerprint=text("fingerprint"),
            job_name=text("job_name"),
            source=text("source"),
            destinations=[str(d) for d in destinations],
            file_count=count("file_count"),
            total_bytes=count("total_bytes"),
            status=text("status"),
            finished=text("finished"),
        )


class History:
    """Append-only log of completed offloads, newest first."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_file(HISTORY_FILE)
        self.entries: list[HistoryEntry] = self._load()

    def _load(self) -> list[HistoryEntry]:
        """Every entry the file can still yield, and nothing that raises.

        A damaged history is worth less than an offload, so a record that will
        not parse is dropped rather than allowed to stop construction. The
        top-level value has to be a list: a bare number is not iterable, and a
        bare object iterates its keys as strings.
        """
        payload = read_json(self.path, [])
        if not isinstance(payload, list):
            return []
        entries = []
        for item in payload:
            try:
                entries.append(HistoryEntry.from_dict(item))
            except Exception:            # noqa: BLE001 - see the docstring
                continue
        return entries

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
