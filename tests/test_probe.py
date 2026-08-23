"""ffprobe output parsing.

Driven with captured-shaped JSON rather than a live ffprobe, so the codec and
rate mapping is tested without needing media on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from offloader import probe


@pytest.mark.parametrize(
    "value,expected",
    [
        ("24/1", 24.0),
        ("30000/1001", pytest.approx(29.97, abs=0.01)),
        ("24000/1001", pytest.approx(23.976, abs=0.01)),
        ("25", 25.0),
        ("0/0", None),
        ("0", None),
        ("", None),
        (None, None),
        ("24/0", None),          # must not raise ZeroDivisionError
        ("garbage", None),
        ("a/b", None),
    ],
)
def test_parse_rate(value, expected):
    assert probe._parse_rate(value) == expected


def _payload(**overrides) -> dict:
    video = {
        "codec_type": "video", "codec_name": "h264",
        "width": 1920, "height": 1080,
        "avg_frame_rate": "24/1", "r_frame_rate": "24/1",
        "nb_frames": "6022",
    }
    audio = {
        "codec_type": "audio", "codec_name": "pcm_s16le",
        "channels": 2, "channel_layout": "stereo",
        "bit_rate": "2304000", "sample_rate": "48000",
    }
    data = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "250.9",
                   "tags": {"timecode": "12:54:38:12"}},
        "streams": [video, audio],
    }
    data.update(overrides)
    return data


def test_builds_full_media_info():
    info = probe._build(_payload())
    assert info.container == "QuickTime"
    assert (info.width, info.height) == (1920, 1080)
    assert info.video_codec == "H264/AVC"
    assert info.fps == 24.0
    assert info.frame_count == 6022
    assert info.timecode == "12:54:38:12 NDF"
    assert info.duration_sec == pytest.approx(250.9)
    assert info.is_video

    assert len(info.audio_tracks) == 1
    track = info.audio_tracks[0]
    assert track.codec == "LINEAR PCM"
    assert track.channels == 2
    assert track.bit_rate_kbps == pytest.approx(2304.0)
    assert track.sample_rate_hz == 48000


@pytest.mark.parametrize("codec,expected", [
    ("h264", "H264/AVC"), ("hevc", "HEVC/H265"), ("prores", "Apple ProRes"),
    ("dnxhd", "DNxHD"), ("cfhd", "CineForm"),
    ("somethingnew", "SOMETHINGNEW"),      # unknown codecs pass through upcased
])
def test_video_codec_display_names(codec, expected):
    data = _payload()
    data["streams"][0]["codec_name"] = codec
    assert probe._build(data).video_codec == expected


@pytest.mark.parametrize("container,expected", [
    ("mov,mp4,m4a,3gp,3g2,mj2", "QuickTime"),
    ("matroska,webm", "Matroska"),
    ("mxf", "MXF"),
    ("weirdformat", "WEIRDFORMAT"),
])
def test_container_display_names(container, expected):
    data = _payload()
    data["format"]["format_name"] = container
    assert probe._build(data).container == expected


def test_frame_count_falls_back_to_duration_times_rate():
    data = _payload()
    del data["streams"][0]["nb_frames"]
    info = probe._build(data)
    assert info.frame_count == pytest.approx(round(250.9 * 24), abs=1)


def test_zero_nb_frames_is_ignored():
    data = _payload()
    data["streams"][0]["nb_frames"] = "0"
    assert probe._build(data).frame_count == pytest.approx(round(250.9 * 24), abs=1)


def test_semicolon_timecode_is_drop_frame():
    data = _payload()
    data["format"]["tags"]["timecode"] = "01:00:00;00"
    info = probe._build(data)
    assert info.timecode.endswith("DF") and not info.timecode.endswith("NDF")
    assert ";" not in info.timecode


def test_timecode_falls_back_to_zero_when_absent():
    data = _payload()
    data["format"].pop("tags")
    assert probe._build(data).timecode == "00:00:00:00 NDF"


def test_timecode_found_on_a_separate_data_stream():
    data = _payload()
    data["format"].pop("tags")
    data["streams"].append({"codec_type": "data", "tags": {"timecode": "10:00:00:00"}})
    assert probe._build(data).timecode == "10:00:00:00 NDF"


def test_audio_only_file_is_not_a_video():
    data = _payload()
    data["streams"] = [s for s in data["streams"] if s["codec_type"] == "audio"]
    info = probe._build(data)
    assert not info.is_video
    assert info.width is None
    assert len(info.audio_tracks) == 1


def test_multiple_audio_tracks_are_all_captured():
    data = _payload()
    data["streams"].append(dict(data["streams"][1]))
    assert len(probe._build(data).audio_tracks) == 2


def test_empty_payload_yields_empty_info():
    info = probe._build({})
    assert not info.is_video
    assert info.container is None
    assert info.audio_tracks == []


def test_unparseable_duration_is_dropped():
    data = _payload()
    data["format"]["duration"] = "N/A"
    assert probe._build(data).duration_sec is None


def test_non_media_extensions_skip_ffprobe_entirely(tmp_path: Path, monkeypatch):
    """A sidecar must not cost a subprocess spawn per file."""
    called = False

    def spy(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("ffprobe should not run for a .txt")

    monkeypatch.setattr(probe.subprocess, "run", spy)
    info = probe.probe(tmp_path / "notes.txt")
    assert not called
    assert not info.is_video


def test_probe_returns_empty_info_when_ffprobe_is_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(probe, "ffprobe_path", lambda: None)
    assert probe.probe(tmp_path / "clip.mov").container is None


def test_probe_survives_a_crashing_ffprobe(tmp_path: Path, monkeypatch):
    """A failed probe must never fail the offload."""
    monkeypatch.setattr(probe, "ffprobe_path", lambda: "ffprobe")
    monkeypatch.setattr(probe.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert probe.probe(tmp_path / "clip.mov").container is None


def test_probe_survives_garbage_output(tmp_path: Path, monkeypatch):
    class Result:
        stdout = "not json at all"

    monkeypatch.setattr(probe, "ffprobe_path", lambda: "ffprobe")
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **k: Result())
    assert probe.probe(tmp_path / "clip.mov").container is None


# ------------------------------------------------------------------- audio


def _wav(**tags) -> dict:
    """A broadcast WAV as ffprobe reports it: one PCM stream, no video."""
    return {
        "format": {"format_name": "wav", "duration": "182.5", "tags": tags},
        "streams": [{
            "codec_type": "audio", "codec_name": "pcm_s24le",
            "channels": 2, "channel_layout": "stereo",
            "bit_rate": "2304000", "sample_rate": "48000",
            "bits_per_raw_sample": "24", "bits_per_sample": 24,
        }],
    }


def test_a_wav_is_audio_not_video():
    info = probe._build(_wav())
    assert info.container == "WAVE"
    assert not info.is_video
    assert info.is_audio
    assert info.width is None and info.height is None


def test_a_clip_with_dialogue_is_still_video():
    """is_audio is "no picture", not "has sound" -- the counts must not overlap."""
    info = probe._build(_payload())
    assert info.audio_tracks and info.is_video
    assert not info.is_audio


def test_bit_depth_is_captured():
    assert probe._build(_wav()).audio_tracks[0].bit_depth == 24


def test_bit_depth_prefers_raw_sample_and_steps_past_a_zero():
    data = _wav()
    data["streams"][0]["bits_per_raw_sample"] = None
    data["streams"][0]["bits_per_sample"] = 0      # what several codecs report
    assert probe._build(data).audio_tracks[0].bit_depth is None

    data["streams"][0]["bits_per_sample"] = 16
    assert probe._build(data).audio_tracks[0].bit_depth == 16


def test_bwf_time_reference_becomes_the_start_clock():
    """1728000000 samples at 48 kHz is 36000 s, which is 10:00:00."""
    info = probe._build(_wav(time_reference="1728000000"))
    assert info.timecode == "10:00:00.000"


def test_an_explicit_timecode_tag_wins_over_the_sample_count():
    info = probe._build(_wav(timecode="09:00:00:00", time_reference="1728000000"))
    assert info.timecode == "09:00:00:00"


def test_a_wav_without_a_clock_reports_none():
    """Better no timecode than a zero that reads as a real 00:00:00."""
    assert probe._build(_wav()).timecode is None


def test_time_reference_of_zero_is_a_real_midnight_start():
    assert probe._build(_wav(time_reference="0")).timecode == "00:00:00.000"


def test_a_broken_time_reference_does_not_raise():
    assert probe._build(_wav(time_reference="not-a-number")).timecode is None


def test_video_timecode_is_untouched_by_the_audio_path():
    assert probe._build(_payload()).timecode == "12:54:38:12 NDF"

