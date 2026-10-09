"""Main window: header, drive panel, the job panel, and the queue."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
)

from .. import PRODUCT_NAME, __version__, engine, history, probe, thumbs
from ..config import config_file, read_json, write_json
from ..presets import Preset
from ..util import format_size
from . import theme
from .drives import DrivesPanel
from .job_panel import JobPanel
from .queue_view import QueuePanel, reveal
from .widgets import Backdrop, Led, card, label, row
from .worker import JobState, QueueController

SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "sound_on_completion": True,
    "warn_on_duplicate": True,
    "job": {},
}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{PRODUCT_NAME} {__version__}")
        self.resize(1280, 880)
        self.setMinimumSize(980, 640)

        self.settings = {**DEFAULT_SETTINGS,
                         **read_json(config_file(SETTINGS_FILE), {})}
        self.controller = QueueController(self)
        self.controller.jobFinished.connect(self._on_job_finished)
        self.controller.jobStarted.connect(lambda _: self._update_status())
        self.controller.itemsChanged.connect(self._update_status)
        self.controller.itemChanged.connect(lambda _: self._update_status())

        # ----------------------------------------------------------- panels
        self.job = JobPanel()
        self.job.apply_options(self.settings.get("job") or {})
        self.job.runRequested.connect(self._enqueue)
        self.job.optionsChanged.connect(
            lambda: self._save_setting("job", self.job.options()))

        job_scroll = QScrollArea()
        job_scroll.setProperty("role", "bare")
        job_scroll.setWidget(self.job)
        job_scroll.setWidgetResizable(True)
        job_scroll.setFrameShape(QFrame.NoFrame)
        job_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.drives = DrivesPanel()
        self.drives.useAsSource.connect(self._set_source)
        self.drives.useAsDestination.connect(self._add_destination)

        self.queue = QueuePanel(self.controller)

        # ----------------------------------------------------------- chrome
        self._activity_led = Led(theme.FG_FAINT)
        self._activity = label("IDLE", "readout")

        header = row(
            label(PRODUCT_NAME.upper(), "wordmark"),
            label(f"v{__version__}", "version"),
            None,
            self._activity_led,
            self._activity,
            spacing=10,
        )

        left = card(self.drives, margins=14, role="rail")
        left.setMinimumWidth(250)
        left.setMaximumWidth(380)

        upper = QSplitter(Qt.Horizontal)
        upper.addWidget(left)
        upper.addWidget(card(job_scroll, margins=18, role="rail"))
        upper.setStretchFactor(1, 1)
        upper.setSizes([290, 900])

        vertical = QSplitter(Qt.Vertical)
        vertical.addWidget(upper)
        vertical.addWidget(card(self.queue, margins=14, role="rail"))
        vertical.setStretchFactor(0, 3)
        vertical.setStretchFactor(1, 2)
        vertical.setSizes([560, 280])

        central = Backdrop()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(18, 12, 18, 14)
        layout.setSpacing(12)
        layout.addWidget(header)
        layout.addWidget(vertical, 1)
        self.setCentralWidget(central)

        self._build_menu()
        self.statusBar().showMessage(self._environment_summary())
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
        self.job.set_source(path)

    def _add_destination(self, path: Path) -> None:
        self.job.add_destination(path)

    # ---------------------------------------------------------------- queueing
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
            QMessageBox.warning(self, "No destinations",
                                "Add at least one destination.")
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
        paused = [i for i in self.controller.items if i.state is JobState.PAUSED]
        queued = [i for i in self.controller.items if i.state is JobState.QUEUED]
        if running:
            item = running[0]
            self.statusBar().showMessage(
                f"{item.name}: {item.stage} {item.current_file} "
                f"— {item.fraction * 100:.0f}%"
                + (f", {len(queued)} waiting" if queued else "")
            )
            self._set_activity(theme.ACCENT, f"{item.stage.upper() or 'RUNNING'}  "
                               f"{item.fraction * 100:3.0f}%", pulse=True)
        elif paused:
            self._set_activity(theme.WARN, "PAUSED")
        elif queued:
            self.statusBar().showMessage(f"{len(queued)} job(s) waiting")
            self._set_activity(theme.FG_MUTED, f"{len(queued)} QUEUED")
        else:
            self._set_activity(theme.FG_FAINT, "IDLE")

    def _set_activity(self, colour: str, text: str, pulse: bool = False) -> None:
        self._activity_led.set_colour(colour, pulse=pulse)
        self._activity.setText(text)
        self._activity.setProperty(
            "role", "readout-accent" if colour == theme.ACCENT else "readout")
        self._activity.style().unpolish(self._activity)
        self._activity.style().polish(self._activity)

    def _on_job_finished(self, identifier: int) -> None:
        item = self.controller.find(identifier)
        if item is None:
            return

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
