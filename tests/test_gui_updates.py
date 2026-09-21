"""The desktop app's update sequence.

The behaviour that needs protecting is the refusal. Offloader's installer will
not replace a running transfer and never force-kills one, so the app must
decline an update rather than queue it, force it, or close itself while a copy
is in flight. Getting that wrong would not look like a bug in testing: it would
look like an update that worked, on a machine that happened to be idle.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtCore import QDeadlineTimer, QEventLoop  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from offloader.gui.updates import PROGRESS_STEP, UpdateController, refusal  # noqa: E402
from offloader.gui.worker import JobState  # noqa: E402
from offloader.update import Release, UpdateError  # noqa: E402

RELEASE = Release(version="0.9.0", asset_name="Offloader-Setup-0.9.0.exe",
                  download_url="https://github.com/owenpkent/offloader/x.exe",
                  notes="Fixes a verification bug.")


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def _pump(app, predicate, timeout_ms: int = 10_000) -> bool:
    deadline = QDeadlineTimer(timeout_ms)
    while not predicate():
        if deadline.hasExpired():
            return False
        app.processEvents(QEventLoop.AllEvents, 20)
    return True


# --------------------------------------------------------------- the refusal


@pytest.mark.parametrize("state", [JobState.RUNNING, JobState.PAUSED])
def test_an_active_job_refuses_the_update(state):
    """A paused job counts. It is a partially written destination waiting to
    continue, and replacing the application under it is not a way to find out
    what happens."""
    reason = refusal([state])
    assert reason is not None
    assert "finish or cancel" in reason.lower()


@pytest.mark.parametrize("states", [
    [],
    [JobState.QUEUED],
    [JobState.DONE, JobState.FAILED, JobState.CANCELLED],
    [JobState.QUEUED, JobState.DONE],
])
def test_an_idle_queue_allows_the_update(states):
    """A queued job has not started writing anything, so it is not a reason to
    decline: the installer replaces the app and the job runs afterwards."""
    assert refusal(states) is None


def test_one_active_job_among_many_still_refuses():
    assert refusal([JobState.DONE, JobState.RUNNING, JobState.QUEUED]) is not None


# ------------------------------------------------------------- checking


def test_a_newer_release_is_reported(qapp):
    seen: list[object] = []
    controller = UpdateController(check=lambda _installed: RELEASE)
    controller.found.connect(seen.append)

    assert controller.check_now()
    assert _pump(qapp, lambda: bool(seen))
    assert seen[0].version == "0.9.0"
    assert controller.release is RELEASE


def test_being_up_to_date_is_reported_separately(qapp):
    """The window says nothing on an automatic check that finds nothing, so
    the two outcomes cannot share a signal."""
    calls: list[int] = []
    controller = UpdateController(check=lambda _installed: None)
    controller.upToDate.connect(lambda: calls.append(1))

    controller.check_now()
    assert _pump(qapp, lambda: bool(calls))
    assert controller.release is None


def test_a_second_check_is_not_started_while_one_runs(qapp):
    import threading

    release = threading.Event()

    def slow(_installed):
        release.wait(5)
        return None

    controller = UpdateController(check=slow)
    assert controller.check_now()
    assert _pump(qapp, lambda: controller.busy)
    assert controller.check_now() is False
    release.set()
    assert _pump(qapp, lambda: not controller.busy)


def test_a_visible_release_can_always_be_prepared(qapp, tmp_path):
    """The ordering the controller has to keep. The check publishes its result
    and clears its own busy flag, and if it did those in the other order there
    would be a window where a release is visible and `prepare` still refuses:
    an update that reports itself and then quietly does nothing."""
    controller = UpdateController(check=lambda _installed: RELEASE,
                                  download=lambda *_a, **_k: (tmp_path / "x", ""),
                                  verify=lambda *_a, **_k: {})
    controller.check_now()
    assert _pump(qapp, lambda: controller.release is not None)

    assert not controller.busy, "a release was visible while still busy"
    assert controller.prepare(tmp_path) is not False


def test_a_check_that_raises_does_not_escape_the_thread(qapp):
    """`update.check` already swallows failures, but an injected or future
    implementation must not be able to take the app down from a pool thread."""
    def explode(_installed):
        raise OSError("no network")

    controller = UpdateController(check=explode)
    controller.check_now()
    assert _pump(qapp, lambda: not controller.busy)


# -------------------------------------------------------------- preparing


def test_preparing_downloads_verifies_and_reports_ready(qapp, tmp_path):
    verified: list[tuple] = []
    ready: list[tuple] = []

    def download(release, directory, progress=None):
        path = Path(directory) / release.asset_name
        path.write_bytes(b"MZ")
        if progress is not None:
            progress(2, 2)
        return path, "d" * 64

    def verify(path, release, **kwargs):
        verified.append((path, release, kwargs))
        return {}

    controller = UpdateController(check=lambda _i: RELEASE,
                                  download=download, verify=verify)
    controller.ready.connect(lambda rel, digest: ready.append((rel, digest)))
    controller.check_now()
    assert _pump(qapp, lambda: controller.release is not None)

    assert controller.prepare(tmp_path)
    assert _pump(qapp, lambda: bool(ready))
    assert ready[0][1] == "d" * 64
    # The digest compared is the one taken while streaming, on both sides.
    assert verified[0][2]["expected_digest"] == "d" * 64
    assert verified[0][2]["actual_digest"] == "d" * 64
    assert controller.installer is not None


def test_nothing_is_prepared_before_a_release_is_found(qapp, tmp_path):
    controller = UpdateController(check=lambda _i: None)
    assert controller.prepare(tmp_path) is False


def test_a_failed_verification_is_reported_and_nothing_is_ready(qapp, tmp_path):
    """The case that matters most: a download that cannot be trusted must not
    leave an installer the window could go on to run."""
    def download(release, directory, progress=None):
        path = Path(directory) / release.asset_name
        path.write_bytes(b"MZ")
        return path, "d" * 64

    def verify(_path, _release, **_kwargs):
        raise UpdateError("the installer was signed by a different certificate")

    failures: list[str] = []
    controller = UpdateController(check=lambda _i: RELEASE,
                                  download=download, verify=verify)
    controller.failed.connect(failures.append)
    controller.check_now()
    assert _pump(qapp, lambda: controller.release is not None)
    controller.prepare(tmp_path)

    assert _pump(qapp, lambda: bool(failures))
    assert "different certificate" in failures[0]
    assert controller.installer is None


def test_progress_is_throttled_but_finishes(qapp, tmp_path):
    """One signal per block would emit thousands of times for a 200 MB
    download; stopping short of the end would leave the bar at 99%."""
    total = PROGRESS_STEP * 4

    def download(release, directory, progress=None):
        path = Path(directory) / release.asset_name
        path.write_bytes(b"MZ")
        step = PROGRESS_STEP // 8
        for done in range(step, total + 1, step):
            progress(done, total)
        return path, "d" * 64

    updates: list[tuple[int, int]] = []
    controller = UpdateController(check=lambda _i: RELEASE, download=download,
                                  verify=lambda *_a, **_k: {})
    controller.progress.connect(lambda done, tot: updates.append((done, tot)))
    controller.check_now()
    assert _pump(qapp, lambda: controller.release is not None)
    controller.prepare(tmp_path)
    assert _pump(qapp, lambda: updates and updates[-1][0] == total)

    assert len(updates) < 32, f"too many progress signals: {len(updates)}"
    assert updates[-1] == (total, total)


# ---------------------------------------------------------------- installing


def test_installing_without_a_verified_installer_refuses(qapp):
    controller = UpdateController()
    with pytest.raises(UpdateError, match="no verified installer"):
        controller.install()


def test_installing_runs_the_verified_file(qapp, tmp_path):
    started: list[Path] = []

    def download(release, directory, progress=None):
        path = Path(directory) / release.asset_name
        path.write_bytes(b"MZ")
        return path, "d" * 64

    controller = UpdateController(check=lambda _i: RELEASE, download=download,
                                  verify=lambda *_a, **_k: {},
                                  apply=started.append)
    controller.check_now()
    assert _pump(qapp, lambda: controller.release is not None)
    controller.prepare(tmp_path)
    assert _pump(qapp, lambda: controller.installer is not None)

    controller.install()
    assert started == [controller.installer]
