"""Drive panel: scanning off the UI thread, and updating without flicker."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QDeadlineTimer, QEventLoop, QThreadPool  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from offloader.gui import drives  # noqa: E402
from offloader.volumes import Volume  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def _volume(root: str, label: str = "CARD", *, card: bool = True,
            free: int = 50_000_000_000) -> Volume:
    return Volume(root=Path(root), label=label, filesystem="exFAT",
                  total_bytes=100_000_000_000, free_bytes=free,
                  drive_type="removable", is_camera_card=card)


def _pump(app, predicate, timeout_ms: int = 10_000) -> bool:
    deadline = QDeadlineTimer(timeout_ms)
    while not predicate():
        if deadline.hasExpired():
            return False
        app.processEvents(QEventLoop.AllEvents, 20)
    return True


def _one_batch(*volumes):
    """A scan_batches stand-in delivering a single, final batch."""
    return lambda: iter([(list(volumes), True)])


def test_watcher_reports_volumes_from_a_pool_thread(qapp, monkeypatch):
    monkeypatch.setattr(drives, "scan_batches", _one_batch(_volume("E:/")))
    watcher = drives.VolumeWatcher()
    received: list[list] = []
    watcher.volumesChanged.connect(received.append)

    watcher.refresh()
    assert _pump(qapp, lambda: received), "watcher never reported"
    assert [v.root for v in received[0]] == [Path("E:/")]
    watcher.stop()


def test_scan_survives_its_task_wrapper_being_collected(qapp, monkeypatch):
    """A QRunnable's Python wrapper can be collected the moment start()
    returns. The signals object must outlive it, or the pool thread emits into
    a deleted object and the drive panel silently stops updating."""
    import gc

    monkeypatch.setattr(drives, "scan_batches", _one_batch(_volume("E:/")))
    watcher = drives.VolumeWatcher()
    received: list[list] = []
    watcher.volumesChanged.connect(received.append)

    watcher.refresh()
    gc.collect()                      # aggressively reclaim the task wrapper
    assert _pump(qapp, lambda: received), "scan result was lost"
    watcher.stop()


def test_a_failing_scan_is_reported_as_empty(qapp, monkeypatch):
    def boom():
        raise OSError("drive not ready")

    monkeypatch.setattr(drives, "scan_batches", boom)
    watcher = drives.VolumeWatcher()
    received: list[list] = []
    watcher.volumesChanged.connect(received.append)

    watcher.refresh()
    assert _pump(qapp, lambda: received)
    assert received[0] == []
    watcher.stop()


def test_stop_suppresses_a_late_result(qapp, monkeypatch):
    monkeypatch.setattr(drives, "scan_batches", _one_batch(_volume("E:/")))
    watcher = drives.VolumeWatcher()
    received: list[list] = []
    watcher.volumesChanged.connect(received.append)

    watcher.refresh()
    watcher.stop()
    QThreadPool.globalInstance().waitForDone(3000)
    qapp.processEvents(QEventLoop.AllEvents, 50)
    assert received == []


def test_refresh_is_not_reentrant(qapp, monkeypatch):
    calls = []

    def scan():
        calls.append(1)
        yield [_volume("E:/")], True

    monkeypatch.setattr(drives, "scan_batches", scan)
    watcher = drives.VolumeWatcher()
    watcher.refresh()
    watcher.refresh()          # ignored while the first is in flight
    watcher.refresh()
    QThreadPool.globalInstance().waitForDone(3000)
    assert len(calls) == 1
    watcher.stop()


# ------------------------------------------------------------------ panel


def test_panel_builds_one_row_per_volume(qapp):
    panel = drives.DrivesPanel()
    panel._rebuild([_volume("E:/", "A005"), _volume("D:/", "Archive", card=False)])
    assert len(panel._rows) == 2
    assert [r.volume.label for r in panel._rows] == ["A005", "Archive"]


def test_panel_updates_in_place_when_the_volume_set_is_unchanged(qapp):
    """Rebuilding on every 4-second poll would destroy scroll position and
    make the panel flicker, so unchanged sets are updated in place."""
    panel = drives.DrivesPanel()
    panel._rebuild([_volume("E:/", "A005", free=50_000_000_000)])
    first_row = panel._rows[0]

    panel._rebuild([_volume("E:/", "A005", free=10_000_000_000)])
    assert panel._rows[0] is first_row              # same widget
    assert panel._rows[0].volume.free_bytes == 10_000_000_000


def test_panel_rebuilds_when_a_card_is_inserted(qapp):
    panel = drives.DrivesPanel()
    panel._rebuild([_volume("D:/", "Archive", card=False)])
    first_row = panel._rows[0]

    panel._rebuild([_volume("E:/", "A005"), _volume("D:/", "Archive", card=False)])
    assert len(panel._rows) == 2
    assert panel._rows[0] is not first_row


def test_panel_rebuilds_when_a_volume_becomes_a_card(qapp):
    """Formatting or writing clips to a disk changes its badge."""
    panel = drives.DrivesPanel()
    panel._rebuild([_volume("E:/", "A005", card=False)])
    panel._rebuild([_volume("E:/", "A005", card=True)])
    assert panel._rows[0].volume.is_camera_card


def test_row_buttons_emit_the_volume_root(qapp):
    panel = drives.DrivesPanel()
    panel._rebuild([_volume("E:/", "A005")])
    sources: list[Path] = []
    destinations: list[Path] = []
    panel.useAsSource.connect(sources.append)
    panel.useAsDestination.connect(destinations.append)

    row = panel._rows[0]
    row.useAsSource.emit(row.volume.root)
    row.useAsDestination.emit(row.volume.root)
    assert sources == [Path("E:/")]
    assert destinations == [Path("E:/")]


# ------------------------------------------------------------------ batching


def _network(root: str, label: str = "NAS") -> Volume:
    return Volume(root=Path(root), label=label, filesystem="NTFS",
                  total_bytes=100_000_000_000, free_bytes=50_000_000_000,
                  drive_type="network", is_camera_card=False)


def test_local_drives_are_delivered_before_network_shares(monkeypatch):
    """The batch split is the fix for a panel that waited on the slowest SMB
    share before showing the card reader plugged in next to the machine."""
    local_root = (Path("E:/"), "removable")
    remote_root = (Path("H:/"), "network")
    monkeypatch.setattr(drives, "list_roots", lambda: [local_root, remote_root])
    monkeypatch.setattr(
        drives, "probe_many",
        lambda roots: [_volume("E:/") if kind != "network" else _network("H:/")
                       for _, kind in roots])

    batches = list(drives.scan_batches())
    assert len(batches) == 2
    first, final = batches
    assert first[1] is False and final[1] is True
    assert [v.drive_type for v in first[0]] == ["removable"]
    assert sorted(v.drive_type for v in final[0]) == ["network", "removable"]


def test_no_network_shares_means_a_single_final_batch(monkeypatch):
    monkeypatch.setattr(drives, "list_roots",
                        lambda: [(Path("E:/"), "removable")])
    monkeypatch.setattr(drives, "probe_many", lambda roots: [_volume("E:/")])
    batches = list(drives.scan_batches())
    assert len(batches) == 1
    assert batches[0][1] is True


def test_partial_batch_keeps_the_known_network_shares(qapp):
    """While a poll's slow network probes are still in flight, the local-only
    partial batch must not tear the share rows down for a few seconds."""
    watcher = drives.VolumeWatcher()
    received: list[list] = []
    watcher.volumesChanged.connect(received.append)

    watcher._volumes = [_volume("E:/"), _network("H:/")]
    watcher._on_batch([_volume("E:/")], False)

    assert received, "partial batch was not reported"
    roots = [v.root for v in received[-1]]
    assert Path("H:/") in roots and Path("E:/") in roots


def test_scanning_state_wraps_a_refresh(qapp, monkeypatch):
    monkeypatch.setattr(drives, "scan_batches", _one_batch(_volume("E:/")))
    watcher = drives.VolumeWatcher()
    states: list[bool] = []
    watcher.scanningChanged.connect(states.append)

    watcher.refresh()
    assert states == [True]
    assert _pump(qapp, lambda: len(states) == 2), "scan never finished"
    assert states == [True, False]
    watcher.stop()


def test_refresh_button_reports_the_scan(qapp):
    panel = drives.DrivesPanel()
    panel._on_scanning(True)
    assert panel._refresh.text() == "Scanning…"
    assert not panel._refresh.isEnabled()
    panel._on_scanning(False)
    assert panel._refresh.text() == "Refresh"
    assert panel._refresh.isEnabled()
