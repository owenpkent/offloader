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
import stat as stat_mod
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
    ScanProblem,
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
    """
    claimed: dict[str, Path] = {}
    collisions: list[tuple[Path, Path, Path]] = []

    for source in sources:
        for root in destination_roots:
            target = _destination_for(source, source_root, root, preserve_structure)
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
    #: Read every source file a second time and compare. Costs a full extra
    #: pass over the card, and is the only thing that catches a read which
    #: returned wrong bytes without the operating system noticing.
    paranoid: bool = False
    #: Let a finished copy replace a file already at the destination whose
    #: content differs. Off by default: a second card with the same folder
    #: layout would otherwise silently replace the first card's verified
    #: copies, and the first card may already have been formatted on the
    #: strength of them. An existing file with identical content is replaced
    #: either way, since that changes nothing.
    replace_existing: bool = False

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


#: `FILE_ATTRIBUTE_REPARSE_POINT`, and the reparse-tag bit Microsoft sets on
#: tags that redirect to another name (symlinks, junctions, mount points).
_REPARSE_POINT = 0x400
_NAME_SURROGATE = 0x20000000


def _is_directory_link(path: str) -> bool:
    """A symlink or junction: a directory entry that is really another name.

    Judged from `lstat`, so the link itself is examined rather than its
    target. On Windows that means the reparse-point attribute, because
    `os.path.islink` is False for a junction. Only name-surrogate reparse
    points count: a cloud-storage placeholder is also a reparse point, but it
    is the real folder, and skipping it would drop real data.
    """
    try:
        info = os.lstat(path)
    except OSError:
        # Not ours to judge: the walk will try to list it and report the
        # failure through `onerror` if it cannot.
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes & _REPARSE_POINT:
        tag = getattr(info, "st_reparse_tag", 0)
        return bool(tag & _NAME_SURROGATE) if tag else True
    return stat_mod.S_ISLNK(info.st_mode)


def scan(root: Path, excludes: Iterable[str] = DEFAULT_EXCLUDES, *,
         errors: list[ScanProblem] | None = None,
         links: list[Path] | None = None) -> list[Path]:
    """Every file under `root`, sorted, with junk filtered out.

    A directory that cannot be listed is appended to `errors` rather than
    dropped. `os.walk` swallows listing errors by default, and the files under
    such a directory would then be absent from the job, its byte total and its
    manifests, with the job reading Verified. A caller that passes no list
    gets the old silent behaviour, so `run()` always passes one.

    Directory symlinks and junctions are never entered, and each one skipped
    is appended to `links` so the caller can say so. Following them is how a
    junction pointing at its own parent walks forever, and how an alias
    listed before its target (`A_alias -> Z_real`) used to claim the real
    directory under the alias name. Not entering any of them means the real
    directory is always scanned under its real name. The root is entered even
    when it is itself a link: that is the folder the operator chose.

    The walk runs on the extended-length form of the root (see `longpath`),
    so a directory whose name Win32 would trim, or one past MAX_PATH, is
    listed as itself. The paths returned are in the form `root` was given in.
    """
    patterns = tuple(excludes)
    found: list[Path] = []
    visited: set[str] = set()
    root = Path(root)
    walk_root = longpath.literal(root)

    def plain(walked: str) -> Path:
        # os.walk joins names onto the top it was given, so every path it
        # reports starts with `walk_root`.
        if walked.startswith(walk_root):
            tail = walked[len(walk_root):].lstrip("\\/")
            return root / tail if tail else root
        return Path(longpath.shorten_for_display(walked))

    def already_seen(walked: str) -> bool:
        try:
            real = os.path.normcase(os.path.realpath(walked))
        except OSError:
            # Enter it: if it really cannot be read, onerror records that.
            return False
        if real in visited:
            return True
        visited.add(real)
        return False

    def onerror(exc: OSError) -> None:
        if errors is None:
            return
        where = plain(str(exc.filename)) if exc.filename else root
        errors.append(ScanProblem(where, exc.strerror or str(exc)))

    visited.add(os.path.normcase(os.path.realpath(walk_root)))
    for dirpath, dirnames, filenames in os.walk(walk_root, onerror=onerror):
        here = plain(dirpath)
        keep: list[str] = []
        for name in sorted(dirnames):
            if is_excluded(here / name, patterns):
                continue
            walked = os.path.join(dirpath, name)
            if _is_directory_link(walked):
                if links is not None:
                    links.append(here / name)
                continue
            if already_seen(walked):
                continue
            keep.append(name)
        dirnames[:] = keep
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
        # Resolved in extended form, so a name Win32 would rewrite is compared
        # as itself and not as whatever its trimmed namesake resolves to.
        if (Path(longpath.extended(target)).resolve()
                == Path(longpath.extended(source)).resolve()):
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


def _confirm_source(source: Path, expected: str, algorithm: str,
                    control: JobControl | None = None) -> bool:
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

    With `control`, the reread honours pause and cancel at every chunk. A
    cancel raises `JobCancelled` out of here, so the caller never promotes the
    file on the strength of a confirmation that did not finish.
    """
    evicted = integrity.evict_from_cache(source)
    again = hash_file(source, algorithm,
                      checkpoint=control.checkpoint if control else None)
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
    """
    source_root = Path(source_root)
    if not source_root.exists():
        raise FileNotFoundError(f"source not found: {source_root}")

    algorithm = get_algorithm(options.algorithm)
    dest_roots = [Path(d) for d in options.destinations]
    if not dest_roots:
        raise ValueError("at least one destination is required")
    assert_safe_destinations(source_root, dest_roots)

    scan_errors: list[ScanProblem] = []
    links: list[Path] = []
    files = scan(source_root, options.excludes, errors=scan_errors, links=links)
    assert_no_destination_collisions(files, source_root, dest_roots,
                                     options.preserve_structure)
    counters = _Counters(job_bytes_total=sum(_size_or_zero(p) for p in files))

    host = sysinfo.collect()
    job = Job(
        # A card offloaded from its root has no folder name; its volume
        # label is what the operator calls it.
        name=(options.job_name or source_root.name
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
        scan_errors=scan_errors,
        preserve_structure=options.preserve_structure,
    )

    for problem in scan_errors:
        counters.errors.append(problem.describe())
    for link in links:
        job.warnings.append(
            f"{link} is a symbolic link or junction and was not followed, so "
            "nothing under it was copied; offload its target directly if it "
            "is needed")

    job.report_dir_name = choose_report_folder(
        job.name, files, source_root, dest_roots, options.preserve_structure)
    if job.report_dir_name != f"{job.name}_Reports":
        job.warnings.append(
            f"{job.name}_Reports already exists at a destination or is part of "
            f"the source, so this job's reports are in {job.report_dir_name} "
            "instead; nothing already there was overwritten")

    thumb_dir = options.thumbnail_dir or (job.report_dir_for(dest_roots[0]) / "thumbs")

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

    for index, source in enumerate(files):
        # Between files is the cheapest place to honour a pause or cancel.
        if control is not None:
            try:
                control.checkpoint()
            except JobCancelled:
                job.cancelled = True
                break

        targets = [
            _destination_for(source, source_root, root, options.preserve_structure)
            for root in dest_roots
        ]

        try:
            stat = longpath.stat(source)
        except OSError as exc:
            # Deleted, renamed or dropped by the card since the scan. One
            # file's disappearance costs that file, recorded as such, not the
            # rest of the card and the paperwork for what did copy.
            reason = ("source vanished after scan"
                      if isinstance(exc, FileNotFoundError)
                      else f"source could not be read: {exc}")
            entry = FileEntry(source=source, source_root=source_root, size=0,
                              created=0.0, modified=0.0,
                              companion_of=belongs_to.get(source),
                              companions=owns.get(source, []))
            counters.errors.append(f"{source}: {reason}")
            for root, target in zip(dest_roots, targets, strict=True):
                entry.destinations.append(
                    Destination(root=root, path=target,
                                status=FileStatus.FAILED, error=reason))
            job.files.append(entry)
            continue

        entry = FileEntry(
            source=source,
            source_root=source_root,
            size=stat.st_size,
            created=getattr(stat, "st_birthtime", stat.st_ctime),
            modified=stat.st_mtime,
            companion_of=belongs_to.get(source),
            companions=owns.get(source, []),
        )

        emit(ProgressEvent(index, len(files), source.name, "copy",
                           0, stat.st_size,
                           counters.job_bytes_done, counters.job_bytes_total))

        if options.skip_existing and all(
            _size_or_none(t) == stat.st_size for t in targets
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
                # The checkpoint is here so a cancel during the reread stops
                # the job before promotion instead of being ignored until the
                # file is already in place and reported Verified.
                evicted = _confirm_source(_src, result.source_checksum,
                                          options.algorithm, control)
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
        cancelled_in_verify = False
        for position, (root, target, partial, dst_sum) in enumerate(
                zip(dest_roots, targets, partials, dst_sums, strict=True)):
            destination = Destination(root=root, path=target, checksum=dst_sum or None)

            # Mirror source timestamps so the destination reads as an archival
            # copy, not a fresh file.
            try:
                longpath.copystat(source, partial)
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
                        # The checkpoint lets a cancel during the reread stop
                        # the job before promotion. JobCancelled is not an
                        # OSError, so retry_mod.call lets it propagate.
                        dst_sum, verify_attempts = retry_mod.call(
                            lambda p=partial: hash_file(
                                p, options.algorithm,
                                checkpoint=control.checkpoint if control else None),
                            options.retry,
                        )
                        if verify_attempts > 1:
                            job.warnings.append(
                                f"{target.name} verified on attempt "
                                f"{verify_attempts} — the destination may be "
                                "failing")
                        destination.checksum = dst_sum or None
                    except JobCancelled:
                        # Nothing unpromoted survives. Destinations already
                        # promoted were verified and stay; their partials
                        # are gone, so discarding the whole list only
                        # removes this one and the ones not yet reached.
                        _discard(partials)
                        # Record this copy and every later one as cancelled.
                        # The entry's status is its weakest copy, and a copy
                        # that was never made must be the weakest, or the
                        # file would read as Verified on the strength of the
                        # destinations that did finish.
                        for later_root, later_target in zip(
                                dest_roots[position:], targets[position:],
                                strict=True):
                            entry.destinations.append(Destination(
                                root=later_root, path=later_target,
                                status=FileStatus.CANCELLED,
                                error="cancelled before verification"))
                        cancelled_in_verify = True
                        break
                    except OSError as exc:
                        destination.status = FileStatus.FAILED
                        destination.error = str(exc)
                        counters.errors.append(f"{target}: {exc}")
                        _discard([partial])
                        entry.destinations.append(destination)
                        continue

                size_ok = _size_or_none(partial) == stat.st_size
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

            if not options.replace_existing and longpath.exists(target):
                # Staging protects an existing file from a copy that fails. This
                # protects it from one that succeeds: a different card with the
                # same folder layout would otherwise replace the first card's
                # verified copy, and that card may already be formatted.
                try:
                    refusal = _existing_file_refusal(
                        target, partial, src_sum, options.algorithm, control)
                except JobCancelled:
                    _discard(partials)
                    cancelled_in_verify = True
                    break
                if refusal is not None:
                    destination.status = FileStatus.FAILED
                    destination.error = refusal
                    counters.errors.append(f"{target}: {refusal}")
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
                dst_stat = longpath.stat(target)
                destination.created = getattr(dst_stat, "st_birthtime", dst_stat.st_ctime)
                destination.modified = dst_stat.st_mtime
            except OSError:
                pass

            entry.destinations.append(destination)

        if cancelled_in_verify:
            job.cancelled = True
            job.files.append(entry)
            break

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
                        source, source_root)
                    if used_proxy:
                        entry.thumbnail_source = picture

                entry.thumbnails = thumbs.extract(
                    picture, entry.media, thumb_dir, options.thumbnail_count,
                    memo=thumb_memo, key=entry.relative.as_posix(),
                )

        job.files.append(entry)

    job.finished = _dt.datetime.now()
    if job.cancelled:
        attempted = {entry.source for entry in job.files}
        # Kept so the manifests can list them unhashed: a cancelled job's
        # manifest must not read as covering the whole card.
        job.not_attempted = [path for path in files if path not in attempted]
        not_attempted = len(files) - len(job.files)
        if not_attempted > 0:
            counters.errors.append(
                f"cancelled — {not_attempted} file(s) not attempted")
    _warn_on_split_companions(job)
    job.notes = "; ".join(counters.errors)
    return job


def _size_or_none(path: Path) -> int | None:
    """The size of whatever is at `path`, or None when nothing readable is."""
    try:
        return longpath.stat(path).st_size
    except (OSError, ValueError):
        return None


def _size_or_zero(path: Path) -> int:
    """For the up-front byte total only. A file that cannot be sized here is
    failed, with its reason, when the copy loop reaches it."""
    return _size_or_none(path) or 0


def choose_report_folder(name: str, files: Sequence[Path], source_root: Path,
                         dest_roots: Sequence[Path], preserve_structure: bool) -> str:
    """`<name>_Reports`, or `<name>_Reports-2` and up when that is taken.

    Taken means it already exists in any destination root, or something being
    copied lands on it. Re-offloading a previous destination (a shuttle drive
    holding `A001/A001_Reports/`) copies that folder, and writing new reports
    over it would replace files this job had just certified, the shuttle's own
    manifest among them. An existing folder from an earlier run is left alone
    for the same reason: its manifest covers files a rerun may skip.
    """
    claimed: set[str] = set()
    for source in files:
        if preserve_structure:
            try:
                first = source.relative_to(source_root).parts[0]
            except (ValueError, IndexError):
                first = source.name
        else:
            first = source.name
        claimed.add(os.path.normcase(first))

    base = f"{name}_Reports"
    candidate, number = base, 1
    while (os.path.normcase(candidate) in claimed
           or any(longpath.exists(Path(root) / candidate) for root in dest_roots)):
        number += 1
        candidate = f"{base}-{number}"
    return candidate


def _existing_file_refusal(target: Path, partial: Path, source_checksum: str,
                           algorithm: str,
                           control: JobControl | None) -> str | None:
    """Why the finished copy at `partial` must not replace `target`, or None.

    Identical content may be replaced: that changes nothing on disk, and it is
    what a re-offload of the same card does. Anything else, or anything that
    cannot be read to find out, is refused.
    """
    advice = "use --replace-existing to overwrite it"
    try:
        if longpath.stat(target).st_size != longpath.stat(partial).st_size:
            return ("an existing file with different content is already there; "
                    + advice)
        checker = get_algorithm(algorithm)
        checkpoint = control.checkpoint if control else None
        if source_checksum and checker.factory is not None:
            existing = hash_file(target, algorithm, checkpoint=checkpoint)
            same = checker.digests_match(existing, source_checksum)
        else:
            same = _same_bytes(target, partial, checkpoint)
    except OSError as exc:
        return (f"an existing file is already there and could not be read to "
                f"compare ({exc}); {advice}")
    if same:
        return None
    return "an existing file with different content is already there; " + advice


def _same_bytes(left: Path, right: Path,
                checkpoint: Callable[[], None] | None) -> bool:
    """Byte-for-byte comparison, for when there is no checksum to compare."""
    with longpath.open_binary(left, "rb") as one, longpath.open_binary(right, "rb") as two:
        while True:
            if checkpoint is not None:
                checkpoint()
            a, b = one.read(CHUNK_SIZE), two.read(CHUNK_SIZE)
            if a != b:
                return False
            if not a:
                return True


def rescan(source_root: Path, destination_roots: Sequence[Path],
           options: OffloadOptions,
           progress: ProgressCallback | None = None) -> Job:
    """Build a Job from an already-offloaded tree without copying anything.

    This is what `offloader report` uses: it re-hashes and re-probes in place so
    a report can be regenerated (or a delivery audited) after the fact.
    """
    source_root = Path(source_root)
    scan_errors: list[ScanProblem] = []
    links: list[Path] = []
    files = scan(source_root, options.excludes, errors=scan_errors, links=links)
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
        scan_errors=scan_errors,
        preserve_structure=options.preserve_structure,
    )
    job.warnings.extend(
        f"{link} is a symbolic link or junction and was not followed"
        for link in links)
    thumb_dir = options.thumbnail_dir or (source_root / f"{job.name}_Reports" / "thumbs")
    thumb_memo = thumbs.DecoderMemo()
    total = sum(_size_or_zero(p) for p in files)
    done = 0

    # A sidecar belongs to a *clip*. Under the data profile nothing is a clip,
    # so stem-matching a dataset would announce that `run_1440.xmp` belongs to
    # `run_1440.h5` on no evidence beyond a shared name.
    belongs_to = (companions.group(files) if options.profile.probes_media
                  else {})
    owns = _invert_companions(belongs_to)

    for index, source in enumerate(files):
        stat = longpath.stat(source)
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
            exists = longpath.exists(target)
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
                        memo=thumb_memo, key=entry.relative.as_posix(),
                    )
            except Exception as exc:            # noqa: BLE001
                entry.media = MediaInfo()
                job.warnings.append(
                    f"{source.name}: metadata could not be read ({exc})")
        job.files.append(entry)

    job.finished = _dt.datetime.now()
    return job
