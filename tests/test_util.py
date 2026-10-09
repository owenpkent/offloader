"""Formatting must match the reference report's strings exactly — these are the
values a post house reads off the PDF when reconciling a delivery."""

from __future__ import annotations

import datetime as _dt

import pytest

from offloader.util import (
    channel_layout_name,
    format_clock,
    format_duration,
    format_elapsed,
    format_file_datetime,
    format_fps,
    format_job_datetime,
    format_sample_rate,
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


#: Elapsed frames at 30000/1001 for one minute, ten minutes and one hour of
#: wall clock, and what each is labelled with and without drop frame. Worked
#: from SMPTE ST 12-1 5.2.2 rather than from this implementation: drop frame
#: renumbers so the label tracks the clock, so the round hours and tens of
#: minutes are exact, while non-drop numbering falls behind by about 3.6
#: seconds an hour, which is the drift drop frame exists to hide.
NTSC_VECTORS = [
    (1798, "00:00:59:28 DF", "00:00:59:28 NDF"),
    (17982, "00:10:00:00 DF", "00:09:59:12 NDF"),
    (107892, "01:00:00:00 DF", "00:59:56:12 NDF"),
]


@pytest.mark.parametrize("frames,dropped,plain", NTSC_VECTORS)
def test_drop_frame_labels_track_the_clock(frames: int, dropped: str, plain: str):
    """REGRESSION. The count was formatted as if the rate were a whole 30 and
    "DF" appended, so an hour of recording read 00:59:56:12 DF."""
    rate = 30000 / 1001
    assert format_timecode(frames, rate, drop_frame=True) == dropped
    assert format_timecode(frames, rate) == plain


def test_drop_frame_skips_the_two_labels_at_a_dropping_minute():
    """The renumbering itself: 00 and 01 are never used at the top of a minute
    that is not a tenth one, and a tenth minute does not drop."""
    rate = 30000 / 1001
    assert format_timecode(1799, rate, drop_frame=True) == "00:00:59:29 DF"
    assert format_timecode(1800, rate, drop_frame=True) == "00:01:00:02 DF"
    assert format_timecode(17981, rate, drop_frame=True) == "00:09:59:29 DF"
    assert format_timecode(17982, rate, drop_frame=True) == "00:10:00:00 DF"


def test_drop_frame_at_the_doubled_rate():
    assert format_timecode(215784, 60000 / 1001, drop_frame=True) == "01:00:00:00 DF"


@pytest.mark.parametrize("fps", [24, 25, 30, 50, 60])
def test_a_drop_frame_flag_at_a_rate_that_has_none_is_not_claimed(fps: int):
    """Drop frame corrects the 1000/1001 rates and nothing else. Renumbering a
    true 30 would introduce the error it exists to remove, and labelling the
    plain count "DF" would pass a malformed file's claim on as a fact."""
    assert format_timecode(1500, fps, drop_frame=True).endswith("NDF")


def test_the_rate_is_judged_as_written_not_as_rounded():
    """29.97 and 30 both round to 30, and only one of them drops frames."""
    assert format_timecode(107892, 29.97, drop_frame=True) == "01:00:00:00 DF"
    assert format_timecode(107892, 30.0, drop_frame=True) == "00:59:56:12 NDF"


def test_every_drop_frame_label_is_distinct_and_ascending():
    """Two hours of frames, because a renumbering that repeats or goes backwards
    would still satisfy every fixed vector above."""
    rate = 30000 / 1001
    labels = [format_timecode(n, rate, drop_frame=True) for n in range(215785)]
    assert len(set(labels)) == len(labels)
    assert labels == sorted(labels)


def test_fps_drops_decimals_when_integral():
    assert format_fps(24.0) == "24 FPS"
    assert format_fps(23.976) == "23.98 FPS"


def test_channel_layout():
    assert channel_layout_name(2, "stereo") == "Stereo"
    assert channel_layout_name(1, "mono") == "Mono"
    assert channel_layout_name(6, None) == "5.1"


@pytest.mark.parametrize("hz,expected", [
    (48000, "48 kHz"),
    (96000, "96 kHz"),
    (44100, "44.1 kHz"),
    (192000, "192 kHz"),
    (0, None),
    (-1, None),
    (None, None),
])
def test_format_sample_rate(hz, expected):
    assert format_sample_rate(hz) == expected


@pytest.mark.parametrize("seconds,expected", [
    (0, "00:00:00.000"),
    (36000.0, "10:00:00.000"),          # a 10:00:00 BWF start
    (36090.5, "10:01:30.500"),
    (-5, "00:00:00.000"),               # a negative origin is not a time
    (86400.0, "00:00:00.000"),          # a full day wraps rather than reading 24
    (86399.9994, "23:59:59.999"),
])
def test_format_clock(seconds, expected):
    assert format_clock(seconds) == expected


def test_format_clock_carries_a_rounded_millisecond_into_the_second():
    assert format_clock(59.9999) == "00:01:00.000"

