"""Thumbnail extraction via ffmpeg.

The reference report samples N frames spread evenly across the clip and letter/
pillar-boxes each into a fixed 16:9 cell, which is why a 3:2 source shows black
side bars. We reproduce that: ffmpeg scales to fit and pads to the cell.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .models import MediaInfo

#: Cell size in pixels. The PDF draws each cell at 137.6 x 78 pt, so 276 x 156
#: is 2x for crisp output at print resolution.
CELL_WIDTH = 276
CELL_HEIGHT = 156


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _sample_offsets(duration: float, count: int) -> list[float]:
    """Evenly spaced sample points, biased off the very start and end so we
    don't grab slates or black frames."""
    if count <= 0 or duration <= 0:
        return []
    if count == 1:
        return [duration * 0.5]
    span = duration * 0.9
    start = duration * 0.05
    return [start + span * i / (count - 1) for i in range(count)]


def extract(
    source: Path,
    media: MediaInfo,
    out_dir: Path,
    count: int = 4,
    timeout: float = 60.0,
) -> list[Path]:
    """Grab `count` thumbnails. Returns [] if ffmpeg is missing, the file has
    no video stream, or extraction fails — thumbnails are never load-bearing."""
    exe = ffmpeg_path()
    if exe is None or not media.is_video or not media.duration_sec:
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = source.stem
    results: list[Path] = []

    vf = (
        f"scale={CELL_WIDTH}:{CELL_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={CELL_WIDTH}:{CELL_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black"
    )

    for index, offset in enumerate(_sample_offsets(media.duration_sec, count)):
        target = out_dir / f"{stem}_thumb{index + 1:02d}.jpg"
        try:
            proc = subprocess.run(
                [
                    exe, "-nostdin", "-v", "error", "-y",
                    # -ss before -i seeks by keyframe: fast, and accurate
                    # enough for a contact sheet.
                    "-ss", f"{offset:.3f}",
                    "-i", str(source),
                    "-frames:v", "1",
                    "-vf", vf,
                    "-q:v", "3",
                    str(target),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (subprocess.SubprocessError, OSError):
            break
        if proc.returncode == 0 and target.exists() and target.stat().st_size > 0:
            results.append(target)

    return results
