"""Data-loss protection.

Every test here corresponds to a way this tool could destroy footage that has no
other copy. The two marked REGRESSION were real, reproduced against the shipped
engine, and are the reason the rest exist.

The standard being defended: an operator reformats a card because this tool said
"Verified". Anything that can make that verdict wrong, or that can damage data
while producing it, belongs here.
"""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest

from offloader import engine, integrity
from offloader.models import FileStatus, VerificationMode

PAYLOAD = b"IRREPLACEABLE FOOTAGE " * 5000


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


def _card(tmp_path: Path, name: str = "A001_C001.mov") -> Path:
    root = tmp_path / "card"
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(PAYLOAD)
    return root


# --------------------------------------------------------------- the source


def test_refuses_to_offload_a_card_onto_itself(tmp_path: Path):
    """REGRESSION. Opening a target with "wb" truncates it. When the target was
    a source file, the original was destroyed before a single byte had been
    read — and the checksum of the resulting empty file was recorded as the
    clip's checksum."""
    card = _card(tmp_path)

    with pytest.raises(engine.UnsafeDestination, match="source itself"):
        engine.run(card, _options(tmp_path, destinations=[card],
                                  preserve_structure=False))

    assert (card / "A001_C001.mov").read_bytes() == PAYLOAD


def test_refuses_a_destination_inside_the_source(tmp_path: Path):
    card = _card(tmp_path)
    with pytest.raises(engine.UnsafeDestination, match="inside the source"):
        engine.run(card, _options(tmp_path, destinations=[card / "backup"]))
    assert (card / "A001_C001.mov").read_bytes() == PAYLOAD


def test_refuses_two_destinations_that_are_the_same_directory(tmp_path: Path):
    card = _card(tmp_path)
    with pytest.raises(engine.UnsafeDestination, match="same"):
        engine.run(card, _options(
            tmp_path, destinations=[tmp_path / "dest", tmp_path / "sub" / ".." / "dest"]))


def test_copy_fanout_refuses_to_write_over_its_own_source(tmp_path: Path):
    """Belt and braces beneath the destination check, since this is the exact
    operation that loses the data."""
    card = _card(tmp_path)
    source = card / "A001_C001.mov"
    with pytest.raises(engine.UnsafeDestination, match="it is the source"):
        engine._copy_fanout(source, [source], "xxh3-64", lambda n: None)
    assert source.read_bytes() == PAYLOAD


def test_the_source_is_never_opened_for_writing(tmp_path: Path, monkeypatch):
    """Nothing in the pipeline may open a file under the source root in any
    mode that can modify it."""
    card = _card(tmp_path)
    real_open = builtins.open
    violations: list[str] = []

    def watching_open(path, mode="r", *args, **kwargs):
        text = str(mode)
        if any(flag in text for flag in ("w", "a", "+", "x")):
            try:
                if Path(path).resolve().is_relative_to(card.resolve()):
                    violations.append(f"{path} opened {mode!r}")
            except (OSError, ValueError):
                pass
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", watching_open)
    engine.run(card, _options(tmp_path))
    assert violations == []


# ---------------------------------------------------------- the destination


def test_a_failed_copy_leaves_the_previous_good_copy_alone(tmp_path: Path,
                                                           monkeypatch):
    """REGRESSION. The destination was opened with "wb" up front, so a copy
    that failed afterwards had already truncated the good archive copy it was
    meant to replace."""
    card = _card(tmp_path)
    archive = tmp_path / "dest"
    archive.mkdir()
    previous = archive / "A001_C001.mov"
    previous.write_bytes(b"PREVIOUS GOOD OFFLOAD " * 3000)
    expected = previous.read_bytes()

    real_open = builtins.open

    class FailingRead:
        def __init__(self, handle):
            self._handle = handle

        def read(self, _size=-1):
            raise OSError("The device is not ready")

        def close(self):
            self._handle.close()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def flaky_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        # Scoped to the card. Patching every binary read is too broad: on macOS
        # `sysinfo.collect()` reaches `platform.mac_ver()`, which reads a system
        # plist, and the failure surfaced there instead of in the copy.
        if "r" in str(mode) and "b" in str(mode):
            try:
                if Path(path).resolve().is_relative_to(card.resolve()):
                    return FailingRead(handle)
            except (OSError, ValueError):
                pass
        return handle

    monkeypatch.setattr(builtins, "open", flaky_open)
    job = engine.run(card, _options(tmp_path))
    monkeypatch.undo()

    assert job.final_status == "Failed"
    assert previous.read_bytes() == expected


def test_a_failed_verification_leaves_the_previous_good_copy_alone(
    tmp_path: Path, monkeypatch
):
    card = _card(tmp_path)
    archive = tmp_path / "dest"
    archive.mkdir()
    previous = archive / "A001_C001.mov"
    previous.write_bytes(b"PREVIOUS GOOD OFFLOAD " * 3000)
    expected = previous.read_bytes()

    monkeypatch.setattr(engine, "hash_file", lambda *a, **k: "0" * 16)
    job = engine.run(card, _options(tmp_path))

    assert job.final_status == "Failed"
    assert previous.read_bytes() == expected


def test_no_partial_files_survive_a_failure(tmp_path: Path, monkeypatch):
    card = _card(tmp_path)
    monkeypatch.setattr(engine, "hash_file", lambda *a, **k: "0" * 16)
    engine.run(card, _options(tmp_path))

    leftovers = list((tmp_path / "dest").rglob(f"*{engine.PARTIAL_SUFFIX}"))
    assert leftovers == []


def test_no_partial_files_survive_a_cancel(tmp_path: Path):
    card = tmp_path / "card"
    card.mkdir()
    for index in range(8):
        (card / f"clip{index:02d}.mov").write_bytes(b"\0" * 400_000)

    control = engine.JobControl()
    seen: set[str] = set()

    def progress(event: engine.ProgressEvent) -> None:
        seen.add(event.file_name)
        if len(seen) == 3:
            control.cancel()

    engine.run(card, _options(tmp_path), progress, control)
    assert list((tmp_path / "dest").rglob(f"*{engine.PARTIAL_SUFFIX}")) == []


def test_an_interrupted_copy_never_leaves_a_plausible_filename(tmp_path: Path):
    """A partial that looks like finished media is the most dangerous artefact
    an offload tool can produce: it survives the eye, and it survives a
    size-only check on the next run.

    The invariant is not "the file is absent" — a cancel can arrive after the
    last byte was already read, and a file that genuinely completed is fine to
    keep. It is that anything bearing the real name is *whole*.
    """
    card = tmp_path / "card"
    card.mkdir()
    payload = b"\0" * (engine.CHUNK_SIZE * 6)
    (card / "big.mov").write_bytes(payload)

    control = engine.JobControl()

    def progress(event: engine.ProgressEvent) -> None:
        if event.job_bytes_done > engine.CHUNK_SIZE:
            control.cancel()

    engine.run(card, _options(tmp_path), progress, control)

    landed = tmp_path / "dest" / "big.mov"
    if landed.exists():
        assert landed.read_bytes() == payload, "a truncated file took the real name"
    assert list((tmp_path / "dest").rglob(f"*{engine.PARTIAL_SUFFIX}")) == []


def test_a_completed_file_is_byte_identical_and_verified(tmp_path: Path):
    card = _card(tmp_path)
    job = engine.run(card, _options(tmp_path))

    copied = tmp_path / "dest" / "A001_C001.mov"
    assert copied.read_bytes() == PAYLOAD
    assert job.files[0].status is FileStatus.VERIFIED
    assert job.final_status == "Verified"


# ------------------------------------------------------------- the verdict


def test_verification_evicts_the_page_cache_before_reading_back(tmp_path: Path,
                                                                monkeypatch):
    """Reading a file back straight after writing it usually reads it out of
    the page cache, which compares memory with memory. The read-back must be
    preceded by an eviction."""
    card = _card(tmp_path)
    evicted: list[Path] = []
    monkeypatch.setattr(engine.integrity, "evict_from_cache",
                        lambda path: evicted.append(Path(path)) or True)

    engine.run(card, _options(tmp_path))
    assert evicted, "full verification did not evict before reading back"
    assert all(p.name.endswith(engine.PARTIAL_SUFFIX) for p in evicted)


def test_source_only_mode_does_not_claim_a_disk_read(tmp_path: Path, monkeypatch):
    card = _card(tmp_path)
    evicted: list[Path] = []
    monkeypatch.setattr(engine.integrity, "evict_from_cache",
                        lambda path: evicted.append(Path(path)) or True)

    engine.run(card, _options(tmp_path, verification=VerificationMode.SOURCE_ONLY))
    assert evicted == []


def test_a_failed_eviction_is_reported_not_hidden(tmp_path: Path, monkeypatch):
    """If the cache could not be dropped, the verification may have been served
    from memory. Say so rather than quietly claim a stronger guarantee."""
    card = _card(tmp_path)
    monkeypatch.setattr(engine.integrity, "evict_from_cache", lambda path: False)

    job = engine.run(card, _options(tmp_path))
    assert any("page cache" in warning for warning in job.warnings)


def test_empty_files_are_flagged(tmp_path: Path):
    """An empty clip hashes and verifies perfectly. It is still a lost take."""
    card = tmp_path / "card"
    card.mkdir()
    (card / "A001_C001.mov").write_bytes(b"")
    (card / "A001_C002.mov").write_bytes(PAYLOAD)

    job = engine.run(card, _options(tmp_path))
    assert any("A001_C001.mov" in warning and "empty" in warning
               for warning in job.warnings)
    # Other warnings may be present (a platform without a cache-eviction call
    # says so once); none of them should be about the non-empty clip.
    assert not any("A001_C002.mov" in warning for warning in job.warnings)


def test_eviction_is_best_effort_and_never_raises(tmp_path: Path):
    target = tmp_path / "x.bin"
    target.write_bytes(b"data")
    assert integrity.evict_from_cache(target) in (True, False)
    # A path that does not exist must not blow up the offload.
    assert integrity.evict_from_cache(tmp_path / "missing.bin") is False


def test_eviction_does_not_damage_the_file(tmp_path: Path):
    target = tmp_path / "x.bin"
    target.write_bytes(PAYLOAD)
    integrity.evict_from_cache(target)
    assert target.read_bytes() == PAYLOAD
