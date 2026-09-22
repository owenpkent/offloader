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
from offloader.update import FeedError, Release, UpdateError  # noqa: E402

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


# ---------------------------------------------- failure is not up to date


@pytest.mark.parametrize("failure", [
    OSError("no network"),
    FeedError("could not reach the release feed"),
    UpdateError("the release feed did not describe a release"),
])
def test_a_check_that_could_not_be_made_reports_failure(qapp, failure):
    """REGRESSION. The controller read `None` as "you are the newest release",
    and `update.check` returns `None` for a failed fetch, a TLS error and an
    unparseable feed alike. So a manual check told the user this was the newest
    release on the strength of a failed DNS lookup, and an automatic failure
    never reached the documented failure path at all."""
    def explode(_installed):
        raise failure

    failures: list[str] = []
    up_to_date: list[int] = []
    controller = UpdateController(check=explode)
    controller.failed.connect(failures.append)
    controller.upToDate.connect(lambda: up_to_date.append(1))

    controller.check_now()
    assert _pump(qapp, lambda: bool(failures))
    assert up_to_date == [], "a failed check claimed the app was up to date"
    assert controller.release is None


def test_a_genuine_up_to_date_answer_is_still_up_to_date(qapp):
    """The other half. Distinguishing failure must not turn every quiet check
    into a warning."""
    failures: list[str] = []
    up_to_date: list[int] = []
    controller = UpdateController(check=lambda _i: None)
    controller.failed.connect(failures.append)
    controller.upToDate.connect(lambda: up_to_date.append(1))

    controller.check_now()
    assert _pump(qapp, lambda: bool(up_to_date))
    assert failures == []


@pytest.mark.parametrize("payload,outcome", [
    ({"tag_name": "v0.9.0", "assets": [
        {"name": "Offloader-Setup-0.9.0.exe",
         "browser_download_url": "https://github.com/a/b/x.exe"}]}, "found"),
    ({"tag_name": "v0.1.0", "assets": []}, "up to date"),
    ("<html>a proxy login page</html>", "failed"),
    (None, "failed"),
])
def test_the_feed_check_separates_its_three_outcomes(payload, outcome):
    """At the source, where the distinction is made. `check_feed` raises for a
    feed it could not read and returns None only when the feed answered and had
    nothing newer."""
    from offloader import update

    def fetch(_url):
        return payload

    if outcome == "failed":
        with pytest.raises(FeedError):
            update.check_feed("0.5.0", fetch=fetch)
        return
    result = update.check_feed("0.5.0", fetch=fetch)
    assert (result is not None) is (outcome == "found")


def test_a_fetch_that_raises_becomes_a_feed_error():
    from offloader import update

    def fetch(_url):
        raise OSError("name or service not known")

    with pytest.raises(FeedError, match="could not reach"):
        update.check_feed("0.5.0", fetch=fetch)
    # The never-raising form is unchanged, for callers with nowhere to put it.
    assert update.check("0.5.0", fetch=fetch) is None


# ------------------------------------------------------------- cancelling


def test_cancel_stops_a_download_in_flight(qapp, tmp_path):
    """REGRESSION. The dialog's Cancel was never connected to anything. It hid
    the dialog, the download and verification carried on, and the app then
    offered to install what the user had just declined."""
    import threading
    import time

    blocked = threading.Event()
    verified: list[int] = []

    def download(release, directory, progress=None):
        path = Path(directory) / release.asset_name
        path.write_bytes(b"MZ partial")
        progress(1, 100)
        blocked.set()
        # Keeps calling back until the cancel is noticed, the way a real
        # download does: the callback is the only place it hands control over
        # often enough to be stopped.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            progress(2, 100)
            time.sleep(0.01)
        raise AssertionError("the download was never cancelled")

    controller = UpdateController(check=lambda _i: RELEASE, download=download,
                                  verify=lambda *_a, **_k: verified.append(1))
    outcomes: list[str] = []
    controller.cancelled.connect(lambda: outcomes.append("cancelled"))
    controller.ready.connect(lambda *_a: outcomes.append("ready"))
    controller.failed.connect(lambda *_a: outcomes.append("failed"))

    controller.check_now()
    assert _pump(qapp, lambda: controller.release is not None)
    assert controller.prepare(tmp_path)
    assert _pump(qapp, lambda: blocked.is_set())

    assert controller.cancel() is True
    assert _pump(qapp, lambda: bool(outcomes))

    assert outcomes == ["cancelled"], "a cancelled download still reported in"
    assert verified == [], "verification continued after the cancel"
    assert controller.installer is None
    assert not (tmp_path / RELEASE.asset_name).exists(), \
        "the incomplete download was left behind"


def test_cancel_after_the_bytes_have_arrived_still_suppresses_ready(qapp, tmp_path):
    """A download small enough to finish between two callbacks would otherwise
    sail past the cancel and offer itself for installation."""
    def download(release, directory, progress=None):
        path = Path(directory) / release.asset_name
        path.write_bytes(b"MZ")
        controller.cancel()
        return path, "d" * 64

    outcomes: list[str] = []
    controller = UpdateController(check=lambda _i: RELEASE, download=download,
                                  verify=lambda *_a, **_k: {})
    controller.cancelled.connect(lambda: outcomes.append("cancelled"))
    controller.ready.connect(lambda *_a: outcomes.append("ready"))

    controller.check_now()
    assert _pump(qapp, lambda: controller.release is not None)
    controller.prepare(tmp_path)
    assert _pump(qapp, lambda: bool(outcomes))

    assert outcomes == ["cancelled"]
    assert controller.installer is None
    assert not (tmp_path / RELEASE.asset_name).exists()


def test_cancelling_removes_only_the_download(qapp, tmp_path):
    """The directory can be one the caller owns."""
    keep = tmp_path / "somebody-elses.txt"
    keep.write_text("keep me", encoding="utf-8")

    def download(release, directory, progress=None):
        (Path(directory) / release.asset_name).write_bytes(b"MZ")
        controller.cancel()
        return Path(directory) / release.asset_name, "d" * 64

    controller = UpdateController(check=lambda _i: RELEASE, download=download,
                                  verify=lambda *_a, **_k: {})
    done: list[int] = []
    controller.cancelled.connect(lambda: done.append(1))
    controller.check_now()
    assert _pump(qapp, lambda: controller.release is not None)
    controller.prepare(tmp_path)
    assert _pump(qapp, lambda: bool(done))

    assert keep.read_text(encoding="utf-8") == "keep me"


def test_cancelling_nothing_says_so(qapp):
    assert UpdateController().cancel() is False


# ------------------------------------------------- whose result it is


def test_a_refused_duplicate_check_keeps_an_announcement(qapp):
    """REGRESSION. A manual check requested during the first 2.5 seconds was
    still running when the launch timer fired. The timer's silent intent
    overwrote it, the controller then refused the duplicate, and the manual
    result was handled as an automatic one -- so the dialog the user asked for
    never appeared."""
    import threading

    release = threading.Event()

    def slow(_installed):
        release.wait(5)
        return None

    controller = UpdateController(check=slow)
    try:
        assert controller.check_now(announce=True)
        assert _pump(qapp, lambda: controller.busy)

        assert controller.check_now(announce=False) is False
        assert controller.announce is True, "the manual request was downgraded"
    finally:
        release.set()
        assert _pump(qapp, lambda: not controller.busy)


def test_a_manual_request_behind_an_automatic_one_is_still_announced(qapp):
    """The other direction: the automatic check is already running when the
    user asks. They asked, so they get an answer."""
    import threading

    release = threading.Event()
    controller = UpdateController(check=lambda _i: release.wait(5) or None)
    try:
        assert controller.check_now(announce=False)
        assert _pump(qapp, lambda: controller.busy)
        assert controller.check_now(announce=True) is False
        assert controller.announce is True
    finally:
        release.set()
        assert _pump(qapp, lambda: not controller.busy)


def test_a_check_that_starts_sets_its_own_intent(qapp):
    """Raised while work is in flight, but not sticky: the next automatic
    check is silent again."""
    controller = UpdateController(check=lambda _i: None)
    controller.check_now(announce=True)
    assert _pump(qapp, lambda: not controller.busy)
    assert controller.announce is True

    controller.check_now(announce=False)
    assert _pump(qapp, lambda: not controller.busy)
    assert controller.announce is False


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
