"""Retrying transient read failures.

A marginal card or reader usually fails intermittently long before it fails for
good. Retrying buys the take back; retrying the *wrong* errors buys nothing and
hides the fault, so most of these tests are about the discrimination rather than
the retrying.
"""

from __future__ import annotations

import builtins
import errno
from pathlib import Path

import pytest

from offloader import engine, retry
from offloader.models import VerificationMode


def _os_error(code: int, *, winerror: int | None = None) -> OSError:
    exc = OSError(code, "simulated")
    if winerror is not None:
        exc.winerror = winerror
    return exc


# ------------------------------------------------------------- what to retry


@pytest.mark.parametrize("code", [errno.EIO, errno.EBUSY, errno.EAGAIN,
                                  errno.ETIMEDOUT, errno.ENODEV])
def test_transient_errno_is_retried(code: int):
    assert retry.is_transient(_os_error(code))


@pytest.mark.parametrize("code", [errno.ENOENT, errno.ENOSPC, errno.EROFS,
                                  errno.EISDIR, errno.ENAMETOOLONG])
def test_permanent_errno_is_not_retried(code: int):
    """Retrying a missing file or a full disk wastes time and buries the real
    problem in a delay."""
    assert not retry.is_transient(_os_error(code))


@pytest.mark.parametrize("winerror,expected", [
    (21, True),      # device not ready
    (23, True),      # CRC — the marginal-media signature
    (32, True),      # sharing violation, usually antivirus
    (1117, True),    # I/O device error
    (5, False),      # access denied
    (112, False),    # disk full
])
def test_windows_error_codes(winerror: int, expected: bool):
    assert retry.is_transient(_os_error(errno.EIO, winerror=winerror)) is expected


def test_non_os_errors_are_never_retried():
    assert not retry.is_transient(ValueError("nope"))
    assert not retry.is_transient(KeyboardInterrupt())


# ------------------------------------------------------------- the retry loop


def test_a_transient_failure_is_retried_and_succeeds():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _os_error(errno.EIO)
        return "ok"

    result, used = retry.call(flaky, retry.RetryPolicy(attempts=3, delay=0),
                              sleep=lambda _s: None)
    assert result == "ok"
    assert used == 3


def test_a_permanent_failure_raises_on_the_first_attempt():
    attempts = {"n": 0}

    def missing():
        attempts["n"] += 1
        raise _os_error(errno.ENOENT)

    with pytest.raises(OSError):
        retry.call(missing, retry.RetryPolicy(attempts=5, delay=0),
                   sleep=lambda _s: None)
    assert attempts["n"] == 1, "a permanent error must not be retried"


def test_exhausting_the_attempts_raises_the_last_error():
    def always():
        raise _os_error(errno.EIO)

    with pytest.raises(OSError) as caught:
        retry.call(always, retry.RetryPolicy(attempts=3, delay=0),
                   sleep=lambda _s: None)
    assert caught.value.errno == errno.EIO


def test_before_retry_runs_between_attempts():
    """The caller clears partial state here; a retry restarts the read."""
    order: list[str] = []
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        order.append(f"try{attempts['n']}")
        if attempts["n"] < 2:
            raise _os_error(errno.EIO)
        return None

    retry.call(flaky, retry.RetryPolicy(attempts=2, delay=0),
               before_retry=lambda: order.append("cleanup"),
               sleep=lambda _s: None)
    assert order == ["try1", "cleanup", "try2"]


def test_delay_backs_off():
    policy = retry.RetryPolicy(attempts=4, delay=2.0, backoff=2.0)
    assert policy.wait_before(1) == 0.0
    assert policy.wait_before(2) == pytest.approx(2.0)
    assert policy.wait_before(3) == pytest.approx(4.0)
    assert policy.wait_before(4) == pytest.approx(8.0)


def test_a_single_attempt_policy_disables_retrying():
    assert not retry.NO_RETRY.enabled
    attempts = {"n": 0}

    def failing():
        attempts["n"] += 1
        raise _os_error(errno.EIO)

    with pytest.raises(OSError):
        retry.call(failing, retry.NO_RETRY, sleep=lambda _s: None)
    assert attempts["n"] == 1


def test_slept_durations_are_reported_to_the_caller():
    seen: list[float] = []
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _os_error(errno.EIO)
        return None

    retry.call(flaky, retry.RetryPolicy(attempts=3, delay=1.0, backoff=2.0),
               on_retry=lambda attempt, exc, pause: seen.append(pause),
               sleep=lambda _s: None)
    assert seen == [pytest.approx(1.0), pytest.approx(2.0)]


# ------------------------------------------------------------- in the engine


def _options(tmp_path: Path, **overrides) -> engine.OffloadOptions:
    defaults = dict(
        destinations=[tmp_path / "dest"],
        algorithm="xxh3-64",
        verification=VerificationMode.FULL,
        thumbnail_count=0,
        extra_probe=False,
        retry=retry.RetryPolicy(attempts=3, delay=0),
    )
    defaults.update(overrides)
    return engine.OffloadOptions(**defaults)


class _FlakyReader:
    """A reader that fails the first N read calls, then works."""

    def __init__(self, handle, failures: dict, limit: int):
        self._handle = handle
        self._failures = failures
        self._limit = limit

    def read(self, size=-1):
        if self._failures["n"] < self._limit:
            self._failures["n"] += 1
            raise _os_error(errno.EIO, winerror=1117)
        return self._handle.read(size)

    def seek(self, pos, whence=0):
        return self._handle.seek(pos, whence)

    def close(self):
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _patch_source_reads(monkeypatch, card: Path, failures: dict, limit: int):
    real_open = builtins.open

    def flaky_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        try:
            inside = Path(path).resolve().is_relative_to(card.resolve())
        except (OSError, ValueError):
            inside = False
        if inside and "r" in str(mode) and "b" in str(mode):
            return _FlakyReader(handle, failures, limit)
        return handle

    monkeypatch.setattr(builtins, "open", flaky_open)


def test_a_flaky_card_read_recovers(tmp_path: Path, monkeypatch):
    card = tmp_path / "card"
    card.mkdir()
    payload = b"IRREPLACEABLE " * 4000
    (card / "A001_C001.mov").write_bytes(payload)

    _patch_source_reads(monkeypatch, card, {"n": 0}, limit=2)
    job = engine.run(card, _options(tmp_path))
    monkeypatch.undo()

    assert job.final_status == "Verified"
    assert (tmp_path / "dest" / "A001_C001.mov").read_bytes() == payload


def test_a_recovered_file_is_still_reported(tmp_path: Path, monkeypatch):
    """A card that needs retries today is a card to stop using, so a silent
    recovery would be the wrong outcome."""
    card = tmp_path / "card"
    card.mkdir()
    (card / "A001_C001.mov").write_bytes(b"x" * 5000)

    _patch_source_reads(monkeypatch, card, {"n": 0}, limit=1)
    job = engine.run(card, _options(tmp_path))
    monkeypatch.undo()

    assert job.final_status == "Verified"
    assert any("attempt 2" in w and "may be failing" in w for w in job.warnings)


def test_retries_leave_no_partial_behind(tmp_path: Path, monkeypatch):
    card = tmp_path / "card"
    card.mkdir()
    (card / "A001_C001.mov").write_bytes(b"x" * 5000)

    _patch_source_reads(monkeypatch, card, {"n": 0}, limit=2)
    engine.run(card, _options(tmp_path))
    monkeypatch.undo()

    assert list((tmp_path / "dest").rglob(f"*{engine.PARTIAL_SUFFIX}")) == []


def test_progress_is_not_double_counted_across_retries(tmp_path: Path,
                                                       monkeypatch):
    """A failed attempt already reported the bytes it read; counting them twice
    would push the job past 100%."""
    card = tmp_path / "card"
    card.mkdir()
    (card / "A001_C001.mov").write_bytes(b"x" * (engine.CHUNK_SIZE * 2))

    real_open = builtins.open
    state = {"attempt": 0}

    class HalfwayFailure:
        def __init__(self, handle):
            self._handle = handle
            self._chunks = 0

        def read(self, size=-1):
            self._chunks += 1
            if state["attempt"] == 0 and self._chunks == 2:
                state["attempt"] = 1
                raise _os_error(errno.EIO, winerror=1117)
            return self._handle.read(size)

        def seek(self, pos, whence=0):
            return self._handle.seek(pos, whence)

        def close(self):
            self._handle.close()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def flaky_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        try:
            inside = Path(path).resolve().is_relative_to(card.resolve())
        except (OSError, ValueError):
            inside = False
        if inside and "r" in str(mode) and "b" in str(mode):
            return HalfwayFailure(handle)
        return handle

    monkeypatch.setattr(builtins, "open", flaky_open)
    events: list[engine.ProgressEvent] = []
    job = engine.run(card, _options(tmp_path), events.append)
    monkeypatch.undo()

    assert job.final_status == "Verified"
    assert all(e.job_bytes_done <= e.job_bytes_total for e in events), \
        "progress exceeded the job total after a retry"


def test_a_permanent_error_fails_without_burning_retries(tmp_path: Path,
                                                         monkeypatch):
    card = tmp_path / "card"
    card.mkdir()
    (card / "A001_C001.mov").write_bytes(b"x" * 1000)

    real_open = builtins.open
    attempts = {"n": 0}

    class GoneReader:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            attempts["n"] += 1
            raise _os_error(errno.ENOENT)

        def close(self):
            self._handle.close()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def gone_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        try:
            inside = Path(path).resolve().is_relative_to(card.resolve())
        except (OSError, ValueError):
            inside = False
        if inside and "r" in str(mode) and "b" in str(mode):
            return GoneReader(handle)
        return handle

    monkeypatch.setattr(builtins, "open", gone_open)
    job = engine.run(card, _options(tmp_path))
    monkeypatch.undo()

    assert job.final_status == "Failed"
    assert attempts["n"] == 1


# ------------------------------------------------------- chunk-level retry


def _patch_chunk_failure(monkeypatch, card: Path, state: dict):
    """Wrap source reads so the first read at `state['fail_at']` fails once,
    recording every open, seek and successful read position."""
    real_open = builtins.open

    class ChunkFlaky:
        def __init__(self, handle):
            self._handle = handle
            state["opens"] += 1

        def read(self, size=-1):
            pos = self._handle.tell()
            if pos == state["fail_at"] and state["failures"] < state["limit"]:
                state["failures"] += 1
                raise _os_error(errno.EIO, winerror=23)   # CRC error
            state["reads"].append(pos)
            return self._handle.read(size)

        def seek(self, pos, whence=0):
            state["seeks"].append(pos)
            return self._handle.seek(pos, whence)

        def close(self):
            self._handle.close()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def flaky_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        try:
            inside = Path(path).resolve().is_relative_to(card.resolve())
        except (OSError, ValueError):
            inside = False
        if inside and "r" in str(mode) and "b" in str(mode):
            return ChunkFlaky(handle)
        return handle

    monkeypatch.setattr(builtins, "open", flaky_open)


def test_a_mid_file_failure_resumes_at_the_failed_chunk(tmp_path: Path,
                                                        monkeypatch):
    """One bad sector must cost a re-read of one chunk, not of the whole clip:
    the reader reopens, seeks back to the last delivered chunk boundary and
    carries on, so the chunks already hashed are never read twice."""
    card = tmp_path / "card"
    card.mkdir()
    payload = (b"A" * engine.CHUNK_SIZE + b"B" * engine.CHUNK_SIZE + b"C" * 1000)
    (card / "A001_C001.mov").write_bytes(payload)

    state = {"fail_at": engine.CHUNK_SIZE, "failures": 0, "limit": 1,
             "opens": 0, "reads": [], "seeks": []}
    _patch_chunk_failure(monkeypatch, card, state)
    job = engine.run(card, _options(tmp_path))
    monkeypatch.undo()

    assert job.final_status == "Verified"
    assert (tmp_path / "dest" / "A001_C001.mov").read_bytes() == payload
    assert state["opens"] == 2, "expected one reopen, not a whole-file restart"
    assert state["seeks"] == [engine.CHUNK_SIZE], \
        "the retry must resume at the failed chunk boundary"
    assert state["reads"].count(0) == 1, "the first chunk was re-read"


def test_a_chunk_recovery_is_still_reported(tmp_path: Path, monkeypatch):
    """Recovering cheaply does not make the card healthy; the warning that a
    read needed a second attempt must survive the chunk-level path."""
    card = tmp_path / "card"
    card.mkdir()
    (card / "A001_C001.mov").write_bytes(b"x" * (engine.CHUNK_SIZE + 500))

    state = {"fail_at": engine.CHUNK_SIZE, "failures": 0, "limit": 1,
             "opens": 0, "reads": [], "seeks": []}
    _patch_chunk_failure(monkeypatch, card, state)
    job = engine.run(card, _options(tmp_path))
    monkeypatch.undo()

    assert job.final_status == "Verified"
    assert any("recovered on retry" in w and "may be failing" in w
               for w in job.warnings)


def test_exhausted_chunk_retries_fall_back_to_a_file_restart(tmp_path: Path,
                                                             monkeypatch):
    """A chunk that never reads good exhausts its per-chunk attempts; the
    whole-file retry then restarts the file as before, and the file fails once
    that is exhausted too. Attempts stay bounded by attempts x attempts."""
    card = tmp_path / "card"
    card.mkdir()
    (card / "A001_C001.mov").write_bytes(b"x" * 1000)

    state = {"fail_at": 0, "failures": 0, "limit": 10 ** 9,
             "opens": 0, "reads": [], "seeks": []}
    _patch_chunk_failure(monkeypatch, card, state)
    job = engine.run(card, _options(
        tmp_path, retry=retry.RetryPolicy(attempts=2, delay=0)))
    monkeypatch.undo()

    assert job.final_status == "Failed"
    assert state["failures"] == 4, \
        "2 chunk attempts per file attempt, 2 file attempts"
    assert list((tmp_path / "dest").rglob(f"*{engine.PARTIAL_SUFFIX}")) == []


def test_retry_is_configurable_from_a_preset(tmp_path: Path):
    from offloader.presets import Preset

    preset = Preset(name="p", destinations=[tmp_path / "d"],
                    retry_attempts=7, retry_wait=0.5)
    options = preset.to_options()
    assert options.retry.attempts == 7
    assert options.retry.delay == pytest.approx(0.5)

    restored = Preset.from_dict(preset.to_dict())
    assert restored.retry_attempts == 7
    assert restored.retry_wait == pytest.approx(0.5)
