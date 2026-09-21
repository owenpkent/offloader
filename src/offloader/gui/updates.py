"""The desktop app's half of updating: check, download, verify, hand over.

`offloader.update` does the work and knows nothing about Qt. This wraps it so
the network and the signature probe happen off the GUI thread, and so the
sequence has somewhere to live that is not the window.

The sequence is not the obvious one, because Offloader's installer refuses
maintenance while a transfer is in flight and never force-kills a running copy.
That rules out the usual desktop pattern of replacing the app underneath
itself. So:

* a job in the queue means the update is declined, with a reason, rather than
  queued behind it or forced through;
* when nothing is running, the installer is launched and the app closes itself
  so the installer can proceed. The app closing is the thing that makes the
  update possible, which is why it is deliberate and announced rather than a
  side effect.

Every step is injectable. A test should be able to drive the whole sequence,
including the refusals, without a network, a certificate, or an installer.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal

from .._version import __version__
from ..update import UpdateError
from ..update import apply as apply_release
from ..update import check as check_release
from ..update import download as download_release
from ..update import verify as verify_release

#: How often an automatic check may run, in milliseconds. Long, because a
#: release appears a few times a year and the check is a network round trip
#: nobody asked for.
CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000

#: Bytes between progress signals. A 200 MB download at chunk granularity
#: would emit thousands of times and the only visible effect is a busier event
#: loop than the copy engine's.
PROGRESS_STEP = 1 << 18


class _Signals(QObject):
    """Owned by the controller, not the runnable.

    A QRunnable's Python wrapper can be collected as soon as `start()` returns,
    so signals belonging to it would be destroyed under the running thread.
    The same reason `drives._ScanTask` keeps them separate.
    """

    found = Signal(object)
    upToDate = Signal()
    progress = Signal(int, int)
    ready = Signal(object, str)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, work: Callable[[], None]) -> None:
        super().__init__()
        self._work = work

    def run(self) -> None:                      # noqa: D102 - Qt entry point
        self._work()


class UpdateController(QObject):
    """Checks for a release, and prepares one for installation.

    Signals carry versions and byte counts, never the download URL: the window
    has no use for it, and a URL that never reaches the interface cannot be
    rendered into one.
    """

    found = Signal(object)
    upToDate = Signal()
    progress = Signal(int, int)
    ready = Signal(object, str)
    failed = Signal(str)

    def __init__(self, parent: QObject | None = None, *,
                 check: Callable[..., object] | None = None,
                 download: Callable[..., tuple[Path, str]] | None = None,
                 verify: Callable[..., object] | None = None,
                 apply: Callable[..., None] | None = None,
                 installed: str = __version__) -> None:
        super().__init__(parent)
        self._check = check or check_release
        self._download = download or download_release
        self._verify = verify or verify_release
        self._apply = apply or apply_release
        self._installed = installed
        self._busy = False
        self._release: object | None = None
        self._installer: Path | None = None

        self._signals = _Signals(self)
        self._signals.found.connect(self.found, Qt.QueuedConnection)
        self._signals.upToDate.connect(self.upToDate, Qt.QueuedConnection)
        self._signals.progress.connect(self.progress, Qt.QueuedConnection)
        self._signals.ready.connect(self.ready, Qt.QueuedConnection)
        self._signals.failed.connect(self.failed, Qt.QueuedConnection)

    # ------------------------------------------------------------- inspection
    @property
    def busy(self) -> bool:
        """Whether a check or a download is already running."""
        return self._busy

    @property
    def release(self) -> object | None:
        """The release last found, if any."""
        return self._release

    @property
    def installer(self) -> Path | None:
        """The verified installer, once `prepare` has succeeded."""
        return self._installer

    # ------------------------------------------------------------------ steps
    def check_now(self) -> bool:
        """Ask for a newer release. Returns whether a check was started."""
        if self._busy:
            return False
        self._busy = True
        QThreadPool.globalInstance().start(_Task(self._run_check))
        return True

    def _run_check(self) -> None:
        release = None
        try:
            release = self._check(self._installed)
        finally:
            # Cleared before the result is published, so that anything which
            # can see a release can also start the download. The other order
            # leaves a window where `release` is set and `prepare` still
            # refuses, which reads as an update that quietly does nothing.
            self._busy = False
        self._release = release
        if release is None:
            self._signals.upToDate.emit()
        else:
            self._signals.found.emit(release)

    def prepare(self, directory: Path | None = None) -> bool:
        """Download and verify the release found earlier.

        Nothing is executed here. `ready` carries the verified installer and
        its digest; deciding to run it is the window's call, because it is the
        step that closes the app.
        """
        if self._busy or self._release is None:
            return False
        self._busy = True
        target = directory
        QThreadPool.globalInstance().start(
            _Task(lambda: self._run_prepare(target)))
        return True

    def _run_prepare(self, directory: Path | None) -> None:
        try:
            release = self._release
            where = directory
            if where is None:
                where = Path(tempfile.mkdtemp(prefix="offloader-update-"))
            Path(where).mkdir(parents=True, exist_ok=True)

            last = 0

            def report(done: int, total: int) -> None:
                nonlocal last
                # Throttled, and always emitted for the final block so the bar
                # finishes rather than stopping just short.
                if done - last >= PROGRESS_STEP or (total and done >= total):
                    last = done
                    self._signals.progress.emit(done, total)

            installer, digest = self._download(release, Path(where),
                                               progress=report)
            self._verify(installer, release, expected_digest=digest,
                         actual_digest=digest)
            self._installer = installer
            self._signals.ready.emit(release, digest)
        except UpdateError as exc:
            self._signals.failed.emit(str(exc))
        except Exception as exc:                 # pragma: no cover - defensive
            # A check that took the app down would be a worse bug than a
            # missed update, so nothing escapes this thread.
            self._signals.failed.emit(f"the update could not be prepared: {exc}")
        finally:
            self._busy = False

    def install(self) -> None:
        """Run the verified installer. Raises `UpdateError` if it cannot."""
        if self._installer is None:
            raise UpdateError("no verified installer is ready")
        self._apply(self._installer)


def refusal(states: list[object]) -> str | None:
    """Why an update cannot be installed right now, or None if it can.

    Takes the queue's states rather than the controller, so the rule is a
    function of what is running and can be tested without a window. A paused
    job counts: it is a partially written destination waiting to continue, and
    replacing the application under it is not a way to find out what happens.
    """
    from .worker import JobState

    active = [state for state in states
              if state in (JobState.RUNNING, JobState.PAUSED)]
    if active:
        return ("Offloader is copying. The installer will not replace a "
                "running transfer, so finish or cancel the job first.")
    return None
