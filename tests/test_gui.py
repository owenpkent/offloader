"""GUI tests, run against Qt's offscreen platform.

These drive the real widgets and the real queue controller â€” the worker thread
actually copies files â€” so they cover the wiring between the interface and the
engine, not just that the modules import.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QDeadlineTimer, QEventLoop, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from offloader import engine  # noqa: E402
from offloader.gui.preset_mode import PresetModePanel  # noqa: E402
from offloader.gui.queue_view import QueuePanel  # noqa: E402
from offloader.gui.simple_mode import SimpleModePanel  # noqa: E402
from offloader.gui.widgets import DestinationList, SourceDropZone  # noqa: E402
from offloader.gui.worker import JobState, QueueController  # noqa: E402
from offloader.models import VerificationMode  # noqa: E402
from offloader.presets import Preset, PresetStore  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def controller(qapp, tmp_path, monkeypatch):
    """A queue controller whose history is written to the temp dir, never the
    user's real configuration."""
    from offloader import history as history_module

    controller = QueueController()
    controller.history = history_module.History(tmp_path / "history.json")
    yield controller
    controller.shutdown(2000)


def _pump(app, controller, predicate, timeout_ms: int = 60_000) -> bool:
    """Spin the event loop until `predicate` holds."""
    deadline = QDeadlineTimer(timeout_ms)
    while not predicate():
        if deadline.hasExpired():
            return False
        app.processEvents(QEventLoop.AllEvents, 50)
    return True


def _preset(tmp_path: Path, **overrides) -> Preset:
    values = dict(
        name="test",
        destinations=[tmp_path / "dest"],
        algorithm="xxh3-64",
        verification=VerificationMode.FULL,
        thumbnail_count=0,
        reports=["csv"],
    )
    values.update(overrides)
    return Preset(**values)


# ------------------------------------------------------------------ queue


def test_queue_runs_a_job_to_completion(qapp, controller, source_tree, tmp_path):
    item = controller.enqueue(source_tree, _preset(tmp_path), "A001")

    assert _pump(qapp, controller, lambda: item.state.is_terminal), "job never finished"
    assert item.state is JobState.DONE
    assert item.job is not None and item.job.final_status == "Verified"
    assert item.fraction == pytest.approx(1.0)
    assert (tmp_path / "dest" / "Clips" / "A001_C001.mov").is_file()


def test_reports_are_written_beside_the_media(qapp, controller, source_tree, tmp_path):
    item = controller.enqueue(source_tree, _preset(tmp_path, reports=["csv", "mhl"]),
                              "A001")
    assert _pump(qapp, controller, lambda: item.state.is_terminal)

    reports = tmp_path / "dest" / "A001_Reports"
    assert {p.name for p in reports.iterdir()} == {"JobReport.csv", "JobReport.mhl"}
    assert {p.name for p in item.reports} == {"JobReport.csv", "JobReport.mhl"}


def test_finished_job_is_recorded_in_history(qapp, controller, source_tree, tmp_path):
    item = controller.enqueue(source_tree, _preset(tmp_path), "A001")
    assert _pump(qapp, controller, lambda: item.state.is_terminal)
    assert len(controller.history.entries) == 1
    assert controller.history.entries[0].job_name == "A001"


def test_preset_use_count_increments_on_success(qapp, controller, source_tree, tmp_path):
    preset = _preset(tmp_path)
    item = controller.enqueue(source_tree, preset, "A001")
    assert _pump(qapp, controller, lambda: item.state.is_terminal)
    assert preset.use_count == 1


def test_cancelling_a_running_job(qapp, controller, tmp_path):
    source = tmp_path / "card"
    source.mkdir()
    for index in range(12):
        (source / f"clip{index:02d}.mov").write_bytes(b"\0" * 400_000)

    item = controller.enqueue(source, _preset(tmp_path), "A001")
    assert _pump(qapp, controller, lambda: item.fraction > 0.05, 20_000)
    controller.cancel(item.identifier)
    assert _pump(qapp, controller, lambda: item.state.is_terminal)

    # A fast runner can finish the whole job between the progress callback and
    # the cancel landing, so "it was cancelled" is not a property this can
    # assert. What must hold either way: a cancelled job produces no paperwork
    # and no history entry, and a completed one is genuinely complete.
    if item.state is JobState.CANCELLED:
        assert item.reports == []
        assert not controller.history.entries
    else:
        assert item.state is JobState.DONE
        assert item.job is not None and item.job.final_status == "Verified"
    assert list((tmp_path / "dest").rglob(f"*{engine.PARTIAL_SUFFIX}")) == []


def test_cancelling_a_queued_job_never_starts_it(qapp, controller, source_tree,
                                                 tmp_path):
    first = controller.enqueue(source_tree, _preset(tmp_path), "first")
    second = controller.enqueue(source_tree, _preset(tmp_path), "second")

    controller.cancel(second.identifier)
    assert second.state is JobState.CANCELLED

    assert _pump(qapp, controller, lambda: first.state.is_terminal)
    assert first.state is JobState.DONE


def test_jobs_run_one_at_a_time_then_drain(qapp, controller, source_tree, tmp_path):
    items = [
        controller.enqueue(source_tree, _preset(tmp_path, destinations=[tmp_path / f"d{i}"]),
                           f"job{i}")
        for i in range(3)
    ]
    running = [i for i in items if i.state is JobState.RUNNING]
    assert len(running) <= 1

    assert _pump(qapp, controller,
                 lambda: all(i.state.is_terminal for i in items), 90_000)
    assert all(i.state is JobState.DONE for i in items)
    assert [i.name for i in items] == ["job0", "job1", "job2"]


def test_queued_jobs_can_be_reordered(qapp, controller, source_tree, tmp_path):
    controller._auto_start = False
    first = controller.enqueue(source_tree, _preset(tmp_path), "first")
    second = controller.enqueue(source_tree, _preset(tmp_path), "second")

    controller.move(second.identifier, -1)
    assert [i.name for i in controller.items] == ["second", "first"]
    controller.move(second.identifier, 5)      # clamps at the end
    assert [i.name for i in controller.items] == ["first", "second"]
    assert first.state is JobState.QUEUED


def test_auto_naming_avoids_collisions(qapp, controller, source_tree, tmp_path):
    controller._auto_start = False
    preset = _preset(tmp_path, naming_template="{card}")
    first = controller.enqueue(source_tree, preset)
    second = controller.enqueue(source_tree, preset)

    assert first.name == source_tree.name
    assert second.name != first.name


def test_a_failing_job_surfaces_its_error(qapp, controller, source_tree, tmp_path):
    item = controller.enqueue(source_tree, _preset(tmp_path, destinations=[]), "bad")
    assert _pump(qapp, controller, lambda: item.state.is_terminal)
    assert item.state is JobState.FAILED
    assert "destination" in (item.error or "")


def test_clear_finished_keeps_pending_work(qapp, controller, source_tree, tmp_path):
    done = controller.enqueue(source_tree, _preset(tmp_path), "done")
    assert _pump(qapp, controller, lambda: done.state.is_terminal)

    controller._auto_start = False
    pending = controller.enqueue(source_tree, _preset(tmp_path), "pending")
    controller.clear_finished()
    assert controller.items == [pending]


# ------------------------------------------------------------------ widgets


def test_queue_panel_tracks_the_controller(qapp, controller, source_tree, tmp_path):
    panel = QueuePanel(controller)
    assert panel.model.rowCount() == 0

    item = controller.enqueue(source_tree, _preset(tmp_path), "A001")
    assert panel.model.rowCount() == 1
    assert panel.model.index(0, 0).data(Qt.DisplayRole) == "A001"

    assert _pump(qapp, controller, lambda: item.state.is_terminal)
    assert panel.model.index(0, 3).data(Qt.DisplayRole) == "Verified"
    assert panel.model.index(0, 4).data(Qt.UserRole) == pytest.approx(1.0)


def test_destination_list_rejects_duplicates(qapp, tmp_path):
    widget = DestinationList()
    assert widget.add_path(tmp_path / "a")
    assert not widget.add_path(tmp_path / "a")
    assert widget.paths() == [tmp_path / "a"]


def test_simple_mode_blocks_a_destination_inside_the_source(qapp, tmp_path):
    source = tmp_path / "card"
    (source / "inner").mkdir(parents=True)
    panel = SimpleModePanel()
    panel.set_source(source)
    panel.add_destination(source / "inner")
    assert not panel._start.isEnabled()

    panel.destinations.set_paths([tmp_path / "outside"])
    assert panel._start.isEnabled()


def test_simple_mode_requires_source_and_destination(qapp, tmp_path):
    panel = SimpleModePanel()
    assert not panel._start.isEnabled()
    panel.set_source(tmp_path / "card")
    assert not panel._start.isEnabled()
    panel.add_destination(tmp_path / "dest")
    assert panel._start.isEnabled()


def test_simple_mode_builds_a_preset_from_its_controls(qapp, tmp_path):
    panel = SimpleModePanel()
    panel.set_source(tmp_path / "card")
    panel.add_destination(tmp_path / "dest")
    preset = panel.build_preset()

    assert preset.destinations == [tmp_path / "dest"]
    assert preset.reports == ["pdf"]
    assert preset.verification is VerificationMode.SOURCE_ONLY


def test_preset_panel_disables_run_for_a_preset_without_destinations(qapp, tmp_path):
    store = PresetStore(tmp_path / "presets.json")
    store.presets.clear()
    store.add(Preset(name="no destinations"))
    store.add(Preset(name="ready", destinations=[tmp_path / "d"]))

    panel = PresetModePanel(store)
    panel.set_source(tmp_path / "card")

    panel._list.setCurrentRow([p.name for p in panel._visible].index("no destinations"))
    assert not panel._run.isEnabled()

    panel._list.setCurrentRow([p.name for p in panel._visible].index("ready"))
    assert panel._run.isEnabled()


def test_preset_panel_emits_the_selected_preset(qapp, tmp_path):
    store = PresetStore(tmp_path / "presets.json")
    store.presets.clear()
    store.add(Preset(name="ready", destinations=[tmp_path / "d"]))

    panel = PresetModePanel(store)
    panel.set_source(tmp_path / "card")
    panel._list.setCurrentRow(0)

    captured = []
    panel.runRequested.connect(lambda source, preset: captured.append((source, preset)))
    panel._run_selected()

    assert captured == [(tmp_path / "card", store.presets[0])]


def test_source_drop_zone_reports_its_path(qapp, tmp_path):
    zone = SourceDropZone()
    assert zone.path is None
    zone.set_path(tmp_path / "A001")
    assert zone.path == tmp_path / "A001"


# ------------------------------------------------------------- the UI sweep


def test_simple_mode_offers_the_second_read_and_it_reaches_the_engine(qapp,
                                                                      tmp_path):
    """A preset field the interface never exposes is a field nobody can use."""
    panel = SimpleModePanel()
    assert panel.build_preset().paranoid is False

    panel._paranoid.setChecked(True)
    assert panel.build_preset().to_options().paranoid is True


def test_the_preset_editor_round_trips_the_second_read(qapp):
    from offloader.gui.preset_editor import PresetEditor

    editor = PresetEditor(Preset(name="Irreplaceable", paranoid=True))
    assert editor._paranoid.isChecked()

    editor._paranoid.setChecked(False)
    editor._accept()
    assert editor.result_preset.paranoid is False


def test_the_preset_editor_is_grouped_rather_than_one_flat_list(qapp):
    """Sixteen fields in a single column read as a wall. The sections are the
    difference between scanning for a setting and hunting for it."""
    from PySide6.QtWidgets import QLabel

    from offloader.gui.preset_editor import PresetEditor

    editor = PresetEditor(Preset(name="p"))
    headings = [w.text() for w in editor.findChildren(QLabel)
                if w.property("role") == "heading"]
    assert headings == ["Preset", "Copying", "Reports"]

    # Regrouping a form is exactly the change that silently drops a field.
    for name in ("_name", "_color", "_destinations", "_algorithm",
                 "_verification", "_thumbnails", "_naming", "_excludes",
                 "_logo", "_footer", "_preserve", "_skip", "_paranoid"):
        assert getattr(editor, name).parent() is not None, f"{name} is orphaned"


# ---------------------------------------------------------------- throughput

def _running_item(**kwargs):
    from offloader.gui.worker import QueueItem

    item = QueueItem(identifier=1, source=Path("card"), name="job",
                     preset=Preset(name="p"), **kwargs)
    item.state = JobState.RUNNING
    return item


def test_rate_is_windowed_not_a_lifetime_average(monkeypatch):
    """A slow first minute must not read as a slow job forever. The regression
    this pins: a card scan plus early probe stalls dragged the lifetime average
    to 3.5 MB/s while clips were demonstrably flying past."""
    from offloader.gui import worker as worker_mod

    clock = {"now": 1000.0}
    monkeypatch.setattr(worker_mod.time, "monotonic", lambda: clock["now"])

    item = _running_item()
    item.started_at = clock["now"]

    # Ten dead seconds of scanning, then a steady 100 MB/s.
    clock["now"] += 10.0
    for _ in range(10):
        clock["now"] += 1.0
        item.bytes_done += 100_000_000
        item.record_progress(item.bytes_done)

    lifetime = item.bytes_done / item.elapsed          # 50 MB/s â€” the old lie
    windowed = item.rate_bytes_per_sec
    assert windowed == pytest.approx(100_000_000, rel=0.05)
    assert windowed > 1.8 * lifetime


def test_rate_decays_during_a_stall_instead_of_freezing(monkeypatch):
    from offloader.gui import worker as worker_mod

    clock = {"now": 0.0}
    monkeypatch.setattr(worker_mod.time, "monotonic", lambda: clock["now"])

    item = _running_item()
    for _ in range(5):
        clock["now"] += 1.0
        item.bytes_done += 100_000_000
        item.record_progress(item.bytes_done)
    flowing = item.rate_bytes_per_sec

    clock["now"] += 3.0                                # stall: no new bytes
    assert item.rate_bytes_per_sec < flowing
    clock["now"] += 10.0                               # window fully drained
    assert item.rate_bytes_per_sec == 0.0
    assert item.eta_seconds is None


def test_rate_survives_a_counter_reset_between_stages(monkeypatch):
    """Copy and verify each count job bytes from zero; a delta computed across
    that boundary would be negative garbage."""
    from offloader.gui import worker as worker_mod

    clock = {"now": 0.0}
    monkeypatch.setattr(worker_mod.time, "monotonic", lambda: clock["now"])

    item = _running_item()
    for _ in range(3):
        clock["now"] += 1.0
        item.bytes_done += 100_000_000
        item.record_progress(item.bytes_done)

    item.bytes_done = 0                                # verify stage begins
    item.record_progress(0)
    for _ in range(2):
        clock["now"] += 1.0
        item.bytes_done += 50_000_000
        item.record_progress(item.bytes_done)
    assert item.rate_bytes_per_sec == pytest.approx(50_000_000, rel=0.05)

