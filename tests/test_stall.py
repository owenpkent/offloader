"""A source that stops delivering bytes without reporting an error.

Retrying needs something to react to. A hung network handle gives it nothing —
it simply stops returning, which is why a dropped SMB session over a VPN reads
as a frozen progress bar rather than a failure. The watchdog exists to tell that
apart from a link that is merely slow, and most of these tests are about that
distinction: an 8 MiB chunk at 350 KB/s legitimately takes half a minute, so a
detector timing whole chunks would cry stall on a working copy.
"""

from __future__ import annotations

import builtins
import threading
import time
from pathlib import Path

from offloader import engine, retry
from offloader.models import Profile, VerificationMode


def _options(tmp_path: Path, **overrides) -> engine.OffloadOptions:
    defaults = dict(
        destinations=[tmp_path / "dest"],
        algorithm="xxh3-64",
        verification=VerificationMode.NONE,
        profile=Profile.DATA,
        retry=retry.RetryPolicy(attempts=1),
        stall_after=0.3,
    )
    defaults.update(overrides)
    return engine.OffloadOptions(**defaults)


class _PacedReader:
    """A reader that waits a given time before each successive sub-read.

    Carries the parts of a binary file the engine uses — `read`, `seek`,
    `close` — because recovering a chunk reopens the source and seeks back.
    """

    def __init__(self, handle, pauses: list[float], entered: list | None = None):
        self._handle = handle
        self._pauses = list(pauses)
        self._entered = entered

    def read(self, size=-1):
        if self._entered is not None:
            self._entered.append(time.monotonic())
        if self._pauses:
            time.sleep(self._pauses.pop(0))
        return self._handle.read(size)

    def seek(self, offset, whence=0):
        return self._handle.seek(offset, whence)

    def close(self):
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._handle.close()


def _pace_source_reads(monkeypatch, card: Path, pauses: list[float],
                       entered: list | None = None) -> None:
    real_open = builtins.open

    def paced_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        try:
            inside = Path(path).resolve().is_relative_to(card.resolve())
        except (OSError, ValueError):
            inside = False
        if inside and "r" in str(mode) and "b" in str(mode):
            return _PacedReader(handle, pauses, entered)
        return handle

    monkeypatch.setattr(builtins, "open", paced_open)


def _run(tmp_path: Path, monkeypatch, payload: bytes, pauses: list[float],
         **overrides):
    """Offload one file whose reads are paced, collecting the stages emitted."""
    # The consumer only looks between chunks, so the poll is the resolution at
    # which a stall is noticed. Shortened here to keep the test quick.
    monkeypatch.setattr(engine, "STALL_POLL", 0.02)
    card = tmp_path / "card"
    card.mkdir()
    (card / "A001_C001.mov").write_bytes(payload)

    stages: list[str] = []
    _pace_source_reads(monkeypatch, card, pauses)
    job = engine.run(card, _options(tmp_path, **overrides),
                     progress=lambda event: stages.append(event.stage))
    monkeypatch.undo()
    return job, stages


def test_a_hung_read_is_reported_as_a_stall(tmp_path: Path, monkeypatch):
    """The failure mode that has no error to retry: bytes simply stop."""
    job, stages = _run(tmp_path, monkeypatch, b"x" * 2048, pauses=[1.0])

    assert "stalled" in stages
    assert any("stalled" in w and "no bytes arriving" in w
               for w in job.warnings)


def test_a_stall_still_produces_a_good_copy(tmp_path: Path, monkeypatch):
    """Reporting a stall is all it does. The bytes are not in question — the
    link they arrived over is."""
    payload = b"IRREPLACEABLE " * 1000
    job, _ = _run(tmp_path, monkeypatch, payload, pauses=[0.8],
                  verification=VerificationMode.FULL)

    assert job.final_status == "Verified"
    assert (tmp_path / "dest" / "A001_C001.mov").read_bytes() == payload


def test_a_slow_link_is_not_a_stall(tmp_path: Path, monkeypatch):
    """The false positive the sub-reads exist to prevent.

    Each 1 MiB sub-read lands inside the threshold, but the 8 MiB chunk they
    build takes several times longer than it. Timing whole chunks would report
    this working copy as stalled; timing bytes does not.
    """
    payload = b"y" * (engine.SUBCHUNK_SIZE * 4)
    job, stages = _run(tmp_path, monkeypatch, payload,
                       pauses=[0.12] * 5, stall_after=0.3)

    assert "stalled" not in stages
    assert not any("stalled" in w for w in job.warnings)
    assert (tmp_path / "dest" / "A001_C001.mov").read_bytes() == payload


def test_zero_disables_the_watchdog(tmp_path: Path, monkeypatch):
    job, stages = _run(tmp_path, monkeypatch, b"x" * 2048, pauses=[0.8],
                       stall_after=0.0)

    assert "stalled" not in stages
    assert not any("stalled" in w for w in job.warnings)


def test_a_cancel_is_noticed_while_a_read_hangs(tmp_path: Path, monkeypatch):
    """A hung read never reaches the reader thread's own checkpoint, so without
    the consumer's the cancel would wait on the operating system too.

    Cancelled from another thread *while* the read is sleeping — cancelling
    before the run would be caught long before any of this, and prove nothing.
    """
    monkeypatch.setattr(engine, "STALL_POLL", 0.02)
    card = tmp_path / "card"
    card.mkdir()
    (card / "A001_C001.mov").write_bytes(b"x" * 2048)

    control = engine.JobControl()
    entered: list[float] = []
    hang = 5.0
    _pace_source_reads(monkeypatch, card, [hang], entered)

    canceller = threading.Timer(0.2, control.cancel)
    canceller.start()
    started = time.monotonic()
    try:
        job = engine.run(card, _options(tmp_path), control=control)
    finally:
        canceller.cancel()
    elapsed = time.monotonic() - started
    monkeypatch.undo()

    assert entered, "the read never started, so nothing was hung to cancel"
    assert elapsed < hang / 2, (
        f"waited {elapsed:.1f}s for a cancel while a read slept {hang}s")
    assert job.cancelled
