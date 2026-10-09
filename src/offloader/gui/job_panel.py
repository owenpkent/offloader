"""The one screen: source, destinations, go.

Everything else lives behind an Advanced disclosure and is remembered between
runs, so the common case is three things and a button, and the operator who
changed the checksum last week does not have to change it again.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..hashers import ALGORITHMS
from ..models import Profile, VerificationMode
from ..presets import Preset
from ..reports import WRITERS
from .widgets import DestinationList, SourceDropZone, button, label, row, section

VERIFICATION_LABELS = {
    VerificationMode.NONE: "None — copy only",
    VerificationMode.SOURCE_ONLY: "Source only — hash on read and write",
    VerificationMode.FULL: "Full — re-read each destination",
}

DEFAULT_OPTIONS = {
    "algorithm": "xxh3-64",
    "verification": VerificationMode.SOURCE_ONLY.value,
    "profile": Profile.MEDIA.value,
    "thumbnail_count": 4,
    "reports": ["pdf"],
    "preserve_structure": True,
    "skip_existing": False,
    "excludes": [],
    "logo": "",
    "footer": "",
    "advanced_open": False,
}


class JobPanel(QWidget):
    """A single job, assembled from inline controls."""

    runRequested = Signal(Path, object, str)   # source, Preset, job name
    optionsChanged = Signal()
    advancedToggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.drop_zone = SourceDropZone()
        self.drop_zone.setMinimumHeight(84)
        self.drop_zone.pathChosen.connect(self._on_source_chosen)

        self.destinations = DestinationList()
        self.destinations.changed.connect(self._sync)
        self.destinations.setMinimumHeight(72)
        self.destinations.setMaximumHeight(104)
        add = button("Add…", ghost=True)
        add.clicked.connect(self.destinations.browse_and_add)
        remove = button("Remove", ghost=True)
        remove.clicked.connect(self.destinations.remove_selected)

        self._start = button("Start offload", accent=True)
        self._start.clicked.connect(self._start_clicked)
        self._hint = label("Choose a source and at least one destination.", "readout")

        # --------------------------------------------------------- advanced
        self._name = QLineEdit()
        self._name.setPlaceholderText("Named after the source folder")
        self._name.textChanged.connect(self._sync)

        self._profile = QComboBox()
        self._profile.addItem("Media — camera card", Profile.MEDIA.value)
        self._profile.addItem("Data — generic transfer", Profile.DATA.value)
        self._profile.currentIndexChanged.connect(self._on_profile_changed)

        self._algorithm = QComboBox()
        for key, algorithm in ALGORITHMS.items():
            self._algorithm.addItem(algorithm.label, key)

        self._verification = QComboBox()
        for mode, text in VERIFICATION_LABELS.items():
            self._verification.addItem(text, mode.value)

        self._thumbnails = QSpinBox()
        self._thumbnails.setRange(0, 8)
        self._thumbnails.setSuffix(" per clip")
        self._thumbnails.setSpecialValueText("Off")

        self._reports: dict[str, QCheckBox] = {}
        report_row = []
        for key in WRITERS:
            box = QCheckBox(key.upper())
            self._reports[key] = box
            report_row.append(box)
        report_row.append(None)

        self._excludes = QLineEdit()
        self._excludes.setPlaceholderText("*.tmp, *.thm")

        self._logo = QLineEdit()
        self._logo.setPlaceholderText("Optional image for the PDF header")
        browse_logo = button("Browse…", ghost=True)
        browse_logo.clicked.connect(self._choose_logo)

        self._footer = QLineEdit()
        self._footer.setPlaceholderText("Footer line for the PDF")

        self._preserve = QCheckBox("Keep folder structure")
        self._preserve.setToolTip("Recreate the source folder structure at each "
                                  "destination; off copies everything flat.")
        self._skip = QCheckBox("Skip existing")
        self._skip.setToolTip("Skip files already present at the destination with "
                              "a matching size.")

        # Two columns of short controls, so the whole section fits beneath the
        # Start row without scrolling (docs/ui-philosophy.md, rules 1 and 5).
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)

        def put(r: int, c: int, text: str, widget: QWidget) -> None:
            grid.addWidget(section(text), r, c * 2, Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(widget, r, c * 2 + 1)

        put(0, 0, "Job name", self._name)
        put(0, 1, "Reports", row(*report_row, spacing=10))
        put(1, 0, "Checksum", self._algorithm)
        put(1, 1, "Thumbs", self._thumbnails)
        put(2, 0, "Verify", self._verification)
        put(2, 1, "Exclude", self._excludes)
        put(3, 0, "Profile", self._profile)
        put(3, 1, "PDF footer", self._footer)
        put(4, 0, "PDF logo", row(self._logo, browse_logo, spacing=6))
        grid.addWidget(row(self._preserve, self._skip, None, spacing=14), 4, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self._advanced = QFrame()
        self._advanced.setProperty("role", "card")
        self._advanced.setProperty("compact", "true")
        advanced_layout = QVBoxLayout(self._advanced)
        advanced_layout.setContentsMargins(14, 12, 14, 12)
        advanced_layout.addLayout(grid)

        self._toggle = button("Advanced ▸", ghost=True)
        self._toggle.setCheckable(True)
        self._toggle.toggled.connect(self.set_advanced_open)
        self._summary = label("", "readout")

        # ----------------------------------------------------------- layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.drop_zone)
        layout.addSpacing(4)
        layout.addWidget(row(section("Destinations"), None, add, remove, spacing=6))
        layout.addWidget(self.destinations)
        layout.addWidget(row(self._hint, None, self._start))
        layout.addSpacing(6)
        layout.addWidget(row(self._toggle, 10, self._summary, None, spacing=6))
        layout.addWidget(self._advanced)
        layout.addStretch(1)

        self.apply_options(DEFAULT_OPTIONS)
        for widget in (self._profile, self._algorithm, self._verification):
            widget.currentIndexChanged.connect(self._on_option_changed)
        self._thumbnails.valueChanged.connect(self._on_option_changed)
        for box in list(self._reports.values()) + [self._preserve, self._skip]:
            box.toggled.connect(self._on_option_changed)
        for field in (self._excludes, self._logo, self._footer):
            field.editingFinished.connect(self._on_option_changed)
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

    def set_advanced_open(self, open_: bool) -> None:
        if self._toggle.isChecked() != open_:
            self._toggle.blockSignals(True)
            self._toggle.setChecked(open_)
            self._toggle.blockSignals(False)
        self._advanced.setVisible(open_)
        self._toggle.setText("ADVANCED ▾" if open_ else "ADVANCED ▸")
        self.advancedToggled.emit(open_)
        self.optionsChanged.emit()

    @property
    def advanced_open(self) -> bool:
        return self._toggle.isChecked()

    def _on_profile_changed(self) -> None:
        # Thumbnails are contact-sheet frames from a clip — meaningless for a
        # generic data transfer, which never decodes a file.
        is_media = self._profile.currentData() == Profile.MEDIA.value
        self._thumbnails.setEnabled(is_media)

    def _on_option_changed(self, *_) -> None:
        self._sync()
        self.optionsChanged.emit()

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
            self._hint.setText("A destination sits inside the source — pick another.")
        else:
            copies = f"{len(destinations)} cop{'ies' if len(destinations) > 1 else 'y'}"
            self._hint.setText(f"READY  {source}  →  {copies}")

        reports = [k for k, box in self._reports.items() if box.isChecked()]
        self._summary.setText("  ·  ".join([
            self._algorithm.currentData() or "",
            f"{self._verification.currentData()} verify",
            ", ".join(reports) if reports else "no reports",
            "data" if self._profile.currentData() == Profile.DATA.value else "media",
        ]))

    @staticmethod
    def _overlaps(source: Path, destination: Path) -> bool:
        """Copying a tree into itself would recurse forever; refuse up front."""
        try:
            source = Path(source).resolve()
            destination = Path(destination).resolve()
        except OSError:
            return False
        return source == destination or source in destination.parents

    def _choose_logo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a logo", "", "Images (*.png *.jpg *.jpeg *.gif)")
        if path:
            self._logo.setText(path)
            self._on_option_changed()

    # ---------------------------------------------------------------- options
    def options(self) -> dict:
        """The advanced settings, as a JSON-friendly dict for settings.json."""
        return {
            "algorithm": self._algorithm.currentData(),
            "verification": self._verification.currentData(),
            "profile": self._profile.currentData(),
            "thumbnail_count": self._thumbnails.value(),
            "reports": [k for k, box in self._reports.items() if box.isChecked()],
            "preserve_structure": self._preserve.isChecked(),
            "skip_existing": self._skip.isChecked(),
            "excludes": self._excludes_list(),
            "logo": self._logo.text().strip(),
            "footer": self._footer.text().strip(),
            "advanced_open": self._toggle.isChecked(),
        }

    def apply_options(self, values: dict) -> None:
        """Restore settings; anything missing or malformed keeps its default."""
        merged = {**DEFAULT_OPTIONS, **(values or {})}

        def select(combo: QComboBox, data) -> None:
            combo.setCurrentIndex(max(0, combo.findData(data)))

        select(self._algorithm, merged["algorithm"])
        select(self._verification, merged["verification"])
        select(self._profile, merged["profile"])
        try:
            self._thumbnails.setValue(int(merged["thumbnail_count"]))
        except (TypeError, ValueError):
            self._thumbnails.setValue(DEFAULT_OPTIONS["thumbnail_count"])
        chosen = merged["reports"] if isinstance(merged["reports"], list) else ["pdf"]
        for key, box in self._reports.items():
            box.setChecked(key in chosen)
        self._preserve.setChecked(bool(merged["preserve_structure"]))
        self._skip.setChecked(bool(merged["skip_existing"]))
        excludes = merged["excludes"] if isinstance(merged["excludes"], list) else []
        self._excludes.setText(", ".join(str(e) for e in excludes))
        self._logo.setText(str(merged["logo"] or ""))
        self._footer.setText(str(merged["footer"] or ""))
        self._on_profile_changed()
        self.set_advanced_open(bool(merged["advanced_open"]))
        self._sync()

    def _excludes_list(self) -> list[str]:
        return [part.strip() for part in self._excludes.text().split(",")
                if part.strip()]

    def build_preset(self) -> Preset:
        """The job configuration, as the options bundle the worker runs."""
        logo = self._logo.text().strip()
        return Preset(
            name="Job",
            destinations=self.destinations.paths(),
            algorithm=self._algorithm.currentData(),
            verification=VerificationMode(self._verification.currentData()),
            profile=Profile(self._profile.currentData()),
            thumbnail_count=self._thumbnails.value(),
            reports=[key for key, box in self._reports.items() if box.isChecked()],
            preserve_structure=self._preserve.isChecked(),
            skip_existing=self._skip.isChecked(),
            excludes=self._excludes_list(),
            logo=Path(logo) if logo else None,
            footer=self._footer.text().strip() or None,
        )

    def _start_clicked(self) -> None:
        source = self.drop_zone.path
        if source is None:
            return
        name = self._name.text().strip() or (Path(source).name or "Offload")
        self.runRequested.emit(source, self.build_preset(), name)
