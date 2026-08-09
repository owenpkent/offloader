"""The drive panel: mounted volumes, capacity, and card detection.

Enumeration touches the disk, so it runs on a pool thread — a sleeping USB
drive must never freeze the interface.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..util import format_size
from ..volumes import Volume, list_roots, order_volumes, probe_many
from . import theme
from .widgets import CapacityBar, button, label, row

REFRESH_MS = 4000


def scan_batches():
    """Yield (volumes, final) — local drives first, network shares after.

    A network probe is a synchronous SMB round-trip that can take seconds; the
    fixed and removable drives — the ones an offload actually uses — must not
    wait behind it. Each phase probes its roots concurrently, so the wait per
    batch is the slowest probe, not the sum.
    """
    roots = list_roots()
    local = [(root, kind) for root, kind in roots if kind != "network"]
    remote = [(root, kind) for root, kind in roots if kind == "network"]
    found = probe_many(local)
    if remote:
        yield order_volumes(found), False
        found = found + probe_many(remote)
    yield order_volumes(found), True


class _ScanSignals(QObject):
    batch = Signal(list, bool)   # volumes, final


class _ScanTask(QRunnable):
    """One volume scan on a pool thread.

    The signals object belongs to the watcher, not to this task: a QRunnable's
    Python wrapper can be collected the moment `start()` returns, and a signals
    object owned by it would be destroyed out from under the running thread.
    """

    def __init__(self, signals: _ScanSignals) -> None:
        super().__init__()
        self._signals = signals

    def run(self) -> None:  # noqa: D102 - QRunnable entry point
        try:
            for volumes, final in scan_batches():
                self._emit(volumes, final)
        except Exception:
            self._emit([], True)

    def _emit(self, volumes: list, final: bool) -> None:
        try:
            self._signals.batch.emit(volumes, final)
        except RuntimeError:
            # The window closed while this scan was in flight; nothing to tell.
            pass


class VolumeWatcher(QObject):
    """Polls for mounted volumes and reports changes."""

    volumesChanged = Signal(list)
    scanningChanged = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._volumes: list[Volume] = []
        self._busy = False
        self._stopped = False
        # Parented, so its lifetime is the watcher's rather than a task's.
        self._signals = _ScanSignals(self)
        self._signals.batch.connect(self._on_batch)
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)

    def start(self) -> None:
        self._stopped = False
        self.refresh()
        self._timer.start()

    def stop(self, wait_ms: int = 2000) -> None:
        """Stop polling and let any in-flight scan finish, so teardown cannot
        race a pool thread that is still touching this object."""
        self._stopped = True
        self._timer.stop()
        if self._busy:
            QThreadPool.globalInstance().waitForDone(wait_ms)

    def refresh(self) -> None:
        if self._busy or self._stopped:
            return
        self._busy = True
        self.scanningChanged.emit(True)
        QThreadPool.globalInstance().start(_ScanTask(self._signals))

    def _on_batch(self, volumes: list, final: bool) -> None:
        if final:
            self._busy = False
        if self._stopped:
            return
        if not final:
            # The local half of a scan whose network shares are still being
            # probed. Keep the shares from the previous scan rather than
            # tearing their rows down for a few seconds every poll. Dedup by
            # root, not resolved root — resolving a network path is itself a
            # round-trip, and this runs on the UI thread.
            known = {v.root: v for v in self._volumes
                     if v.drive_type == "network"}
            for volume in volumes:
                known[volume.root] = volume
            volumes = sorted(known.values(),
                             key=lambda v: (not v.is_camera_card, str(v.root)))
        self._volumes = volumes
        self.volumesChanged.emit(volumes)
        if final:
            self.scanningChanged.emit(False)


class VolumeRow(QFrame):
    """One volume: name, capacity bar, and the two ways to use it."""

    useAsSource = Signal(Path)
    useAsDestination = Signal(Path)

    def __init__(self, volume: Volume, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        self.volume = volume

        title = label(volume.display_name, "heading")
        badge = label("CARD" if volume.is_camera_card else volume.drive_type.upper(),
                      "muted")
        badge.setStyleSheet(
            f"color: {theme.ACCENT if volume.is_camera_card else theme.FG_MUTED};"
            "font-size: 10px; font-weight: 700; letter-spacing: 1px;"
        )

        self._bar = CapacityBar(volume.percent_used)
        detail = label(
            f"{format_size(volume.free_bytes)} free of "
            f"{format_size(volume.total_bytes)}"
            + (f" · {volume.filesystem}" if volume.filesystem else ""),
            "muted",
        )
        detail.setStyleSheet("font-size: 11px;")

        source_button = button("Source", flat=True,
                               tooltip=f"Offload from {volume.root}")
        source_button.clicked.connect(lambda: self.useAsSource.emit(volume.root))
        dest_button = button("Destination", flat=True,
                             tooltip=f"Copy to {volume.root}")
        dest_button.clicked.connect(lambda: self.useAsDestination.emit(volume.root))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        layout.addWidget(row(title, None, badge))
        layout.addWidget(label(str(volume.root), "muted"))
        layout.addWidget(self._bar)
        layout.addWidget(detail)
        layout.addWidget(row(source_button, dest_button, None))

    def matches(self, volume: Volume) -> bool:
        """Whether an updated Volume describes this same row, so refreshes can
        update in place instead of rebuilding and losing scroll position."""
        return (volume.root == self.volume.root
                and volume.label == self.volume.label
                and volume.is_camera_card == self.volume.is_camera_card)

    def update_usage(self, volume: Volume) -> None:
        self.volume = volume
        self._bar.set_percent(volume.percent_used)


class DrivesPanel(QWidget):
    """Scrollable list of volumes, cards first."""

    useAsSource = Signal(Path)
    useAsDestination = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[VolumeRow] = []

        self.watcher = VolumeWatcher(self)
        self.watcher.volumesChanged.connect(self._rebuild)
        self.watcher.scanningChanged.connect(self._on_scanning)

        self._refresh = button("Refresh", flat=True)
        self._refresh.clicked.connect(self.watcher.refresh)
        header = row(label("Drives", "heading"), None, self._refresh)

        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 6, 0)
        self._container_layout.setSpacing(8)
        self._container_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(self._container)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(header)
        layout.addWidget(scroll, 1)

    def start(self) -> None:
        self.watcher.start()

    def stop(self) -> None:
        self.watcher.stop()

    def _on_scanning(self, scanning: bool) -> None:
        # The busy state the panel was missing: without it a slow network
        # share made "Refresh" look like a button that does nothing.
        self._refresh.setEnabled(not scanning)
        self._refresh.setText("Scanning…" if scanning else "Refresh")

    def _rebuild(self, volumes: list) -> None:
        same_set = (
            len(volumes) == len(self._rows)
            and all(existing.matches(new)
                    for existing, new in zip(self._rows, volumes, strict=True))
        )
        if same_set:
            for existing, new in zip(self._rows, volumes, strict=True):
                existing.update_usage(new)
            return

        for existing in self._rows:
            self._container_layout.removeWidget(existing)
            existing.deleteLater()
        self._rows.clear()

        for index, volume in enumerate(volumes):
            widget = VolumeRow(volume)
            widget.useAsSource.connect(self.useAsSource)
            widget.useAsDestination.connect(self.useAsDestination)
            self._container_layout.insertWidget(index, widget)
            self._rows.append(widget)
