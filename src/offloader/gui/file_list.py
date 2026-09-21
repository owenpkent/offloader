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

from ..models import FileStatus, Job
from ..util import format_size
from . import theme
from .widgets import label, row

COLUMNS = ("", "File", "Size", "Source", "Destination")
COL_MARK = 0
COL_SIZE = 2
COL_SOURCE = 3
COL_DESTINATION = 4

#: One glyph per verdict. Doubled for a verified copy, because that is two
#: separate facts — it was written, and it was read back and matched.
MARKS = {
    FileStatus.VERIFIED: "✓✓",
    FileStatus.COPIED: "✓",
    FileStatus.FAILED: "✕",
    FileStatus.SKIPPED: "–",
    FileStatus.CANCELLED: "⊘",
}


def abbreviate(checksum: str | None) -> str:
    """`28f7e67d9b39ea9b` as `28F7:EA9B`.

    Head *and* tail: a prefix on its own is not enough to tell a matching pair
    from a near-miss, which is the only thing these two columns exist to show.
    """
    if not checksum:
        return "—"
    clean = checksum.strip().upper()
    if len(clean) <= 9:
        return clean
    return f"{clean[:4]}:{clean[-4:]}"


def _destination_checksum(entry) -> tuple[str, bool]:
    """The destinations' checksum and whether they all agree with each other.

    Several destinations are written from the same read, so a disagreement
    between them is a real finding and must not be hidden by showing only the
    first one.
    """
    sums = [d.checksum for d in entry.destinations if d.checksum]
    if not sums:
        return "—", True
    first = sums[0]
    return first, all(value == first for value in sums)


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
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        entry = self.files[index.row()]
        column = index.column()
        destination, agree = _destination_checksum(entry)
        matched = bool(entry.checksum) and destination == entry.checksum

        if role == Qt.DisplayRole:
            return (
                MARKS.get(entry.status, "?"),
                str(entry.relative),
                format_size(entry.size),
                abbreviate(entry.checksum),
                abbreviate(destination) if agree else "mismatch",
            )[column]

        if role == Qt.ForegroundRole:
            if column == COL_MARK:
                return QColor(theme.status_color(entry.status.value.lower()))
            if column == COL_DESTINATION:
                # Green only when this column has actually proved something:
                # the two sides agree, and every destination agrees too.
                if not agree:
                    return QColor(theme.status_color("failed"))
                if matched:
                    return QColor(theme.status_color("verified"))
            return None

        if role == Qt.ToolTipRole:
            lines = [str(entry.source)]
            if entry.checksum:
                lines.append(f"source:      {entry.checksum}")
            for destination_entry in entry.destinations:
                lines.append(
                    f"{destination_entry.status.value.lower():<11}  "
                    f"{destination_entry.checksum or '—'}  "
                    f"{destination_entry.path}")
                if destination_entry.error:
                    lines.append(f"  error: {destination_entry.error}")
            if entry.checksum and not matched and destination != "—":
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
        self._empty = label(self.DEFAULT_MESSAGE, "muted")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(row(self._title, 12, self._summary, None))
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
        has_rows = bool(job is not None and job.files)
        self.table.setVisible(has_rows)
        self._empty.setVisible(not has_rows)
        if not has_rows:
            self._empty.setText(message)
            self._summary.setText("")
            return
        self._summary.setText(self._describe(job))

    @staticmethod
    def _describe(job: Job) -> str:
        verified = sum(1 for entry in job.files
                       if entry.status is FileStatus.VERIFIED)
        failed = sum(1 for entry in job.files
                     if entry.status is FileStatus.FAILED)
        parts = [f"{len(job.files)} files",
                 format_size(job.total_bytes),
                 job.hash_label]
        # Counted rather than inferred from the job's verdict: "Verified: 5 of
        # 6" is the number that decides whether a card can be erased.
        if verified:
            parts.append(f"verified {verified} of {len(job.files)}")
        if failed:
            parts.append(f"{failed} failed")
        return "  ·  ".join(parts)
