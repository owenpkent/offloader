"""Simple mode: source, destinations, go.

Everything is on one screen with no saved state â€” for the one-off offload where
building a preset would be more work than the job itself.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..hashers import ALGORITHMS
from ..models import Profile, VerificationMode
from ..presets import Preset
from ..reports import WRITERS
from .preset_editor import PARANOID_LABEL, PARANOID_TOOLTIP, VERIFICATION_LABELS
from .widgets import DestinationList, SourceDropZone, button, column, label, row


class SimpleModePanel(QWidget):
    """A single ad-hoc job, assembled from inline controls."""

    runRequested = Signal(Path, object, str)   # source, Preset, job name

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._queue_busy = False

        self.drop_zone = SourceDropZone()
        self.drop_zone.pathChosen.connect(self._on_source_chosen)

        self.destinations = DestinationList()
        self.destinations.changed.connect(self._sync)
        add = button("Addâ€¦", flat=True)
        add.clicked.connect(self.destinations.browse_and_add)
        remove = button("Remove", flat=True)
        remove.clicked.connect(self.destinations.remove_selected)

        self._name = QLineEdit()
        self._name.setPlaceholderText("Named after the source folder")
        self._name.textChanged.connect(self._sync)

        self._algorithm = QComboBox()
        for key, algorithm in ALGORITHMS.items():
            self._algorithm.addItem(algorithm.picker_label, key)
        self._algorithm.setCurrentIndex(max(0, self._algorithm.findData("xxh3-64")))

        self._verification = QComboBox()
        for mode, text in VERIFICATION_LABELS.items():
            self._verification.addItem(text, mode.value)
        self._verification.setCurrentIndex(
            max(0, self._verification.findData(VerificationMode.SOURCE_ONLY.value)))

        self._profile = QComboBox()
        self._profile.addItem("Media â€” camera card", Profile.MEDIA.value)
        self._profile.addItem("Data â€” any large transfer", Profile.DATA.value)
        self._profile.setCurrentIndex(
            max(0, self._profile.findData(Profile.MEDIA.value)))
        self._profile.currentIndexChanged.connect(self._on_profile_changed)

        self._thumbnails = QSpinBox()
        self._thumbnails.setRange(0, 8)
        self._thumbnails.setValue(4)
        self._thumbnails.setSuffix(" per clip")
        self._thumbnails.setSpecialValueText("Off")

        self._reports: dict[str, QCheckBox] = {}
        report_row = []
        for key in WRITERS:
            box = QCheckBox(key.upper())
            box.setChecked(key == "pdf")
            self._reports[key] = box
            report_row.append(box)
        report_row.append(None)

        self._preserve = QCheckBox("Recreate the source folder structure")
        self._preserve.setChecked(True)
        self._paranoid = QCheckBox(PARANOID_LABEL)
        self._paranoid.setToolTip(PARANOID_TOOLTIP)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.addRow("Job name", self._name)
        form.addRow("Profile", self._profile)
        form.addRow("Checksum", self._algorithm)
        form.addRow("Verification", self._verification)
        form.addRow("Thumbnails", self._thumbnails)
        form.addRow("Reports", row(*report_row))
        form.addRow("Options", column(self._preserve, self._paranoid))

        self._start = button("Start offload", accent=True)
        self._start.clicked.connect(self._start_clicked)
        self._hint = label("Choose a source and at least one destination.", "muted")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.drop_zone)
        layout.addWidget(row(label("Destinations", "heading"), None, add, remove))
        layout.addWidget(self.destinations)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addWidget(self._hint)
        layout.addWidget(row(None, self._start))

        self._sync()

    # ---------------------------------------------------------------- state
    def set_source(self, path: Path) -> None:
        self.drop_zone.set_path(path)
        self._on_source_chosen(path)

    def add_destination(self, path: Path) -> None:
        self.destinations.add_path(path)

    def _on_source_chosen(self, path: Path) -> None:
        if not self._name.text().strip():
            self._name.setPlaceholderText(Path(path).name or "Offload")
        self._sync()

    def set_queue_busy(self, busy: bool) -> None:
        """Tell the panel whether the queue is already working. Jobs run one
        at a time, so while one is running the button cannot start anything â€”
        it enqueues. It should say so, rather than promise an immediate
        offload it cannot deliver."""
        if busy == self._queue_busy:
            return
        self._queue_busy = busy
        self._start.setText("Add to queue" if busy else "Start offload")
        self._sync()

    def _sync(self) -> None:
        source = self.drop_zone.path
        destinations = self.destinations.paths()
        overlapping = source is not None and any(
            self._overlaps(source, destination) for destination in destinations
        )

        self._start.setEnabled(bool(source and destinations) and not overlapping)
        if source is None:
            self._hint.setText("Choose a source card or folder.")
        elif not destinations:
            self._hint.setText("Add at least one destination.")
        elif overlapping:
            self._hint.setText("A destination sits inside the source â€” pick another.")
        else:
            copies = f"{len(destinations)} cop{'ies' if len(destinations) > 1 else 'y'}"
            ready = f"Ready: {source} â†’ {copies}"
            if self._queue_busy:
                ready += " â€” runs after the current job"
            self._hint.setText(ready)

    @staticmethod
    def _overlaps(source: Path, destination: Path) -> bool:
        """Copying a tree into itself would recurse forever; refuse up front."""
        try:
            source = Path(source).resolve()
            destination = Path(destination).resolve()
        except OSError:
            return False
        return source == destination or source in destination.parents

    def _on_profile_changed(self) -> None:
        # Thumbnails are contact-sheet frames from a clip â€” meaningless for a
        # generic data transfer, which never decodes a file. Grey the control
        # so the disabled state explains itself.
        is_media = self._profile.currentData() == Profile.MEDIA.value
        self._thumbnails.setEnabled(is_media)

    def build_preset(self) -> Preset:
        return Preset(
            name="Simple mode",
            destinations=self.destinations.paths(),
            algorithm=self._algorithm.currentData(),
            verification=VerificationMode(self._verification.currentData()),
            profile=Profile(self._profile.currentData()),
            thumbnail_count=self._thumbnails.value(),
            reports=[key for key, box in self._reports.items() if box.isChecked()],
            preserve_structure=self._preserve.isChecked(),
            paranoid=self._paranoid.isChecked(),
        )

    def _start_clicked(self) -> None:
        source = self.drop_zone.path
        if source is None:
            return
        name = self._name.text().strip() or (Path(source).name or "Offload")
        self.runRequested.emit(source, self.build_preset(), name)
