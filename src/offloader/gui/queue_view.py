"""The job queue: table, progress delegate, and transport controls."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..util import format_elapsed, format_size
from . import theme
from .widgets import button, label, row
from .worker import JobState, QueueController, QueueItem

COLUMNS = ("Job", "Source", "Preset", "Status", "Progress", "Throughput")
COL_STATUS = 3
COL_PROGRESS = 4


def _throughput(item: QueueItem) -> str:
    if item.state is JobState.RUNNING:
        rate = item.rate_bytes_per_sec
        eta = item.eta_seconds
        parts = []
        if rate > 0:
            parts.append(f"{format_size(int(rate))}/s")
        if eta is not None:
            parts.append(f"ETA {format_elapsed(eta)}")
        return "  ·  ".join(parts)
    if item.state.is_terminal and item.job is not None:
        return (f"{format_size(item.job.total_bytes)} in "
                f"{format_elapsed(item.job.elapsed_sec)}")
    if item.state is JobState.PAUSED:
        return "Paused"
    return ""


class QueueModel(QAbstractTableModel):
    def __init__(self, controller: QueueController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        controller.itemsChanged.connect(self._reset)
        controller.itemChanged.connect(self._refresh_one)

    # ---------------------------------------------------------------- Qt API
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.controller.items)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.controller.items[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            return (
                item.name,
                str(item.source),
                item.preset.name,
                item.status_text,
                "",                      # painted by ProgressDelegate
                _throughput(item),
            )[column]

        if role == Qt.UserRole and column == COL_PROGRESS:
            return item.fraction

        if role == Qt.ForegroundRole and column == COL_STATUS:
            state = ("failed" if item.state is JobState.FAILED
                     else item.status_text.lower())
            return QColor(theme.status_color(state))

        if role == Qt.ToolTipRole:
            if item.error:
                return item.error
            if item.state is JobState.RUNNING and item.current_file:
                return f"{item.stage}: {item.current_file}"
            if item.reports:
                return "\n".join(str(p) for p in item.reports)
            return str(item.source)

        if role == Qt.TextAlignmentRole and column == len(COLUMNS) - 1:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    # ---------------------------------------------------------------- update
    def _reset(self) -> None:
        self.beginResetModel()
        self.endResetModel()

    def _refresh_one(self, identifier: int) -> None:
        item = self.controller.find(identifier)
        if item is None:
            return
        try:
            index = self.controller.items.index(item)
        except ValueError:
            return
        self.dataChanged.emit(self.index(index, 0),
                              self.index(index, len(COLUMNS) - 1))

    def item_at(self, index: QModelIndex) -> QueueItem | None:
        if not index.isValid():
            return None
        return self.controller.items[index.row()]


class ProgressDelegate(QStyledItemDelegate):
    """Draws the progress column as a bar rather than a number."""

    def paint(self, painter: QPainter, option, index) -> None:
        fraction = index.data(Qt.UserRole)
        if fraction is None:
            super().paint(painter, option, index)
            return

        rect = option.rect.adjusted(6, 0, -6, 0)
        height = 8
        bar = rect.adjusted(0, (rect.height() - height) // 2, 0,
                            -(rect.height() - height) // 2)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(theme.BG))
        painter.drawRoundedRect(bar, 4, 4)

        width = int(bar.width() * max(0.0, min(1.0, float(fraction))))
        if width > 0:
            filled = bar.adjusted(0, 0, width - bar.width(), 0)
            painter.setBrush(QColor(theme.ACCENT))
            painter.drawRoundedRect(filled, 4, 4)
        painter.restore()


def reveal(path: Path) -> None:
    """Open a folder in the platform file manager."""
    target = Path(path)
    directory = target if target.is_dir() else target.parent
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(directory)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(directory)])
        else:
            subprocess.Popen(["xdg-open", str(directory)])
    except OSError:
        pass


class QueuePanel(QWidget):
    """Queue table plus the transport controls that act on the selection."""

    def __init__(self, controller: QueueController, parent=None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.model = QueueModel(controller, self)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(self))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(30)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.resizeSection(4, 150)

        self._pause = button("Pause", flat=True)
        self._cancel = button("Cancel", flat=True)
        self._up = button("Move up", flat=True, tooltip="Run this job sooner")
        self._down = button("Move down", flat=True, tooltip="Run this job later")
        self._remove = button("Remove", flat=True)
        self._reports = button("Show reports", flat=True)
        self._clear = button("Clear finished", flat=True)

        self._pause.clicked.connect(self._toggle_pause)
        self._cancel.clicked.connect(self._cancel_selected)
        self._up.clicked.connect(lambda: self._move(-1))
        self._down.clicked.connect(lambda: self._move(1))
        self._remove.clicked.connect(self._remove_selected)
        self._reports.clicked.connect(self._open_reports)
        self._clear.clicked.connect(controller.clear_finished)

        self._empty = label("Nothing queued. Drop a card on a preset to start.",
                            "muted")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(row(label("Queue", "heading"), None, self._clear))
        layout.addWidget(self._empty)
        layout.addWidget(self.table, 1)
        layout.addWidget(row(self._pause, self._cancel, 12, self._up, self._down,
                             12, self._remove, self._reports, None))

        self.table.selectionModel().selectionChanged.connect(self._sync_buttons)
        controller.itemsChanged.connect(self._sync_buttons)
        controller.itemChanged.connect(lambda _: self._sync_buttons())
        controller.jobStarted.connect(self._select_job)
        self._sync_buttons()

    def _select_job(self, identifier: int) -> None:
        """Follow the running job, so the transport controls act on it without
        the operator having to click the row first."""
        item = self.controller.find(identifier)
        if item is None:
            return
        try:
            self.table.selectRow(self.controller.items.index(item))
        except ValueError:
            pass

    # ---------------------------------------------------------------- helpers
    def _selected(self) -> QueueItem | None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            return None
        return self.model.item_at(indexes[0])

    def _sync_buttons(self) -> None:
        has_rows = bool(self.controller.items)
        self._empty.setVisible(not has_rows)
        self.table.setVisible(has_rows)

        item = self._selected()
        running = item is not None and item.state is JobState.RUNNING
        paused = item is not None and item.state is JobState.PAUSED
        queued = item is not None and item.state is JobState.QUEUED
        terminal = item is not None and item.state.is_terminal

        self._pause.setEnabled(running or paused)
        self._pause.setText("Resume" if paused else "Pause")
        self._cancel.setEnabled(item is not None and not terminal)
        self._up.setEnabled(queued)
        self._down.setEnabled(queued)
        self._remove.setEnabled(terminal or queued)
        self._reports.setEnabled(bool(item and item.reports))
        self._clear.setEnabled(any(i.state.is_terminal for i in self.controller.items))

    # ---------------------------------------------------------------- actions
    def _toggle_pause(self) -> None:
        item = self._selected()
        if item is None:
            return
        if item.state is JobState.PAUSED:
            self.controller.resume(item.identifier)
        else:
            self.controller.pause(item.identifier)

    def _cancel_selected(self) -> None:
        item = self._selected()
        if item is not None:
            self.controller.cancel(item.identifier)

    def _move(self, offset: int) -> None:
        item = self._selected()
        if item is None:
            return
        self.controller.move(item.identifier, offset)
        try:
            new_row = self.controller.items.index(item)
        except ValueError:
            return
        self.table.selectRow(new_row)

    def _remove_selected(self) -> None:
        item = self._selected()
        if item is not None:
            self.controller.remove(item.identifier)

    def _open_reports(self) -> None:
        item = self._selected()
        if item and item.reports:
            reveal(item.reports[0])
