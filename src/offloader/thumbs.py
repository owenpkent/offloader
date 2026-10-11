"""Thumbnail extraction via ffmpeg.

The reference report samples N frames spread evenly across the clip and letter/
pillar-boxes each into a fixed 16:9 cell, which is why a 3:2 source shows black
side bars. We reproduce that: ffmpeg scales to fit and pads to the cell.
"""

from __future__ import annotations

import hashlib
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


class DecoderMemo:
    """Remembers, per file suffix, that this ffmpeg produced no frames.

    A decoder ffmpeg lacks — BRAW without the Blackmagic SDK is the common
    case — fails identically for every clip, and paying four doomed process
    spawns per clip is a real cost on a several-hundred-clip card. One memo
    lives for one job: the first clip of a suffix pays the probe, the rest
    skip. Scoped to the job rather than the process so a swapped-in ffmpeg
    gets a fresh chance, and a single corrupt file can mute at most one
    offload's contact sheet, never the tool's.
    """

    def __init__(self) -> None:
        self._dead: set[str] = set()

    def is_dead(self, source: Path) -> bool:
        return source.suffix.lower() in self._dead

    def record_failure(self, source: Path) -> None:
        self._dead.add(source.suffix.lower())


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


def thumbnail_stem(source: Path, key: str | None = None) -> str:
    """`C001_1a2b3c4d`: the clip's stem plus a stable hash of `key`."""
    digest = hashlib.sha1(
        (key if key is not None else str(source)).encode("utf-8", "surrogatepass")
    ).hexdigest()[:8]
    return f"{Path(source).stem}_{digest}"


def extract(
    source: Path,
    media: MediaInfo,
    out_dir: Path,
    count: int = 4,
    timeout: float = 60.0,
    memo: DecoderMemo | None = None,
    key: str | None = None,
) -> list[Path]:
    """Grab `count` thumbnails. Returns [] if ffmpeg is missing, the file has
    no video stream, or extraction fails — thumbnails are never load-bearing.

    With a `memo`, a suffix whose every extraction failed is skipped for the
    rest of that memo's lifetime instead of re-spawning ffmpeg per clip.

    `key` names the clip, normally its path relative to the source root.
    It goes into the filenames, so two clips with one stem in different
    folders (A/C001.mov and B/C001.mov) cannot overwrite each other's
    frames and put the wrong picture beside a verified clip."""
    exe = ffmpeg_path()
    if exe is None or not media.is_video or not media.duration_sec:
        return []
    if memo is not None and memo.is_dead(source):
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = thumbnail_stem(source, key)
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

    if memo is not None and not results:
        # Every sample offset failed after real attempts (ffmpeg present, a
        # video stream, a duration). The overwhelmingly likely cause is a
        # decoder this ffmpeg does not have, which the next clip has too.
        memo.record_failure(source)
    return results
