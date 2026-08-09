"""Preset editor dialog."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..hashers import ALGORITHMS
from ..models import Profile, VerificationMode
from ..naming import TOKENS
from ..presets import PRESET_COLORS, Preset
from ..reports import WRITERS
from . import theme
from .widgets import DestinationList, button, label, row

VERIFICATION_LABELS = {
    VerificationMode.NONE: "None — copy only",
    VerificationMode.SOURCE_ONLY: "Source only — hash while reading and writing",
    VerificationMode.FULL: "Full — re-read each destination from disk",
}


def _color_icon(color: str, size: int = 14) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QColor(theme.BORDER))
    painter.setBrush(QColor(color))
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pixmap)


class PresetEditor(QDialog):
    """Create or edit a preset. `result_preset` holds the value on accept."""

    def __init__(self, preset: Preset | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit preset" if preset else "New preset")
        self.setMinimumWidth(560)
        self._source = preset or Preset(name="New preset")
        self.result_preset: Preset | None = None

        self._name = QLineEdit(self._source.name)
        self._name.setPlaceholderText("Dailies — two copies")

        self._color = QComboBox()
        for color in PRESET_COLORS:
            self._color.addItem(_color_icon(color), color, color)
        index = self._color.findData(self._source.color)
        self._color.setCurrentIndex(max(0, index))

        self._destinations = DestinationList()
        self._destinations.set_paths(self._source.destinations)
        add = button("Add…", flat=True)
        add.clicked.connect(self._destinations.browse_and_add)
        remove = button("Remove", flat=True)
        remove.clicked.connect(self._destinations.remove_selected)

        self._algorithm = QComboBox()
        for key, algorithm in ALGORITHMS.items():
            self._algorithm.addItem(algorithm.label, key)
        self._algorithm.setCurrentIndex(
            max(0, self._algorithm.findData(self._source.algorithm)))

        self._verification = QComboBox()
        for mode, text in VERIFICATION_LABELS.items():
            self._verification.addItem(text, mode.value)
        self._verification.setCurrentIndex(
            max(0, self._verification.findData(self._source.verification.value)))

        self._profile = QComboBox()
        self._profile.addItem("Media — camera card (ffprobe, thumbnails, BRAW)",
                              Profile.MEDIA.value)
        self._profile.addItem("Data — any large transfer (copy and verify only)",
                              Profile.DATA.value)
        self._profile.setCurrentIndex(
            max(0, self._profile.findData(self._source.profile.value)))
        self._profile.currentIndexChanged.connect(self._on_profile_changed)

        self._thumbnails = QSpinBox()
        self._thumbnails.setRange(0, 8)
        self._thumbnails.setValue(self._source.thumbnail_count)
        self._thumbnails.setSuffix(" per clip")
        self._thumbnails.setSpecialValueText("Off")
        self._thumbnails.setEnabled(self._source.profile is Profile.MEDIA)

        self._reports: dict[str, QCheckBox] = {}
        report_row = []
        for key in WRITERS:
            box = QCheckBox(key.upper())
            box.setChecked(key in self._source.reports)
            self._reports[key] = box
            report_row.append(box)
        report_row.append(None)

        self._naming = QLineEdit(self._source.naming_template)
        self._naming.setPlaceholderText("{card}")
        tokens = label("  ".join(sorted(TOKENS)), "muted")
        tokens.setStyleSheet("font-size: 11px;")
        tokens.setWordWrap(True)
        tokens.setToolTip("\n".join(f"{k}  {v}" for k, v in sorted(TOKENS.items())))

        self._preserve = QCheckBox("Recreate the source folder structure")
        self._preserve.setChecked(self._source.preserve_structure)
        self._skip = QCheckBox("Skip files already present at matching size")
        self._skip.setChecked(self._source.skip_existing)

        self._excludes = QLineEdit(", ".join(self._source.excludes))
        self._excludes.setPlaceholderText("*.tmp, *.thm")

        self._logo = QLineEdit(str(self._source.logo) if self._source.logo else "")
        self._logo.setPlaceholderText("Optional image for the PDF header")
        browse_logo = button("Browse…", flat=True)
        browse_logo.clicked.connect(self._choose_logo)

        self._footer = QLineEdit(self._source.footer or "")
        self._footer.setPlaceholderText("Offloader Version 0.1.0")

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.addRow("Name", self._name)
        form.addRow("Colour", self._color)
        form.addRow("Destinations", self._destinations)
        form.addRow("", row(add, remove, None))
        form.addRow("Profile", self._profile)
        form.addRow("Checksum", self._algorithm)
        form.addRow("Verification", self._verification)
        form.addRow("Thumbnails", self._thumbnails)
        form.addRow("Reports", row(*report_row))
        form.addRow("Job name", self._naming)
        form.addRow("", tokens)
        form.addRow("Exclude", self._excludes)
        form.addRow("PDF logo", row(self._logo, browse_logo))
        form.addRow("PDF footer", self._footer)
        form.addRow("", self._preserve)
        form.addRow("", self._skip)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_profile_changed(self) -> None:
        # A data transfer never decodes a file, so contact-sheet thumbnails do
        # not apply — disable the control rather than let it imply otherwise.
        self._thumbnails.setEnabled(self._profile.currentData() == Profile.MEDIA.value)

    def _choose_logo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a logo", "", "Images (*.png *.jpg *.jpeg *.gif)")
        if path:
            self._logo.setText(path)

    def _accept(self) -> None:
        excludes = [part.strip() for part in self._excludes.text().split(",")
                    if part.strip()]
        logo_text = self._logo.text().strip()
        footer_text = self._footer.text().strip()

        self.result_preset = replace(
            self._source,
            name=self._name.text().strip() or "Untitled",
            color=self._color.currentData(),
            destinations=self._destinations.paths(),
            algorithm=self._algorithm.currentData(),
            verification=VerificationMode(self._verification.currentData()),
            profile=Profile(self._profile.currentData()),
            thumbnail_count=self._thumbnails.value(),
            reports=[key for key, box in self._reports.items() if box.isChecked()],
            naming_template=self._naming.text().strip() or "{card}",
            preserve_structure=self._preserve.isChecked(),
            skip_existing=self._skip.isChecked(),
            excludes=excludes,
            logo=Path(logo_text) if logo_text else None,
            footer=footer_text or None,
        )
        self.accept()
