"""Formatting must match the reference report's strings exactly — these are the
values a post house reads off the PDF when reconciling a delivery."""

from __future__ import annotations

import datetime as _dt

import pytest

from offloader.util import (
    channel_layout_name,
    format_duration,
    format_elapsed,
    format_file_datetime,
    format_fps,
    format_job_datetime,
    format_size,
    format_timecode,
)


@pytest.mark.parametrize(
    "size,expected",
    [
        (512, "512 bytes"),
        (258_758_961, "258.8 MB"),      # reference: A001_08041254_C001.mp4
        (100_683_160, "100.7 MB"),      # reference: C002
        (8_601_234, "8.6 MB"),
        (27_798_474_225, "27.80 GB"),   # reference: C001.braw
        (39_422_587_134, "39.42 GB"),   # reference: C047.braw
    ],
)
def test_format_size_matches_reference(size, expected):
    assert format_size(size) == expected


@pytest.mark.parametrize(
    "seconds,expected",
    [(36, "36 sec"), (59, "59 sec"), (60, "1:00 min"), (104, "1:44 min"),
     (251, "4:11 min"), (124, "2:04 min")],
)
def test_format_duration_matches_reference(seconds, expected):
    assert format_duration(seconds) == expected


def test_format_elapsed():
    assert format_elapsed(3840) == "1:04:00"      # reference total time
    assert format_elapsed(59) == "0:00:59"


def test_file_datetime_pads_month_but_not_day():
    # The reference renders "2026 08 4, 12:54" — zero-padded month, bare day.
    assert format_file_datetime(_dt.datetime(2026, 8, 4, 12, 54)) == "2026 08 4, 12:54"


def test_job_datetime():
    assert (format_job_datetime(_dt.datetime(2026, 8, 4, 5, 2, 57))
            == "August 04, 2026-05_02_57")


def test_timecode():
    assert format_timecode(0, 24) == "00:00:00:00 NDF"
    assert format_timecode(6022, 24) == "00:04:10:22 NDF"
    assert format_timecode(6022, 24, drop_frame=True).endswith("DF")


def test_fps_drops_decimals_when_integral():
    assert format_fps(24.0) == "24 FPS"
    assert format_fps(23.976) == "23.98 FPS"


def test_channel_layout():
    assert channel_layout_name(2, "stereo") == "Stereo"
    assert channel_layout_name(1, "mono") == "Mono"
    assert channel_layout_name(6, None) == "5.1"
