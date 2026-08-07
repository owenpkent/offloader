"""Data model for an offload job.

A `Job` is what the reports render. The engine produces one; the CLI's
`--rescan` path can also reconstruct one from an already-offloaded tree, which
is what makes the report generator testable without moving bytes.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


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
class Job:
    """A complete offload, ready to render."""

    name: str
    source_root: Path
    destination_roots: list[Path]
    verification: VerificationMode = VerificationMode.SOURCE_ONLY
    hash_label: str = "XXHash3-64"
    started: _dt.datetime = field(default_factory=_dt.datetime.now)
    finished: _dt.datetime | None = None
    files: list[FileEntry] = field(default_factory=list)
    os_version: str = ""
    processors: int = 0
    system_ram: str = ""
    notes: str = ""
    cancelled: bool = False
    #: Things that did not fail the job but that a human should see before
    #: erasing a card — empty files, verifications that may have been served
    #: from cache.
    warnings: list[str] = field(default_factory=list)

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
    def final_status(self) -> str:
        if any(f.status is FileStatus.FAILED for f in self.files):
            return "Failed"
        if self.cancelled:
            return "Cancelled"
        if self.verification is VerificationMode.NONE:
            return "Copied"
        if all(f.status is FileStatus.VERIFIED for f in self.files) and self.files:
            return "Verified"
        return "Incomplete"

    @property
    def verification_label(self) -> str:
        """"XXHash3-64 Checksum(source only)" — matches the reference header."""
        if self.verification is VerificationMode.NONE:
            return "None"
        return f"{self.hash_label} Checksum{self.verification.label}"
