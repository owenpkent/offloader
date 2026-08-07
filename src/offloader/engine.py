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
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import braw as braw_mod
from . import companions, integrity, longpath, sysinfo, thumbs
from . import probe as probe_mod
from . import retry as retry_mod
from .hashers import get_algorithm, hash_file, new_hasher
from .models import (
    Destination,
    FileEntry,
    FileStatus,
    Job,
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


@dataclass
class OffloadOptions:
    destinations: Sequence[Path]
    algorithm: str = "xxh3-64"
    verification: VerificationMode = VerificationMode.SOURCE_ONLY
    thumbnail_count: int = 4
    excludes: Sequence[str] = DEFAULT_EXCLUDES
    #: Preserve the source tree under each destination root.
    preserve_structure: bool = True
    #: Skip files that already exist at the destination with a matching size.
    skip_existing: bool = False
    job_name: str | None = None
    thumbnail_dir: Path | None = None
    extra_probe: bool = True
    #: How hard to try again when a read fails for a transient-looking reason.
    #: Marginal cards and readers routinely succeed on a second attempt.
    retry: retry_mod.RetryPolicy = field(default_factory=retry_mod.RetryPolicy)


@dataclass
class _Counters:
    job_bytes_total: int = 0
    job_bytes_done: int = 0
    errors: list[str] = field(default_factory=list)


def is_excluded(path: Path, patterns: Iterable[str]) -> bool:
    name = path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def scan(root: Path, excludes: Iterable[str] = DEFAULT_EXCLUDES) -> list[Path]:
    """Every file under `root`, sorted, with junk filtered out."""
    patterns = tuple(excludes)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not is_excluded(here / d, patterns))
        for filename in sorted(filenames):
            candidate = here / filename
            if not is_excluded(candidate, patterns):
                found.append(candidate)
    return found


def _destination_for(source: Path, source_root: Path, dest_root: Path,
                     preserve: bool) -> Path:
    if preserve:
        try:
            return dest_root / source.relative_to(source_root)
        except ValueError:
            pass
    return dest_root / source.name


def _copy_fanout(source: Path, targets: Sequence[Path], algorithm: str,
                 on_chunk: Callable[[int], None],
                 control: JobControl | None = None) -> tuple[str, list[str]]:
    """Stream `source` into every target at once.

    `targets` are the *in-flight* paths — the caller renames them into place
    once it is satisfied. Nothing here ever opens a final destination name, so a
    copy that fails or is interrupted cannot damage a good file already sitting
    there.

    Returns the source checksum plus one checksum per target, computed from the
    bytes actually handed to each write() call.
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

    def read_ahead() -> None:
        """Keep the queue fed so the next read overlaps the current write."""
        try:
            with longpath.open_binary(source, "rb") as reader:
                while not stop.is_set():
                    if control is not None:
                        control.checkpoint()
                    chunk = reader.read(CHUNK_SIZE)
                    if not chunk:
                        break
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

    return src_hasher.hexdigest(), [h.hexdigest() for h in dst_hashers]


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
    """
    source_root = Path(source_root)
    if not source_root.exists():
        raise FileNotFoundError(f"source not found: {source_root}")

    algorithm = get_algorithm(options.algorithm)
    dest_roots = [Path(d) for d in options.destinations]
    if not dest_roots:
        raise ValueError("at least one destination is required")
    assert_safe_destinations(source_root, dest_roots)

    files = scan(source_root, options.excludes)
    counters = _Counters(job_bytes_total=sum(p.stat().st_size for p in files))

    host = sysinfo.collect()
    job = Job(
        name=options.job_name or source_root.name,
        source_root=source_root,
        destination_roots=dest_roots,
        verification=options.verification,
        hash_label=algorithm.label,
        started=_dt.datetime.now(),
        os_version=host.os_version,
        processors=host.processors,
        system_ram=host.system_ram,
    )

    thumb_dir = options.thumbnail_dir or (dest_roots[0] / f"{job.name}_Reports" / "thumbs")

    #: Whether the "cache could not be evicted" limitation has been reported.
    evict_noted = False

    def emit(event: ProgressEvent) -> None:
        if progress:
            progress(event)

    for index, source in enumerate(files):
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
            source_root=source_root,
            size=stat.st_size,
            created=getattr(stat, "st_birthtime", stat.st_ctime),
            modified=stat.st_mtime,
        )

        targets = [
            _destination_for(source, source_root, root, options.preserve_structure)
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

            (src_sum, dst_sums), used = retry_mod.call(
                lambda _src=source, _partials=partials: _copy_fanout(
                    _src, _partials, options.algorithm, on_chunk, control),
                options.retry, on_retry=note_retry, before_retry=rewind,
            )
            if used > 1:
                # Not a failure, but a card that needs retries today is a card
                # to stop using.
                job.warnings.append(
                    f"{source.name} copied on attempt {used} of "
                    f"{options.retry.attempts} — the source may be failing")
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
                        # Said once per job, not once per clip: where the
                        # platform has no eviction call at all (macOS has no
                        # posix_fadvise), repeating it per file would bury the
                        # warnings that are about actual media.
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
            entry.media = probe_mod.probe(source)

            if braw_mod.is_braw(source):
                # A clip whose recording was interrupted has no moov atom. It
                # copies and verifies perfectly and will not play, so the time
                # to notice is now, while the card is still in hand.
                check = braw_mod.check_container(source)
                if check.is_fatal:
                    job.warnings.append(f"{source.name}: {check.detail}")

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
                        source, source_root)
                    if used_proxy:
                        entry.thumbnail_source = picture

                entry.thumbnails = thumbs.extract(
                    picture, entry.media, thumb_dir, options.thumbnail_count,
                )

        job.files.append(entry)

    job.finished = _dt.datetime.now()
    if job.cancelled:
        not_attempted = len(files) - len(job.files)
        if not_attempted > 0:
            counters.errors.append(
                f"cancelled — {not_attempted} file(s) not attempted")
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
        name=options.job_name or source_root.name,
        source_root=source_root,
        destination_roots=[Path(d) for d in destination_roots] or [source_root],
        verification=options.verification,
        hash_label=algorithm.label,
        started=_dt.datetime.now(),
        os_version=host.os_version,
        processors=host.processors,
        system_ram=host.system_ram,
    )
    thumb_dir = options.thumbnail_dir or (source_root / f"{job.name}_Reports" / "thumbs")
    total = sum(p.stat().st_size for p in files)
    done = 0

    for index, source in enumerate(files):
        stat = source.stat()
        entry = FileEntry(
            source=source,
            source_root=source_root,
            size=stat.st_size,
            created=getattr(stat, "st_birthtime", stat.st_ctime),
            modified=stat.st_mtime,
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
            entry.media = probe_mod.probe(source)
            if options.thumbnail_count > 0 and entry.media.is_video:
                entry.thumbnails = thumbs.extract(
                    source, entry.media, thumb_dir, options.thumbnail_count
                )
        job.files.append(entry)

    job.finished = _dt.datetime.now()
    return job
