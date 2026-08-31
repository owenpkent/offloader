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


# --------------------------------------------------------------------------
# FileControl: pausing a CLI job from another process
# --------------------------------------------------------------------------


def test_file_control_reads_each_state(tmp_path: Path):
    path = tmp_path / "job.control"
    control = engine.FileControl(path, poll=0.0)

    assert control.read() == "run", "an absent file releases the job"
    for word in ("pause", "run", "cancel"):
        path.write_text(word + "\n", encoding="utf-8")
        assert control.read() == word
    path.write_text("  PAUSE  \n", encoding="utf-8")
    assert control.read() == "pause", "case and whitespace are not the point"


def test_a_damaged_control_file_is_no_opinion_not_a_cancel(tmp_path: Path):
    """A stray byte must never stop an offload that is hours in.

    A control file is exactly the kind of small text file a sync client or an
    editor rewrites in two steps, so a half-written read has to leave the job
    alone rather than be guessed at.
    """
    path = tmp_path / "job.control"
    control = engine.FileControl(path, poll=0.0)

    for junk in ("", "   ", "\x00\x00", "stop", "resume please", "PAUS"):
        path.write_text(junk, encoding="utf-8")
        assert control.read() is None, f"{junk!r} should be no opinion"

    # And no opinion really does leave the state alone.
    path.write_text("pause", encoding="utf-8")
    control.sync(force=True)
    assert control.paused
    path.write_text("nonsense", encoding="utf-8")
    control.sync(force=True)
    assert control.paused, "a damaged file must not release a paused job either"


def test_claim_resets_a_stale_pause_from_a_previous_job(tmp_path: Path):
    """Otherwise the next job stops dead and nobody can see why."""
    path = tmp_path / "job.control"
    path.write_text("pause\n", encoding="utf-8")

    control = engine.FileControl(path, poll=0.0)
    control.claim()

    assert path.read_text(encoding="utf-8").strip() == "run"
    control.sync(force=True)
    assert not control.paused


def test_file_control_pauses_and_resumes_a_running_offload(tmp_path: Path):
    source = tmp_path / "card"
    source.mkdir()
    for index in range(10):
        (source / f"clip{index:02d}.mov").write_bytes(b"\0" * 400_000)

    path = tmp_path / "job.control"
    control = engine.FileControl(path, poll=0.02)
    control.claim()
    result: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: result.update(job=engine.run(source, _options(tmp_path),
                                                    control=control)),
        daemon=True,
    )
    worker.start()

    path.write_text("pause\n", encoding="utf-8")
    deadline = time.monotonic() + 5
    while not control.paused and time.monotonic() < deadline:
        time.sleep(0.02)
    assert control.paused, "the job never saw the pause"

    # It must actually stop moving, not merely set a flag.
    settled = len(list((tmp_path / "dest").rglob("*.mov")))
    time.sleep(0.3)
    assert len(list((tmp_path / "dest").rglob("*.mov"))) == settled
    assert worker.is_alive()

    path.write_text("run\n", encoding="utf-8")
    worker.join(timeout=20)
    assert not worker.is_alive(), "the job never saw the resume"
    assert result["job"].total_files == 10
    assert not result["job"].cancelled


def test_deleting_the_control_file_releases_a_paused_job(tmp_path: Path):
    path = tmp_path / "job.control"
    control = engine.FileControl(path, poll=0.0)
    path.write_text("pause\n", encoding="utf-8")
    control.sync(force=True)
    assert control.paused

    path.unlink()
    control.sync(force=True)
    assert not control.paused


def test_file_control_cancels_a_running_offload(tmp_path: Path):
    source = tmp_path / "card"
    source.mkdir()
    for index in range(12):
        (source / f"clip{index:02d}.mov").write_bytes(b"\0" * 400_000)

    path = tmp_path / "job.control"
    control = engine.FileControl(path, poll=0.0)
    control.claim()

    # Written from the progress callback rather than after a sleep: a dozen
    # 400 KB files finish faster than any wall-clock guess, and what is under
    # test is the cancel, not the race.
    seen: set[str] = set()

    def progress(event: engine.ProgressEvent) -> None:
        seen.add(event.file_name)
        if len(seen) == 3:
            path.write_text("cancel\n", encoding="utf-8")

    job = engine.run(source, _options(tmp_path), progress, control)

    assert job.cancelled
    assert 0 < len(job.files) < 12
    # Whatever it did record actually landed, whole.
    for entry in job.files:
        for destination in entry.destinations:
            assert destination.path.exists()
            assert destination.path.stat().st_size == entry.size
    partials = list((tmp_path / "dest").rglob("*" + engine.PARTIAL_SUFFIX))
    assert partials == [], "a cancel must not leave a half-written file"


def test_cancel_reaches_a_job_that_is_already_paused(tmp_path: Path):
    """A paused job polls, so it can still be told to stop."""
    source = tmp_path / "card"
    source.mkdir()
    for index in range(10):
        (source / f"clip{index:02d}.mov").write_bytes(b"\0" * 400_000)

    path = tmp_path / "job.control"
    control = engine.FileControl(path, poll=0.02)
    control.claim()
    result: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: result.update(job=engine.run(source, _options(tmp_path),
                                                    control=control)),
        daemon=True,
    )
    worker.start()
    path.write_text("pause\n", encoding="utf-8")
    deadline = time.monotonic() + 5
    while not control.paused and time.monotonic() < deadline:
        time.sleep(0.02)
    assert control.paused

    path.write_text("cancel\n", encoding="utf-8")
    worker.join(timeout=20)
    assert not worker.is_alive()
    assert result["job"].cancelled


def test_state_changes_are_announced_once(tmp_path: Path):
    path = tmp_path / "job.control"
    seen: list[str] = []
    control = engine.FileControl(path, poll=0.0, on_change=seen.append)

    for word in ("pause", "pause", "pause", "run", "run"):
        path.write_text(word, encoding="utf-8")
        control.sync(force=True)
    assert seen == ["pause", "run"], "only transitions are worth reporting"


# --------------------------------------------------------------------------
# Through the command line
# --------------------------------------------------------------------------


def test_control_command_writes_each_state(tmp_path: Path, capsys):
    """Also catches the handler never being registered, which argparse hides:
    `--help` works from the parser alone, so the command looks fine until it
    is actually run."""
    from offloader import cli

    path = tmp_path / "job.control"
    for flag, word in (("--pause", "pause"), ("--resume", "run"),
                       ("--cancel", "cancel")):
        assert cli.main(["control", str(path), flag]) == 0
        assert path.read_text(encoding="utf-8").strip() == word
    capsys.readouterr()

    assert cli.main(["control", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "cancel"


def test_control_command_on_a_path_with_no_job(tmp_path: Path, capsys):
    from offloader import cli

    assert cli.main(["control", str(tmp_path / "nope.control")]) == 2
    assert "no control file" in capsys.readouterr().err


def test_control_command_writes_atomically(tmp_path: Path):
    """A job polls between chunks, so it must never read a truncated file."""
    from offloader import cli

    path = tmp_path / "job.control"
    cli.main(["control", str(path), "--pause"])
    assert not list(tmp_path.glob("*.writing")), "staging file left behind"
    assert path.read_text(encoding="utf-8").strip() == "pause"


def test_offload_cli_is_pausable_with_a_control_file(tmp_path: Path):
    """End to end: the flag wires a FileControl into the run."""
    from offloader import cli

    source = tmp_path / "card"
    source.mkdir()
    for index in range(10):
        (source / f"clip{index:02d}.mov").write_bytes(b"\0" * 400_000)
    path = tmp_path / "job.control"
    path.write_text("pause\n", encoding="utf-8")     # stale, must be reset

    result: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: result.update(code=cli.main([
            "offload", "--source", str(source), "--dest", str(tmp_path / "dest"),
            "--control-file", str(path), "--no-probe", "--report", "csv",
            "--quiet",
        ])),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=30)

    assert not worker.is_alive(), "claim() did not clear the stale pause"
    assert result["code"] == 0
    assert len(list((tmp_path / "dest").rglob("*.mov"))) == 10
