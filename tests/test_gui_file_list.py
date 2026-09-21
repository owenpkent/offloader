"""The per-file detail pane: the two checksums, side by side.

This pane exists to show the claim the tool makes, so the tests are mostly
about the cases where the claim is *not* clean — a destination that disagrees
with the source, destinations that disagree with each other, a file that
failed. Those are the rows an operator must not misread as verified.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from offloader.gui import theme  # noqa: E402
from offloader.gui.file_list import (  # noqa: E402
    COL_DESTINATION,
    COL_MARK,
    COL_SOURCE,
    FileListPanel,
    FileModel,
    abbreviate,
)
from offloader.models import (  # noqa: E402
    Destination,
    FileEntry,
    FileStatus,
    Job,
    VerificationMode,
)

SOURCE_ROOT = Path("E:\\")
DEST_ROOT = Path("D:\\archive")


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def _entry(name: str, checksum: str | None,
           destinations: list[tuple[str | None, FileStatus]],
           size: int = 1024) -> FileEntry:
    return FileEntry(
        source=SOURCE_ROOT / name, source_root=SOURCE_ROOT, size=size,
        created=0.0, modified=0.0, checksum=checksum,
        destinations=[
            Destination(root=DEST_ROOT, path=DEST_ROOT / name,
                        status=status, checksum=value)
            for value, status in destinations
        ],
    )


def _job(*entries: FileEntry) -> Job:
    job = Job(name="A001", source_root=SOURCE_ROOT,
              destination_roots=[DEST_ROOT],
              verification=VerificationMode.FULL, hash_label="XXHash3-64")
    job.files.extend(entries)
    return job


def _display(model: FileModel, row: int, column: int):
    return model.data(model.index(row, column), Qt.DisplayRole)


def _colour(model: FileModel, row: int, column: int):
    return model.data(model.index(row, column), Qt.ForegroundRole)


# ------------------------------------------------------------- abbreviation


@pytest.mark.parametrize("value,expected", [
    ("28f7e67d9b39ea9b", "28F7:EA9B"),
    ("3f2a9c17b48e05d1", "3F2A:05D1"),
    (None, "—"),
    ("", "—"),
    ("abc", "ABC"),
])
def test_abbreviation(value, expected):
    assert abbreviate(value) == expected


def test_abbreviation_keeps_the_tail():
    """Two checksums sharing a prefix must not abbreviate to the same string —
    the whole point of these columns is telling a match from a near-miss."""
    one = abbreviate("28f7e67d9b39aaaa")
    two = abbreviate("28f7e67d9b39bbbb")
    assert one != two


# ------------------------------------------------------------------- the rows


def test_a_verified_file_shows_both_hashes_and_a_double_mark(qapp):
    checksum = "28f7e67d9b39ea9b"
    model = FileModel()
    model.set_job(_job(_entry("A.braw", checksum,
                              [(checksum, FileStatus.VERIFIED)])))

    assert _display(model, 0, COL_MARK) == "✓✓"
    assert _display(model, 0, COL_SOURCE) == "28F7:EA9B"
    assert _display(model, 0, COL_DESTINATION) == "28F7:EA9B"
    assert _colour(model, 0, COL_DESTINATION).name() == \
        theme.status_color("verified")


def test_a_destination_that_does_not_match_is_not_coloured_as_verified(qapp):
    """The pair is the evidence. A destination hash that differs from the
    source must not be painted the same green as one that agrees, even if the
    engine somehow called the file copied."""
    model = FileModel()
    model.set_job(_job(_entry("A.braw", "28f7e67d9b39ea9b",
                              [("ffffffffffffffff", FileStatus.COPIED)])))

    assert _display(model, 0, COL_SOURCE) != _display(model, 0, COL_DESTINATION)
    assert _colour(model, 0, COL_DESTINATION) is None


def test_destinations_disagreeing_with_each_other_say_mismatch(qapp):
    """Both copies came from one read, so they cannot legitimately differ.
    Showing only the first would hide it."""
    model = FileModel()
    entry = _entry("A.braw", "28f7e67d9b39ea9b",
                   [("28f7e67d9b39ea9b", FileStatus.VERIFIED),
                    ("0000000000000000", FileStatus.FAILED)])
    model.set_job(_job(entry))

    assert _display(model, 0, COL_DESTINATION) == "mismatch"
    assert _colour(model, 0, COL_DESTINATION).name() == \
        theme.status_color("failed")


def test_a_failed_file_is_marked_and_coloured_as_failed(qapp):
    model = FileModel()
    model.set_job(_job(_entry("A.braw", None, [(None, FileStatus.FAILED)])))

    assert _display(model, 0, COL_MARK) == "✕"
    assert _colour(model, 0, COL_MARK).name() == theme.status_color("failed")
    assert _display(model, 0, COL_SOURCE) == "—"


def test_the_tooltip_carries_the_full_hashes_not_the_abbreviation(qapp):
    """The abbreviation is for scanning; anyone checking a value against a
    manifest needs all of it."""
    checksum = "28f7e67d9b39ea9b"
    model = FileModel()
    model.set_job(_job(_entry("A.braw", checksum,
                              [(checksum, FileStatus.VERIFIED)])))

    tooltip = model.data(model.index(0, COL_SOURCE), Qt.ToolTipRole)
    assert checksum in tooltip
    assert str(DEST_ROOT / "A.braw") in tooltip


def test_an_empty_model_has_no_rows(qapp):
    model = FileModel()
    assert model.rowCount() == 0
    model.set_job(None)
    assert model.rowCount() == 0


# ----------------------------------------------------------------- the panel


def test_the_summary_counts_verified_files(qapp):
    good = "28f7e67d9b39ea9b"
    panel = FileListPanel()
    panel.show_job(_job(
        _entry("A.braw", good, [(good, FileStatus.VERIFIED)]),
        _entry("B.braw", good, [(good, FileStatus.VERIFIED)]),
        _entry("C.braw", None, [(None, FileStatus.FAILED)]),
    ))

    summary = panel._summary.text()
    assert "3 files" in summary
    assert "verified 2 of 3" in summary
    assert "1 failed" in summary
    assert panel.model.rowCount() == 3


def test_a_job_with_no_files_shows_the_waiting_message(qapp):
    panel = FileListPanel()
    panel.show_job(None, "Running — the file list appears once the job finishes.")
    assert "Running" in panel._empty.text()
    assert panel.model.rowCount() == 0


def test_showing_the_same_job_again_does_not_rebuild(qapp):
    """Called on every progress event of the selected job, so a rebuild here
    would reset the table dozens of times a second."""
    good = "28f7e67d9b39ea9b"
    job = _job(_entry("A.braw", good, [(good, FileStatus.VERIFIED)]))
    panel = FileListPanel()
    panel.show_job(job)

    resets = []
    panel.model.modelReset.connect(lambda: resets.append(1))
    panel.show_job(job)
    panel.show_job(job)

    assert resets == []


def test_the_waiting_message_still_updates_while_empty(qapp):
    """Queued to running is a change the operator should see, and both states
    have no job object to tell apart."""
    panel = FileListPanel()
    panel.show_job(None, "Queued — the file list appears once the job finishes.")
    panel.show_job(None, "Running — the file list appears once the job finishes.")
    assert "Running" in panel._empty.text()
