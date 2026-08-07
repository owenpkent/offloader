"""Formatting helpers.

All of these reproduce the exact string forms used by ShotPut Pro's PDF report,
which is why the units are decimal (SI) rather than binary: the reference report
renders 258_758_961 bytes as "258.8 MB", not "246.8 MB".
"""

from __future__ import annotations

import datetime as _dt

def format_size(num_bytes: int) -> str:
    """Format a byte count with the report's variable precision.

    The reference uses 1 decimal for MB values ("258.8 MB", "8.6 MB") and
    2 decimals for GB values ("27.8 GB", "483.11 GB"). Reproduce that.

    >>> format_size(258758961)
    '258.8 MB'
    >>> format_size(512)
    '512 bytes'
    """
    if num_bytes < 1000:
        return f"{num_bytes} bytes"
    kb = num_bytes / 1000.0
    if kb < 1000:
        return f"{kb:.1f} KB"
    mb = kb / 1000.0
    if mb < 1000:
        return f"{mb:.1f} MB"
    gb = mb / 1000.0
    if gb < 1000:
        return f"{gb:.2f} GB"
    return f"{gb / 1000.0:.2f} TB"


def format_duration(seconds: float) -> str:
    """Clip duration: "36 sec" under a minute, else "4:11 min"."""
    if seconds < 60:
        return f"{int(round(seconds))} sec"
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d} min"


def format_elapsed(seconds: float) -> str:
    """Job wall-clock: "1:04:00"."""
    total = int(round(seconds))
    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def format_file_datetime(ts: float | _dt.datetime) -> str:
    """File timestamps: "2026 08 4, 12:54" (padded month, unpadded day)."""
    dt = ts if isinstance(ts, _dt.datetime) else _dt.datetime.fromtimestamp(ts)
    return f"{dt.year} {dt.month:02d} {dt.day}, {dt:%H:%M}"


def format_job_datetime(ts: float | _dt.datetime) -> str:
    """Job start/finish: "August 04, 2026-05_02_57"."""
    dt = ts if isinstance(ts, _dt.datetime) else _dt.datetime.fromtimestamp(ts)
    return f"{dt:%B} {dt.day:02d}, {dt.year}-{dt:%H_%M_%S}"


def format_timecode(frames: int, fps: float, drop_frame: bool = False) -> str:
    """Frame count to "HH:MM:SS:FF NDF"/"DF" timecode."""
    rate = int(round(fps)) or 1
    hours, rem = divmod(frames, rate * 3600)
    minutes, rem = divmod(rem, rate * 60)
    secs, fr = divmod(rem, rate)
    tag = "DF" if drop_frame else "NDF"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{fr:02d} {tag}"


def format_fps(fps: float) -> str:
    """"24 FPS" for integral rates, "23.98 FPS" otherwise."""
    if abs(fps - round(fps)) < 0.001:
        return f"{int(round(fps))} FPS"
    return f"{fps:.2f} FPS"


def channel_layout_name(channels: int, layout: str | None = None) -> str:
    """Human name for an audio track: "Stereo", "Mono", "5.1"."""
    if layout and layout not in ("unknown",):
        return {"mono": "Mono", "stereo": "Stereo"}.get(layout, layout)
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(channels, f"{channels} ch")
