"""The job queue: table, progress delegate, and transport controls."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QStyle,
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
COL_THROUGHPUT = 5

#: What each engine stage is doing, in the operator's words.
STAGE_VERBS = {
    "copy": "Copying",
    "verify": "Verifying",
    "probe": "Reading metadata",
    "thumbs": "Extracting thumbnails",
}


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


def _active_summary(item: QueueItem) -> str:
    """The running job in one line: what, on which file, how fast."""
    if item.state is JobState.PAUSED:
        return f"Paused — {item.name} at {item.fraction * 100:.0f}%"
    verb = STAGE_VERBS.get(item.stage, item.stage.capitalize() or "Running")
    text = f"{verb} {item.current_file}" if item.current_file else verb
    text += f" — {item.fraction * 100:.0f}%"
    rate = _throughput(item)
    if rate:
        text += f" · {rate}"
    return text


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

    def refresh_throughput(self) -> None:
        """Repaint the rate column without a progress event. During a stall no
        events arrive, which is exactly when the displayed rate must be seen
        to fall rather than freeze at its last healthy value."""
        rows = len(self.controller.items)
        if rows:
            self.dataChanged.emit(self.index(0, COL_THROUGHPUT),
                                  self.index(rows - 1, COL_THROUGHPUT))

    def item_at(self, index: QModelIndex) -> QueueItem | None:
        if not index.isValid():
            return None
        return self.controller.items[index.row()]


class ProgressDelegate(QStyledItemDelegate):
    """Draws the progress column as a bar with its percentage beside it.

    The colours are chosen against both grounds the cell can have: on the
    normal row the old accent-on-near-black bar read fine, but the running row
    is auto-selected, and an accent bar on the accent selection colour was
    invisible — the operator's own job was the one row without a readable bar.
    """

    TEXT_WIDTH = 40
    BAR_HEIGHT = 10

    def paint(self, painter: QPainter, option, index) -> None:
        fraction = index.data(Qt.UserRole)
        if fraction is None:
            super().paint(painter, option, index)
            return

        selected = bool(option.state & QStyle.State_Selected)
        painter.save()
        if selected:
            painter.fillRect(option.rect, option.palette.highlight())

        rect = option.rect.adjusted(6, 0, -6, 0)
        bar = rect.adjusted(0, (rect.height() - self.BAR_HEIGHT) // 2,
                            -self.TEXT_WIDTH,
                            -(rect.height() - self.BAR_HEIGHT) // 2)

        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 90) if selected
                         else QColor(theme.PROGRESS_TRACK))
        painter.drawRoundedRect(bar, 4, 4)

        clamped = max(0.0, min(1.0, float(fraction)))
        width = int(bar.width() * clamped)
        if width > 0:
            filled = bar.adjusted(0, 0, width - bar.width(), 0)
            painter.setBrush(QColor("#ffffff") if selected
                             else QColor(theme.ACCENT))
            painter.drawRoundedRect(filled, 4, 4)

        text_rect = rect.adjusted(rect.width() - self.TEXT_WIDTH + 6, 0, 0, 0)
        painter.setPen(QColor("#ffffff") if selected else QColor(theme.FG))
        painter.drawText(text_rect, Qt.AlignRight | Qt.AlignVCenter,
                         f"{clamped * 100:.0f}%")
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
        header.resizeSection(4, 170)
        # Fixed, not ResizeToContents: the rate string changes width on every
        # update, and letting it drive the layout shoved the column — the one
        # being read — around as values grew and shrank.
        header.setSectionResizeMode(COL_THROUGHPUT, QHeaderView.Fixed)
        header.resizeSection(COL_THROUGHPUT, 190)

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

        # The running job promoted to where the eye already is: the queue rows
        # are 30 px tall at the bottom of the window, and a job in flight
        # looked almost identical to an idle queue.
        self._active = label("", "heading")
        self._active.setStyleSheet(f"color: {theme.ACCENT};")
        self._active.setVisible(False)

        # Repaints the decaying rate during stalls, when no progress events
        # arrive to do it. Runs only while a job is active.
        self._ticker = QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._tick)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(row(label("Queue", "heading"), None, self._clear))
        layout.addWidget(self._active)
        layout.addWidget(self._empty)
        layout.addWidget(self.table, 1)
        layout.addWidget(row(self._pause, self._cancel, 12, self._up, self._down,
                             12, self._remove, self._reports, None))

        self.table.selectionModel().selectionChanged.connect(self._sync_buttons)
        controller.itemsChanged.connect(self._sync_buttons)
        controller.itemChanged.connect(lambda _: self._sync_buttons())
        controller.jobStarted.connect(self._select_job)
        self._sync_buttons()

    def _active_item(self) -> QueueItem | None:
        return next((i for i in self.controller.items
                     if i.state in (JobState.RUNNING, JobState.PAUSED)), None)

    def _update_active(self) -> None:
        item = self._active_item()
        if item is None:
            self._active.setVisible(False)
            self._ticker.stop()
            return
        self._active.setText(_active_summary(item))
        self._active.setVisible(True)
        if not self._ticker.isActive():
            self._ticker.start()

    def _tick(self) -> None:
        self._update_active()
        self.model.refresh_throughput()

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
        self._update_active()

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
