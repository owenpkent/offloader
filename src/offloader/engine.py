"""The offload pipeline: scan, copy to N destinations, verify, collect metadata.

Source bytes are read exactly once and fanned out to every destination in the
same pass, so adding a second destination costs write bandwidth but not read
bandwidth.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import os
import queue
import shutil
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import braw as braw_mod
from . import companions, integrity, longpath, sysinfo, thumbs, volumes
from . import probe as probe_mod
from . import retry as retry_mod
from .hashers import get_algorithm, hash_file, new_hasher
from .models import (
    Destination,
    FileEntry,
    FileStatus,
    Job,
    MediaInfo,
    Profile,
    VerificationMode,
)

#: Junk that camera cards and operating systems leave behind. Copying these
#: would inflate file counts and pollute the report.
DEFAULT_EXCLUDES = (
    ".DS_Store", "._*", "Thumbs.db", "desktop.ini", ".Spotlight-V100",
    ".Trashes", ".fseventsd", "$RECYCLE.BIN", "System Volume Information",
)

CHUNK_SIZE = 8 << 20  # 8 MiB — large enough to keep spinning disks streaming.

#: Chunks the reader may run ahead of the writer. A sequential read-then-write
#: loop never overlaps the two, so it settles at the harmonic mean of read and
#: write speed; one thread of read-ahead recovers much of that -- +30% in an
#: A/B of the two loop shapes on a 4 GB exFAT-to-NVMe offload (see
#: docs/performance.md, and note the caveats there about absolute figures).
#: Bounded, so memory stays at READ_AHEAD x CHUNK_SIZE however fast either
#: side runs.
READ_AHEAD = 3


#: Extension worn by a copy that is still in flight. A destination file only
#: takes its real name once it is complete — and, under full verification, once
#: it has been proven — so an interrupted offload can never leave something that
#: looks like finished media.
PARTIAL_SUFFIX = ".offloader-partial"


class JobCancelled(Exception):
    """Raised inside the copy loop when the caller cancels."""


class UnsafeDestination(ValueError):
    """A destination that could destroy the source it is copying."""


def assert_safe_destinations(source_root: Path, destinations: Sequence[Path]) -> None:
    """Refuse destinations that can eat the source.

    Two real ways to lose the only copy of a day's footage:

    * A destination equal to, or inside, the source. Opening the target for
      writing truncates it, and if that target *is* a source file the original
      is gone before it is ever read — with the checksum of an empty file
      dutifully recorded.
    * Two destinations resolving to the same directory, which would have two
      writers fighting over one file.

    Enforced here rather than in the interface so the CLI, the GUI and any
    library caller are all covered.
    """
    source = Path(source_root).resolve()
    seen: dict[Path, Path] = {}

    for destination in destinations:
        resolved = Path(destination).resolve()

        if resolved == source:
            raise UnsafeDestination(
                f"destination {destination} is the source itself; "
                "copying a card onto itself would destroy it"
            )
        if source in resolved.parents:
            raise UnsafeDestination(
                f"destination {destination} is inside the source {source_root}; "
                "choose a destination outside it"
            )
        if resolved in seen:
            raise UnsafeDestination(
                f"destinations {seen[resolved]} and {destination} are the same "
                "directory"
            )
        seen[resolved] = Path(destination)


def assert_no_destination_collisions(
    sources: Sequence[Path], source_root: Path,
    destination_roots: Sequence[Path], preserve_structure: bool,
    relatives: dict[Path, Path] | None = None,
) -> None:
    """Refuse a layout that maps two source files onto one destination path.

    Flattening a tree is a lossy operation whenever two cards, or two folders
    on one card, hold a clip of the same name. Copying both leaves one file on
    disk, and because each is verified as it lands, before the next one
    overwrites it, both are reported VERIFIED. A report that attests to a file
    which is not at the destination is worse than no report, so this is caught
    up front, before a single byte moves.

    Paths are compared with `os.path.normcase`, so on Windows this also
    catches two names differing only in case: distinct on the case-sensitive
    volume they came from, one file on the volume they are going to.

    `relatives` maps a source to its destination-relative path, for a
    selection whose layout the engine did not derive. It matters more there
    than for a card, not less: a selection is drawn from several volumes at
    once, so two files of the same name arriving from different trees is the
    normal case rather than the unlucky one.
    """
    claimed: dict[str, Path] = {}
    collisions: list[tuple[Path, Path, Path]] = []

    for source in sources:
        for root in destination_roots:
            if relatives is not None:
                target = root / relatives[source]
            else:
                target = _destination_for(source, source_root, root,
                                          preserve_structure)
            key = os.path.normcase(str(target))
            if key in claimed:
                collisions.append((claimed[key], source, target))
            else:
                claimed[key] = source

    if collisions:
        first, second, target = collisions[0]
        extra = (f" (and {len(collisions) - 1} more)" if len(collisions) > 1 else "")
        raise UnsafeDestination(
            f"{first} and {second} would both be copied to {target}{extra}; "
            "one would silently overwrite the other. Keep the source folder "
            "structure, or offload the colliding folders separately."
        )


class JobControl:
    """Cooperative pause/resume/cancel for a running offload.

    Checked once per chunk, so a pause takes effect within one 8 MiB read and a
    cancel never leaves a half-written file behind — `run()` deletes partial
    destinations on the way out.
    """

    def __init__(self) -> None:
        self._resume = threading.Event()
        self._resume.set()
        self._cancelled = threading.Event()

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def cancel(self) -> None:
        # Release any pause first, or a paused job would never see the cancel.
        self._cancelled.set()
        self._resume.set()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def checkpoint(self) -> None:
        """Block while paused; raise `JobCancelled` if cancelled."""
        if self._cancelled.is_set():
            raise JobCancelled()
        if not self._resume.is_set():
            self._resume.wait()
        if self._cancelled.is_set():
            raise JobCancelled()


class FileControl(JobControl):
    """A `JobControl` driven by a file, so another process can pause a job.

    The desktop app has transport buttons and a `JobControl` to wire them to.
    The CLI has neither, and usually has no console to press a key in either: a
    1.9 TB offload is started detached, over ssh, or by a scheduler, and the
    person who wants it paused is not sitting at that terminal. So the
    instruction has to arrive from outside the process.

    A file is the smallest thing that works everywhere. It needs no signal
    support -- Windows has almost none beyond SIGINT, and this tool is
    Windows-first -- no port, no daemon and no shared memory. It survives the
    terminal going away, any user with write access can set it, and it can be
    *read* to see what state a job is in.

    The file holds one word: `run`, `pause` or `cancel`. Anything else --
    empty, garbled, half-written, or unreadable because another process has it
    open at that moment -- is **no opinion**, and leaves the job in whatever
    state it is already in. That asymmetry is deliberate. Inferring `cancel`
    from a damaged control file would let a stray byte stop an offload that is
    hours in, and a control file is exactly the kind of thing that gets
    clobbered by a sync client or a text editor writing in two steps.

    Polling is rate-limited by `poll` seconds because `checkpoint()` runs once
    per 8 MiB chunk -- around 16 times a second at 130 MB/s -- and the control
    file may well be on a network path.
    """

    RUN = "run"
    PAUSE = "pause"
    CANCEL = "cancel"
    STATES = (RUN, PAUSE, CANCEL)

    def __init__(self, path: Path, poll: float = 0.5,
                 on_change: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.path = Path(path)
        self.poll = max(0.05, poll)
        self._on_change = on_change
        self._state = self.RUN
        self._checked = 0.0
        self._lock = threading.Lock()
        # Injectable so a test can decide when the rate limiter has elapsed.
        # `poll` has a 50 ms floor, deliberately: a caller asking for 0 on a
        # network control path would otherwise stat it sixteen times a second.
        # That floor also means a small fixture can finish between two reads,
        # which is a race in the test rather than a behaviour worth having.
        self._clock = clock

    @property
    def state(self) -> str:
        return self._state

    def claim(self) -> None:
        """Write `run`, taking ownership of the path for this job.

        A control file left saying `pause` by a previous job would otherwise
        stop the next one before it copied a byte, and the reason would not be
        obvious to anyone watching. A job starts by saying what it is doing.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.RUN + "\n", encoding="utf-8")

    def read(self) -> str | None:
        """The word in the file, or None for "no opinion"."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Deleting the file releases the job rather than stranding it.
            return self.RUN
        except OSError:
            return None
        except UnicodeDecodeError:
            # Not an OSError, so it escaped the handler above and aborted the
            # checkpoint. A control file being written underneath us, or one an
            # editor saved in another encoding, is damaged content: the
            # documented answer to that is no opinion, not a stopped transfer.
            return None
        # The whole value, not its first token: `cancel pending upload` is
        # something an editor or a sync left behind, not an instruction to stop
        # a running offload.
        word = text.strip().lower()
        if word not in self.STATES:
            return None
        return word

    def sync(self, force: bool = False) -> None:
        with self._lock:
            now = self._clock()
            if not force and now - self._checked < self.poll:
                return
            self._checked = now
        state = self.read()
        if state is None or state == self._state:
            return
        self._state = state
        if state == self.PAUSE:
            self.pause()
        elif state == self.RUN:
            self.resume()
        elif state == self.CANCEL:
            self.cancel()
        if self._on_change is not None:
            self._on_change(state)

    def checkpoint(self) -> None:
        self.sync()
        # Poll while held, or a `resume` written to the file would never be
        # seen: JobControl.checkpoint would be blocked inside Event.wait().
        while self.paused and not self.cancelled:
            time.sleep(self.poll)
            self.sync(force=True)
        super().checkpoint()


@dataclass
class ProgressEvent:
    """Emitted as the job runs, for CLI progress bars and (later) the GUI."""

    file_index: int
    file_total: int
    file_name: str
    stage: str                 # "copy" | "verify" | "probe" | "thumbs"
    bytes_done: int = 0
    bytes_total: int = 0
    job_bytes_done: int = 0
    job_bytes_total: int = 0


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(frozen=True)
class SelectedFile:
    """One file to offload, and where it lands under each destination root.

    A scanned card is a single tree, so a destination path can be derived from
    the source root. A selection is not: `timeline.selection` draws files from
    several volumes at once, and how they are laid out at the destination is
    the selector's decision rather than something the engine can infer. Each
    file therefore carries its own destination-relative path, and the engine
    infers nothing.
    """

    source: Path
    #: Where this file lands beneath each destination root. Relative, and
    #: checked to be: see `assert_selection_is_safe`.
    relative: Path
    #: The tree the file was found in. Reporting only -- `FileEntry.relative`
    #: uses it to show a path a reader can place.
    root: Path = Path()


def assert_selection_is_safe(selection: Sequence[SelectedFile],
                             destinations: Sequence[Path]) -> None:
    """Refuse a selection that could destroy what it is copying.

    `assert_safe_destinations` asks whether the destination is inside the
    source, which is the right question for a card: the source root *is* the
    card, and writing into it is how you lose it. It is the wrong question for
    a selection, where the search roots are a library being read and the
    destination is very often a new folder inside it -- resolving a cut
    against `E:/ChairsDoc` and collecting its gaps into
    `E:/ChairsDoc/RowV6_Media` is the ordinary case, not a mistake.

    The property that actually matters is narrower, and checkable exactly: no
    file being read may sit at or beneath somewhere being written. That admits
    the ordinary case and still refuses the one that eats data.

    A relative path that is absolute, or that climbs out with `..`, would
    escape the destination root and write somewhere the caller never named.
    Both are refused here, before any byte moves, rather than at the point of
    use where they would leave a half-copied job behind.
    """
    resolved_dests: dict[Path, Path] = {}
    for destination in destinations:
        resolved = Path(destination).resolve()
        if resolved in resolved_dests:
            raise UnsafeDestination(
                f"destinations {resolved_dests[resolved]} and {destination} "
                "are the same directory"
            )
        resolved_dests[resolved] = Path(destination)

    for entry in selection:
        relative = Path(entry.relative)
        if relative.is_absolute() or relative.drive or relative.anchor:
            raise UnsafeDestination(
                f"selection for {entry.source} has an absolute destination "
                f"path {relative}; it must be relative to the destination root"
            )
        if any(part == os.pardir for part in relative.parts):
            raise UnsafeDestination(
                f"selection for {entry.source} escapes the destination with "
                f"{relative}"
            )

        source = Path(entry.source).resolve()
        for resolved, original in resolved_dests.items():
            if source == resolved or resolved in source.parents:
                raise UnsafeDestination(
                    f"{entry.source} is inside the destination {original}; "
                    "offloading it would copy a file over itself"
                )


@dataclass
class OffloadOptions:
    destinations: Sequence[Path]
    algorithm: str = "xxh3-64"
    # FULL by default: the read-back is the only mode that proves what is on
    # the destination device, and an offload tool's default should be the one
    # whose "Verified" means the most. The cost is one extra read of each copy
    # at the destination's own speed; anyone racing a deadline can opt down.
    verification: VerificationMode = VerificationMode.FULL
    thumbnail_count: int = 4
    excludes: Sequence[str] = DEFAULT_EXCLUDES
    #: Preserve the source tree under each destination root.
    preserve_structure: bool = True
    #: Skip files that already exist at the destination with a matching size.
    skip_existing: bool = False
    #: Move the camera's proxy directories before the originals. Ordering only:
    #: the same files are copied either way, and the report still reads in tree
    #: order. See `order_for_transfer`.
    proxies_first: bool = True
    job_name: str | None = None
    thumbnail_dir: Path | None = None
    extra_probe: bool = True
    #: The workflow this offload is. `Profile.DATA` is a generic large-data
    #: transfer: no file is treated as media, so ffprobe, thumbnails and the
    #: BRAW check are all switched off regardless of the media-only knobs above.
    profile: Profile = Profile.MEDIA
    #: How hard to try again when a read fails for a transient-looking reason.
    #: Marginal cards and readers routinely succeed on a second attempt.
    retry: retry_mod.RetryPolicy = field(default_factory=retry_mod.RetryPolicy)
    #: An explicit file set to offload, in place of scanning the source root.
    #: Files may come from any number of volumes and each carries its own
    #: destination-relative path, so `preserve_structure` and `excludes` do
    #: not apply -- the selector has already decided both. `proxies_first`
    #: does not apply either: a selection is transferred in the order given,
    #: which is the only order the selector can be held to.
    selection: Sequence[SelectedFile] | None = None
    #: Read every source file a second time and compare. Costs a full extra
    #: pass over the card, and is the only thing that catches a read which
    #: returned wrong bytes without the operating system noticing.
    paranoid: bool = False

    def __post_init__(self) -> None:
        # The data profile is defined by the absence of media work, so enforce
        # it here rather than trusting every caller to zero the media knobs.
        # A library caller that sets only `profile=Profile.DATA` gets a clean
        # generic transfer; the CLI and presets get the same guarantee.
        if self.profile is Profile.DATA:
            self.extra_probe = False
            self.thumbnail_count = 0


@dataclass
class _Counters:
    job_bytes_total: int = 0
    job_bytes_done: int = 0
    errors: list[str] = field(default_factory=list)


def is_excluded(path: Path, patterns: Iterable[str]) -> bool:
    name = path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def scan(root: Path, excludes: Iterable[str] = DEFAULT_EXCLUDES) -> list[Path]:
    """Every file under `root`, sorted, with junk filtered out.

    Directories are visited at most once each. A Windows junction pointing at
    its own parent otherwise walks forever, and `Path.is_symlink()` is False
    for a junction, so the usual symlink guard does not see it. Termination is
    left to chance without this: today it only stops because Windows refuses
    paths past MAX_PATH, and it stops having already returned the same file
    dozens of times.

    Only directories the walk will actually enter are recorded. `os.walk`
    does not follow a directory symlink, so recording one would mark its
    target as seen without anything having scanned it: an alias `a -> z`
    listed before `z` would then hide `z` itself, and neither path's files
    would be found. A junction is not a symlink to `os.walk`, which does enter
    it, so a junction still goes through the guard.
    """
    patterns = tuple(excludes)
    found: list[Path] = []
    visited: set[str] = set()

    def already_seen(directory: Path) -> bool:
        if os.path.islink(directory):
            # Not followed by os.walk, so not a visit. See the docstring.
            return False
        try:
            real = os.path.normcase(os.path.realpath(directory))
        except OSError:                  # pragma: no cover - unreadable entry
            return True
        if real in visited:
            return True
        visited.add(real)
        return False

    # The root is always entered, even when it is itself a symlink.
    visited.add(os.path.normcase(os.path.realpath(root)))
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if not is_excluded(here / d, patterns) and not already_seen(here / d)
        )
        for filename in sorted(filenames):
            candidate = here / filename
            if not is_excluded(candidate, patterns):
                found.append(candidate)
    return found


def is_proxy(path: Path, source_root: Path) -> bool:
    """Whether `path` sits inside a proxy directory under `source_root`."""
    try:
        relative = Path(path).relative_to(source_root)
    except ValueError:                   # not under this root at all
        return False
    names = {name.lower() for name in companions.PROXY_DIRECTORIES}
    # parts[:-1] is the directories only -- a *file* called "proxy" is a clip
    # with an unlucky name, not a proxy.
    return any(part.lower() in names for part in relative.parts[:-1])


def order_for_transfer(files: Sequence[Path], source_root: Path,
                       proxies_first: bool = True) -> list[Path]:
    """Scan order, with the proxy directories hoisted to the front.

    Proxies are a rounding error next to the originals -- a typical 27-clip
    BRAW card is ~110 GB of original against ~0.4 GB of H.264 -- so moving them
    first costs the job well under a percent of its runtime and hands the edit
    something to cut with minutes in, rather than after the last original has
    landed. That is the whole reason this is the default.

    It also improves the contact sheet. Thumbnails for a camera original ffmpeg
    cannot decode are borrowed from the matching proxy, and
    `companions.thumbnail_source` looks at the destination before the source:
    with the proxies already down, that read comes off the destination disk
    instead of competing with the copy for the card.

    The partition is stable, so scan order survives inside each group, and a
    card with no proxy directory is returned untouched.
    """
    if not proxies_first:
        return list(files)
    root = Path(source_root)
    proxies = [path for path in files if is_proxy(path, root)]
    if not proxies:
        return list(files)
    originals = [path for path in files if not is_proxy(path, root)]
    return proxies + originals


def _destination_for(source: Path, source_root: Path, dest_root: Path,
                     preserve: bool) -> Path:
    if preserve:
        try:
            return dest_root / source.relative_to(source_root)
        except ValueError:
            pass
    return dest_root / source.name


def _close_quietly(handle: object | None) -> None:
    """Close a file handle, swallowing anything it raises.

    Deliberately not just `OSError`. This runs on the way out of a failure and
    must never *become* the failure: a raise from here would skip the sentinel
    the reader thread owes its consumer, and the copy would hang rather than
    report the error that actually happened.
    """
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        pass


@dataclass
class _CopyResult:
    """What one pass of `_copy_fanout` produced."""

    source_checksum: str
    destination_checksums: list[str]
    #: (offset, attempts) for every chunk that did not read first time. The copy
    #: succeeded, but a card that needs these is a card on its way out.
    recovered_reads: list[tuple[int, int]] = field(default_factory=list)


def _copy_fanout(source: Path, targets: Sequence[Path], algorithm: str,
                 on_chunk: Callable[[int], None],
                 control: JobControl | None = None,
                 retry: retry_mod.RetryPolicy = retry_mod.NO_RETRY) -> _CopyResult:
    """Stream `source` into every target at once.

    `targets` are the *in-flight* paths — the caller renames them into place
    once it is satisfied. Nothing here ever opens a final destination name, so a
    copy that fails or is interrupted cannot damage a good file already sitting
    there.

    Returns the source checksum plus one checksum per target, computed from the
    bytes actually handed to each write() call.

    `retry` applies to *source reads only*, chunk by chunk. Writes are left to
    the caller's whole-file retry: a write that fails part-way leaves the
    destination at a length nothing here knows, whereas a failed read has
    produced nothing at all.
    """
    source = Path(source)
    src_hasher = new_hasher(algorithm)
    dst_hashers = [new_hasher(algorithm) for _ in targets]

    for target in targets:
        # Last line of defence: opening a target with "wb" truncates it, so a
        # target that *is* the source would destroy the original before a byte
        # of it was read. assert_safe_destinations should have caught this long
        # before now; refuse anyway rather than trust that it did.
        if Path(target).resolve() == source.resolve():
            raise UnsafeDestination(
                f"refusing to write {target}: it is the source file")
        longpath.makedirs(target.parent)

    chunks: queue.Queue = queue.Queue(maxsize=READ_AHEAD)
    stop = threading.Event()
    failure: list[BaseException] = []
    recovered: list[tuple[int, int]] = []

    def read_ahead() -> None:
        """Keep the queue fed so the next read overlaps the current write.

        A transient read failure is retried *here*, at the chunk that failed,
        rather than by restarting the file. Nothing has been hashed yet — the
        hashers only ever see a chunk once it has been delivered whole — so
        there is no checksum state to unwind, and recovering a bad sector costs
        one 8 MiB re-read instead of a re-read of everything before it. On a
        79 GB clip that is the difference between seconds and a quarter of an
        hour.
        """
        reader = None
        offset = 0
        try:
            reader = longpath.open_binary(source, "rb")

            stale = False

            def read_one() -> bytes:
                # Recovery happens here rather than in `before_retry` because
                # `retry.call` invokes that outside the clause that catches
                # OSError: a reopen that failed would escape the loop with
                # attempts still unspent, and get wrapped in `Exhausted`, which
                # closes the whole-file retry too. Inside the operation, a
                # reader that is slow to come back costs one attempt of the
                # chunk's own budget, which is what the budget is for.
                nonlocal reader, stale
                if stale:
                    # Reopen rather than seek alone: a reader that dropped off
                    # the bus needs its handle re-established, which restarting
                    # the whole file used to get for free.
                    _close_quietly(reader)
                    reader = longpath.open_binary(source, "rb")
                    reader.seek(offset)
                    stale = False
                return reader.read(CHUNK_SIZE)

            def recover() -> None:
                nonlocal stale
                stale = True

            def back_off(pause: float) -> None:
                # Sleep in slices so a pause or cancel is honoured while the
                # backoff is waited out. A card failing over a stretch can
                # spend several seconds per chunk here, and a cancel that only
                # lands once the stretch is over is not much of a cancel.
                deadline = time.monotonic() + pause
                while not stop.is_set():
                    if control is not None:
                        control.checkpoint()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    time.sleep(min(0.2, remaining))

            while not stop.is_set():
                if control is not None:
                    control.checkpoint()
                try:
                    chunk, attempts = retry_mod.call(read_one, retry,
                                                     before_retry=recover,
                                                     sleep=back_off)
                except OSError as exc:
                    if retry.enabled and retry_mod.is_transient(exc):
                        raise retry_mod.Exhausted(
                            f"read failed at offset {offset} after "
                            f"{retry.attempts} attempts: {exc}") from exc
                    raise
                if attempts > 1:
                    recovered.append((offset, attempts))
                if not chunk:
                    break
                offset += len(chunk)
                # Time-boxed so a consumer that died still lets us exit.
                while not stop.is_set():
                    try:
                        chunks.put(chunk, timeout=0.2)
                        break
                    except queue.Full:
                        continue
        except BaseException as exc:      # re-raised on the calling thread
            failure.append(exc)
        finally:
            _close_quietly(reader)
            # The sentinel must be delivered, not attempted: if the queue
            # happens to be full at EOF a dropped sentinel leaves the consumer
            # blocked on get() forever. Only give up once `stop` is set, which
            # means the consumer has already left the loop.
            while not stop.is_set():
                try:
                    chunks.put(None, timeout=0.2)
                    break
                except queue.Full:
                    continue

    thread = threading.Thread(target=read_ahead, name=f"read:{source.name}",
                              daemon=True)
    handles: list = []
    started = False
    try:
        for target in targets:
            handles.append(longpath.open_binary(target, "wb"))
        thread.start()
        started = True

        while True:
            chunk = chunks.get()
            if chunk is None:
                break
            src_hasher.update(chunk)
            for handle, hasher in zip(handles, dst_hashers, strict=True):
                handle.write(chunk)
                hasher.update(chunk)
            on_chunk(len(chunk))

        if failure:
            raise failure[0]

        for handle in handles:
            handle.flush()
            # Durability is the whole point of an offload: without this the
            # bytes may still be in the page cache when we declare "Verified",
            # and a full verification would re-read what it just wrote.
            os.fsync(handle.fileno())
    finally:
        stop.set()
        if started:
            # Drain so a reader parked on a full queue can observe `stop`.
            while thread.is_alive():
                try:
                    chunks.get_nowait()
                except queue.Empty:
                    thread.join(timeout=0.05)
        for handle in handles:
            handle.close()

    return _CopyResult(src_hasher.hexdigest(),
                       [h.hexdigest() for h in dst_hashers],
                       recovered)


def _confirm_source(source: Path, expected: str, algorithm: str) -> bool:
    """Read `source` a second time and insist it hashes the same.

    The gap this closes: a read that returns wrong bytes *without raising*. The
    checksum is computed from whatever was read, so a bad read produces a
    destination that faithfully matches a corrupted source and verifies clean at
    every level — file hashes, directory hashes, the lot. Nothing but reading
    twice can see it.

    Raises `UnstableRead` on a disagreement rather than choosing a winner: there
    is no basis for deciding which of the two reads was the true one.

    Returns whether the page cache was actually dropped first. A second read
    served out of memory compares the first read against itself, so a caller
    that cannot evict has to say so rather than claim the guarantee.
    """
    evicted = integrity.evict_from_cache(source)
    again = hash_file(source, algorithm)
    if again != expected:
        raise retry_mod.UnstableRead(
            f"two reads of {source.name} disagreed ({expected} then {again}) — "
            "the source did not return the same bytes twice"
        )
    return evicted


def _describe_recovery(recovered: list[tuple[int, int]]) -> str:
    """What a file's recovered reads amount to, in one line.

    A single bad sector is worth naming exactly; a run of them is worth
    bounding, because the useful fact stops being *which* byte and becomes how
    much of the file would not read first time.
    """
    if len(recovered) == 1:
        offset, attempts = recovered[0]
        return f"recovered a failed read at byte {offset} on attempt {attempts}"
    offsets = [offset for offset, _ in recovered]
    worst = max(attempts for _, attempts in recovered)
    return (f"recovered {len(recovered)} failed reads between byte "
            f"{min(offsets)} and byte {max(offsets)}, the worst on "
            f"attempt {worst}")


def _invert_companions(belongs_to: dict[Path, Path]) -> dict[Path, list[Path]]:
    """clip -> its companions, from companion -> its clip."""
    owns: dict[Path, list[Path]] = {}
    for companion, clip in belongs_to.items():
        owns.setdefault(clip, []).append(companion)
    for paths in owns.values():
        paths.sort()
    return owns


def _warn_on_split_companions(job: Job) -> None:
    """A clip and the files that belong to it have to share a fate.

    A graded BRAW delivered without its `.sidecar` has lost the grade, and a
    per-file table showing one Verified row and one Failed row twenty lines
    apart is not how anyone finds that out.
    """
    by_source = {entry.source: entry for entry in job.files}
    for entry in job.files:
        if entry.companion_of is None or entry.status is not FileStatus.FAILED:
            continue
        clip = by_source.get(entry.companion_of)
        if clip is None or clip.status is FileStatus.FAILED:
            continue
        job.warnings.append(
            f"{entry.name} did not copy but {clip.name} did — the clip has "
            "been separated from a file that belongs with it"
        )


def _discard(targets: Iterable[Path]) -> None:
    """Delete half-written destinations. A partial file that looks complete is
    worse than no file at all."""
    for target in targets:
        try:
            longpath.unlink(target)
        except OSError:
            pass


def run(source_root: Path, options: OffloadOptions,
        progress: ProgressCallback | None = None,
        control: JobControl | None = None) -> Job:
    """Execute an offload and return the finished Job.

    Pass a `JobControl` to allow pausing or cancelling mid-flight; a cancelled
    job returns normally, with `Job.cancelled` set and the files it did finish
    intact.

    With `options.selection` set, the file set is taken from the selection
    instead of by scanning, and `source_root` becomes a label for the job
    rather than a tree to walk -- it may be a timeline file, which is what the
    files were selected from.
    """
    source_root = Path(source_root)
    if not source_root.exists():
        raise FileNotFoundError(f"source not found: {source_root}")

    algorithm = get_algorithm(options.algorithm)
    dest_roots = [Path(d) for d in options.destinations]
    if not dest_roots:
        raise ValueError("at least one destination is required")

    #: Per-file destination-relative paths, for a selection. None for a card,
    #: where the destination is derived from the source root as it always was.
    relatives: dict[Path, Path] | None = None

    if options.selection is not None:
        assert_selection_is_safe(options.selection, dest_roots)
        files = [Path(entry.source) for entry in options.selection]
        seen = {os.path.normcase(str(p)) for p in files}
        if len(seen) != len(files):
            raise ValueError("selection lists the same source file twice")
        relatives = {Path(e.source): Path(e.relative) for e in options.selection}
        roots = {Path(e.source): Path(e.root) for e in options.selection}
    else:
        assert_safe_destinations(source_root, dest_roots)
        files = scan(source_root, options.excludes)
        roots = {}

    assert_no_destination_collisions(files, source_root, dest_roots,
                                     options.preserve_structure, relatives)
    counters = _Counters(job_bytes_total=sum(p.stat().st_size for p in files))

    # `files` stays the tree order the card is laid out in and remains the
    # authority on what the job contains; `transfer` is only the sequence the
    # bytes move in. job.files is put back into `files` order before returning.
    # A selection is already in the order its selector chose, and reordering
    # by proxy directory would only second-guess that.
    if options.selection is not None:
        transfer = list(files)
    else:
        transfer = order_for_transfer(files, source_root, options.proxies_first)

    host = sysinfo.collect()
    job = Job(
        # A selection names its job after the timeline it came from, and a
        # timeline is a file: ".xml" in a report header helps nobody. A card
        # offloaded from its root has no folder name; its volume label is what
        # the operator calls it.
        name=(options.job_name
              or (source_root.stem if source_root.is_file() else source_root.name)
              or volumes.volume_label(source_root) or "Offload"),
        source_root=source_root,
        destination_roots=dest_roots,
        verification=options.verification,
        profile=options.profile,
        hash_label=algorithm.label,
        started=_dt.datetime.now(),
        os_version=host.os_version,
        processors=host.processors,
        system_ram=host.system_ram,
        paranoid=options.paranoid,
    )

    thumb_dir = options.thumbnail_dir or (dest_roots[0] / f"{job.name}_Reports" / "thumbs")

    # A sidecar belongs to a *clip*. Under the data profile nothing is a clip,
    # so stem-matching a dataset would announce that `run_1440.xmp` belongs to
    # `run_1440.h5` on no evidence beyond a shared name.
    belongs_to = (companions.group(files) if options.profile.probes_media
                  else {})
    owns = _invert_companions(belongs_to)

    #: Whether the "cache could not be evicted" limitation has been reported.
    #: Said once per job, not once per clip: where the platform has no eviction
    #: call at all (macOS has no posix_fadvise), repeating it per file would
    #: bury the warnings that are about actual media.
    evict_noted = False
    reread_noted = False

    def emit(event: ProgressEvent) -> None:
        if progress:
            progress(event)

    # One memo per job: the first clip of a suffix this ffmpeg cannot decode
    # pays the failed extraction, the remaining clips skip it.
    thumb_memo = thumbs.DecoderMemo()

    for index, source in enumerate(transfer):
        # Between files is the cheapest place to honour a pause or cancel.
        if control is not None:
            try:
                control.checkpoint()
            except JobCancelled:
                job.cancelled = True
                break

        stat = source.stat()
        entry = FileEntry(
            source=source,
            # For a selection this is the tree the file came from, not the job
            # label, so the report shows a path a reader can place.
            source_root=roots.get(source, source_root),
            size=stat.st_size,
            created=getattr(stat, "st_birthtime", stat.st_ctime),
            modified=stat.st_mtime,
            companion_of=belongs_to.get(source),
            companions=owns.get(source, []),
        )

        if relatives is not None:
            targets = [root / relatives[source] for root in dest_roots]
        else:
            targets = [
                _destination_for(source, source_root, root,
                                 options.preserve_structure)
                for root in dest_roots
            ]

        emit(ProgressEvent(index, len(files), source.name, "copy",
                           0, stat.st_size,
                           counters.job_bytes_done, counters.job_bytes_total))

        if options.skip_existing and all(
            t.exists() and t.stat().st_size == stat.st_size for t in targets
        ):
            entry.checksum = None
            for root, target in zip(dest_roots, targets, strict=True):
                entry.destinations.append(
                    Destination(root=root, path=target, status=FileStatus.SKIPPED)
                )
            counters.job_bytes_done += stat.st_size
            job.files.append(entry)
            continue

        if stat.st_size == 0:
            # Legitimate for a sidecar, alarming for a clip. Say so rather than
            # report "Verified" on a file that contains nothing.
            job.warnings.append(f"{source.name} is empty (0 bytes)")

        # Write under a temporary name and only rename once the copy is proven.
        # An existing good file at the destination is never opened, so a failed
        # or interrupted attempt cannot take it down with it.
        partials = [t.with_name(t.name + PARTIAL_SUFFIX) for t in targets]

        bytes_at_start = counters.job_bytes_done

        try:
            # These close over the loop variables and are all invoked inside
            # retry_mod.call below, before the loop advances — but bind them
            # anyway, so the safety is visible here rather than depending on
            # when the callee happens to call back.
            def on_chunk(n: int, _idx=index, _src=source, _st=stat) -> None:
                counters.job_bytes_done += n
                emit(ProgressEvent(_idx, len(files), _src.name, "copy",
                                   0, _st.st_size,
                                   counters.job_bytes_done, counters.job_bytes_total))

            def rewind(_partials=partials, _mark=bytes_at_start) -> None:
                # A retry restarts the file, so discard what the failed attempt
                # wrote and give back the progress it claimed.
                _discard(_partials)
                counters.job_bytes_done = _mark

            def note_retry(attempt: int, exc: BaseException, pause: float,
                           _idx=index, _src=source, _st=stat) -> None:
                emit(ProgressEvent(_idx, len(files), _src.name, "retry",
                                   0, _st.st_size,
                                   counters.job_bytes_done,
                                   counters.job_bytes_total))
                counters.errors.append(
                    f"{_src.name}: read failed ({exc}); "
                    f"attempt {attempt} of {options.retry.attempts}")

            def copy_once(_src=source, _partials=partials, _idx=index,
                          _st=stat) -> _CopyResult:
                nonlocal reread_noted
                result = _copy_fanout(_src, _partials, options.algorithm,
                                      on_chunk, control, options.retry)
                if not options.paranoid:
                    return result
                emit(ProgressEvent(_idx, len(files), _src.name, "reread",
                                   0, _st.st_size,
                                   counters.job_bytes_done,
                                   counters.job_bytes_total))
                # Raises UnstableRead on a disagreement, which the retry around
                # this call treats as transient: the honest response to a source
                # that read differently twice is to read it again, not to guess
                # which of the two was right.
                evicted = _confirm_source(_src, result.source_checksum,
                                          options.algorithm)
                if not evicted and not reread_noted:
                    reread_noted = True
                    job.warnings.append(
                        "could not evict files from the page cache on this "
                        "platform, so the second read may have come from memory "
                        "rather than the device — --paranoid proved less than "
                        "it appears to"
                    )
                return result

            result, used = retry_mod.call(
                copy_once, options.retry,
                on_retry=note_retry, before_retry=rewind,
            )
            src_sum, dst_sums = result.source_checksum, result.destination_checksums
            if used > 1:
                # Not a failure, but a card that needs retries today is a card
                # to stop using.
                job.warnings.append(
                    f"{source.name} copied on attempt {used} of "
                    f"{options.retry.attempts} — the source may be failing")
            if result.recovered_reads:
                # Recovered without restarting the file, which is why the copy
                # succeeded at all — but the sectors that needed it are real.
                # Said once per file: a card failing over a contiguous stretch
                # produces one of these every 8 MiB, and a warning list that
                # long is one nobody reads to the end.
                job.warnings.append(
                    f"{source.name}: {_describe_recovery(result.recovered_reads)}"
                    " — the source may be failing")
            entry.checksum = src_sum or None
        except JobCancelled:
            _discard(partials)
            job.cancelled = True
            break
        except (OSError, UnsafeDestination) as exc:
            _discard(partials)
            counters.errors.append(f"{source}: {exc}")
            for root, target in zip(dest_roots, targets, strict=True):
                entry.destinations.append(
                    Destination(root=root, path=target,
                                status=FileStatus.FAILED, error=str(exc))
                )
            job.files.append(entry)
            continue

        # strict: a short dst_sums would silently drop a destination from the
        # report while its file sat on disk unrecorded.
        for root, target, partial, dst_sum in zip(dest_roots, targets, partials,
                                                  dst_sums, strict=True):
            destination = Destination(root=root, path=target, checksum=dst_sum or None)

            # Mirror source timestamps so the destination reads as an archival
            # copy, not a fresh file.
            try:
                shutil.copystat(source, partial)
            except OSError:
                pass

            if options.verification is VerificationMode.NONE:
                destination.status = FileStatus.COPIED
            else:
                if options.verification is VerificationMode.FULL:
                    emit(ProgressEvent(index, len(files), source.name, "verify",
                                       0, stat.st_size,
                                       counters.job_bytes_done,
                                       counters.job_bytes_total))
                    # Evict first, or the read-back is served from the page
                    # cache and verifies our own memory against itself.
                    if not integrity.evict_from_cache(partial) and not evict_noted:
                        evict_noted = True
                        job.warnings.append(
                            "could not evict files from the page cache on this "
                            "platform, so full verification may have read from "
                            "memory rather than the device"
                        )
                    try:
                        dst_sum, verify_attempts = retry_mod.call(
                            lambda p=partial: hash_file(p, options.algorithm),
                            options.retry,
                        )
                        if verify_attempts > 1:
                            job.warnings.append(
                                f"{target.name} verified on attempt "
                                f"{verify_attempts} — the destination may be "
                                "failing")
                        destination.checksum = dst_sum or None
                    except OSError as exc:
                        destination.status = FileStatus.FAILED
                        destination.error = str(exc)
                        counters.errors.append(f"{target}: {exc}")
                        _discard([partial])
                        entry.destinations.append(destination)
                        continue

                size_ok = partial.exists() and partial.stat().st_size == stat.st_size
                sum_ok = (src_sum == dst_sum) if src_sum else True
                if size_ok and sum_ok:
                    destination.status = FileStatus.VERIFIED
                else:
                    destination.status = FileStatus.FAILED
                    destination.error = "checksum mismatch" if not sum_ok else "size mismatch"
                    counters.errors.append(f"{target}: {destination.error}")

            if destination.status is FileStatus.FAILED:
                # Leave whatever was already at the destination untouched.
                _discard([partial])
                entry.destinations.append(destination)
                continue

            try:
                longpath.replace(partial, target)   # atomic within a filesystem
            except OSError as exc:
                destination.status = FileStatus.FAILED
                destination.error = f"could not put the file in place: {exc}"
                counters.errors.append(f"{target}: {exc}")
                _discard([partial])
                entry.destinations.append(destination)
                continue

            try:
                dst_stat = target.stat()
                destination.created = getattr(dst_stat, "st_birthtime", dst_stat.st_ctime)
                destination.modified = dst_stat.st_mtime
            except OSError:
                pass

            entry.destinations.append(destination)

        if options.extra_probe:
            emit(ProgressEvent(index, len(files), source.name, "probe",
                               0, stat.st_size,
                               counters.job_bytes_done, counters.job_bytes_total))
            # Everything from here down is metadata: nice to have, and not
            # worth a transfer. The bytes are already copied and verified by
            # this point, so a clip whose container will not parse costs its
            # own metadata and a line in the report, not the rest of the card.
            try:
                entry.media = probe_mod.probe(source)

                if braw_mod.is_braw(source):
                    # A clip whose recording was interrupted has no moov atom.
                    # It copies and verifies perfectly and will not play, so
                    # the time to notice is now, while the card is in hand.
                    check = braw_mod.check_container(source)
                    if check.is_fatal:
                        job.warnings.append(f"{source.name}: {check.detail}")
            except Exception as exc:            # noqa: BLE001 - see above
                entry.media = MediaInfo()
                job.warnings.append(
                    f"{source.name}: metadata could not be read ({exc})")

            if options.thumbnail_count > 0 and entry.media.is_video:
                emit(ProgressEvent(index, len(files), source.name, "thumbs",
                                   0, stat.st_size,
                                   counters.job_bytes_done, counters.job_bytes_total))
                # Read thumbnails from the destination: it is the copy we are
                # certifying, and on a card offload it is also the faster disk.
                verified = next(
                    (d.path for d in entry.destinations
                     if d.status in (FileStatus.VERIFIED, FileStatus.COPIED)),
                    source,
                )
                # Camera originals ffmpeg cannot decode borrow the picture from
                # the proxy the camera recorded alongside them.
                picture, used_proxy = companions.thumbnail_source(
                    verified, dest_roots[0] if options.preserve_structure else None)
                if used_proxy:
                    entry.thumbnail_source = picture
                elif companions.needs_proxy(source):
                    picture, used_proxy = companions.thumbnail_source(
                        source, roots.get(source, source_root))
                    if used_proxy:
                        entry.thumbnail_source = picture

                entry.thumbnails = thumbs.extract(
                    picture, entry.media, thumb_dir, options.thumbnail_count,
                    memo=thumb_memo,
                )

        job.files.append(entry)

    job.finished = _dt.datetime.now()

    # Which order the bytes moved in is an I/O decision and nobody reading a
    # report cares; a contact sheet that opened with the proxy folder and
    # buried the clips behind it would be a regression. Sort the rows back into
    # tree order so the paperwork is byte-identical whichever way this ran.
    # Anything not in `position` (there should be nothing) sorts to the end
    # rather than raising while a job is being written up.
    position = {path: rank for rank, path in enumerate(files)}
    job.files.sort(key=lambda entry: position.get(entry.source, len(files)))

    if job.cancelled:
        not_attempted = len(files) - len(job.files)
        if not_attempted > 0:
            counters.errors.append(
                f"cancelled — {not_attempted} file(s) not attempted")
    _warn_on_split_companions(job)
    job.notes = "; ".join(counters.errors)
    return job


def rescan(source_root: Path, destination_roots: Sequence[Path],
           options: OffloadOptions,
           progress: ProgressCallback | None = None) -> Job:
    """Build a Job from an already-offloaded tree without copying anything.

    This is what `offloader report` uses: it re-hashes and re-probes in place so
    a report can be regenerated (or a delivery audited) after the fact.
    """
    source_root = Path(source_root)
    files = scan(source_root, options.excludes)
    algorithm = get_algorithm(options.algorithm)
    host = sysinfo.collect()

    job = Job(
        # A card offloaded from its root has no folder name; its volume
        # label is what the operator calls it.
        name=(options.job_name or source_root.name
              or volumes.volume_label(source_root) or "Offload"),
        source_root=source_root,
        destination_roots=[Path(d) for d in destination_roots] or [source_root],
        verification=options.verification,
        profile=options.profile,
        hash_label=algorithm.label,
        started=_dt.datetime.now(),
        os_version=host.os_version,
        processors=host.processors,
        system_ram=host.system_ram,
    )
    thumb_dir = options.thumbnail_dir or (source_root / f"{job.name}_Reports" / "thumbs")
    thumb_memo = thumbs.DecoderMemo()
    total = sum(p.stat().st_size for p in files)
    done = 0

    # A sidecar belongs to a *clip*. Under the data profile nothing is a clip,
    # so stem-matching a dataset would announce that `run_1440.xmp` belongs to
    # `run_1440.h5` on no evidence beyond a shared name.
    belongs_to = (companions.group(files) if options.profile.probes_media
                  else {})
    owns = _invert_companions(belongs_to)

    for index, source in enumerate(files):
        stat = source.stat()
        entry = FileEntry(
            source=source,
            source_root=source_root,
            size=stat.st_size,
            created=getattr(stat, "st_birthtime", stat.st_ctime),
            modified=stat.st_mtime,
            companion_of=belongs_to.get(source),
            companions=owns.get(source, []),
        )
        if progress:
            progress(ProgressEvent(index, len(files), source.name, "verify",
                                   0, stat.st_size, done, total))
        if algorithm.factory is not None:
            try:
                entry.checksum = hash_file(source, options.algorithm)
            except OSError:
                entry.checksum = None
        done += stat.st_size

        for root in job.destination_roots:
            target = _destination_for(source, source_root, root, options.preserve_structure)
            exists = target.exists()
            entry.destinations.append(
                Destination(
                    root=root,
                    path=target,
                    status=FileStatus.VERIFIED if exists else FileStatus.SKIPPED,
                    checksum=entry.checksum if exists else None,
                    created=entry.created if exists else None,
                    modified=entry.modified if exists else None,
                )
            )

        if options.extra_probe:
            # Same reasoning as in run(): a rescan exists to regenerate
            # paperwork, and one unparseable clip must not stop it.
            try:
                entry.media = probe_mod.probe(source)
                if options.thumbnail_count > 0 and entry.media.is_video:
                    entry.thumbnails = thumbs.extract(
                        source, entry.media, thumb_dir, options.thumbnail_count,
                        memo=thumb_memo,
                    )
            except Exception as exc:            # noqa: BLE001
                entry.media = MediaInfo()
                job.warnings.append(
                    f"{source.name}: metadata could not be read ({exc})")
        job.files.append(entry)

    job.finished = _dt.datetime.now()
    return job
