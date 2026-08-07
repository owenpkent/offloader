from __future__ import annotations

from pathlib import Path

import pytest

from offloader import engine, history
from offloader.models import VerificationMode


@pytest.fixture
def log(tmp_path: Path) -> history.History:
    return history.History(tmp_path / "history.json")


def _offload(source: Path, tmp_path: Path, name: str) -> object:
    return engine.run(source, engine.OffloadOptions(
        destinations=[tmp_path / name],
        verification=VerificationMode.SOURCE_ONLY,
        thumbnail_count=0,
        extra_probe=False,
        job_name=name,
    ))


def test_fingerprint_is_stable_and_order_independent(source_tree: Path):
    paths = engine.scan(source_tree)
    first = history.fingerprint(paths, source_tree)
    second = history.fingerprint(list(reversed(paths)), source_tree)
    assert first == second and first


def test_fingerprint_changes_when_content_size_changes(source_tree: Path):
    before = history.fingerprint(engine.scan(source_tree), source_tree)
    (source_tree / "Clips" / "A001_C001.mov").write_bytes(b"x" * 999)
    after = history.fingerprint(engine.scan(source_tree), source_tree)
    assert before != after


def test_fingerprint_changes_when_a_file_is_added(source_tree: Path):
    before = history.fingerprint(engine.scan(source_tree), source_tree)
    (source_tree / "Clips" / "A001_C003.mov").write_bytes(b"new")
    assert history.fingerprint(engine.scan(source_tree), source_tree) != before


def test_recording_then_finding_a_duplicate(source_tree: Path, tmp_path: Path,
                                            log: history.History):
    job = _offload(source_tree, tmp_path, "first")
    signature = history.fingerprint(engine.scan(source_tree), source_tree)

    assert log.find(signature) == []
    log.record(job, signature)

    matches = log.find(signature)
    assert len(matches) == 1
    assert matches[0].job_name == "first"
    assert matches[0].file_count == job.total_files
    assert "first" in matches[0].describe()


def test_a_cancelled_offload_is_not_treated_as_a_duplicate(
    source_tree: Path, tmp_path: Path, log: history.History
):
    """A run that did not finish is a reason to offload again, not to warn."""
    control = engine.JobControl()
    control.cancel()
    job = engine.run(source_tree, engine.OffloadOptions(
        destinations=[tmp_path / "d"], thumbnail_count=0, extra_probe=False,
    ), control=control)

    signature = history.fingerprint(engine.scan(source_tree), source_tree)
    log.record(job, signature)
    assert log.find(signature) == []


def test_history_persists_and_is_newest_first(source_tree: Path, tmp_path: Path):
    path = tmp_path / "history.json"
    log = history.History(path)
    signature = history.fingerprint(engine.scan(source_tree), source_tree)
    log.record(_offload(source_tree, tmp_path, "older"), signature)
    log.record(_offload(source_tree, tmp_path, "newer"), signature)

    reloaded = history.History(path)
    assert [e.job_name for e in reloaded.entries] == ["newer", "older"]
    assert list(reloaded.used_names()) == ["newer", "older"]


def test_history_is_capped(tmp_path: Path, source_tree: Path, log: history.History,
                           monkeypatch):
    monkeypatch.setattr(history, "MAX_ENTRIES", 3)
    job = _offload(source_tree, tmp_path, "job")
    for index in range(6):
        log.record(job, f"signature-{index}")
    assert len(log.entries) == 3
    assert log.entries[0].fingerprint == "signature-5"


def test_unreadable_history_is_treated_as_empty(tmp_path: Path):
    path = tmp_path / "history.json"
    path.write_text("<<<not json", encoding="utf-8")
    assert history.History(path).entries == []
