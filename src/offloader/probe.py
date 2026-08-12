"""Media metadata via ffprobe.

Codec and container names are mapped to the display forms the reference report
uses ("H264/AVC", "QuickTime", "LINEAR PCM") rather than ffmpeg's internal ids.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

from .models import AudioTrack, CameraInfo, MediaInfo
from .util import format_timecode

#: Extensions we bother probing. Everything else is treated as a data file and
#: listed without a metadata block, exactly as the reference does for sidecars.
MEDIA_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mxf", ".avi", ".mkv", ".mts", ".m2ts", ".mpg",
    ".mpeg", ".r3d", ".braw", ".ari", ".arx", ".dng", ".cine", ".crm", ".insv",
    ".wav", ".aif", ".aiff", ".mp3", ".bwf",
}

_VIDEO_CODECS = {
    "h264": "H264/AVC",
    "hevc": "HEVC/H265",
    "prores": "Apple ProRes",
    "dnxhd": "DNxHD",
    "mjpeg": "Motion JPEG",
    "mpeg2video": "MPEG-2",
    "vp9": "VP9",
    "av1": "AV1",
    "cfhd": "CineForm",
}

_AUDIO_CODECS = {
    "pcm_s16le": "LINEAR PCM",
    "pcm_s24le": "LINEAR PCM",
    "pcm_s32le": "LINEAR PCM",
    "pcm_f32le": "LINEAR PCM",
    "aac": "AAC",
    "mp3": "MP3",
    "ac3": "AC-3",
    "flac": "FLAC",
}

_CONTAINERS = {
    "mov,mp4,m4a,3gp,3g2,mj2": "QuickTime",
    "matroska,webm": "Matroska",
    "mpegts": "MPEG-TS",
    "mxf": "MXF",
    "avi": "AVI",
    "wav": "WAVE",
}


class FFprobeUnavailable(RuntimeError):
    """ffprobe is not on PATH."""


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def _parse_rate(value: str | None) -> float | None:
    """ffprobe rates arrive as "24/1" or "24000/1001".

    A frame rate that is not a finite positive number is not a frame rate.
    `float()` accepts "inf" and "nan" without complaint, and everything
    downstream calls `round()` on the result, which does not.
    """
    if not value or value in ("0/0", "0"):
        return None
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            den_f = float(den)
            rate = float(num) / den_f if den_f else None
        except (TypeError, ValueError):
            return None
    else:
        try:
            rate = float(value)
        except (TypeError, ValueError):
            return None
    if rate is None or not math.isfinite(rate) or rate <= 0:
        return None
    return rate


def _as_int(value: object) -> int | None:
    """ffprobe writes the literal string "N/A" in numeric fields routinely:
    for codecs that declare no sample rate, for data streams, and for anything
    it only partly decoded. That is ordinary output, not corruption."""
    try:
        return int(value)                        # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    try:
        parsed = float(value)                    # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _tags(obj: object) -> dict:
    """A stream's or format's `tags`, defended against it not being a map."""
    if isinstance(obj, dict):
        tags = obj.get("tags")
        if isinstance(tags, dict):
            return tags
    return {}


def _from_braw(path: Path) -> MediaInfo:
    """BRAW metadata, read straight out of the container.

    ffprobe returns an empty document for BRAW — not an error, nothing at all —
    so this is the only source of resolution, frame rate, camera and lens.
    """
    from . import braw as braw_mod

    info = braw_mod.read_info(path)
    if info is None:
        return MediaInfo(container="Blackmagic RAW")

    # The container line already says "Blackmagic RAW"; the codec slot carries
    # what varies between clips.
    codec = info.compression_ratio or None

    media = MediaInfo(
        container="Blackmagic RAW",
        width=info.width,
        height=info.height,
        video_codec=codec,
        fps=info.fps,
        duration_sec=info.duration_sec,
        frame_count=info.frame_count,
        camera=CameraInfo(
            model=info.camera_type,
            lens=info.lens_type,
            reel=info.reel,
            scene=info.scene,
            take=info.take,
            good_take=info.good_take,
            camera_number=info.camera_number,
            compression=info.compression_ratio,
            colour_science=(f"Gen {info.colour_science_gen}"
                            if info.colour_science_gen else None),
            lut=info.lut_name,
            firmware=info.firmware_version,
            serial=info.camera_id,
        ),
    )
    if media.fps:
        media.timecode = format_timecode(0, media.fps)
    return media


def probe(path: Path, timeout: float = 30.0) -> MediaInfo:
    """Read metadata for one file.

    Returns an empty MediaInfo for non-media files, unreadable files, and when
    ffprobe is missing. A failed probe must never fail the offload, so this
    catches everything rather than only the failures we predicted: metadata is
    a convenience, and no clip is worth abandoning a transfer over.
    """
    try:
        return _probe(path, timeout)
    except Exception:                    # noqa: BLE001 - see the docstring
        return MediaInfo()


def _probe(path: Path, timeout: float) -> MediaInfo:
    path = Path(path)
    if path.suffix.lower() == ".braw":
        return _from_braw(path)

    exe = ffprobe_path()
    if exe is None or path.suffix.lower() not in MEDIA_EXTENSIONS:
        return MediaInfo()

    try:
        proc = subprocess.run(
            [exe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        data = json.loads(proc.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return MediaInfo()

    return _build(data)


def _build(data: dict) -> MediaInfo:
    fmt = data.get("format")
    fmt = fmt if isinstance(fmt, dict) else {}
    # ffprobe is the only thing that should be writing this document, but it is
    # still parsed JSON from a subprocess: entries need not be maps.
    streams = [s for s in data.get("streams", []) if isinstance(s, dict)]
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]

    info = MediaInfo()
    format_name = fmt.get("format_name", "")
    info.container = _CONTAINERS.get(format_name, format_name.upper() or None)

    duration = fmt.get("duration") or (video or {}).get("duration")
    info.duration_sec = _as_float(duration) if duration else None

    if video is not None:
        info.width = video.get("width")
        info.height = video.get("height")
        codec = video.get("codec_name", "")
        info.video_codec = _VIDEO_CODECS.get(codec, codec.upper() or None)
        info.fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(
            video.get("r_frame_rate")
        )

        frames = video.get("nb_frames")
        if frames and str(frames).isdigit() and int(frames) > 0:
            info.frame_count = int(frames)
        elif info.fps and info.duration_sec:
            info.frame_count = int(round(info.duration_sec * info.fps))

        # Timecode lives on the format, the video stream, or a dedicated
        # data stream depending on the camera.
        tc = (
            _tags(fmt).get("timecode")
            or _tags(video).get("timecode")
            or next(
                (_tags(s).get("timecode") for s in streams
                 if _tags(s).get("timecode")),
                None,
            )
        )
        if tc:
            info.timecode = f"{tc} NDF" if ";" not in tc else f"{tc.replace(';', ':')} DF"
        elif info.fps:
            info.timecode = format_timecode(0, info.fps)

    for stream in audios:
        codec = stream.get("codec_name", "")
        bit_rate = _as_float(stream.get("bit_rate"))
        info.audio_tracks.append(
            AudioTrack(
                channels=_as_int(stream.get("channels")) or 0,
                layout=stream.get("channel_layout") or "",
                codec=_AUDIO_CODECS.get(codec, codec.upper() or "Unknown"),
                bit_rate_kbps=bit_rate / 1000.0 if bit_rate else None,
                sample_rate_hz=_as_int(stream.get("sample_rate")),
            )
        )
    return info
