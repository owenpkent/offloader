"""Main window: mode switch, drive panel, and the queue."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import PRODUCT_NAME, __version__, engine, history, probe, thumbs
from ..config import config_file, read_json, write_json
from ..presets import Preset, PresetStore
from ..util import format_size
from . import theme
from .drives import DrivesPanel
from .preset_mode import PresetModePanel
from .queue_view import QueuePanel, reveal
from .simple_mode import SimpleModePanel
from .widgets import button, label, row
from .worker import JobState, QueueController

SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "sound_on_completion": True,
    "warn_on_duplicate": True,
    "mode": "preset",
}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{PRODUCT_NAME} {__version__}")
        self.resize(1280, 840)
        self.setMinimumSize(980, 640)

        self.settings = {**DEFAULT_SETTINGS,
                         **read_json(config_file(SETTINGS_FILE), {})}
        self.store = PresetStore()
        self.controller = QueueController(self)
        self.controller.jobFinished.connect(self._on_job_finished)
        self.controller.jobStarted.connect(lambda _: self._update_status())
        self.controller.itemsChanged.connect(self._update_status)
        self.controller.itemChanged.connect(lambda _: self._update_status())

        # ----------------------------------------------------------- panels
        self.simple = SimpleModePanel()
        self.simple.runRequested.connect(self._enqueue_simple)
        self.presets = PresetModePanel(self.store)
        self.presets.runRequested.connect(self._enqueue_preset)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.presets)
        self.stack.addWidget(self.simple)

        self.drives = DrivesPanel()
        self.drives.useAsSource.connect(self._set_source)
        self.drives.useAsDestination.connect(self._add_destination)

        self.queue = QueuePanel(self.controller)

        # ----------------------------------------------------------- chrome
        self._preset_button = button("Presets")
        self._simple_button = button("Simple")
        for widget in (self._preset_button, self._simple_button):
            widget.setProperty("mode", "true")
            widget.setCheckable(True)
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self._preset_button, 0)
        group.addButton(self._simple_button, 1)
        group.idClicked.connect(self._set_mode)

        header = row(
            label(PRODUCT_NAME, "title"),
            24,
            self._preset_button,
            self._simple_button,
            None,
        )

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.drives)
        left.setMinimumWidth(240)
        left.setMaximumWidth(360)

        upper = QSplitter(Qt.Horizontal)
        upper.addWidget(left)
        upper.addWidget(self.stack)
        upper.setStretchFactor(1, 1)
        upper.setSizes([280, 900])

        vertical = QSplitter(Qt.Vertical)
        vertical.addWidget(upper)
        vertical.addWidget(self.queue)
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 2)
        vertical.setSizes([520, 300])

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(12)
        layout.addWidget(header)
        layout.addWidget(vertical, 1)
        self.setCentralWidget(central)

        self._build_menu()
        self.statusBar().showMessage(self._environment_summary())

        self._set_mode(1 if self.settings.get("mode") == "simple" else 0)
        self.drives.start()

    # ---------------------------------------------------------------- chrome
    def _build_menu(self) -> None:
        job_menu = self.menuBar().addMenu("&Job")

        start = QAction("Start next queued", self)
        start.triggered.connect(self.controller.start_next)
        job_menu.addAction(start)

        cancel_all = QAction("Cancel all", self)
        cancel_all.triggered.connect(self.controller.cancel_all)
        job_menu.addAction(cancel_all)

        job_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        job_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("&View")
        for index, name in ((0, "Preset mode"), (1, "Simple mode")):
            action = QAction(name, self)
            action.triggered.connect(lambda _=False, i=index: self._set_mode(i))
            view_menu.addAction(action)

        options_menu = self.menuBar().addMenu("&Options")
        self._sound_action = QAction("Sound on completion", self, checkable=True)
        self._sound_action.setChecked(bool(self.settings["sound_on_completion"]))
        self._sound_action.toggled.connect(
            lambda on: self._save_setting("sound_on_completion", on))
        options_menu.addAction(self._sound_action)

        self._duplicate_action = QAction("Warn on duplicate offload", self,
                                         checkable=True)
        self._duplicate_action.setChecked(bool(self.settings["warn_on_duplicate"]))
        self._duplicate_action.toggled.connect(
            lambda on: self._save_setting("warn_on_duplicate", on))
        options_menu.addAction(self._duplicate_action)

        options_menu.addSeparator()
        open_config = QAction("Open configuration folder", self)
        open_config.triggered.connect(lambda: reveal(config_file(SETTINGS_FILE)))
        options_menu.addAction(open_config)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("About", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

    def _save_setting(self, key: str, value) -> None:
        self.settings[key] = value
        write_json(config_file(SETTINGS_FILE), self.settings)

    def _set_mode(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self._preset_button.setChecked(index == 0)
        self._simple_button.setChecked(index == 1)
        self._save_setting("mode", "simple" if index else "preset")

    def _environment_summary(self) -> str:
        missing = []
        if probe.ffprobe_path() is None:
            missing.append("ffprobe (no media metadata)")
        if thumbs.ffmpeg_path() is None:
            missing.append("ffmpeg (no thumbnails)")
        if missing:
            return "Not found: " + ", ".join(missing)
        return "ffmpeg and ffprobe found — metadata and thumbnails enabled"

    def _show_about(self) -> None:
        QMessageBox.about(
            self, f"About {PRODUCT_NAME}",
            f"<b>{PRODUCT_NAME} {__version__}</b><br><br>"
            "Verified media offload with ShotPut Pro-compatible reports.<br><br>"
            f"<span style='color:{theme.FG_MUTED}'>{self._environment_summary()}"
            "</span>",
        )

    # ---------------------------------------------------------------- routing
    def _set_source(self, path: Path) -> None:
        self.simple.set_source(path)
        self.presets.set_source(path)

    def _add_destination(self, path: Path) -> None:
        self.simple.add_destination(path)
        if self.stack.currentIndex() == 0:
            self.statusBar().showMessage(
                f"{path} added to Simple mode destinations. "
                "Edit a preset to add it there.", 6000)

    # ---------------------------------------------------------------- queueing
    def _enqueue_simple(self, source: Path, preset: Preset, name: str) -> None:
        self._enqueue(source, preset, name)

    def _enqueue_preset(self, source: Path, preset: Preset) -> None:
        self._enqueue(source, preset, None)

    def _enqueue(self, source: Path, preset: Preset, name: str | None) -> None:
        if not self._check_destinations(source, preset):
            return
        if not self._check_duplicate(source):
            return
        item = self.controller.enqueue(source, preset, name)
        self.statusBar().showMessage(f"Queued “{item.name}”", 5000)

    def _check_destinations(self, source: Path, preset: Preset) -> bool:
        """Refuse destinations that sit inside the source, and warn when one
        does not have room."""
        if not preset.destinations:
            QMessageBox.warning(
                self, "No destinations",
                f"“{preset.name}” has no destinations. Edit it to add one.")
            return False

        try:
            engine.assert_safe_destinations(source, preset.destinations)
        except engine.UnsafeDestination as exc:
            QMessageBox.warning(self, "Unsafe destination", str(exc))
            return False

        try:
            paths = engine.scan(source, preset.to_options().excludes)
            needed = sum(path.stat().st_size for path in paths)
        except OSError:
            return True
        if needed == 0:
            QMessageBox.warning(self, "Nothing to offload",
                                f"No files found under {source}.")
            return False

        import shutil
        for destination in preset.destinations:
            try:
                free = shutil.disk_usage(
                    destination if Path(destination).exists()
                    else Path(destination).anchor).free
            except OSError:
                continue
            if free < needed:
                answer = QMessageBox.question(
                    self, "Not enough space",
                    f"{destination} has {format_size(free)} free but the offload "
                    f"needs {format_size(needed)}.\n\nQueue it anyway?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return False
        return True

    def _check_duplicate(self, source: Path) -> bool:
        """Human-error protection: warn when this exact card was already
        offloaded successfully."""
        if not self.settings.get("warn_on_duplicate", True):
            return True
        try:
            paths = engine.scan(source)
            signature = history.fingerprint(paths, Path(source))
        except OSError:
            return True

        previous = self.controller.history.find(signature)
        if not previous:
            return True

        answer = QMessageBox.question(
            self, "Already offloaded",
            f"This card looks like one you have already offloaded:\n\n"
            f"{previous[0].describe()}\n\nOffload it again?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    # ---------------------------------------------------------------- events
    def _update_status(self) -> None:
        running = [i for i in self.controller.items if i.state is JobState.RUNNING]
        queued = [i for i in self.controller.items if i.state is JobState.QUEUED]
        if running:
            item = running[0]
            self.statusBar().showMessage(
                f"{item.name}: {item.stage} {item.current_file} "
                f"— {item.fraction * 100:.0f}%"
                + (f", {len(queued)} waiting" if queued else "")
            )
        elif queued:
            self.statusBar().showMessage(f"{len(queued)} job(s) waiting")

    def _on_job_finished(self, identifier: int) -> None:
        item = self.controller.find(identifier)
        if item is None:
            return
        self.presets.reload()      # refresh use counts

        if self.settings.get("sound_on_completion", True):
            QApplication.beep()

        if item.state is JobState.FAILED:
            QMessageBox.critical(
                self, "Offload failed",
                f"“{item.name}” did not complete.\n\n{item.error or 'Unknown error'}")
            self.statusBar().showMessage(f"“{item.name}” failed", 10000)
        elif item.state is JobState.CANCELLED:
            self.statusBar().showMessage(f"“{item.name}” cancelled", 6000)
        elif item.job is not None and item.job.warnings:
            shown = item.job.warnings[:12]
            body = "\n".join(f"• {warning}" for warning in shown)
            if len(item.job.warnings) > len(shown):
                body += f"\n… and {len(item.job.warnings) - len(shown)} more"
            QMessageBox.warning(
                self, "Offload finished with warnings",
                f"“{item.name}” completed, but with "
                f"{len(item.job.warnings)} warning(s):\n\n{body}")
            self.statusBar().showMessage(
                f"“{item.name}” {item.job.final_status.lower()} with "
                f"{len(item.job.warnings)} warning(s)", 10000)
        elif item.job is not None:
            self.statusBar().showMessage(
                f"“{item.name}” {item.job.final_status.lower()} — "
                f"{item.job.total_files} files, {format_size(item.job.total_bytes)}",
                10000,
            )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        active = [i for i in self.controller.items if not i.state.is_terminal]
        if active:
            answer = QMessageBox.question(
                self, "Offload in progress",
                f"{len(active)} job(s) are still running or queued.\n\n"
                "Cancel them and quit?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.drives.stop()
        self.controller.shutdown()
        event.accept()
