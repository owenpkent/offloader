"""Data model for an offload job.

A `Job` is what the reports render. The engine produces one; `engine.rescan`
(behind the CLI's `report` command) reconstructs one from an already-offloaded
tree, which is what makes the report generator testable without moving bytes.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Profile(str, Enum):
    """What kind of source is being offloaded.

    The verified copy engine is identical for both — every byte is read once,
    checksummed, fanned out and (optionally) read back. The profile only
    decides how much *media-specific* work runs on top of that:

    - `MEDIA` (default): the camera-card workflow. ffprobe metadata, contact-
      sheet thumbnails and the BRAW container check, with camera originals in
      mind.
    - `DATA`: any large one-way transfer — datasets, disk images, backups,
      render output. Skips media probing entirely, so nothing depends on
      ffmpeg and no file is treated as a clip. The copy, the checksums, the
      verification and the MHL/CSV/PDF paperwork are exactly the same.
    """

    MEDIA = "media"
    DATA = "data"

    @property
    def label(self) -> str:
        return {Profile.MEDIA: "Media", Profile.DATA: "Data"}[self]

    @property
    def probes_media(self) -> bool:
        return self is Profile.MEDIA


class VerificationMode(str, Enum):
    """How much re-reading the engine does to prove the copy landed intact."""

    NONE = "none"
    #: Hash the source while reading and hash the bytes as they are written.
    #: Catches corruption in transit but trusts the destination's own cache.
    SOURCE_ONLY = "source-only"
    #: Re-open each destination file and hash it off the platter after the
    #: copy completes. Slower, and the only mode that proves what's on disk.
    FULL = "full"

    @property
    def label(self) -> str:
        return {
            VerificationMode.NONE: "None",
            VerificationMode.SOURCE_ONLY: "(source only)",
            VerificationMode.FULL: "(source and destination)",
        }[self]


class FileStatus(str, Enum):
    VERIFIED = "Verified"
    COPIED = "Copied"
    FAILED = "Failed"
    SKIPPED = "Skipped"
    CANCELLED = "Cancelled"


#: Worst-to-best. `FileEntry.status` reports the weakest copy, so a file is
#: never described more favourably than its least reliable destination.
_STATUS_ORDER = (
    FileStatus.FAILED,
    FileStatus.CANCELLED,
    FileStatus.SKIPPED,
    FileStatus.COPIED,
    FileStatus.VERIFIED,
)


@dataclass
class AudioTrack:
    channels: int = 2
    layout: str = "stereo"
    codec: str = "LINEAR PCM"
    bit_rate_kbps: float | None = None
    sample_rate_hz: int | None = None


@dataclass
class CameraInfo:
    """What the camera recorded about itself.

    Populated for formats that carry it in-container — BRAW today — and left
    empty otherwise, so reports can show a camera line only when there is one.
    """

    model: str | None = None            # "Blackmagic PYXIS 6K"
    lens: str | None = None
    reel: str | None = None
    scene: str | None = None
    take: str | None = None
    good_take: bool | None = None
    camera_number: str | None = None
    compression: str | None = None      # "8:1"
    colour_science: str | None = None   # "Gen 5"
    lut: str | None = None
    firmware: str | None = None
    serial: str | None = None

    def slate(self) -> str | None:
        parts = [f"Reel {self.reel}" if self.reel else None,
                 f"Scene {self.scene}" if self.scene else None,
                 f"Take {self.take}" if self.take else None]
        present = [p for p in parts if p]
        return " · ".join(present) if present else None

    def summary(self) -> str | None:
        bits = [b for b in (self.model, self.lens) if b]
        return "   ".join(bits) if bits else None

    def __bool__(self) -> bool:
        return any((self.model, self.lens, self.reel, self.scene, self.take))


@dataclass
class MediaInfo:
    """Everything ffprobe told us about a media file. All fields optional:
    non-media files carry an empty MediaInfo and render without a metadata
    block."""

    container: str | None = None          # "QuickTime"
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None        # "H264/AVC"
    fps: float | None = None
    duration_sec: float | None = None
    frame_count: int | None = None
    timecode: str | None = None           # "12:54:38:12 NDF"
    audio_tracks: list[AudioTrack] = field(default_factory=list)
    camera: CameraInfo = field(default_factory=CameraInfo)

    @property
    def is_video(self) -> bool:
        return self.width is not None and self.height is not None


@dataclass
class Destination:
    """One copy of a source file, plus the verdict on that copy."""

    root: Path
    path: Path
    status: FileStatus = FileStatus.COPIED
    checksum: str | None = None
    created: float | None = None
    modified: float | None = None
    error: str | None = None


@dataclass
class FileEntry:
    """A single source file and every destination it was written to."""

    source: Path
    source_root: Path
    size: int
    created: float
    modified: float
    checksum: str | None = None
    media: MediaInfo = field(default_factory=MediaInfo)
    thumbnails: list[Path] = field(default_factory=list)
    #: Set when the contact sheet came from a matching proxy because the
    #: original could not be decoded.
    thumbnail_source: Path | None = None
    destinations: list[Destination] = field(default_factory=list)
    #: The clip this file belongs to, when it is a sidecar or a proxy rather
    #: than a take in its own right.
    companion_of: Path | None = None
    #: The sidecars and proxies that belong to this clip.
    companions: list[Path] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.source.name

    @property
    def relative(self) -> Path:
        try:
            return self.source.relative_to(self.source_root)
        except ValueError:
            return Path(self.source.name)

    @property
    def status(self) -> FileStatus:
        """Worst status across destinations — a file is only as good as its
        weakest copy."""
        if not self.destinations:
            return FileStatus.SKIPPED
        return min((d.status for d in self.destinations), key=_STATUS_ORDER.index)

    @property
    def is_video(self) -> bool:
        return self.media.is_video


@dataclass
class ScanProblem:
    """A directory under the source that the scan could not list."""

    path: Path
    error: str

    def describe(self) -> str:
        return f"could not list {self.path}: {self.error}"


@dataclass
class ManifestRow:
    """One line of a manifest for one destination. See `Job.manifest_rows`."""

    #: Where the file is, or should be, at that destination.
    path: Path
    size: int | None
    modified: float | None
    #: The checksum to verify against. None means there is nothing to verify
    #: against, which a verifier must treat as a failure, never as a pass.
    checksum: str | None
    #: This destination did not receive a good copy.
    failed: bool


@dataclass
class Job:
    """A complete offload, ready to render."""

    name: str
    source_root: Path
    destination_roots: list[Path]
    verification: VerificationMode = VerificationMode.SOURCE_ONLY
    #: Which workflow produced this job. `DATA` means no media metadata was
    #: collected, so reports show a plain file listing rather than clip rows.
    profile: Profile = Profile.MEDIA
    hash_label: str = "XXHash3-64"
    started: _dt.datetime = field(default_factory=_dt.datetime.now)
    finished: _dt.datetime | None = None
    files: list[FileEntry] = field(default_factory=list)
    os_version: str = ""
    processors: int = 0
    system_ram: str = ""
    notes: str = ""
    cancelled: bool = False
    #: Every source file was read a second time and the two reads compared.
    #: Recorded because it changes what "Verified" is worth, and a delivery
    #: should be able to say which one it got.
    paranoid: bool = False
    #: Things that did not fail the job but that a human should see before
    #: erasing a card — empty files, verifications that may have been served
    #: from cache.
    warnings: list[str] = field(default_factory=list)
    #: Directories under the source that could not be listed. Every file in
    #: one is unknown, so the job cannot be Verified: it reads Failed, and the
    #: manifests carry an unhashed entry for each so `verify` fails as well.
    scan_errors: list[ScanProblem] = field(default_factory=list)
    #: Source files the scan found but the job never reached, because it was
    #: cancelled first. Recorded so the manifests can list them unhashed
    #: instead of reading as complete.
    not_attempted: list[Path] = field(default_factory=list)
    #: Whether the source tree was recreated under each destination root.
    #: Needed to say where a file that never arrived *would* have been.
    preserve_structure: bool = True
    #: The folder, inside each destination root, that holds this job's
    #: paperwork. Empty means the conventional `<name>_Reports`; the engine
    #: picks `<name>_Reports-2` and up when that one already exists or is
    #: part of what is being copied.
    report_dir_name: str = ""
    #: Every report or manifest that could not be written, one line each.
    #: Never changes a file's status, since the copies are what they are, but
    #: it keeps the job from reading as a plain "Verified": a later `verify`
    #: depends on the manifest that is missing.
    report_failures: list[str] = field(default_factory=list)

    @property
    def reports_folder(self) -> str:
        """The paperwork folder's name inside a destination root."""
        return self.report_dir_name or f"{self.name}_Reports"

    def report_dir_for(self, root: Path) -> Path:
        return Path(root) / self.reports_folder

    def manifest_rows(self, destination_index: int = 0) -> list[ManifestRow]:
        """What a manifest for one destination has to say, file by file.

        Every scanned file the job touched is listed for that destination,
        not only the ones that arrived: a manifest that leaves out a failed
        copy reads as complete, and `verify` then passes a destination that
        is missing a clip. A failed copy is listed with the *source* checksum
        when one was taken (so verify finds it missing or different), and
        unhashed otherwise (so verify reports it as having no checksum, which
        fails). Files the job never reached and directories it could not list
        are listed unhashed for the same reason.

        Skipped files are left out. They were not checksummed this run, so a
        manifest entry would certify a file nobody read; an earlier manifest
        that did read them is never overwritten.
        """
        rows: list[ManifestRow] = []
        roots = self.destination_roots
        root = roots[destination_index] if destination_index < len(roots) else None

        def expected_path(relative: Path, name: str) -> Path | None:
            if root is None:
                return None
            return root / relative if self.preserve_structure else root / name

        for entry in self.files:
            if destination_index < len(entry.destinations):
                destination = entry.destinations[destination_index]
                if destination.status is FileStatus.SKIPPED:
                    continue
                if destination.status in (FileStatus.FAILED, FileStatus.CANCELLED):
                    # Never the destination's own checksum here: for a failed
                    # verification that is the hash of the bad bytes.
                    rows.append(ManifestRow(destination.path, entry.size,
                                            entry.modified, entry.checksum, True))
                else:
                    rows.append(ManifestRow(destination.path, entry.size,
                                            entry.modified,
                                            destination.checksum or entry.checksum,
                                            False))
                continue
            if not entry.destinations or root is None:
                # A job assembled without destination records (a library
                # caller, a test) describes the source itself, as it always has.
                if entry.checksum:
                    rows.append(ManifestRow(entry.source, entry.size,
                                            entry.modified, entry.checksum, False))
                continue
            # Records for other destinations but not this one: a cancel landed
            # before it was reached, so the file is not there.
            first = entry.destinations[0]
            try:
                relative = Path(first.path).relative_to(first.root)
            except ValueError:
                relative = entry.relative
            rows.append(ManifestRow(root / relative, entry.size, entry.modified,
                                    entry.checksum, True))

        for source in self.not_attempted:
            try:
                relative = source.relative_to(self.source_root)
            except ValueError:
                relative = Path(source.name)
            path = expected_path(relative, source.name)
            if path is not None:
                rows.append(ManifestRow(path, None, None, None, True))

        for problem in self.scan_errors:
            try:
                relative = problem.path.relative_to(self.source_root)
            except ValueError:
                relative = Path(problem.path.name)
            # Recorded under the destination layout even when flattening, as a
            # marker: nothing from inside it reached any destination.
            if root is not None:
                rows.append(ManifestRow(root / relative, None, None, None, True))
        return rows

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def total_files(self) -> int:
        return len(self.files)

    @property
    def video_files(self) -> int:
        return sum(1 for f in self.files if f.is_video)

    @property
    def elapsed_sec(self) -> float:
        if self.finished is None:
            return 0.0
        return (self.finished - self.started).total_seconds()

    @property
    def copy_status(self) -> str:
        """The verdict on the copies alone, before any paperwork."""
        if self.scan_errors or any(f.status is FileStatus.FAILED for f in self.files):
            return "Failed"
        if self.cancelled:
            return "Cancelled"
        if self.verification is VerificationMode.NONE:
            return "Copied"
        if all(f.status is FileStatus.VERIFIED for f in self.files) and self.files:
            return "Verified"
        return "Incomplete"

    @property
    def final_status(self) -> str:
        status = self.copy_status
        if self.report_failures and status in ("Verified", "Copied"):
            # The copies are good, but the manifest a later `verify` needs may
            # be the thing that is missing. Not a plain success.
            return f"{status}, reports failed"
        return status

    @property
    def verification_label(self) -> str:
        """"XXHash3-64 Checksum(source only)" — matches the reference header."""
        if self.verification is VerificationMode.NONE:
            return "None"
        return f"{self.hash_label} Checksum{self.verification.label}"
