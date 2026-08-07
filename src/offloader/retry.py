"""Retrying reads that failed for a reason likely to go away.

Cards and readers fail intermittently long before they fail permanently. A
marginal reader drops off the bus for a moment; a sector needs a second attempt;
antivirus holds a handle open for a beat. robocopy has retried this way for
decades (`/R`, `/W`) and it is the main thing it does that this engine did not.

The discrimination matters more than the retrying. Retrying a missing file, a
permission denial or a full disk wastes time and hides the real problem, so only
errors with a plausible transient cause are retried, and a file that needed one
is reported — a card that reads on the third attempt today is a card to stop
using.
"""

from __future__ import annotations

import errno
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

T = TypeVar("T")

#: POSIX errno values worth a second attempt.
_TRANSIENT_ERRNO = {
    errno.EIO,        # low-level I/O error — the classic marginal-media signal
    errno.EBUSY,
    errno.EAGAIN,
    errno.EINTR,
    errno.ETIMEDOUT,
    errno.ENODEV,     # device dropped off the bus and may come back
    errno.ENXIO,
}

#: Windows error codes worth a second attempt. Python surfaces these on
#: OSError.winerror, which is distinct from the mapped errno.
_TRANSIENT_WINERROR = {
    21,    # ERROR_NOT_READY — "The device is not ready"
    23,    # ERROR_CRC — "Data error (cyclic redundancy check)"
    32,    # ERROR_SHARING_VIOLATION — usually antivirus, and usually brief
    33,    # ERROR_LOCK_VIOLATION
    64,    # ERROR_NETNAME_DELETED — network destination blipped
    121,   # ERROR_SEM_TIMEOUT
    170,   # ERROR_BUSY
    1117,  # ERROR_IO_DEVICE
    1167,  # ERROR_DEVICE_NOT_CONNECTED
}

#: Never retried: retrying cannot help and the delay hides the real fault.
_PERMANENT_ERRNO = {
    errno.ENOENT,     # the file is gone
    errno.ENOSPC,     # the destination is full
    errno.EROFS,
    errno.EISDIR,
    errno.ENOTDIR,
    errno.ENAMETOOLONG,
}


@dataclass(frozen=True)
class RetryPolicy:
    """How hard to try again. `attempts` counts the first try."""

    attempts: int = 3
    delay: float = 2.0
    backoff: float = 1.5

    @property
    def enabled(self) -> bool:
        return self.attempts > 1

    def wait_before(self, attempt: int) -> float:
        """Seconds to wait before `attempt` (1-based; attempt 1 never waits)."""
        if attempt <= 1:
            return 0.0
        return self.delay * (self.backoff ** (attempt - 2))


NO_RETRY = RetryPolicy(attempts=1)


def is_transient(exc: BaseException) -> bool:
    """Whether this failure has a plausible chance of not recurring."""
    if not isinstance(exc, OSError):
        return False
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _TRANSIENT_WINERROR
    if exc.errno in _PERMANENT_ERRNO:
        return False
    return exc.errno in _TRANSIENT_ERRNO


def call(operation: Callable[[], T], policy: RetryPolicy,
         *, on_retry: Callable[[int, BaseException, float], None] | None = None,
         before_retry: Callable[[], None] | None = None,
         sleep: Callable[[float], None] = time.sleep) -> tuple[T, int]:
    """Run `operation`, retrying transient failures.

    Returns (result, attempts used). `before_retry` runs after a failed attempt
    and before the next — the caller uses it to clear partial state, since a
    retry restarts the whole read rather than resuming it.
    """
    last: BaseException | None = None
    for attempt in range(1, max(1, policy.attempts) + 1):
        if attempt > 1:
            pause = policy.wait_before(attempt)
            if on_retry is not None:
                on_retry(attempt, last, pause)
            if before_retry is not None:
                before_retry()
            if pause:
                sleep(pause)
        try:
            return operation(), attempt
        except OSError as exc:
            last = exc
            if not is_transient(exc) or attempt >= policy.attempts:
                raise
    raise last if last is not None else RuntimeError("retry loop exited")
