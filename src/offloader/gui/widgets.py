"""Reusable widgets: drop targets, capacity bars, small layout helpers."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme


def label(text: str, role: str = "") -> QLabel:
    widget = QLabel(text)
    if role:
        widget.setProperty("role", role)
    return widget


def card(*children: QWidget, spacing: int = 10, margins: int = 14) -> QFrame:
    """A bordered panel holding a vertical stack of widgets."""
    frame = QFrame()
    frame.setProperty("role", "card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    for child in children:
        layout.addWidget(child)
    return frame


def row(*children, spacing: int = 8) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for child in children:
        if child is None:
            layout.addStretch(1)
        elif isinstance(child, int):
            layout.addSpacing(child)
        else:
            layout.addWidget(child)
    return widget


def _directories_from(event) -> list[Path]:
    """Directories in a drag payload. Files are mapped to their parent, so
    dropping a clip on a destination means "put it in that folder"."""
    if not event.mimeData().hasUrls():
        return []
    found: list[Path] = []
    for url in event.mimeData().urls():
        if not url.isLocalFile():
            continue
        path = Path(url.toLocalFile())
        candidate = path if path.is_dir() else path.parent
        if candidate.is_dir() and candidate not in found:
            found.append(candidate)
    return found


class CapacityBar(QWidget):
    """A slim used/free bar for a volume."""

    def __init__(self, percent: float = 0.0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._percent = percent
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_percent(self, percent: float) -> None:
        self._percent = max(0.0, min(100.0, percent))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        painter.setBrush(QColor(theme.BG))
        painter.drawRoundedRect(self.rect(), 3, 3)

        # Amber past 80 %, red past 95 % — a destination that is nearly full is
        # the thing most likely to ruin an offload.
        colour = theme.ACCENT
        if self._percent >= 95:
            colour = theme.BAD
        elif self._percent >= 80:
            colour = theme.WARN

        width = int(self.width() * self._percent / 100.0)
        if width > 0:
            painter.setBrush(QColor(colour))
            painter.drawRoundedRect(0, 0, max(width, 6), self.height(), 3, 3)


class ColorChip(QWidget):
    """The colour swatch shown against a preset."""

    def __init__(self, color: str, diameter: int = 12,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedSize(diameter, diameter)

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.setBrush(QColor(self._color))
        painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))


class SourceDropZone(QFrame):
    """Large drop target for the card being offloaded."""

    pathChosen = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "dropzone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(88)
        self._path: Path | None = None

        self._title = label("Drop a card or folder here", "heading")
        self._title.setAlignment(Qt.AlignCenter)
        self._detail = label("or click to browse", "muted")
        self._detail.setAlignment(Qt.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.addStretch(1)
        layout.addWidget(self._title)
        layout.addWidget(self._detail)
        layout.addStretch(1)

    @property
    def path(self) -> Path | None:
        return self._path

    def set_path(self, path: Path | None) -> None:
        self._path = Path(path) if path else None
        if self._path is None:
            self._title.setText("Drop a card or folder here")
            self._detail.setText("or click to browse")
        else:
            self._title.setText(self._path.name or str(self._path))
            self._detail.setText(str(self._path))
        self._set_active(False)

    def _set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    # ---------------------------------------------------------------- events
    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        directory = QFileDialog.getExistingDirectory(self, "Choose a source folder")
        if directory:
            chosen = Path(directory)
            self.set_path(chosen)
            self.pathChosen.emit(chosen)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if _directories_from(event):
            self._set_active(True)
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._set_active(False)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        directories = _directories_from(event)
        self._set_active(False)
        if directories:
            self.set_path(directories[0])
            self.pathChosen.emit(directories[0])
            event.acceptProposedAction()


class DestinationList(QListWidget):
    """Destination roots, populated by drag-and-drop or the Add button."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DropOnly)
        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.setAlternatingRowColors(True)
        self.setMinimumHeight(90)

    def paths(self) -> list[Path]:
        return [Path(self.item(i).data(Qt.UserRole)) for i in range(self.count())]

    def set_paths(self, paths) -> None:
        self.clear()
        for path in paths:
            self._append(Path(path))
        self.changed.emit()

    def add_path(self, path: Path) -> bool:
        path = Path(path)
        if path in self.paths():
            return False
        self._append(path)
        self.changed.emit()
        return True

    def _append(self, path: Path) -> None:
        item = QListWidgetItem(str(path))
        item.setData(Qt.UserRole, str(path))
        item.setToolTip(str(path))
        self.addItem(item)

    def remove_selected(self) -> None:
        for item in self.selectedItems():
            self.takeItem(self.row(item))
        self.changed.emit()

    def browse_and_add(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose a destination")
        if directory:
            self.add_path(Path(directory))

    # ---------------------------------------------------------------- events
    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if _directories_from(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if _directories_from(event):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        added = False
        for directory in _directories_from(event):
            added |= self.add_path(directory)
        if added:
            event.acceptProposedAction()


def button(text: str, *, accent: bool = False, flat: bool = False,
           tooltip: str = "") -> QPushButton:
    widget = QPushButton(text)
    if accent:
        widget.setProperty("accent", "true")
    if flat:
        widget.setProperty("flat", "true")
    if tooltip:
        widget.setToolTip(tooltip)
    widget.setCursor(Qt.PointingHandCursor)
    return widget
