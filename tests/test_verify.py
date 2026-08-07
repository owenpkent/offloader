"""Re-verification against an MHL manifest.

This is the gate between a card and the format button, so its failure modes
matter more than its success path: it must never say "verified" about a tree it
did not actually check.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from offloader import engine, verify
from offloader.models import VerificationMode
from offloader.reports import write_mhl


@pytest.fixture
def offloaded(tmp_path: Path):
    """A real offload with a real manifest beside it."""
    card = tmp_path / "card"
    (card / "Clips").mkdir(parents=True)
    (card / "Clips" / "A001_C001.mov").write_bytes(bytes(range(256)) * 400)
    (card / "Clips" / "A001_C002.mov").write_bytes(bytes(range(128)) * 300)
    (card / "notes.txt").write_text("slate", encoding="utf-8")

    destination = tmp_path / "archive"
    job = engine.run(card, engine.OffloadOptions(
        destinations=[destination], algorithm="xxh3-64",
        verification=VerificationMode.FULL, thumbnail_count=0, extra_probe=False,
        job_name="A001"))

    manifest = write_mhl(job, destination / "A001_Reports" / "JobReport.mhl")
    return destination, manifest


def test_a_good_tree_passes(offloaded):
    _, manifest = offloaded
    report = verify.verify_manifest(manifest)

    assert report.passed
    assert report.checked == 3
    assert report.failures == []
    assert "all 3 files match" in report.summary()


def test_a_flipped_bit_is_caught(offloaded):
    """Same size, same name, same timestamp — only the content differs. This is
    what bit rot looks like, and nothing but a checksum finds it."""
    destination, manifest = offloaded
    victim = destination / "Clips" / "A001_C001.mov"
    before = victim.stat().st_size

    payload = bytearray(victim.read_bytes())
    payload[5000] ^= 0x01
    victim.write_bytes(bytes(payload))
    assert victim.stat().st_size == before

    report = verify.verify_manifest(manifest)
    assert not report.passed
    failures = report.failures
    assert len(failures) == 1
    assert failures[0].result is verify.EntryResult.MISMATCH
    assert failures[0].expected != failures[0].actual
    assert "MISMATCH" in failures[0].describe()


def test_a_missing_file_is_caught(offloaded):
    destination, manifest = offloaded
    (destination / "Clips" / "A001_C002.mov").unlink()

    report = verify.verify_manifest(manifest)
    assert not report.passed
    assert [v.result for v in report.failures] == [verify.EntryResult.MISSING]


def test_a_truncated_file_is_caught(offloaded):
    destination, manifest = offloaded
    victim = destination / "Clips" / "A001_C001.mov"
    victim.write_bytes(victim.read_bytes()[:-100])

    report = verify.verify_manifest(manifest)
    assert not report.passed
    verdict = report.failures[0]
    assert verdict.result is verify.EntryResult.MISMATCH
    assert verdict.actual_size != verdict.expected_size


def test_an_unreadable_file_is_reported_not_skipped(offloaded, monkeypatch):
    """A file that cannot be read is not a pass."""
    _, manifest = offloaded

    def refuse(path, algorithm, **kwargs):
        raise OSError("The device is not ready")

    monkeypatch.setattr(verify, "hash_file", refuse)
    report = verify.verify_manifest(manifest)

    assert not report.passed
    assert all(v.result is verify.EntryResult.UNREADABLE for v in report.failures)
    assert "not ready" in report.failures[0].describe()


def test_files_not_in_the_manifest_are_surfaced(offloaded):
    """Something arrived that the offload never recorded — worth knowing before
    the tree is treated as authoritative."""
    destination, manifest = offloaded
    (destination / "Clips" / "stowaway.mov").write_bytes(b"unexpected")

    report = verify.verify_manifest(manifest)
    assert any(p.name == "stowaway.mov" for p in report.unlisted)
    # It is a warning, not a corruption: the listed files still matched.
    assert report.passed


def test_an_empty_manifest_never_reports_success(tmp_path: Path):
    """A manifest with nothing in it must not read as 'all files verified'."""
    manifest = tmp_path / "empty.mhl"
    manifest.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><hashlist version="1.1">'
        "</hashlist>", encoding="utf-8")

    report = verify.verify_manifest(manifest)
    assert not report.passed
    assert report.checked == 0
    assert "no files" in report.summary()


def test_an_entry_without_a_checksum_is_not_a_pass(tmp_path: Path):
    manifest = tmp_path / "j.mhl"
    (tmp_path / "clip.mov").write_bytes(b"data")
    manifest.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><hashlist version="1.1">'
        "<hash><file>clip.mov</file><size>4</size></hash></hashlist>",
        encoding="utf-8")

    report = verify.verify_manifest(manifest)
    assert not report.passed
    assert report.failures[0].result is verify.EntryResult.NO_CHECKSUM


def test_verification_evicts_the_cache_by_default(offloaded, monkeypatch):
    _, manifest = offloaded
    evicted: list[Path] = []
    monkeypatch.setattr(verify, "evict_from_cache",
                        lambda p: evicted.append(Path(p)) or True)

    verify.verify_manifest(manifest)
    assert len(evicted) == 3

    evicted.clear()
    verify.verify_manifest(manifest, bypass_cache=False)
    assert evicted == []


def test_paths_are_resolved_relative_to_the_manifest(offloaded, tmp_path: Path):
    """An MHL travels with its media; moving the whole tree must not break it."""
    destination, manifest = offloaded
    moved = tmp_path / "moved-archive"
    destination.rename(moved)

    report = verify.verify_manifest(moved / "A001_Reports" / "JobReport.mhl")
    assert report.passed


def test_find_manifests_locates_every_mhl(offloaded, tmp_path: Path):
    destination, _ = offloaded
    assert len(verify.find_manifests(destination)) == 1
    assert verify.find_manifests(tmp_path / "nowhere") == []


def test_verify_tree_covers_multiple_destinations(tmp_path: Path):
    card = tmp_path / "card"
    card.mkdir()
    (card / "clip.mov").write_bytes(b"x" * 1000)

    job = engine.run(card, engine.OffloadOptions(
        destinations=[tmp_path / "d1", tmp_path / "d2"], algorithm="xxh3-64",
        verification=VerificationMode.FULL, thumbnail_count=0, extra_probe=False,
        job_name="A001"))
    write_mhl(job, tmp_path / "d1" / "A001_Reports" / "JobReport.mhl",
              destination_index=0)
    write_mhl(job, tmp_path / "d2" / "A001_Reports" / "JobReport.mhl",
              destination_index=1)

    reports = verify.verify_tree(tmp_path)
    assert len(reports) == 2
    assert all(r.passed for r in reports)


def test_a_manifest_for_the_second_copy_points_at_the_second_copy(tmp_path: Path):
    """Regression guard: an MHL written for destination 2 that listed
    destination 1's paths would verify the wrong disk."""
    card = tmp_path / "card"
    card.mkdir()
    (card / "clip.mov").write_bytes(b"x" * 1000)

    job = engine.run(card, engine.OffloadOptions(
        destinations=[tmp_path / "d1", tmp_path / "d2"], algorithm="xxh3-64",
        verification=VerificationMode.FULL, thumbnail_count=0, extra_probe=False,
        job_name="A001"))

    second = write_mhl(job, tmp_path / "d2" / "A001_Reports" / "JobReport.mhl",
                       destination_index=1)
    listed = [n.findtext("file") for n in ET.parse(second).getroot().iter("hash")]
    assert listed == ["../clip.mov"]

    # Deleting the *first* copy must not affect the second's verification.
    (tmp_path / "d1" / "clip.mov").unlink()
    assert verify.verify_manifest(second).passed
