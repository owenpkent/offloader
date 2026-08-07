"""Media metadata via ffprobe.

Codec and container names are mapped to the display forms the reference report
uses ("H264/AVC", "QuickTime", "LINEAR PCM") rather than ffmpeg's internal ids.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .models import AudioTrack, MediaInfo
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
    """ffprobe rates arrive as "24/1" or "24000/1001"."""
    if not value or value in ("0/0", "0"):
        return None
    if "/" in value:
        num, _, den = value.partition("/")
        try:
            den_f = float(den)
            return float(num) / den_f if den_f else None
        except ValueError:
            return None
    try:
        return float(value)
    except ValueError:
        return None


def probe(path: Path, timeout: float = 30.0) -> MediaInfo:
    """Read metadata for one file. Returns an empty MediaInfo for non-media
    files, unreadable files, or when ffprobe is missing — a failed probe must
    never fail the offload."""
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
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]

    info = MediaInfo()
    format_name = fmt.get("format_name", "")
    info.container = _CONTAINERS.get(format_name, format_name.upper() or None)

    duration = fmt.get("duration") or (video or {}).get("duration")
    try:
        info.duration_sec = float(duration) if duration else None
    except (TypeError, ValueError):
        info.duration_sec = None

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
            fmt.get("tags", {}).get("timecode")
            or video.get("tags", {}).get("timecode")
            or next(
                (s.get("tags", {}).get("timecode") for s in streams
                 if s.get("tags", {}).get("timecode")),
                None,
            )
        )
        if tc:
            info.timecode = f"{tc} NDF" if ";" not in tc else f"{tc.replace(';', ':')} DF"
        elif info.fps:
            info.timecode = format_timecode(0, info.fps)

    for stream in audios:
        codec = stream.get("codec_name", "")
        bit_rate = stream.get("bit_rate")
        sample_rate = stream.get("sample_rate")
        info.audio_tracks.append(
            AudioTrack(
                channels=int(stream.get("channels") or 0),
                layout=stream.get("channel_layout") or "",
                codec=_AUDIO_CODECS.get(codec, codec.upper() or "Unknown"),
                bit_rate_kbps=float(bit_rate) / 1000.0 if bit_rate else None,
                sample_rate_hz=int(sample_rate) if sample_rate else None,
            )
        )
    return info
