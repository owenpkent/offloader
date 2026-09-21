"""What the queue says while a source has stopped delivering bytes.

The engine's side of this is in `test_stall.py`. This is the half the operator
actually sees, and the thing it is fixing is a *lie*: during a stall no
progress events arrive, so the throughput column went on displaying the last
rate it measured. A frozen job that reads "573.8 MB/s" is worse than one that
reads nothing, because it invites waiting.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtWidgets import QApplication  # noqa: E402

from offloader.gui.queue_view import (  # noqa: E402
    STAGE_VERBS,
    _active_summary,
    _throughput,
)
from offloader.gui.worker import JobState, QueueController, QueueItem  # noqa: E402
from offloader.models import VerificationMode  # noqa: E402
from offloader.presets import Preset  # noqa: E402

GB = 1024 ** 3


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def _preset(tmp_path: Path) -> Preset:
    return Preset(name="test", destinations=[tmp_path / "dest"],
                  algorithm="xxh3-64", verification=VerificationMode.FULL,
                  thumbnail_count=0, reports=["csv"])


def _running(tmp_path: Path, **overrides) -> QueueItem:
    values = dict(identifier=1, source=Path("E:\\"), name="A001",
                  preset=_preset(tmp_path), state=JobState.RUNNING,
                  fraction=0.5, stage="copy",
                  current_file="A001_C007.braw",
                  bytes_done=int(4 * GB), bytes_total=int(8 * GB),
                  started_at=time.monotonic() - 60)
    values.update(overrides)
    return QueueItem(**values)


# ------------------------------------------------------------- the item's clock


def test_an_item_that_is_not_stalled_has_no_stall_duration(tmp_path):
    assert _running(tmp_path).stalled_for == 0.0


def test_the_duration_is_measured_from_when_the_stall_began(tmp_path):
    item = _running(tmp_path, stage="stalled")
    item.stalled_since = time.monotonic() - 8
    assert item.stalled_for == pytest.approx(8, abs=1)


# ------------------------------------------------- the controller's transitions


def test_the_first_stalled_event_starts_the_clock(qapp, tmp_path):
    controller = QueueController()
    try:
        item = _running(tmp_path)
        controller.items = [item]
        controller._on_progress(item.identifier, 0.5, "stalled",
                                item.current_file, 1, 2)
        assert item.stalled_since is not None
    finally:
        controller.shutdown(2000)


def test_later_stalled_events_do_not_restart_the_clock(qapp, tmp_path):
    """One event arrives per poll for as long as the stall lasts. Re-stamping
    on each would report the age of the last event — about a second, forever —
    instead of how long the source has actually been silent."""
    controller = QueueController()
    try:
        item = _running(tmp_path)
        controller.items = [item]
        controller._on_progress(item.identifier, 0.5, "stalled", "f", 1, 2)
        began = item.stalled_since
        time.sleep(0.05)
        controller._on_progress(item.identifier, 0.5, "stalled", "f", 1, 2)
        assert item.stalled_since == began
    finally:
        controller.shutdown(2000)


def test_any_other_stage_clears_the_clock(qapp, tmp_path):
    """Bytes moved again, so the stall is over and the next one must be timed
    from its own beginning."""
    controller = QueueController()
    try:
        item = _running(tmp_path)
        controller.items = [item]
        controller._on_progress(item.identifier, 0.5, "stalled", "f", 1, 2)
        controller._on_progress(item.identifier, 0.6, "copy", "f", 2, 3)
        assert item.stalled_since is None
        assert item.stalled_for == 0.0
    finally:
        controller.shutdown(2000)


# ------------------------------------------------------------- what is rendered


def test_a_stalled_job_shows_no_rate_at_all(tmp_path):
    """The bug this closes: the column is populated from a trailing window of
    samples, and during a stall no new ones arrive."""
    item = _running(tmp_path, stage="stalled")
    item.stalled_since = time.monotonic() - 34
    item.record_progress(item.bytes_done)

    text = _throughput(item)
    assert "MB/s" not in text and "GB/s" not in text
    assert "ETA" not in text
    assert "34s" in text


def test_a_moving_job_still_shows_its_rate(tmp_path):
    """The other half — suppressing the rate must be specific to a stall, not
    a blanket loss of the figure."""
    item = _running(tmp_path)
    item._samples.append((time.monotonic() - 2,
                          item.bytes_done - 200 * 1024 * 1024))

    assert "/s" in _throughput(item)


def test_the_summary_line_names_the_file_it_is_waiting_on(tmp_path):
    item = _running(tmp_path, stage="stalled")
    item.stalled_since = time.monotonic() - 12

    summary = _active_summary(item)
    assert "Stalled on" in summary
    assert "A001_C007.braw" in summary


def test_the_stalled_and_retry_stages_read_as_words(tmp_path):
    """Both are stages the engine emits, and neither had a verb — they would
    have surfaced as the bare stage name."""
    assert STAGE_VERBS["stalled"] == "Stalled on"
    assert STAGE_VERBS["retry"] == "Retrying"


def test_an_unmapped_stage_still_says_something(tmp_path):
    """`reread` has no verb and should not render as an empty line."""
    item = _running(tmp_path, stage="reread")
    assert "Reread" in _active_summary(item)
