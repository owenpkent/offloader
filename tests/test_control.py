"""Pause, resume and cancel — the behaviour the GUI's transport controls rely on."""

from __future__ import annotations

import itertools
import threading
import time
from pathlib import Path

import pytest

from offloader import engine
from offloader.models import FileStatus, VerificationMode


def _every_checkpoint_reads():
    """A clock that has always elapsed, so `sync()` never skips a read.

    `FileControl.poll` has a 50 ms floor, so a fixture of a few small files can
    finish between two reads of the control file and never see an instruction
    written midway. That is a race in the test, not behaviour worth asserting:
    these tests are about what happens once the word is read. Driving the rate
    limiter instead of racing it is what makes them mean the same thing on a
    fast Linux runner as on a slow Windows one.
    """
    ticks = itertools.count(0.0, 3600.0)
    return lambda: next(ticks)


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
    control = engine.FileControl(path, poll=0.02, clock=_every_checkpoint_reads())
    control.claim()

    # Written from the progress callback rather than from this thread after the
    # worker starts: ten 400 KB files can be finished before the write lands,
    # and what is under test is the pause, not that race.
    seen: set[str] = set()
    asked = threading.Event()

    def progress(event: engine.ProgressEvent) -> None:
        seen.add(event.file_name)
        # Once only: several events fire per file, and rewriting `pause` after
        # the resume below would put the job straight back to sleep.
        if len(seen) == 3 and not asked.is_set():
            asked.set()
            path.write_text("pause\n", encoding="utf-8")

    result: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: result.update(job=engine.run(source, _options(tmp_path),
                                                    progress, control)),
        daemon=True,
    )
    worker.start()

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
    control = engine.FileControl(path, poll=0.0, clock=_every_checkpoint_reads())
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
    control = engine.FileControl(path, poll=0.02, clock=_every_checkpoint_reads())
    control.claim()
    seen: set[str] = set()
    asked = threading.Event()

    def progress(event: engine.ProgressEvent) -> None:
        seen.add(event.file_name)
        if len(seen) == 3 and not asked.is_set():
            asked.set()
            path.write_text("pause\n", encoding="utf-8")

    result: dict[str, object] = {}
    worker = threading.Thread(
        target=lambda: result.update(job=engine.run(source, _options(tmp_path),
                                                    progress, control)),
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + 5
    while not control.paused and time.monotonic() < deadline:
        time.sleep(0.02)
    assert control.paused

    path.write_text("cancel\n", encoding="utf-8")
    worker.join(timeout=20)
    assert not worker.is_alive()
    assert result["job"].cancelled


@pytest.mark.parametrize("word", ["run", "pause", "cancel"])
def test_a_valid_word_with_anything_after_it_is_no_opinion(tmp_path: Path,
                                                           word: str):
    """REGRESSION. Only the first token was compared, so `cancel pending upload`
    left behind by an editor or a sync read as a cancellation."""
    path = tmp_path / "job.control"
    control = engine.FileControl(path, poll=0.0)

    path.write_text(f"{word} pending upload\n", encoding="utf-8")
    assert control.read() is None


def test_invalid_utf8_in_the_control_file_is_no_opinion(tmp_path: Path):
    """REGRESSION. `UnicodeDecodeError` is not an `OSError`, so it escaped the
    handler and aborted the checkpoint instead of being read as damage."""
    path = tmp_path / "job.control"
    control = engine.FileControl(path, poll=0.0)

    path.write_bytes(b"\xff\xfe")
    assert control.read() is None


def test_a_damaged_control_file_leaves_a_paused_job_paused(tmp_path: Path):
    path = tmp_path / "job.control"
    control = engine.FileControl(path, poll=0.0)
    path.write_text("pause\n", encoding="utf-8")
    control.sync(force=True)
    assert control.paused

    path.write_bytes(b"\xff\xfe")
    control.sync(force=True)
    assert control.paused, "damaged content must not change the state"
    assert not control.cancelled

    path.write_text("cancel upload now\n", encoding="utf-8")
    control.sync(force=True)
    assert control.paused
    assert not control.cancelled, "a trailing token must not cancel a job"


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


# ------------------------------------------------ an unsafe control-file path
#
# `--control-file` is user input, and `claim()` writes to it before the engine
# has validated anything else. Each of these used to truncate the named file
# to the five bytes of `run\n`, and the job then copied and "verified" it.

_CLIP = bytes(range(256)) * 64          # 16,384 bytes of recognisable content


def _card(tmp_path: Path) -> Path:
    card = tmp_path / "card"
    card.mkdir()
    (card / "clip.mov").write_bytes(_CLIP)
    return card


def _offload_with_control(card: Path, dest: Path, control: Path) -> int:
    from offloader import cli

    return cli.main([
        "offload", "--source", str(card), "--dest", str(dest),
        "--control-file", str(control), "--no-probe", "--report", "csv",
        "--quiet",
    ])


def _nothing_copied(dest: Path) -> None:
    assert not list(dest.rglob("*.mov"))
    assert not list(dest.rglob("*" + engine.PARTIAL_SUFFIX))


def test_control_file_that_is_a_source_file_is_refused(tmp_path: Path, capsys):
    card = _card(tmp_path)
    dest = tmp_path / "dest"

    code = _offload_with_control(card, dest, card / "clip.mov")

    assert code != 0
    assert (card / "clip.mov").read_bytes() == _CLIP
    _nothing_copied(dest)
    assert "control file" in capsys.readouterr().err


def test_control_file_that_is_a_good_destination_file_is_refused(tmp_path: Path):
    card = _card(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    good = dest / "clip.mov"
    good.write_bytes(b"an earlier good copy" * 100)

    code = _offload_with_control(card, dest, good)

    assert code != 0
    assert good.read_bytes() == b"an earlier good copy" * 100
    assert not list(dest.rglob("*" + engine.PARTIAL_SUFFIX))


def test_control_path_inside_the_source_is_refused_before_it_is_created(
    tmp_path: Path,
):
    card = _card(tmp_path)
    dest = tmp_path / "dest"
    path = card / "job.control"

    assert _offload_with_control(card, dest, path) != 0

    assert not path.exists()
    _nothing_copied(dest)


def test_control_path_that_is_a_symlink_to_a_source_file_is_refused(
    tmp_path: Path,
):
    card = _card(tmp_path)
    dest = tmp_path / "dest"
    link = tmp_path / "job.control"
    try:
        link.symlink_to(card / "clip.mov")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not permitted on this machine")

    assert _offload_with_control(card, dest, link) != 0

    assert (card / "clip.mov").read_bytes() == _CLIP
    _nothing_copied(dest)


def test_library_run_refuses_a_control_file_inside_the_source(tmp_path: Path):
    card = _card(tmp_path)
    control = engine.FileControl(card / "clip.mov")

    with pytest.raises(engine.UnsafeControlFile):
        engine.run(card, _options(tmp_path), control=control)

    assert (card / "clip.mov").read_bytes() == _CLIP
    assert not (tmp_path / "dest").exists()


def test_library_run_refuses_a_control_file_that_is_a_promotion_target(
    tmp_path: Path,
):
    card = _card(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "clip.mov").write_bytes(b"x" * 10)
    control = engine.FileControl(dest / "clip.mov")

    with pytest.raises(engine.UnsafeControlFile):
        engine.run(card, _options(tmp_path), control=control)

    assert (dest / "clip.mov").read_bytes() == b"x" * 10


def test_control_file_inside_a_destination_root_is_still_allowed(tmp_path: Path):
    card = _card(tmp_path)
    dest = tmp_path / "dest"

    assert _offload_with_control(card, dest, dest / "job.control") == 0

    assert (dest / "clip.mov").read_bytes() == _CLIP


def test_claim_refuses_content_that_is_not_a_control_word(tmp_path: Path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"precious" * 100)

    with pytest.raises(engine.UnsafeControlFile):
        engine.FileControl(path).claim()

    assert path.read_bytes() == b"precious" * 100


def test_claim_refuses_a_directory(tmp_path: Path):
    with pytest.raises(engine.UnsafeControlFile):
        engine.FileControl(tmp_path).claim()


@pytest.mark.parametrize("before", [b"", b"pause\n", b"  CANCEL \r\n"])
def test_claim_overwrites_an_empty_or_stale_control_file(
    tmp_path: Path, before: bytes,
):
    path = tmp_path / "job.control"
    path.write_bytes(before)

    engine.FileControl(path).claim()

    assert path.read_text(encoding="utf-8").strip() == "run"


def test_claim_creates_a_missing_file_and_its_parent(tmp_path: Path):
    path = tmp_path / "new" / "job.control"

    engine.FileControl(path).claim()

    assert path.read_text(encoding="utf-8").strip() == "run"
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


def test_cancel_during_the_existing_file_check_records_the_missing_copy(
    tmp_path: Path, monkeypatch,
):
    """A cancel while a differing file already at destination two is being
    compared must not leave the file reading as Verified on the strength of
    destination one, and must not touch the file that was already there."""
    card = _one_file_card(tmp_path)
    first, second = tmp_path / "d1", tmp_path / "d2"
    second.mkdir()
    existing = second / "clip.mov"
    original = b"\x00" * 300_000           # same size, different content
    existing.write_bytes(original)
    control = engine.JobControl()
    real_hash_file = engine.hash_file

    def hash_file(path, *args, **kwargs):
        if Path(path) == existing:
            control.cancel()
        return real_hash_file(path, *args, **kwargs)

    monkeypatch.setattr(engine, "hash_file", hash_file)

    job = engine.run(card, _options(tmp_path, destinations=[first, second]),
                     control=control)

    assert job.cancelled
    assert job.final_status != "Verified"
    (entry,) = job.files
    assert [d.status for d in entry.destinations] == [
        FileStatus.VERIFIED, FileStatus.CANCELLED]
    assert entry.destinations[1].error == "cancelled before verification"
    assert entry.status is FileStatus.CANCELLED
    assert (first / "clip.mov").exists()
    assert existing.read_bytes() == original
    assert not list(tmp_path.rglob("*.offloader-partial"))
