"""CSV/TXT job report — one row per source/destination pair."""

from __future__ import annotations

import csv
from pathlib import Path

from ..models import Job
from ..util import format_file_datetime, format_size

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
    "Error",
]


def write_csv(job: Job, path: Path, *, delimiter: str = ",", **_options) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter=delimiter)
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
