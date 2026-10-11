"""Pause, resume and cancel — the behaviour the GUI's transport controls rely on."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from offloader import engine
from offloader.models import FileStatus, VerificationMode


def _options(tmp_path: Path, **overrides) -> engine.OffloadOptions:
    defaults = dict(
        destinations=[tmp_path / "dest"],
        algorithm="xxh3-64",
        verification=VerificationMode.SOURCE_ONLY,
        thumbnail_count=0,
        extra_probe=False,
    )
    defaults.update(overrides)
    return engine.OffloadOptions(**defaults)


def test_cancel_before_start_copies_nothing(source_tree: Path, tmp_path: Path):
    control = engine.JobControl()
    control.cancel()

    job = engine.run(source_tree, _options(tmp_path), control=control)

    assert job.cancelled
    assert job.final_status == "Cancelled"
    assert job.files == []
    assert "not attempted" in job.notes


def test_cancel_midway_keeps_finished_files_and_drops_the_partial(
    tmp_path: Path,
):
    source = tmp_path / "card"
    source.mkdir()
    for index in range(6):
        (source / f"clip{index:02d}.mov").write_bytes(b"\0" * 200_000)

    control = engine.JobControl()
    seen: list[str] = []

    def progress(event: engine.ProgressEvent) -> None:
        seen.append(event.file_name)
        # Cancel once a couple of files are done, mid-run.
        if len(set(seen)) == 3:
            control.cancel()

    job = engine.run(source, _options(tmp_path), progress, control)

    assert job.cancelled
    assert 0 < len(job.files) < 6
    # Everything recorded actually completed; nothing is left half-written.
    for entry in job.files:
        assert entry.status is not FileStatus.FAILED
        for destination in entry.destinations:
            assert destination.path.exists()
            assert destination.path.stat().st_size == entry.size

    written = {p.name for p in (tmp_path / "dest").iterdir() if p.is_file()}
    assert written == {entry.name for entry in job.files}


def test_pause_blocks_then_resume_completes(tmp_path: Path):
    source = tmp_path / "card"
    source.mkdir()
    for index in range(8):
        (source / f"clip{index:02d}.mov").write_bytes(b"\0" * 300_000)

    control = engine.JobControl()
    result: dict[str, object] = {}

    def run() -> None:
        result["job"] = engine.run(source, _options(tmp_path), control=control)

    control.pause()
    assert control.paused

    worker = threading.Thread(target=run, daemon=True)
    worker.start()

    # Paused before the first checkpoint, so nothing should land.
    worker.join(timeout=0.6)
    assert worker.is_alive(), "paused job should not have run to completion"
    assert "job" not in result

    control.resume()
    worker.join(timeout=30)
    assert not worker.is_alive()

    job = result["job"]
    assert not job.cancelled
    assert job.total_files == 8
    assert job.final_status == "Verified"


def test_cancel_releases_a_paused_job(tmp_path: Path):
    """A paused job must still observe a cancel, or the UI would hang on quit."""
    source = tmp_path / "card"
    source.mkdir()
    (source / "clip.mov").write_bytes(b"\0" * 100_000)

    control = engine.JobControl()
    control.pause()
    result: dict[str, object] = {}

    worker = threading.Thread(
        target=lambda: result.update(job=engine.run(source, _options(tmp_path),
                                                    control=control)),
        daemon=True,
    )
    worker.start()
    time.sleep(0.2)
    control.cancel()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert result["job"].cancelled
    assert not control.paused


def test_control_state_flags():
    control = engine.JobControl()
    assert not control.paused and not control.cancelled

    control.pause()
    assert control.paused

    control.resume()
    assert not control.paused

    control.cancel()
    assert control.cancelled


# ------------------------------------------- pause and cancel during a reread


def _one_file_card(tmp_path: Path, size: int = 300_000) -> Path:
    card = tmp_path / "card"
    card.mkdir()
    (card / "clip.mov").write_bytes(b"\x5a" * size)
    return card


def _assert_nothing_promoted(job, tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    assert job.cancelled
    assert job.final_status != "Verified"
    assert not (dest / "clip.mov").exists()
    assert not list(dest.rglob("*.offloader-partial"))


def test_cancel_during_paranoid_reread_does_not_promote(tmp_path: Path):
    """A cancel issued while the source is being reread must stop the job
    before the file is promoted, not be ignored and reported Verified."""
    card = _one_file_card(tmp_path)
    control = engine.JobControl()

    def progress(event: engine.ProgressEvent) -> None:
        if event.stage == "reread":
            control.cancel()

    job = engine.run(card, _options(tmp_path, paranoid=True), progress, control)

    _assert_nothing_promoted(job, tmp_path)


def test_pause_during_paranoid_reread_blocks_until_resume(
    tmp_path: Path, monkeypatch
):
    card = _one_file_card(tmp_path, size=100_000)
    control = engine.JobControl()
    real = engine.hash_file
    # Small chunks so the reread performs several checkpoints.
    monkeypatch.setattr(
        engine, "hash_file",
        lambda path, key, chunk_size=8 << 20, **kw: real(
            path, key, 10_000, **kw))
    paused_once: list[bool] = []

    def progress(event: engine.ProgressEvent) -> None:
        if event.stage == "reread" and not paused_once:
            paused_once.append(True)
            control.pause()
            threading.Timer(0.3, control.resume).start()

    started = time.monotonic()
    job = engine.run(card, _options(tmp_path, paranoid=True), progress, control)
    elapsed = time.monotonic() - started

    assert paused_once
    assert elapsed >= 0.25
    assert not job.cancelled
    assert job.final_status == "Verified"


def test_cancel_during_full_verification_reread_does_not_promote(
    tmp_path: Path,
):
    card = _one_file_card(tmp_path)
    control = engine.JobControl()

    def progress(event: engine.ProgressEvent) -> None:
        if event.stage == "verify":
            control.cancel()

    job = engine.run(
        card, _options(tmp_path, verification=VerificationMode.FULL),
        progress, control)

    _assert_nothing_promoted(job, tmp_path)


def test_cancel_during_the_second_destination_reread_records_the_missing_copy(
    tmp_path: Path,
):
    """A cancel while destination two is being reread must not leave the file
    reading as Verified on the strength of destination one. The missing copy
    is recorded as cancelled, so the entry's status is its weakest copy."""
    card = _one_file_card(tmp_path)
    first, second = tmp_path / "d1", tmp_path / "d2"
    control = engine.JobControl()
    verifies: list[str] = []

    def progress(event: engine.ProgressEvent) -> None:
        if event.stage == "verify":
            verifies.append(event.file_name)
            if len(verifies) == 2:
                control.cancel()

    job = engine.run(
        card, _options(tmp_path, destinations=[first, second],
                       verification=VerificationMode.FULL),
        progress, control)

    assert job.cancelled
    assert job.final_status != "Verified"
    (entry,) = job.files
    assert entry.status is FileStatus.CANCELLED
    assert [d.status for d in entry.destinations] == [
        FileStatus.VERIFIED, FileStatus.CANCELLED]
    assert (first / "clip.mov").exists()
    assert not (second / "clip.mov").exists()
    assert not list(tmp_path.rglob("*.offloader-partial"))
