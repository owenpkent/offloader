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
    """A reader that fails its first N reads, then works.

    Stands in for what `open` returns, so it has to carry the parts of a binary
    file the engine actually uses — `seek` and `close` as well as `read`, since
    recovering a bad chunk reopens the source and seeks back to it.
    """

    def __init__(self, handle, failures: dict, limit: int):
        self._handle = handle
        self._failures = failures
        self._limit = limit

    def read(self, size=-1):
        if self._failures["n"] < self._limit:
            self._failures["n"] += 1
            raise _os_error(errno.EIO, winerror=1117)
        return self._handle.read(size)

    def seek(self, offset, whence=0):
        return self._handle.seek(offset, whence)

    def close(self):
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._handle.close()


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

        def seek(self, offset, whence=0):
            return self._handle.seek(offset, whence)

        def close(self):
            self._handle.close()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._handle.close()

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

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._handle.close()

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


# --------------------------------------------------------- chunk-level retry


class _BadSector:
    """A reader that fails every read starting at one offset, `times` times.

    Records the offset of every read attempted, across reopens, which is what
    lets a test tell a chunk-level retry from a restart of the whole file: a
    restart reads offset 0 again, a chunk-level retry does not.
    """

    def __init__(self, handle, log: list, failures: dict, offset: int, times: int):
        self._handle = handle
        self._log = log
        self._failures = failures
        self._offset = offset
        self._times = times

    def read(self, size=-1):
        at = self._handle.tell()
        self._log.append(at)
        if at == self._offset and self._failures["n"] < self._times:
            self._failures["n"] += 1
            raise _os_error(errno.EIO, winerror=1117)
        return self._handle.read(size)

    def seek(self, offset, whence=0):
        return self._handle.seek(offset, whence)

    def close(self):
        self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._handle.close()


def _patch_bad_sector(monkeypatch, card: Path, log: list, failures: dict,
                      offset: int, times: int) -> None:
    real_open = builtins.open

    def flaky_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        try:
            inside = Path(path).resolve().is_relative_to(card.resolve())
        except (OSError, ValueError):
            inside = False
        if inside and "r" in str(mode) and "b" in str(mode):
            return _BadSector(handle, log, failures, offset, times)
        return handle

    monkeypatch.setattr(builtins, "open", flaky_open)


def _chunked_card(tmp_path: Path, chunks: int) -> tuple[Path, bytes]:
    card = tmp_path / "card"
    card.mkdir()
    payload = bytes(range(256)) * (engine.CHUNK_SIZE * chunks // 256)
    (card / "A001_C001.mov").write_bytes(payload)
    return card, payload


def test_a_bad_sector_is_recovered_without_re_reading_the_file(
    tmp_path: Path, monkeypatch
):
    """The point of retrying per chunk. Restarting a 79 GB clip to recover a
    few bytes near the end is most of an hour; re-reading the chunk is a
    moment."""
    monkeypatch.setattr(engine, "CHUNK_SIZE", 4096)
    card, payload = _chunked_card(tmp_path, 3)
    log: list[int] = []
    _patch_bad_sector(monkeypatch, card, log, {"n": 0}, offset=4096, times=1)

    job = engine.run(card, _options(tmp_path))
    monkeypatch.undo()

    assert job.final_status == "Verified"
    assert (tmp_path / "dest" / "A001_C001.mov").read_bytes() == payload
    assert log.count(0) == 1, f"the file was restarted: {log}"


def test_a_recovered_chunk_is_reported_with_where_it_was(tmp_path: Path,
                                                         monkeypatch):
    monkeypatch.setattr(engine, "CHUNK_SIZE", 4096)
    card, _payload = _chunked_card(tmp_path, 3)
    _patch_bad_sector(monkeypatch, card, [], {"n": 0}, offset=8192, times=1)

    job = engine.run(card, _options(tmp_path))
    monkeypatch.undo()

    assert any("byte 8192" in w and "may be failing" in w for w in job.warnings), \
        job.warnings


def test_a_sector_that_never_reads_does_not_restart_the_whole_file(
    tmp_path: Path, monkeypatch
):
    """Once the chunk has had every attempt the policy allows, running the same
    attempts again from byte zero only repeats them against the same fault."""
    monkeypatch.setattr(engine, "CHUNK_SIZE", 4096)
    card, _payload = _chunked_card(tmp_path, 3)
    log: list[int] = []
    _patch_bad_sector(monkeypatch, card, log, {"n": 0}, offset=4096, times=99)

    job = engine.run(card, _options(tmp_path))
    monkeypatch.undo()

    assert job.final_status == "Failed"
    assert log.count(0) == 1, f"the file was restarted: {log}"
    assert log.count(4096) == 3, f"the chunk got {log.count(4096)} attempts: {log}"


def test_the_failure_still_names_the_offset_that_could_not_be_read(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(engine, "CHUNK_SIZE", 4096)
    card, _payload = _chunked_card(tmp_path, 3)
    _patch_bad_sector(monkeypatch, card, [], {"n": 0}, offset=4096, times=99)

    job = engine.run(card, _options(tmp_path))
    monkeypatch.undo()

    assert "offset 4096" in job.notes, job.notes


def test_writes_are_still_retried_at_the_whole_file(tmp_path: Path, monkeypatch):
    """A read that fails produced nothing, so it can be resumed. A write that
    fails part-way leaves the destination at a length the copy loop does not
    know, so it starts over."""
    monkeypatch.setattr(engine, "CHUNK_SIZE", 4096)
    card, payload = _chunked_card(tmp_path, 2)
    real_open = builtins.open
    state = {"failed": False}

    class FailingWrite:
        def __init__(self, handle):
            self._handle = handle

        def write(self, data):
            if not state["failed"]:
                state["failed"] = True
                raise _os_error(errno.EIO, winerror=1117)
            return self._handle.write(data)

        def flush(self):
            return self._handle.flush()

        def fileno(self):
            return self._handle.fileno()

        def close(self):
            self._handle.close()

    def flaky_open(path, mode="r", *args, **kwargs):
        handle = real_open(path, mode, *args, **kwargs)
        if "w" in str(mode) and "b" in str(mode):
            return FailingWrite(handle)
        return handle

    monkeypatch.setattr(builtins, "open", flaky_open)
    job = engine.run(card, _options(tmp_path))
    monkeypatch.undo()

    assert job.final_status == "Verified"
    assert (tmp_path / "dest" / "A001_C001.mov").read_bytes() == payload
