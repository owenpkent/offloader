"""Reusable widgets: drop targets, meters, LEDs, small layout helpers."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
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


def section(text: str) -> QLabel:
    """An uppercase, letter-spaced micro-heading for a panel."""
    return label(text.upper(), "section")


def hairline(vertical: bool = False) -> QFrame:
    frame = QFrame()
    frame.setProperty("role", "vhairline" if vertical else "hairline")
    if vertical:
        frame.setFixedWidth(1)
    else:
        frame.setFixedHeight(1)
    return frame


def card(*children: QWidget, spacing: int = 10, margins: int = 14,
         role: str = "card") -> QFrame:
    """A bordered panel holding a vertical stack of widgets."""
    frame = QFrame()
    frame.setProperty("role", role)
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


def _glow(painter: QPainter, rect: QRectF, colour: str, radius: float,
          spread: int = 6, alpha: int = 70) -> None:
    """A soft halo around a rounded rect: a few concentric strokes of falling
    opacity. Cheap, and it reads as light rather than as a border."""
    base = QColor(colour)
    painter.setBrush(Qt.NoBrush)
    for step in range(spread, 0, -1):
        base.setAlpha(int(alpha * (1 - step / (spread + 1)) ** 2))
        painter.setPen(QPen(base, 1))
        painter.drawRoundedRect(rect.adjusted(-step, -step, step, step),
                                radius + step, radius + step)


class Backdrop(QWidget):
    """The window ground: a faint dot grid and a cold glow in one corner, so
    the panels sit on something rather than on flat black."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAutoFillBackground(False)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(theme.BG))

        glow = QRadialGradient(QPointF(self.width() * 0.82, -40),
                               max(self.width(), self.height()) * 0.6)
        tint = QColor(theme.ACCENT)
        tint.setAlpha(22)
        glow.setColorAt(0.0, tint)
        tint.setAlpha(0)
        glow.setColorAt(1.0, tint)
        painter.fillRect(self.rect(), glow)

        dot = QColor(theme.FG)
        dot.setAlpha(14)
        painter.setPen(Qt.NoPen)
        painter.setBrush(dot)
        pitch = 22
        for x in range(pitch, self.width(), pitch):
            for y in range(pitch, self.height(), pitch):
                painter.drawRect(x, y, 1, 1)


class Led(QWidget):
    """A small status lamp. Lit colours get a halo; "off" is a dark bead."""

    def __init__(self, colour: str = theme.FG_FAINT, diameter: int = 8,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._colour = colour
        self._lit = colour != theme.FG_FAINT
        self._pulse = 0.0
        self._pulsing = False
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self.setFixedSize(diameter + 10, diameter + 10)

    def set_colour(self, colour: str, pulse: bool = False) -> None:
        self._colour = colour
        self._lit = colour not in (theme.FG_FAINT, theme.FG_MUTED)
        self._pulsing = pulse and self._lit
        if self._pulsing and not self._timer.isActive():
            self._timer.start()
        elif not self._pulsing:
            self._timer.stop()
            self._pulse = 0.0
        self.update()

    def _tick(self) -> None:
        self._pulse = (self._pulse + 0.05) % 1.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        centre = QPointF(self.width() / 2, self.height() / 2)
        radius = (min(self.width(), self.height()) - 10) / 2
        colour = QColor(self._colour)

        if self._lit:
            import math
            breath = 0.6 + 0.4 * (0.5 + 0.5 * math.sin(self._pulse * 2 * math.pi)) \
                if self._pulsing else 1.0
            halo = QRadialGradient(centre, radius + 5)
            glow = QColor(colour)
            glow.setAlpha(int(110 * breath))
            halo.setColorAt(0.0, glow)
            glow.setAlpha(0)
            halo.setColorAt(1.0, glow)
            painter.setPen(Qt.NoPen)
            painter.setBrush(halo)
            painter.drawEllipse(centre, radius + 5, radius + 5)

        painter.setPen(QPen(QColor(theme.BG), 1))
        painter.setBrush(colour if self._lit else QColor(theme.BORDER))
        painter.drawEllipse(centre, radius, radius)
        if self._lit:
            spark = QColor("#ffffff")
            spark.setAlpha(150)
            painter.setPen(Qt.NoPen)
            painter.setBrush(spark)
            painter.drawEllipse(QPointF(centre.x() - radius * 0.3,
                                        centre.y() - radius * 0.3),
                                radius * 0.3, radius * 0.3)


def _paint_meter(painter: QPainter, rect: QRectF, fraction: float, colour: str,
                 segments: int = 0) -> None:
    """A recessed track with a lit fill. With segments, the fill is drawn as
    discrete cells with a hairline gap, like an LED ladder."""
    painter.setRenderHint(QPainter.Antialiasing)
    radius = rect.height() / 2

    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(theme.BG))
    painter.drawRoundedRect(rect, radius, radius)
    inset = QColor(theme.BORDER_SOFT)
    painter.setPen(QPen(inset, 1))
    painter.setBrush(Qt.NoBrush)
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

    fraction = max(0.0, min(1.0, fraction))
    if fraction <= 0:
        return

    lit = QColor(colour)
    fill_width = rect.width() * fraction
    fill = QRectF(rect.left(), rect.top(), max(fill_width, rect.height()), rect.height())

    painter.save()
    clip = QPainterPath()
    clip.addRoundedRect(rect, radius, radius)
    painter.setClipPath(clip)
    _glow(painter, fill, colour, radius, spread=4, alpha=90)

    gradient = QLinearGradient(fill.topLeft(), fill.bottomLeft())
    gradient.setColorAt(0.0, lit.lighter(135))
    gradient.setColorAt(0.5, lit)
    gradient.setColorAt(1.0, lit.darker(125))
    painter.setPen(Qt.NoPen)
    painter.setBrush(gradient)
    painter.drawRoundedRect(fill, radius, radius)

    if segments:
        gap = QColor(theme.BG)
        gap.setAlpha(190)
        painter.setPen(QPen(gap, 1))
        pitch = rect.width() / segments
        x = rect.left() + pitch
        while x < fill.right():
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            x += pitch
    painter.restore()


class CapacityBar(QWidget):
    """A slim used/free meter for a volume."""

    def __init__(self, percent: float = 0.0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._percent = percent
        self.setFixedHeight(7)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_percent(self, percent: float) -> None:
        self._percent = max(0.0, min(100.0, percent))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        # Amber past 80 %, red past 95 % — a destination that is nearly full is
        # the thing most likely to ruin an offload.
        colour = theme.ACCENT
        if self._percent >= 95:
            colour = theme.BAD
        elif self._percent >= 80:
            colour = theme.WARN
        _paint_meter(painter, QRectF(self.rect()), self._percent / 100.0, colour,
                     segments=24)


class ColorChip(QWidget):
    """The colour swatch shown against a preset: a lit vertical bar."""

    def __init__(self, color: str, diameter: int = 12,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self.setFixedWidth(diameter)
        self.setMinimumHeight(diameter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)

    def set_color(self, color: str) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        bar = QRectF(self.width() / 2 - 1.5, 2, 3, self.height() - 4)
        _glow(painter, bar, self._color, 1.5, spread=4, alpha=120)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self._color))
        painter.drawRoundedRect(bar, 1.5, 1.5)


class SourceDropZone(QFrame):
    """Large drop target for the card being offloaded. Painted by hand, so
    the dashed edge can glow when a drag is over it."""

    pathChosen = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(96)
        self.setCursor(Qt.PointingHandCursor)
        self._path: Path | None = None
        self._active = False
        self._hover = False
        self.setAttribute(Qt.WA_Hover, True)

        self._eyebrow = section("Source")
        self._title = label("Drop a card or folder here", "heading")
        self._detail = label("or click to browse", "readout")

        text = QWidget()
        text_layout = QVBoxLayout(text)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)
        text_layout.addWidget(self._eyebrow)
        text_layout.addWidget(self._title)
        text_layout.addWidget(self._detail)

        self._led = Led(theme.FG_FAINT, diameter=10)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(22, 16, 18, 16)
        layout.setSpacing(14)
        layout.addWidget(self._led, 0, Qt.AlignTop)
        layout.addWidget(text, 1)

    @property
    def path(self) -> Path | None:
        return self._path

    def set_path(self, path: Path | None) -> None:
        self._path = Path(path) if path else None
        if self._path is None:
            self._title.setText("Drop a card or folder here")
            self._detail.setText("or click to browse")
            self._led.set_colour(theme.FG_FAINT)
        else:
            self._title.setText(self._path.name or str(self._path))
            self._detail.setText(str(self._path))
            self._led.set_colour(theme.ACCENT)
        self._set_active(False)

    def _set_active(self, active: bool) -> None:
        self._active = active
        self.update()

    # ---------------------------------------------------------------- paint
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        radius = 10.0

        armed = self._path is not None
        if self._active:
            _glow(painter, rect, theme.ACCENT, radius, spread=8, alpha=120)
        elif armed:
            _glow(painter, rect, theme.ACCENT, radius, spread=5, alpha=45)

        fill = QColor("#0f1a21" if self._active else
                      ("#101519" if armed or self._hover else theme.BG_PANEL))
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)

        edge = QColor(theme.ACCENT if (self._active or armed) else
                      (theme.FG_MUTED if self._hover else theme.BORDER))
        pen = QPen(edge, 1.5 if self._active else 1)
        if not armed:
            pen.setStyle(Qt.DashLine)
            pen.setDashPattern([5, 4])
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

    # ---------------------------------------------------------------- events
    def enterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._hover = False
        self.update()

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
        self.setAlternatingRowColors(False)
        self.setMinimumHeight(90)
        self.setFrameShape(QFrame.StyledPanel)

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
        item = QListWidgetItem(f"{self.count() + 1:02d}   {path}")
        item.setData(Qt.UserRole, str(path))
        item.setToolTip(str(path))
        self.addItem(item)

    def remove_selected(self) -> None:
        for item in self.selectedItems():
            self.takeItem(self.row(item))
        self._renumber()
        self.changed.emit()

    def _renumber(self) -> None:
        for index in range(self.count()):
            item = self.item(index)
            item.setText(f"{index + 1:02d}   {item.data(Qt.UserRole)}")

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
           ghost: bool = False, tooltip: str = "") -> QPushButton:
    widget = QPushButton(text)
    if accent:
        widget.setProperty("accent", "true")
    if flat:
        widget.setProperty("flat", "true")
    if ghost:
        widget.setProperty("ghost", "true")
        widget.setText(text.upper())
    if tooltip:
        widget.setToolTip(tooltip)
    widget.setCursor(Qt.PointingHandCursor)
    return widget
