"""Configuration persistence, thumbnail sampling, and host facts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from offloader import config, sysinfo, thumbs
from offloader.models import MediaInfo


# ------------------------------------------------------------------ config


def test_write_then_read_round_trips(tmp_path: Path):
    target = tmp_path / "settings.json"
    payload = {"sound": True, "mode": "preset", "count": 3}
    config.write_json(target, payload)
    assert config.read_json(target, None) == payload


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path):
    target = tmp_path / "settings.json"
    config.write_json(target, {"a": 1})
    config.write_json(target, {"a": 2})
    assert config.read_json(target, None) == {"a": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_write_creates_missing_parents(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "settings.json"
    config.write_json(target, {"ok": True})
    assert target.is_file()


def test_read_falls_back_on_corrupt_json(tmp_path: Path):
    target = tmp_path / "settings.json"
    target.write_text("{{{ not json", encoding="utf-8")
    sentinel = {"default": True}
    assert config.read_json(target, sentinel) is sentinel


def test_read_falls_back_on_missing_file(tmp_path: Path):
    assert config.read_json(tmp_path / "nope.json", []) == []


def test_written_json_is_utf8_and_human_readable(tmp_path: Path):
    target = tmp_path / "settings.json"
    config.write_json(target, {"name": "A001_café_日本"})
    raw = target.read_text(encoding="utf-8")
    assert json.loads(raw)["name"] == "A001_café_日本"
    assert "\n" in raw          # indented, so it can be hand-edited


def test_config_dir_exists_and_is_stable():
    first = config.config_dir()
    assert first.is_dir()
    assert config.config_dir() == first
    assert config.config_file("x.json").parent == first


# ------------------------------------------------------------------ thumbnails


@pytest.mark.parametrize("count", [1, 2, 4, 8])
def test_sample_offsets_count_and_bounds(count: int):
    duration = 120.0
    offsets = thumbs._sample_offsets(duration, count)
    assert len(offsets) == count
    assert all(0 < o < duration for o in offsets)
    assert offsets == sorted(offsets)


def test_sample_offsets_avoid_the_very_start_and_end():
    """Slates live at the head and black frames at the tail."""
    offsets = thumbs._sample_offsets(100.0, 4)
    assert offsets[0] >= 4.0
    assert offsets[-1] <= 96.0


def test_single_sample_lands_mid_clip():
    assert thumbs._sample_offsets(60.0, 1) == [30.0]


@pytest.mark.parametrize("duration,count", [(0, 4), (-1, 4), (60, 0), (60, -2)])
def test_sample_offsets_degenerate_inputs(duration, count):
    assert thumbs._sample_offsets(duration, count) == []


def test_extract_returns_nothing_without_ffmpeg(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(thumbs, "ffmpeg_path", lambda: None)
    media = MediaInfo(width=1920, height=1080, duration_sec=10.0)
    assert thumbs.extract(tmp_path / "clip.mov", media, tmp_path / "out") == []


def test_extract_skips_files_with_no_video_stream(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(thumbs, "ffmpeg_path", lambda: "ffmpeg")
    audio_only = MediaInfo(duration_sec=10.0)
    assert thumbs.extract(tmp_path / "take.wav", audio_only, tmp_path / "out") == []


def test_extract_skips_zero_duration_media(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(thumbs, "ffmpeg_path", lambda: "ffmpeg")
    still = MediaInfo(width=6064, height=4048, duration_sec=0.0)
    assert thumbs.extract(tmp_path / "still.braw", still, tmp_path / "out") == []


def test_extract_survives_a_failing_ffmpeg(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(thumbs, "ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(thumbs.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    media = MediaInfo(width=1920, height=1080, duration_sec=10.0)
    assert thumbs.extract(tmp_path / "clip.mov", media, tmp_path / "out") == []


def test_cell_is_sixteen_by_nine():
    """Cells are 2x the PDF's 137.6 x 78 pt so they stay crisp in print."""
    assert thumbs.CELL_WIDTH / thumbs.CELL_HEIGHT == pytest.approx(16 / 9, abs=0.02)


# ------------------------------------------------------------------ sysinfo


def test_collect_returns_usable_host_facts():
    host = sysinfo.collect()
    assert host.os_version
    assert host.processors >= 1
    # Either a real reading or an honest blank — never a wrong number.
    assert host.system_ram == "" or host.system_ram.endswith(" GB")


def test_ram_reading_is_plausible():
    total = sysinfo._ram_bytes()
    assert total == 0 or 256 * 1024**2 < total < 8 * 1024**4
