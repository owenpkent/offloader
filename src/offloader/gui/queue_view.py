"""The job queue: table, progress delegate, and transport controls."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
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
from .widgets import Led, MiniMeter, _paint_meter, button, label, row, section
from .worker import JobState, QueueController, QueueItem

COLUMNS = ("Job", "Source", "Options", "Status", "Progress", "Throughput")
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


def _state_colour(item: QueueItem) -> str:
    if item.state is JobState.FAILED:
        return theme.BAD
    if item.state is JobState.PAUSED:
        return theme.WARN
    if item.state is JobState.DONE:
        return theme.OK
    if item.state.is_terminal:
        return theme.FG_MUTED
    if item.state is JobState.RUNNING:
        return theme.ACCENT
    return theme.FG_MUTED


def _mono_font(point_size: int) -> QFont:
    font = QFont()
    for family in ("JetBrains Mono", "Cascadia Code", "Consolas", "DejaVu Sans Mono"):
        font.setFamily(family)
        if QFont(family).exactMatch():
            break
    font.setPointSize(point_size)
    return font


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
                f"{item.preset.algorithm} · {item.preset.verification.value}",
                item.status_text,
                "",                      # painted by ProgressDelegate
                _throughput(item),
            )[column]

        if role == Qt.UserRole and column == COL_PROGRESS:
            return item.fraction

        if role == Qt.UserRole + 1 and column == COL_PROGRESS:
            return _state_colour(item)

        if role == Qt.ForegroundRole and column == COL_STATUS:
            state = ("failed" if item.state is JobState.FAILED
                     else item.status_text.lower())
            return QColor(theme.status_color(state))

        if role == Qt.ForegroundRole and column in (1, 2, len(COLUMNS) - 1):
            return QColor(theme.FG_MUTED)

        if role == Qt.FontRole and column in (1, 2, COL_STATUS, len(COLUMNS) - 1):
            font = _mono_font(9)
            if column == COL_STATUS:
                font.setBold(True)
                font.setCapitalization(QFont.AllUppercase)
                font.setLetterSpacing(QFont.AbsoluteSpacing, 1.2)
            return font

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
        colour = index.data(Qt.UserRole + 1) or theme.ACCENT

        painter.save()
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor(theme.ACCENT_DIM))

        rect = option.rect.adjusted(8, 0, -48, 0)
        height = 7
        bar = QRectF(rect.left(), rect.top() + (rect.height() - height) / 2,
                     rect.width(), height)
        _paint_meter(painter, bar, float(fraction), colour, segments=20)

        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(_mono_font(9))
        painter.setPen(QPen(QColor(colour if fraction > 0 else theme.FG_FAINT)))
        painter.drawText(option.rect.adjusted(0, 0, -8, 0),
                         Qt.AlignRight | Qt.AlignVCenter,
                         f"{float(fraction) * 100:3.0f}%")
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
    """A one-line strip that always shows what is happening, and the full
    table with its transport controls one click away."""

    expandedChanged = Signal(bool)

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
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setMinimumHeight(150)
        self.table.setMaximumHeight(210)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.resizeSection(4, 210)

        self._pause = button("Pause", ghost=True)
        self._cancel = button("Cancel", ghost=True)
        self._up = button("▲", ghost=True, tooltip="Run this job sooner")
        self._down = button("▼", ghost=True, tooltip="Run this job later")
        self._remove = button("Remove", ghost=True)
        self._reports = button("Reports", ghost=True)
        self._clear = button("Clear finished", ghost=True)

        self._pause.clicked.connect(self._toggle_pause)
        self._cancel.clicked.connect(self._cancel_selected)
        self._up.clicked.connect(lambda: self._move(-1))
        self._down.clicked.connect(lambda: self._move(1))
        self._remove.clicked.connect(self._remove_selected)
        self._reports.clicked.connect(self._open_reports)
        self._clear.clicked.connect(controller.clear_finished)

        # ------------------------------------------------------------ strip
        self._count = label("", "readout")
        self._empty = label("NO JOBS  ·  choose a source and a destination to start",
                            "readout")
        self._live_led = Led(theme.FG_FAINT)
        self._live_name = label("", "dim")
        self._live_status = label("", "readout")
        self._live_meter = MiniMeter(150)
        self._live_percent = label("", "readout")
        self._live_rate = label("", "readout")
        self._live = row(self._live_led, self._live_name, 4, self._live_status, 10,
                         self._live_meter, self._live_percent, 10, self._live_rate,
                         spacing=6)

        self._details = button("Details ▸", ghost=True)
        self._details.setCheckable(True)
        self._details.toggled.connect(self.set_expanded)

        strip = row(section("Queue"), 8, self._count, 14, self._empty, self._live,
                    None, self._details, spacing=6)

        self._body = QWidget()
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        body.addWidget(self.table)
        body.addWidget(row(self._pause, self._cancel, 12, self._up, self._down,
                           12, self._remove, self._reports, None, self._clear,
                           spacing=6))
        self._body.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(strip)
        layout.addWidget(self._body)

        self.table.selectionModel().selectionChanged.connect(self._sync_buttons)
        controller.itemsChanged.connect(self._sync_buttons)
        controller.itemChanged.connect(lambda _: self._sync_buttons())
        controller.jobStarted.connect(self._select_job)
        self._sync_buttons()

    # ---------------------------------------------------------------- strip
    @property
    def expanded(self) -> bool:
        return self._details.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        if self._details.isChecked() != expanded:
            self._details.blockSignals(True)
            self._details.setChecked(expanded)
            self._details.blockSignals(False)
        self._body.setVisible(expanded)
        self._details.setText("DETAILS ▾" if expanded else "DETAILS ▸")
        self.expandedChanged.emit(expanded)

    def _refresh_strip(self) -> None:
        items = self.controller.items
        self._empty.setVisible(not items)
        self._live.setVisible(bool(items))
        total = len(items)
        done = sum(1 for i in items if i.state.is_terminal)
        self._count.setText(f"{done}/{total}" if total else "")
        if not items:
            return

        focus = next((i for i in items if i.state is JobState.RUNNING), None) \
            or next((i for i in items if i.state is JobState.PAUSED), None) \
            or items[-1]
        colour = _state_colour(focus)
        self._live_led.set_colour(colour, pulse=focus.state is JobState.RUNNING)
        self._live_name.setText(focus.name)
        self._live_status.setText(focus.status_text.upper())
        self._live_status.setStyleSheet(f"color: {colour};")
        self._live_meter.set_value(focus.fraction, colour)
        self._live_percent.setText(f"{focus.fraction * 100:3.0f}%")
        self._live_rate.setText(_throughput(focus))

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
        self._refresh_strip()

        item = self._selected()
        running = item is not None and item.state is JobState.RUNNING
        paused = item is not None and item.state is JobState.PAUSED
        queued = item is not None and item.state is JobState.QUEUED
        terminal = item is not None and item.state.is_terminal

        self._pause.setEnabled(running or paused)
        self._pause.setText("RESUME" if paused else "PAUSE")
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
