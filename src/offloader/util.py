"""Formatting helpers.

All of these reproduce the exact string forms used by ShotPut Pro's PDF report,
which is why the units are decimal (SI) rather than binary: the reference report
renders 258_758_961 bytes as "258.8 MB", not "246.8 MB".
"""

from __future__ import annotations

import datetime as _dt
import math

#: (unit, bytes per unit, decimals). The decimal counts are the reference
#: report's, not ours: 1 for KB/MB, 2 for GB/TB.
_SIZE_UNITS: tuple[tuple[str, int, int], ...] = (
    ("bytes", 1, 0),
    ("KB", 10 ** 3, 1),
    ("MB", 10 ** 6, 1),
    ("GB", 10 ** 9, 2),
    ("TB", 10 ** 12, 2),
)


def _is_finite(value: object) -> bool:
    """`math.isfinite` without the surprises: an int too large to convert to
    float is still a finite number, and a non-number is not."""
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    try:
        return math.isfinite(value)              # type: ignore[arg-type]
    except (TypeError, OverflowError):
        return False


def format_size(num_bytes: int) -> str:
    """Format a byte count with the report's variable precision.

    The reference uses 1 decimal for MB values ("258.8 MB", "8.6 MB") and
    2 decimals for GB values ("27.8 GB", "483.11 GB"). Reproduce that.

    The unit is chosen against the *rounded* mantissa rather than the raw one,
    so a count just below a decade boundary promotes instead of printing a
    mantissa of 1000 in the smaller unit. Sign is handled separately, so a
    negative count promotes the same way a positive one does.

    >>> format_size(258758961)
    '258.8 MB'
    >>> format_size(512)
    '512 bytes'
    >>> format_size(999_999)
    '1.0 MB'
    >>> format_size(-1024)
    '-1.0 KB'
    """
    if not _is_finite(num_bytes):
        # A size we failed to measure is not a size. "nan TB" in a report is
        # worse than admitting to nothing.
        return "0 bytes"

    sign = "-" if num_bytes < 0 else ""
    magnitude = abs(num_bytes)

    for unit, scale, decimals in _SIZE_UNITS:
        # The mantissa rounds up to 1000 just below the boundary, so promote a
        # half-step early. All integer arithmetic, so an enormous count picks
        # its unit without ever overflowing a float.
        promote_at = 1000 * scale - scale // (2 * 10 ** decimals)
        if magnitude >= promote_at and unit != _SIZE_UNITS[-1][0]:
            continue
        if decimals == 0:
            return f"{sign}{magnitude} {unit}"
        try:
            return f"{sign}{magnitude / scale:.{decimals}f} {unit}"
        except OverflowError:               # pragma: no cover - absurd counts
            return f"{sign}{magnitude // scale} {unit}"
    raise AssertionError("unreachable: the last unit always matches")


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


def whole_frame_rate(fps: float) -> int:
    """Frames per second as a positive whole number, for timecode arithmetic.

    Anything unusable (nan, inf, zero, negative) becomes 1. A probe that could
    not read the rate must not be able to crash a report, and a negative rate
    must not flip the sign of the timecode fields.
    """
    if not _is_finite(fps) or fps <= 0:
        return 1
    return max(1, int(round(fps)))


#: Frames skipped at the top of a dropping minute, by whole frame rate. SMPTE
#: ST 12-1 5.2.2 defines the numbering for these: two labels per minute at 30,
#: and the same proportion at the doubled rates.
_DROPPED_PER_MINUTE = {30: 2, 60: 4, 120: 8}

#: How far a rate may sit from exactly `whole * 1000/1001` and still be that
#: rate. Wide enough for the 29.97 and 59.94 a file is likely to write out,
#: narrow enough that a true 30 is nowhere near it.
_NTSC_TOLERANCE = 0.01


def _dropped_per_minute(fps: float, rate: int) -> int | None:
    """How many labels a minute drops at this rate, or None if it drops none.

    Drop frame corrects the 1000/1001 rates and nothing else: it exists because
    29.97 frames a second falls behind the clock, and renumbering a true 30
    would introduce exactly the error it is there to remove. So the flag is
    honoured against the rate the file actually states, not against the rounded
    one -- a file claiming DF at 30/1 is malformed, and the plain count is the
    truthful reading of it.
    """
    dropped = _DROPPED_PER_MINUTE.get(rate)
    if dropped is None:
        return None
    if abs(fps - rate * 1000.0 / 1001.0) > _NTSC_TOLERANCE:
        return None
    return dropped


def _drop_frame_number(frames: int, rate: int, dropped: int) -> int:
    """An elapsed frame count as the frame *number* drop-frame labels it.

    Drop frame renumbers rather than discarding: no picture is lost, the labels
    00 and 01 are simply never used at the top of a minute that is not a tenth
    one, which keeps the running label within a couple of frames of the clock
    on the wall. So the conversion is to work out how many labels have been
    skipped before this point and add them back, then format normally.
    """
    per_dropping_minute = rate * 60 - dropped
    per_ten_minutes = rate * 600 - 9 * dropped
    tens, rest = divmod(frames, per_ten_minutes)
    skipped = 9 * dropped * tens
    if rest >= dropped:
        # The first minute of each ten does not drop, so the frames inside it
        # are taken off before counting the dropping minutes after it.
        skipped += dropped * ((rest - dropped) // per_dropping_minute)
    return frames + skipped


def format_timecode(frames: int, fps: float, drop_frame: bool = False) -> str:
    """Frame count to "HH:MM:SS:FF NDF"/"DF" timecode.

    `frames` is elapsed frames, not a frame number. At a 1000/1001 rate those
    differ: an hour of wall clock is 107,892 frames at 30000/1001, and the
    whole point of drop frame is that it labels that 01:00:00:00 rather than
    00:59:56:12. Formatting the count directly and appending "DF" reports the
    latter with the former's label, which is about 3.6 seconds an hour wrong in
    the field a sound report is read for.
    """
    rate = whole_frame_rate(fps)
    number = int(frames)
    dropped = _dropped_per_minute(fps, rate) if drop_frame else None
    if dropped is not None and number > 0:
        number = _drop_frame_number(number, rate, dropped)
    hours, rem = divmod(number, rate * 3600)
    minutes, rem = divmod(rem, rate * 60)
    secs, fr = divmod(rem, rate)
    # The tag reports the numbering that was actually applied. A file claiming
    # DF at a rate that has none is malformed, and labelling the plain count
    # "DF" would pass its error on as a fact.
    tag = "DF" if dropped is not None else "NDF"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{fr:02d} {tag}"


def format_sample_rate(hz: int | None) -> str | None:
    """48000 -> "48 kHz", 44100 -> "44.1 kHz"."""
    if not hz or hz <= 0:
        return None
    khz = hz / 1000.0
    return f"{khz:g} kHz"


def format_clock(seconds: float) -> str:
    """Seconds since midnight as "HH:MM:SS.mmm".

    Used for the start time of a broadcast WAV. It is deliberately not frame
    timecode: a BWF stores its origin as a sample count, and the frame rate to
    divide it by lives in iXML, which ffprobe does not read. Rendering
    "10:00:00:00" would mean picking a rate at random and printing a guess in
    the field a sound report is read for, so the sub-second part is shown as
    milliseconds instead -- exact, and visibly not frames.
    """
    if seconds < 0:
        seconds = 0.0
    total = int(seconds)
    ms = int(round((seconds - total) * 1000))
    if ms == 1000:                       # rounding carried into the next second
        total, ms = total + 1, 0
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours % 24:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def format_fps(fps: float) -> str:
    """"24 FPS" for integral rates, "23.98 FPS" otherwise.

    A rate that is not a finite number renders as nothing at all, which the
    report writers already treat as "omit this field".
    """
    if not _is_finite(fps):
        return ""
    if abs(fps - round(fps)) < 0.001:
        return f"{int(round(fps))} FPS"
    return f"{fps:.2f} FPS"


#: U+FFFD REPLACEMENT CHARACTER.
_REPLACEMENT = chr(0xFFFD)


def _is_xml_char(code: int) -> bool:
    """The XML 1.0 Char production."""
    return (
        code in (0x09, 0x0A, 0x0D)
        or 0x20 <= code <= 0xD7FF
        or 0xE000 <= code <= 0xFFFD
        or 0x10000 <= code <= 0x10FFFF
    )


def xml_safe(text: object) -> str:
    """Text that serialises and reads back, for manifests and reports alike.

    Filenames come off a filesystem, not out of a validator. A POSIX name that
    is not valid UTF-8 arrives as lone surrogates via surrogateescape, and NTFS
    accepts unpaired surrogates outright; neither can be encoded as UTF-8 at
    all. Control characters encode fine but fall outside the XML 1.0 Char
    production, so a manifest containing one will not parse again.

    One bad filename must not strand a whole delivery's paperwork, so the
    offending characters are replaced with U+FFFD rather than passed through
    or allowed to raise.
    """
    return "".join(
        character if _is_xml_char(ord(character)) else _REPLACEMENT
        for character in str(text)
    )


def channel_layout_name(channels: int, layout: str | None = None) -> str:
    """Human name for an audio track: "Stereo", "Mono", "5.1"."""
    if layout and layout not in ("unknown",):
        return {"mono": "Mono", "stereo": "Stereo"}.get(layout, layout)
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(channels, f"{channels} ch")
