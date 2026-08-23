"""CSV/TXT job report — one row per source/destination pair."""

from __future__ import annotations

import csv
from pathlib import Path

from ..models import Job
from ..util import format_file_datetime, format_size, xml_safe


class _SafeWriter:
    """A csv.writer that cannot be killed by a filename.

    The underlying stream is UTF-8 with the default strict error handling, so
    a name carrying a lone surrogate raises UnicodeEncodeError mid-row and
    takes the whole report with it. Filtering here covers every column at
    once, and matches what the MHL writer has always done.
    """

    def __init__(self, writer) -> None:
        self._writer = writer

    def writerow(self, values) -> None:
        self._writer.writerow(
            [xml_safe(v) if isinstance(v, str) else v for v in values])

COLUMNS = [
    "File Name",
    "Relative Path",
    "Size (bytes)",
    "Size",
    "Checksum Type",
    "Source Checksum",
    "Source Path",
    "Destination",
    "Destination Path",
    "Destination Checksum",
    "Status",
    "Created",
    "Modified",
    "Container",
    "Resolution",
    "Video Codec",
    "FPS",
    "Duration (sec)",
    "Frames",
    "Timecode",
    "Audio Codec",
    "Audio Channels",
    "Sample Rate (Hz)",
    "Bit Depth",
    "Camera",
    "Lens",
    "Reel",
    "Scene",
    "Take",
    "Good Take",
    "Colour Science",
    "Error",
]


def _audio_columns(media) -> list:
    """Codec, channels, sample rate and bit depth of the first audio track."""
    if not media.audio_tracks:
        return ["", "", "", ""]
    track = media.audio_tracks[0]
    return [
        track.codec or "",
        track.channels or "",
        track.sample_rate_hz or "",
        track.bit_depth or "",
    ]


def write_csv(job: Job, path: Path, *, delimiter: str = ",", **_options) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = _SafeWriter(csv.writer(handle, delimiter=delimiter))
        writer.writerow([f"# {job.name}"])
        writer.writerow([
            f"# Source: {job.source_root}",
            f"Files: {job.total_files}",
            f"Size: {format_size(job.total_bytes)}",
            f"Verification: {job.verification_label}",
            f"Status: {job.final_status}",
        ])
        writer.writerow(COLUMNS)

        for entry in job.files:
            media = entry.media
            base = [
                entry.name,
                str(entry.relative),
                entry.size,
                format_size(entry.size),
                job.hash_label,
                entry.checksum or "",
                str(entry.source),
            ]
            tail = [
                media.container or "",
                f"{media.width}x{media.height}" if media.is_video else "",
                media.video_codec or "",
                f"{media.fps:.3f}" if media.fps else "",
                f"{media.duration_sec:.3f}" if media.duration_sec else "",
                media.frame_count or "",
                media.timecode or "",
                # The first track speaks for the file: a sound card's rows are
                # one track each, and a clip's extra tracks share its format.
                *_audio_columns(media),
                media.camera.model or "",
                media.camera.lens or "",
                media.camera.reel or "",
                media.camera.scene or "",
                media.camera.take or "",
                ("yes" if media.camera.good_take else
                 "no" if media.camera.good_take is False else ""),
                media.camera.colour_science or "",
            ]

            if not entry.destinations:
                writer.writerow(base + ["", "", "", "Skipped",
                                        format_file_datetime(entry.created),
                                        format_file_datetime(entry.modified)]
                                + tail + [""])
                continue

            for number, destination in enumerate(entry.destinations, start=1):
                writer.writerow(
                    base
                    + [
                        f"Destination {number}",
                        str(destination.path),
                        destination.checksum or "",
                        destination.status.value,
                        format_file_datetime(entry.created),
                        format_file_datetime(entry.modified),
                    ]
                    + tail
                    + [destination.error or ""]
                )
    return path
