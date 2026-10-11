"""Per-file detail for one job: what landed, and what it hashed to.

The queue answers "did the job succeed". This answers "which file, and prove
it" — the source checksum and the destination's beside each other, which is the
whole claim the tool makes and the one thing the interface never showed. It is
in the CSV and the PDF; an operator deciding whether to erase a card should not
have to open a report to see it.

Hashes are abbreviated head:tail rather than truncated, because the first four
characters alone cannot distinguish a matching pair from a mismatched one that
happens to share a prefix. The full value is in the tooltip.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..models import FileStatus, Job, VerificationMode
from ..util import format_size
from . import theme
from .widgets import label, row

COLUMNS = ("", "File", "Size", "Source", "Destination")
#: Without a read-back the destination column is the hash of the bytes as they
#: were handed to the writer, so the header must not call it "Destination".
AS_WRITTEN_HEADER = "As written"
MISMATCH = "mismatch"
COL_MARK = 0
COL_SIZE = 2
COL_SOURCE = 3
COL_DESTINATION = 4

#: One glyph per verdict. Doubled for a verified copy, because that is two
#: separate facts — it was written, and it was read back and matched. Only a
#: FULL job earns it: see `mark_for`.
MARKS = {
    FileStatus.VERIFIED: "✓✓",
    FileStatus.COPIED: "✓",
    FileStatus.FAILED: "✕",
    FileStatus.SKIPPED: "–",
    FileStatus.CANCELLED: "⊘",
}
#: Verified by size alone: the algorithm produced no checksum, so nothing was
#: compared but length.
SIZE_ONLY_MARK = "≈"


def read_back(job: Job | None) -> bool:
    """Whether this job's VERIFIED status means the destination was re-read.

    `SOURCE_ONLY` also produces VERIFIED, but compares the source stream with
    the bytes handed to the writer; the destination is never read back. A FULL
    job whose page cache could not be evicted re-read the file, but possibly
    from memory, so that is not a confirmed read-back either.
    """
    return (job is not None
            and job.verification is VerificationMode.FULL
            and not job.cache_eviction_failed)


def mark_for(status: FileStatus, job: Job | None, checksummed: bool = True) -> str:
    """The glyph for a file: the double check only for a read-back job.

    `checksummed` is False when the algorithm produced no checksum: VERIFIED
    then rests on file size alone and gets its own mark.
    """
    if status is FileStatus.VERIFIED:
        if not checksummed:
            return SIZE_ONLY_MARK
        if not read_back(job):
            return MARKS[FileStatus.COPIED]
    return MARKS.get(status, "?")


def _is_c4(value: str) -> bool:
    # A C4 ID is "c4" + base58, 90 characters, and case-sensitive. No hex
    # digest is that long.
    return len(value) == 90 and value[:2] == "c4"


def same_checksum(a: str, b: str) -> bool:
    """Hex digests compare case-insensitively; C4 identifiers exactly."""
    if _is_c4(a) or _is_c4(b):
        return a == b
    return a.lower() == b.lower()


def abbreviate(checksum: str | None) -> str:
    """`28f7e67d9b39ea9b` as `28F7:EA9B`.

    Head *and* tail: a prefix on its own is not enough to tell a matching pair
    from a near-miss, which is the only thing these two columns exist to show.
    """
    if not checksum:
        return "—"
    clean = checksum.strip()
    # Hex is shown upper-cased; a C4 ID is case-sensitive base58, so folding it
    # would let two different IDs abbreviate identically.
    if not _is_c4(clean):
        clean = clean.upper()
    if len(clean) <= 9:
        return clean
    return f"{clean[:4]}:{clean[-4:]}"


def _destination_checksum(entry) -> tuple[str, bool]:
    """The destinations' checksum and whether they all agree with each other.

    Several destinations are written from the same read, so a disagreement
    between them is a real finding and must not be hidden by showing only the
    first one.
    """
    # A FAILED destination keeps the write-stream digest the engine seeded it
    # with, which describes no file on disk (the partial was discarded). It is
    # blanked here, in the pane, rather than in the engine.
    sums = [d.checksum for d in entry.destinations
            if d.checksum and d.status is not FileStatus.FAILED]
    if not sums:
        return "—", True
    first = sums[0]
    return first, all(same_checksum(value, first) for value in sums)


def _contradicts_source(entry) -> bool:
    """Some destination's digest, failed ones included, differs from the source.

    This is how a full-verification mismatch shows: that destination is FAILED
    and its digest is the reread one, which is real evidence of a difference.
    """
    if not entry.checksum:
        return False
    return any(d.checksum and not same_checksum(d.checksum, entry.checksum)
               for d in entry.destinations)


def read_back_like(job: Job | None) -> bool:
    """Whether the destination column holds a re-read digest at all.

    True for any FULL job, even one with failed eviction (the file was re-read;
    it is just not a confirmed device read), so the header stays "Destination".
    """
    return job is not None and job.verification is VerificationMode.FULL


def _tooltip_header(job: Job | None) -> str | None:
    if job is None or read_back(job):
        return None
    if job.verification is VerificationMode.FULL:
        return ("the destination was re-read, but the page cache could not be "
                "evicted first, so the read may have come from memory")
    return ("the hash of the bytes as written (handed to the writer); the "
            "destination file was not read back")


class FileModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._job: Job | None = None

    def set_job(self, job: Job | None) -> None:
        self.beginResetModel()
        self._job = job
        self.endResetModel()

    @property
    def job(self) -> Job | None:
        return self._job

    @property
    def files(self) -> list:
        return list(self._job.files) if self._job is not None else []

    # ---------------------------------------------------------------- Qt API
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.files)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            if section == COL_DESTINATION and not read_back_like(self._job):
                return AS_WRITTEN_HEADER
            return COLUMNS[section]
        if (role == Qt.ToolTipRole and orientation == Qt.Horizontal
                and section == COL_DESTINATION):
            return _tooltip_header(self._job)
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        entry = self.files[index.row()]
        column = index.column()
        destination, agree = _destination_checksum(entry)
        matched = (bool(entry.checksum) and destination != "—"
                   and same_checksum(destination, entry.checksum))
        mismatch = (not agree) or _contradicts_source(entry)
        checksummed = bool(entry.checksum)
        # Green means one thing: the destination was independently read back
        # and matched. Never for none/source-only/COPIED/FAILED/CANCELLED rows.
        proved = (entry.status is FileStatus.VERIFIED
                  and read_back(self._job) and checksummed
                  and matched and not mismatch)

        if role == Qt.DisplayRole:
            if mismatch:
                shown = MISMATCH
            elif entry.status is FileStatus.FAILED:
                shown = "—"
            else:
                shown = abbreviate(destination)
            return (
                mark_for(entry.status, self._job, checksummed),
                str(entry.relative),
                format_size(entry.size),
                abbreviate(entry.checksum),
                shown,
            )[column]

        if role == Qt.ForegroundRole:
            if column == COL_MARK:
                return QColor(theme.status_color(entry.status.value.lower()))
            if column == COL_DESTINATION:
                if mismatch:
                    return QColor(theme.status_color("failed"))
                if proved:
                    return QColor(theme.status_color("verified"))
            return None

        if role == Qt.ToolTipRole:
            lines = [str(entry.source)]
            if entry.status is FileStatus.VERIFIED:
                if not checksummed:
                    lines.append(
                        "size matched, no checksum: the algorithm produced no "
                        "checksum, so only the file length was compared")
                elif read_back(self._job):
                    lines.append(
                        "verified: written and read back from the destination")
                elif self._job is not None and                         self._job.verification is VerificationMode.FULL:
                    lines.append(
                        "verified, but not read back from the device: the "
                        "page cache could not be evicted, so the re-read may "
                        "have come from memory")
                else:
                    lines.append(
                        "verified in transit (source only): the destination "
                        "was not read back. Its value is the hash of the bytes "
                        "as written, not of the file on disk")
            if entry.checksum:
                lines.append(f"source:      {entry.checksum}")
            for destination_entry in entry.destinations:
                failed = destination_entry.status is FileStatus.FAILED
                value = destination_entry.checksum or "—"
                if failed and (not entry.checksum or not value or same_checksum(
                        value, entry.checksum)):
                    # The seeded write-stream digest of a failed copy describes
                    # no file on disk; only a differing re-read digest is kept.
                    value = "—"
                lines.append(
                    f"{destination_entry.status.value.lower():<11}  "
                    f"{value}  {destination_entry.path}")
                if destination_entry.error:
                    lines.append(f"  error: {destination_entry.error}")
            if mismatch:
                lines.append("the destination does not match the source")
            return "\n".join(lines)

        if role == Qt.TextAlignmentRole:
            if column == COL_MARK:
                return int(Qt.AlignCenter)
            if column == COL_SIZE:
                return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


class FileListPanel(QWidget):
    """The selected job's files, or a line saying why there are none to show."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.model = FileModel(self)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(26)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_MARK, QHeaderView.Fixed)
        header.resizeSection(COL_MARK, 34)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in (COL_SIZE, COL_SOURCE, COL_DESTINATION):
            header.setSectionResizeMode(column, QHeaderView.Fixed)
        header.resizeSection(COL_SIZE, 76)
        # Wide enough for "mismatch" as well as a head:tail pair, so the column
        # does not jump width when a job goes wrong. Kept as tight as that
        # allows, because everything here is width the file name does not get —
        # and a clip name truncated to "A002_08…" identifies nothing.
        header.resizeSection(COL_SOURCE, 90)
        header.resizeSection(COL_DESTINATION, 90)

        self._title = label("Files", "heading")
        self._summary = label("", "muted")
        self._warnings = label("", "muted")
        self._warnings.setVisible(False)
        self._empty = label(self.DEFAULT_MESSAGE, "muted")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(row(self._title, 12, self._summary, 12,
                             self._warnings, None))
        layout.addWidget(self._empty)
        layout.addWidget(self.table, 1)
        self.show_job(None, None)

    DEFAULT_MESSAGE = "Select a job to see the files it copied."

    def show_job(self, job: Job | None, pending: str | None = None) -> None:
        """Display `job`'s files, or `pending` when there are none yet."""
        message = pending or self.DEFAULT_MESSAGE
        if job is self.model.job:
            # The same job object cannot have grown rows — the engine hands the
            # job over once, finished. Only the waiting message can change, and
            # rebuilding here would reset the table on every progress event of
            # a selected running job.
            if job is None:
                self._empty.setText(message)
            return

        self.model.set_job(job)
        self._show_warnings(job)
        has_rows = bool(job is not None and job.files)
        self.table.setVisible(has_rows)
        self._empty.setVisible(not has_rows)
        if not has_rows:
            self._empty.setText(message)
            self._summary.setText("")
            return
        self._summary.setText(self._describe(job))

    def _show_warnings(self, job: Job | None) -> None:
        """A count in the header, the full list in the tooltip.

        Shown for completed and cancelled jobs alike: the completion dialog
        appears once and is skipped for a cancelled job.
        """
        warnings = list(job.warnings) if job is not None else []
        self._warnings.setVisible(bool(warnings))
        if warnings:
            noun = "warning" if len(warnings) == 1 else "warnings"
            self._warnings.setText(f"⚠ {len(warnings)} {noun}")
            self._warnings.setToolTip("\n".join(warnings))
        else:
            self._warnings.setText("")
            self._warnings.setToolTip("")

    @staticmethod
    def _describe(job: Job) -> str:
        total = len(job.files)
        checked = [entry for entry in job.files
                   if entry.status is FileStatus.VERIFIED]
        verified = sum(1 for entry in checked if entry.checksum)
        size_only = len(checked) - verified
        failed = sum(1 for entry in job.files
                     if entry.status is FileStatus.FAILED)
        if read_back(job):
            how = ""
        elif job.verification is VerificationMode.FULL:
            how = ", read-back could not be confirmed (cache not evicted)"
        else:
            how = " in transit (source only, not read back)"

        # Counted rather than inferred from the job's verdict: "Verified: 5 of
        # 6" is the number that decides whether a card can be erased.
        counts = []
        if verified:
            counts.append(f"{verified} verified{how}")
        if size_only:
            counts.append(f"{size_only} size-checked (no checksum)")
        if failed:
            counts.append(f"{failed} failed")

        if job.cancelled:
            # Only the files reached are listed; say how many were not, so
            # "2 verified" cannot be read as the whole card.
            reached = (f"{total} of {job.scanned_files}" if job.scanned_files
                       else f"{total}")
            head = "; ".join([f"cancelled after {reached} files", *counts])
            return "  ·  ".join([head, format_size(job.total_bytes),
                                 job.hash_label])

        parts = [f"{total} files", format_size(job.total_bytes), job.hash_label]
        if verified:
            parts.append(f"verified{how} {verified} of {total}")
        if size_only:
            parts.append(f"size-checked (no checksum) {size_only} of {total}")
        if failed:
            parts.append(f"{failed} failed")
        return "  ·  ".join(parts)
