from __future__ import annotations

from pathlib import Path

import pytest

from offloader import engine, hashers
from offloader.models import FileStatus, VerificationMode


def _options(tmp_path: Path, **overrides) -> engine.OffloadOptions:
    defaults = dict(
        destinations=[tmp_path / "dest"],
        algorithm="xxh3-64",
        verification=VerificationMode.FULL,
        thumbnail_count=0,
        extra_probe=False,
    )
    defaults.update(overrides)
    return engine.OffloadOptions(**defaults)


def test_scan_skips_os_junk(source_tree: Path):
    names = {p.name for p in engine.scan(source_tree)}
    assert names == {"A001_C001.mov", "A001_C002.mov", "notes.txt"}


def test_offload_copies_and_verifies(source_tree: Path, tmp_path: Path):
    job = engine.run(source_tree, _options(tmp_path))

    assert job.total_files == 3
    assert job.final_status == "Verified"
    assert all(f.status is FileStatus.VERIFIED for f in job.files)

    copied = tmp_path / "dest" / "Clips" / "A001_C001.mov"
    assert copied.exists()
    assert copied.read_bytes() == (source_tree / "Clips" / "A001_C001.mov").read_bytes()


def test_offload_preserves_structure_and_checksums(source_tree: Path, tmp_path: Path):
    job = engine.run(source_tree, _options(tmp_path))

    entry = next(f for f in job.files if f.name == "A001_C001.mov")
    expected = hashers.hash_file(source_tree / "Clips" / "A001_C001.mov", "xxh3-64")
    assert entry.checksum == expected
    assert entry.destinations[0].checksum == expected
    assert entry.relative == Path("Clips") / "A001_C001.mov"


def test_flat_mode_drops_directories(source_tree: Path, tmp_path: Path):
    engine.run(source_tree, _options(tmp_path, preserve_structure=False))
    assert (tmp_path / "dest" / "A001_C001.mov").exists()
    assert not (tmp_path / "dest" / "Clips").exists()


def test_multiple_destinations_get_identical_copies(source_tree: Path, tmp_path: Path):
    job = engine.run(
        source_tree,
        _options(tmp_path, destinations=[tmp_path / "d1", tmp_path / "d2"]),
    )
    for entry in job.files:
        assert len(entry.destinations) == 2
        digests = {d.checksum for d in entry.destinations} | {entry.checksum}
        assert len(digests) == 1
    assert (tmp_path / "d1" / "notes.txt").read_bytes() == \
           (tmp_path / "d2" / "notes.txt").read_bytes()


def test_full_verification_detects_a_corrupted_destination(
    source_tree: Path, tmp_path: Path, monkeypatch
):
    """Corrupt the destination after it is written but before it is re-read,
    which is precisely what full verification exists to catch."""
    real_hash_file = engine.hash_file
    victim = tmp_path / "dest" / "Clips" / "A001_C001.mov"

    def corrupting_hash_file(path: Path, algorithm: str, **kwargs):
        if Path(path) == victim and victim.exists():
            victim.write_bytes(b"rot" * 1000)
        return real_hash_file(path, algorithm, **kwargs)

    monkeypatch.setattr(engine, "hash_file", corrupting_hash_file)
    job = engine.run(source_tree, _options(tmp_path))

    failed = [f for f in job.files if f.status is FileStatus.FAILED]
    assert [f.name for f in failed] == ["A001_C001.mov"]
    assert job.final_status == "Failed"
    assert "mismatch" in failed[0].destinations[0].error


def test_source_only_mode_skips_the_reread(source_tree: Path, tmp_path: Path,
                                           monkeypatch):
    calls: list[Path] = []
    monkeypatch.setattr(
        engine, "hash_file",
        lambda path, algorithm, **kw: calls.append(Path(path)) or "deadbeef",
    )
    job = engine.run(source_tree, _options(tmp_path,
                                           verification=VerificationMode.SOURCE_ONLY))
    assert calls == []
    assert job.final_status == "Verified"


def test_no_verification_reports_copied(source_tree: Path, tmp_path: Path):
    job = engine.run(source_tree, _options(tmp_path, verification=VerificationMode.NONE))
    assert job.final_status == "Copied"
    assert all(f.status is FileStatus.COPIED for f in job.files)


def test_skip_existing_leaves_files_alone(source_tree: Path, tmp_path: Path):
    engine.run(source_tree, _options(tmp_path))
    job = engine.run(source_tree, _options(tmp_path, skip_existing=True))
    assert all(f.status is FileStatus.SKIPPED for f in job.files)


def test_extra_excludes_are_honoured(source_tree: Path, tmp_path: Path):
    job = engine.run(source_tree, _options(
        tmp_path, excludes=tuple(engine.DEFAULT_EXCLUDES) + ("*.txt",)))
    assert "notes.txt" not in {f.name for f in job.files}


def test_missing_source_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        engine.run(tmp_path / "nope", _options(tmp_path))


def test_destination_required(source_tree: Path, tmp_path: Path):
    with pytest.raises(ValueError, match="at least one destination"):
        engine.run(source_tree, _options(tmp_path, destinations=[]))


def test_progress_reports_reach_completion(source_tree: Path, tmp_path: Path):
    events: list[engine.ProgressEvent] = []
    engine.run(source_tree, _options(tmp_path), events.append)
    assert events
    assert events[-1].job_bytes_done == events[-1].job_bytes_total
    assert {e.stage for e in events} <= {"copy", "verify", "probe", "thumbs"}


def test_rescan_describes_a_tree_without_copying(source_tree: Path, tmp_path: Path):
    engine.run(source_tree, _options(tmp_path))
    job = engine.rescan(source_tree, [tmp_path / "dest"], _options(tmp_path))

    assert job.total_files == 3
    assert all(d.status is FileStatus.VERIFIED
               for f in job.files for d in f.destinations)


def test_rescan_flags_a_missing_destination(source_tree: Path, tmp_path: Path):
    job = engine.rescan(source_tree, [tmp_path / "never-written"], _options(tmp_path))
    assert all(d.status is FileStatus.SKIPPED
               for f in job.files for d in f.destinations)
