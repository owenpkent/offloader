"""Property-based tests.

The reports and the naming layer both take strings that came off a camera card,
which in practice means any Unicode at all: accented takes, CJK slates, emoji
from a DIT's naming macro, and the occasional control character from a corrupt
directory entry. These tests assert the invariants that must hold for *every*
such input rather than for the handful a fixture happens to contain.

Run longer sweeps with:  pytest --fuzz
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import math
import re
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from offloader import naming, util
from offloader.hashers import ALGORITHMS, hash_file, new_hasher
from offloader.models import (
    AudioTrack,
    Destination,
    FileEntry,
    FileStatus,
    Job,
    MediaInfo,
    VerificationMode,
)
from offloader.presets import Preset
from offloader.reports import layout, write_csv, write_html, write_mhl, write_pdf

fitz = pytest.importorskip("fitz", reason="PyMuPDF needed to inspect the PDF")

# --------------------------------------------------------------------- strategies

#: Anything a filesystem might hand us, including control characters and
#: surrogates-adjacent oddities.
hostile_text = st.text(min_size=0, max_size=120)

#: Names that are legal to actually create on disk.
safe_name = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"),
                           whitelist_characters="._- "),
    min_size=1, max_size=24,
).map(lambda s: s.strip(" .")).filter(bool)

byte_sizes = st.integers(min_value=0, max_value=10**15)
positive_seconds = st.floats(min_value=0, max_value=60 * 60 * 24,
                             allow_nan=False, allow_infinity=False)


def _entry(name: str, *, size: int = 1024, checksum: str = "abc123",
           destinations: int = 1, media: MediaInfo | None = None) -> FileEntry:
    when = _dt.datetime(2026, 8, 4, 12, 0).timestamp()
    root = Path("/src")
    return FileEntry(
        source=root / name,
        source_root=root,
        size=size,
        created=when,
        modified=when,
        checksum=checksum,
        media=media or MediaInfo(),
        destinations=[
            Destination(root=Path(f"/dst{i}"), path=Path(f"/dst{i}") / name,
                        status=FileStatus.VERIFIED, checksum=checksum,
                        created=when, modified=when)
            for i in range(destinations)
        ],
    )


def _job(files: list[FileEntry], **overrides) -> Job:
    values = dict(
        name="A001",
        source_root=Path("/src"),
        destination_roots=[Path("/dst0")],
        verification=VerificationMode.SOURCE_ONLY,
        hash_label="XXHash3-64",
        started=_dt.datetime(2026, 8, 4, 5, 0),
        finished=_dt.datetime(2026, 8, 4, 6, 0),
        files=files,
        os_version="Windows 11 (Build 26200)",
        processors=16,
        system_ram="32 GB",
    )
    values.update(overrides)
    return Job(**values)


def _spans(page):
    return [s for b in page.get_text("dict")["blocks"] if b["type"] == 0
            for line in b["lines"] for s in line["spans"]]


# --------------------------------------------------------------------- formatting


@given(byte_sizes)
def test_format_size_always_parses_back(size: int):
    text = util.format_size(size)
    match = re.fullmatch(r"(\d+(?:\.\d+)?) (bytes|KB|MB|GB|TB)", text)
    assert match, f"unparseable size string {text!r} for {size}"

    scale = {"bytes": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[match.group(2)]
    recovered = float(match.group(1)) * scale
    # One decimal of mantissa, so the worst case is a value just above the
    # decade start (1.049 -> "1.0"): 5%. The exact byte count is printed
    # alongside in the detail listing, which is what anyone reconciles against.
    assert math.isclose(recovered, size, rel_tol=0.05, abs_tol=1.0)


@given(st.integers(min_value=0, max_value=10**14),
       st.integers(min_value=0, max_value=10**14))
def test_format_size_is_monotonic(a: int, b: int):
    """A bigger file must never render as a smaller string."""
    small, large = sorted((a, b))

    def magnitude(text: str) -> float:
        value, unit = text.rsplit(" ", 1)
        return float(value) * {"bytes": 1, "KB": 1e3, "MB": 1e6,
                               "GB": 1e9, "TB": 1e12}[unit]

    assert magnitude(util.format_size(small)) <= magnitude(util.format_size(large)) * 1.01


@given(positive_seconds)
def test_format_duration_shape(seconds: float):
    text = util.format_duration(seconds)
    assert re.fullmatch(r"\d+ sec|\d+:\d{2} min", text), text


@given(st.floats(min_value=0, max_value=10**7, allow_nan=False,
                 allow_infinity=False))
def test_format_elapsed_shape(seconds: float):
    assert re.fullmatch(r"\d+:\d{2}:\d{2}", util.format_elapsed(seconds))


@given(st.integers(min_value=0, max_value=10**8),
       st.sampled_from([23.976, 24, 25, 29.97, 30, 50, 59.94, 60]))
def test_timecode_fields_stay_in_range(frames: int, fps: float):
    text = util.format_timecode(frames, fps)
    body, tag = text.rsplit(" ", 1)
    hours, minutes, seconds, remainder = (int(p) for p in body.split(":"))
    assert tag in ("NDF", "DF")
    assert 0 <= minutes < 60 and 0 <= seconds < 60
    assert 0 <= remainder < max(1, round(fps))
    assert hours >= 0


@given(st.datetimes(min_value=_dt.datetime(1980, 1, 1),
                    max_value=_dt.datetime(2099, 12, 31)))
def test_datetime_formats_never_crash(when: _dt.datetime):
    assert re.fullmatch(r"\d{4} \d{2} \d{1,2}, \d{2}:\d{2}",
                        util.format_file_datetime(when))
    assert util.format_job_datetime(when)


# --------------------------------------------------------------------- naming


@given(hostile_text)
def test_sanitize_output_is_always_a_legal_filename(text: str):
    cleaned = naming.sanitize(text)
    assert cleaned, "sanitize must never return an empty name"
    assert not re.search(r'[<>:"/\\|?*]', cleaned)
    assert not any(ord(c) < 32 for c in cleaned)
    assert cleaned == cleaned.rstrip(". ")


@given(hostile_text, st.lists(hostile_text, max_size=8))
def test_build_never_collides_with_taken_names(template: str, taken: list[str]):
    name = naming.build(template, Path("A001"), taken=taken)
    assert name
    assert name.casefold() not in {t.casefold() for t in taken}


@given(st.lists(safe_name, min_size=1, max_size=25, unique_by=str.casefold))
def test_sequential_builds_are_all_distinct(seed_names: list[str]):
    """Queueing the same card repeatedly must keep producing fresh names, or
    two jobs would share one report folder."""
    taken: list[str] = []
    for _ in range(len(seed_names)):
        name = naming.build("{card}", Path("A001"), taken=taken)
        assert name.casefold() not in {t.casefold() for t in taken}
        taken.append(name)


@given(st.integers(min_value=1, max_value=999))
def test_index_token_pads_to_three_digits(index: int):
    values = naming.context(Path("A001"), index=index)
    assert re.fullmatch(r"\d{3,}", values["index"])


# --------------------------------------------------------------------- hashing


@given(st.binary(max_size=200_000),
       st.lists(st.integers(min_value=1, max_value=5000), min_size=1, max_size=40),
       st.sampled_from([k for k, a in ALGORITHMS.items() if a.factory]))
def test_chunked_hashing_matches_whole_buffer(payload: bytes,
                                              chunk_sizes: list[int], key: str):
    """Digest must not depend on how the stream was split — the engine's chunk
    boundaries fall wherever the read happens to land."""
    reference = new_hasher(key)
    reference.update(payload)

    incremental = new_hasher(key)
    offset = 0
    index = 0
    while offset < len(payload):
        size = chunk_sizes[index % len(chunk_sizes)]
        incremental.update(payload[offset:offset + size])
        offset += size
        index += 1

    assert incremental.hexdigest() == reference.hexdigest()


@given(payload=st.binary(max_size=100_000),
       key=st.sampled_from([k for k, a in ALGORITHMS.items() if a.factory]))
def test_hash_file_matches_in_memory(payload: bytes, key: str, tmp_path_factory):
    target = tmp_path_factory.mktemp("fuzz") / "blob.bin"
    target.write_bytes(payload)
    expected = new_hasher(key)
    expected.update(payload)
    assert hash_file(target, key) == expected.hexdigest()


# --------------------------------------------------------------------- PDF


@settings(max_examples=40, deadline=None)
@given(names=st.lists(hostile_text, min_size=1, max_size=6), size=byte_sizes)
def test_pdf_survives_hostile_filenames(names: list[str], size: int, tmp_path_factory):
    """No input may push text off the page or crash the writer."""
    out = tmp_path_factory.mktemp("pdf") / "JobReport.pdf"
    files = [_entry(name or "unnamed", size=size) for name in names]
    write_pdf(_job(files), out)

    with fitz.open(out) as document:
        assert document.page_count >= 1
        for page in document:
            for span in _spans(page):
                assert span["bbox"][0] >= -1.0
                assert span["bbox"][2] <= layout.PAGE_WIDTH + 1.0
                assert span["bbox"][3] <= layout.PAGE_HEIGHT + 1.0


@settings(max_examples=25, deadline=None)
@given(depth=st.integers(min_value=1, max_value=6),
       segment=st.integers(min_value=1, max_value=400))
def test_pdf_long_paths_stay_on_the_page(depth: int, segment: int,
                                         tmp_path_factory):
    """Windows paths get long; the detail line must elide, not overrun."""
    out = tmp_path_factory.mktemp("pdf") / "JobReport.pdf"
    deep = "/".join("d" * segment for _ in range(depth))
    entry = _entry("clip.mov")
    entry.destinations[0].path = Path(f"/{deep}/clip.mov")
    entry.source = Path(f"/{deep}/clip.mov")

    write_pdf(_job([entry], destination_roots=[Path(f"/{deep}")]), out)
    with fitz.open(out) as document:
        for page in document:
            for span in _spans(page):
                assert span["bbox"][2] <= layout.PAGE_WIDTH + 1.0


@settings(max_examples=20, deadline=None)
@given(file_count=st.integers(min_value=0, max_value=30),
       destinations=st.integers(min_value=1, max_value=4))
def test_pdf_page_count_grows_with_content(file_count: int, destinations: int,
                                           tmp_path_factory):
    out = tmp_path_factory.mktemp("pdf") / "JobReport.pdf"
    files = [_entry(f"clip{i:03d}.mov", destinations=destinations)
             for i in range(file_count)]
    write_pdf(_job(files), out)
    with fitz.open(out) as document:
        assert document.page_count >= 1
        # Every page must carry its footer.
        for number, page in enumerate(document, start=1):
            assert f"Page {number}" in page.get_text()


@settings(max_examples=20, deadline=None)
@given(width=st.integers(min_value=0, max_value=8000),
       height=st.integers(min_value=0, max_value=8000),
       fps=st.floats(min_value=0.01, max_value=1000, allow_nan=False,
                     allow_infinity=False))
def test_pdf_handles_extreme_media_metadata(width: int, height: int, fps: float,
                                            tmp_path_factory):
    out = tmp_path_factory.mktemp("pdf") / "JobReport.pdf"
    media = MediaInfo(
        container="QuickTime", width=width or None, height=height or None,
        video_codec="H264/AVC", fps=fps, duration_sec=fps * 10,
        frame_count=int(fps * 100), timecode="00:00:00:00 NDF",
        audio_tracks=[AudioTrack(2, "stereo", "LINEAR PCM", 2304.0, 48000)],
    )
    write_pdf(_job([_entry("clip.mov", media=media)]), out)
    assert out.stat().st_size > 0


# --------------------------------------------------------------------- CSV / MHL / HTML


@settings(max_examples=40, deadline=None)
@given(names=st.lists(hostile_text, min_size=1, max_size=5))
def test_csv_keeps_its_column_count(names: list[str], tmp_path_factory):
    """A filename containing commas, quotes or newlines must not shift columns."""
    out = tmp_path_factory.mktemp("csv") / "JobReport.csv"
    write_csv(_job([_entry(name or "unnamed") for name in names]), out)

    rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))
    header = next(r for r in rows if r and r[0] == "File Name")
    body = rows[rows.index(header) + 1:]
    assert len(body) == len(names)
    for row in body:
        assert len(row) == len(header)


@settings(max_examples=40, deadline=None)
@given(names=st.lists(hostile_text, min_size=1, max_size=5))
def test_mhl_is_always_well_formed_xml(names: list[str], tmp_path_factory):
    out = tmp_path_factory.mktemp("mhl") / "JobReport.mhl"
    write_mhl(_job([_entry(name or "unnamed") for name in names]), out)
    root = ET.parse(out).getroot()          # raises if malformed
    assert root.tag == "hashlist"


@settings(max_examples=40, deadline=None)
@given(names=st.lists(hostile_text, min_size=1, max_size=5))
def test_html_never_emits_raw_angle_brackets_from_input(names: list[str],
                                                        tmp_path_factory):
    out = tmp_path_factory.mktemp("html") / "JobReport.html"
    write_html(_job([_entry(name or "unnamed") for name in names]), out,
               thumbnails=False)
    document = out.read_text(encoding="utf-8")

    # Nothing the input contributed may survive as markup.
    for name in names:
        if "<" in name:
            assert name not in document
    assert document.count("<script") == 0


# --------------------------------------------------------------------- presets


@given(
    st.builds(
        Preset,
        name=safe_name,
        destinations=st.lists(safe_name.map(lambda s: Path("D:/") / s), max_size=4),
        algorithm=st.sampled_from(sorted(ALGORITHMS)),
        verification=st.sampled_from(list(VerificationMode)),
        thumbnail_count=st.integers(min_value=0, max_value=8),
        reports=st.lists(st.sampled_from(["pdf", "csv", "mhl", "html"]),
                         max_size=4, unique=True),
        excludes=st.lists(safe_name, max_size=4),
        naming_template=hostile_text,
        use_count=st.integers(min_value=0, max_value=10**6),
    )
)
def test_preset_round_trips_through_json(preset: Preset):
    restored = Preset.from_dict(preset.to_dict())
    assert restored == preset


@given(st.dictionaries(st.text(max_size=20), st.none() | st.text(max_size=20),
                       max_size=10))
def test_preset_from_dict_tolerates_garbage(payload: dict):
    """A hand-edited or version-skewed presets.json must still load."""
    preset = Preset.from_dict(payload)
    assert preset.name
    assert preset.algorithm in ALGORITHMS
    assert isinstance(preset.verification, VerificationMode)
    assert preset.summary()
