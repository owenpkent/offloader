"""Preset mode: pick a card, drop it on a saved workflow."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Signal
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ..presets import SORT_MODES, Preset, PresetStore
from .preset_editor import PresetEditor
from .widgets import ColorChip, SourceDropZone, button, label, row


class PresetRow(QWidget):
    """Colour chip, name, and one-line summary."""

    def __init__(self, preset: Preset, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        name = label(preset.name, "heading")
        summary = label(preset.summary(), "muted")
        summary.setStyleSheet("font-size: 11px;")

        usage = label(f"used {preset.use_count}×" if preset.in_use else "unused",
                      "muted")
        usage.setStyleSheet("font-size: 11px;")

        text = QWidget()
        text_layout = QVBoxLayout(text)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        text_layout.addWidget(name)
        text_layout.addWidget(summary)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addWidget(row(ColorChip(preset.color), 4, text, None, usage))


class PresetList(QListWidget):
    """Preset list that also accepts a card dropped straight onto a row."""

    sourceDropped = Signal(int, Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DropOnly)
        self.setAlternatingRowColors(True)

    def _directory_from(self, event) -> Path | None:
        if not event.mimeData().hasUrls():
            return None
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            candidate = path if path.is_dir() else path.parent
            if candidate.is_dir():
                return candidate
        return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._directory_from(event):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._directory_from(event):
            # Highlight the row under the cursor so the target is unambiguous.
            item = self.itemAt(event.position().toPoint())
            if item is not None:
                self.setCurrentItem(item)
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt naming
        directory = self._directory_from(event)
        item = self.itemAt(event.position().toPoint())
        if directory is None or item is None:
            return
        self.sourceDropped.emit(self.row(item), directory)
        event.acceptProposedAction()


class PresetModePanel(QWidget):
    """Preset list, editor controls, and the run button."""

    runRequested = Signal(Path, object)   # source, Preset

    def __init__(self, store: PresetStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self._visible: list[Preset] = []

        self.drop_zone = SourceDropZone()

        self._sort = _sort_combo()
        self._sort.currentTextChanged.connect(lambda _: self.reload())

        self._list = PresetList()
        self._list.itemDoubleClicked.connect(lambda _: self._edit())
        self._list.currentRowChanged.connect(lambda _: self._sync())
        self._list.sourceDropped.connect(self._on_dropped)

        self._new = button("New", flat=True)
        self._edit_button = button("Edit", flat=True)
        self._duplicate = button("Duplicate", flat=True)
        self._delete = button("Delete", flat=True)
        self._new.clicked.connect(self._create)
        self._edit_button.clicked.connect(self._edit)
        self._duplicate.clicked.connect(self._duplicate_selected)
        self._delete.clicked.connect(self._delete_selected)

        self._run = button("Add to queue", accent=True)
        self._run.clicked.connect(self._run_selected)

        self._hint = label("Drop a card on a preset, or pick both and add to queue.",
                           "muted")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.drop_zone)
        layout.addWidget(row(label("Presets", "heading"), None,
                             label("Sort", "muted"), self._sort))
        layout.addWidget(self._list, 1)
        layout.addWidget(row(self._new, self._edit_button, self._duplicate,
                             self._delete, None))
        layout.addWidget(self._hint)
        layout.addWidget(row(None, self._run))

        self.drop_zone.pathChosen.connect(lambda _: self._sync())
        self.reload()

    # ---------------------------------------------------------------- state
    def set_source(self, path: Path) -> None:
        self.drop_zone.set_path(path)
        self._sync()

    def selected_preset(self) -> Preset | None:
        index = self._list.currentRow()
        if 0 <= index < len(self._visible):
            return self._visible[index]
        return None

    def reload(self) -> None:
        previous = self.selected_preset()
        self._visible = self.store.sorted(self._sort.currentText())

        self._list.clear()
        for preset in self._visible:
            item = QListWidgetItem()
            widget = PresetRow(preset)
            # sizeHint() under-reports for the nested layout and clips
            # descenders, so pin a height that fits both text lines.
            item.setSizeHint(QSize(0, max(58, widget.sizeHint().height())))
            if not preset.is_runnable:
                item.setToolTip("This preset has no destinations yet — edit it first.")
            self._list.addItem(item)
            self._list.setItemWidget(item, widget)

        if previous is not None and previous in self._visible:
            self._list.setCurrentRow(self._visible.index(previous))
        elif self._visible:
            self._list.setCurrentRow(0)
        self._sync()

    def _sync(self) -> None:
        preset = self.selected_preset()
        has_source = self.drop_zone.path is not None
        self._run.setEnabled(bool(preset and preset.is_runnable and has_source))
        for widget in (self._edit_button, self._duplicate, self._delete):
            widget.setEnabled(preset is not None)

        if preset is None:
            self._hint.setText("Create a preset to get started.")
        elif not preset.is_runnable:
            self._hint.setText(f"“{preset.name}” has no destinations — edit it first.")
        elif not has_source:
            self._hint.setText("Choose a card above, or drop one on a preset.")
        else:
            self._hint.setText(
                f"Ready: {self.drop_zone.path} → “{preset.name}”")

    # ---------------------------------------------------------------- actions
    def _store_index(self, preset: Preset) -> int:
        return self.store.presets.index(preset)

    def _create(self) -> None:
        dialog = PresetEditor(None, self)
        if dialog.exec() and dialog.result_preset:
            self.store.add(dialog.result_preset)
            self.reload()

    def _edit(self) -> None:
        preset = self.selected_preset()
        if preset is None:
            return
        dialog = PresetEditor(preset, self)
        if dialog.exec() and dialog.result_preset:
            self.store.update(self._store_index(preset), dialog.result_preset)
            self.reload()

    def _duplicate_selected(self) -> None:
        preset = self.selected_preset()
        if preset is None:
            return
        self.store.duplicate(self._store_index(preset))
        self.reload()

    def _delete_selected(self) -> None:
        preset = self.selected_preset()
        if preset is None:
            return
        confirm = QMessageBox.question(
            self, "Delete preset", f"Delete “{preset.name}”?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            self.store.remove(self._store_index(preset))
            self.reload()

    def _run_selected(self) -> None:
        preset = self.selected_preset()
        source = self.drop_zone.path
        if preset and source:
            self.runRequested.emit(source, preset)

    def _on_dropped(self, index: int, source: Path) -> None:
        if not 0 <= index < len(self._visible):
            return
        preset = self._visible[index]
        self.drop_zone.set_path(source)
        self._sync()
        if not preset.is_runnable:
            QMessageBox.information(
                self, "No destinations",
                f"“{preset.name}” has no destinations yet. Edit it to add one.")
            return
        self.runRequested.emit(source, preset)


def _sort_combo():
    from PySide6.QtWidgets import QComboBox

    combo = QComboBox()
    combo.addItems(SORT_MODES)
    combo.setFixedWidth(110)
    return combo
