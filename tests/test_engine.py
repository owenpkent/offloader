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
    which is precisely what full verification exists to catch.

    The injection point is the in-flight partial, because a file only takes its
    final name once it has been proven.
    """
    real_hash_file = engine.hash_file
    victim = (tmp_path / "dest" / "Clips"
              / ("A001_C001.mov" + engine.PARTIAL_SUFFIX))

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


# ------------------------------------------------------- overlapped read-ahead


def test_read_ahead_thread_does_not_leak(source_tree: Path, tmp_path: Path):
    import threading

    before = threading.active_count()
    engine.run(source_tree, _options(tmp_path))
    assert threading.active_count() == before


def test_large_file_copies_byte_identically(tmp_path: Path):
    """Spans many chunks, so the queue actually cycles."""
    import os as _os

    source = tmp_path / "card"
    source.mkdir()
    payload = _os.urandom(engine.CHUNK_SIZE * 3 + 12345)
    (source / "big.bin").write_bytes(payload)

    job = engine.run(source, _options(tmp_path))
    copied = (tmp_path / "dest" / "big.bin").read_bytes()

    assert copied == payload
    assert job.files[0].checksum == hashers.hash_file(source / "big.bin", "xxh3-64")
    assert job.final_status == "Verified"


def test_a_write_failure_surfaces_and_does_not_hang(tmp_path: Path, monkeypatch):
    """If the consumer dies the reader must observe it and exit, rather than
    parking forever on a queue nobody is draining."""
    import builtins
    import threading
    import time

    source = tmp_path / "big.bin"
    source.write_bytes(b"\0" * (engine.CHUNK_SIZE * 5))
    real_open = builtins.open

    class ExplodingHandle:
        def __init__(self) -> None:
            self.writes = 0

        def write(self, data):
            self.writes += 1
            if self.writes > 1:
                raise OSError("destination full")

        def flush(self):
            pass

        def close(self):
            pass

    def fake_open(path, mode="r", *args, **kwargs):
        if "w" in str(mode):
            return ExplodingHandle()
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)

    before = threading.active_count()
    started = time.monotonic()
    with pytest.raises(OSError, match="destination full"):
        engine._copy_fanout(source, [tmp_path / "out.bin"], "xxh3-64", lambda n: None)
    elapsed = time.monotonic() - started

    assert elapsed < 10, "reader thread was not released promptly"
    assert threading.active_count() == before


def test_read_ahead_is_bounded(tmp_path: Path):
    """Memory must stay at READ_AHEAD x CHUNK_SIZE however fast the source is."""
    assert 1 <= engine.READ_AHEAD <= 8
    assert engine.READ_AHEAD * engine.CHUNK_SIZE <= 64 << 20


def test_sentinel_is_delivered_even_when_the_queue_is_full(tmp_path: Path,
                                                           monkeypatch):
    """Regression: the reader's end-of-file sentinel used to be posted with
    put_nowait, so if the queue happened to be full at EOF it was dropped and
    the consumer blocked on get() forever. A slow consumer keeps the queue full
    and reproduces it deterministically."""
    import threading
    import time

    monkeypatch.setattr(engine, "READ_AHEAD", 1)
    source = tmp_path / "big.bin"
    source.write_bytes(b"\0" * (engine.CHUNK_SIZE * 3))

    def slow(_n: int) -> None:
        time.sleep(0.3)          # guarantees the reader outruns the writer

    result: dict[str, object] = {}

    def run() -> None:
        result["digest"] = engine._copy_fanout(
            source, [tmp_path / "out.bin"], "xxh3-64", slow)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=30)

    assert not worker.is_alive(), "copy deadlocked waiting for the sentinel"
    assert (tmp_path / "out.bin").stat().st_size == engine.CHUNK_SIZE * 3
    assert result["digest"][0] == hashers.hash_file(source, "xxh3-64")


# --------------------------------------------------------------- proxy order


@pytest.fixture()
def proxy_card(tmp_path: Path) -> Path:
    """A Blackmagic-shaped card: originals at the root, proxies in Proxy/."""
    root = tmp_path / "A006"
    (root / "Proxy").mkdir(parents=True)
    for index in (1, 2, 3):
        (root / f"C{index:03d}.braw").write_bytes(b"original" * 200 * index)
        (root / "Proxy" / f"C{index:03d}.mp4").write_bytes(b"proxy" * index)
    return root


def _transferred(source: Path, tmp_path: Path, **overrides) -> list[str]:
    """The order files actually moved in, from the progress stream."""
    moved: list[str] = []

    def progress(event: engine.ProgressEvent) -> None:
        if not moved or moved[-1] != event.file_name:
            moved.append(event.file_name)

    engine.run(source, _options(tmp_path, **overrides), progress)
    return moved


def test_is_proxy_matches_directories_not_filenames(tmp_path: Path):
    assert engine.is_proxy(tmp_path / "Proxy" / "C001.mp4", tmp_path)
    assert engine.is_proxy(tmp_path / "a" / "proxies" / "C001.mp4", tmp_path)
    # A clip that happens to be called "proxy" is still a clip.
    assert not engine.is_proxy(tmp_path / "proxy", tmp_path)
    assert not engine.is_proxy(tmp_path / "C001.braw", tmp_path)
    # Outside the root there is no relative path to inspect.
    assert not engine.is_proxy(Path("/elsewhere/Proxy/C001.mp4"), tmp_path)


def test_order_for_transfer_hoists_proxies(proxy_card: Path):
    files = engine.scan(proxy_card)
    order = engine.order_for_transfer(files, proxy_card)

    assert [p.name for p in order[:3]] == ["C001.mp4", "C002.mp4", "C003.mp4"]
    assert [p.name for p in order[3:]] == ["C001.braw", "C002.braw", "C003.braw"]
    assert sorted(order) == sorted(files), "reordering must not add or drop files"


def test_order_for_transfer_is_stable_within_each_group(proxy_card: Path):
    files = engine.scan(proxy_card)
    order = engine.order_for_transfer(files, proxy_card)

    for group in (True, False):
        assert ([p for p in order if engine.is_proxy(p, proxy_card) is group]
                == [p for p in files if engine.is_proxy(p, proxy_card) is group])


def test_order_for_transfer_leaves_a_card_without_proxies_alone(source_tree: Path):
    files = engine.scan(source_tree)
    assert engine.order_for_transfer(files, source_tree) == files


def test_order_for_transfer_off_is_scan_order(proxy_card: Path):
    files = engine.scan(proxy_card)
    assert engine.order_for_transfer(files, proxy_card, proxies_first=False) == files


def test_offload_moves_proxies_first_by_default(proxy_card: Path, tmp_path: Path):
    moved = _transferred(proxy_card, tmp_path)
    assert moved == ["C001.mp4", "C002.mp4", "C003.mp4",
                     "C001.braw", "C002.braw", "C003.braw"]


def test_offload_honours_originals_first(proxy_card: Path, tmp_path: Path):
    moved = _transferred(proxy_card, tmp_path, proxies_first=False)
    assert moved == ["C001.braw", "C002.braw", "C003.braw",
                     "C001.mp4", "C002.mp4", "C003.mp4"]


def test_report_reads_in_tree_order_whichever_way_it_ran(proxy_card: Path,
                                                         tmp_path: Path):
    """Transfer order is an I/O decision; the paperwork must not notice it."""
    first = engine.run(proxy_card, _options(tmp_path / "a"))
    plain = engine.run(proxy_card, _options(tmp_path / "b", proxies_first=False))

    rows = [f.source.relative_to(proxy_card).as_posix() for f in first.files]
    assert rows == [f.source.relative_to(proxy_card).as_posix() for f in plain.files]
    assert rows == ["C001.braw", "C002.braw", "C003.braw",
                    "Proxy/C001.mp4", "Proxy/C002.mp4", "Proxy/C003.mp4"]
    assert [f.checksum for f in first.files] == [f.checksum for f in plain.files]


def test_both_orders_deliver_the_same_tree(proxy_card: Path, tmp_path: Path):
    engine.run(proxy_card, _options(tmp_path / "a"))
    engine.run(proxy_card, _options(tmp_path / "b", proxies_first=False))

    def tree(root: Path) -> dict[str, bytes]:
        return {p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*") if p.is_file()}

    assert tree(tmp_path / "a" / "dest") == tree(tmp_path / "b" / "dest")


def test_cancelled_job_still_reports_in_tree_order(proxy_card: Path, tmp_path: Path):
    """The sort back must not trip over a partially populated job."""
    control = engine.JobControl()

    def progress(event: engine.ProgressEvent) -> None:
        if event.file_index >= 2:
            control.cancel()

    job = engine.run(proxy_card, _options(tmp_path), progress, control)

    assert job.cancelled
    rows = [f.source.relative_to(proxy_card).as_posix() for f in job.files]
    assert rows == sorted(rows, key=lambda r: ("Proxy/" in r, r))
