"""Property-based tests for the edges the happy path never reaches.

`test_fuzz.py` fuzzes what a *filename* can contain. This module fuzzes what a
*card* can contain: a BRAW clip whose atom headers lie, a tree whose shape maps
two sources onto one destination, a directory junction that points at its own
parent, a retry policy someone hand-edited into a preset.

The contract under test is narrow and worth stating plainly, because every
failure below is a violation of one of these three:

  1. No input may make an offload lose a file without saying so. A report that
     says VERIFIED is an attestation; producing one for a file that is not at
     the destination is worse than crashing.
  2. No single unreadable or malformed file may abort a job. A card with one
     bad clip must still offload the other clips and still write a report.
  3. A parser fed hostile bytes may return nothing, but it may not raise
     something the caller has no reason to catch.

Tests marked `xfail(strict=True)` are confirmed live bugs, not aspirations.
Each names the offending line. If you fix one, the marker turns the test XPASS
and pytest will tell you to delete the marker.

Run longer sweeps with:  pytest --fuzz tests/test_fuzz_edges.py
"""

from __future__ import annotations

import datetime as _dt
import errno
import json
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from xml.etree import ElementTree as ET

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from offloader import ascmhl, braw, engine, naming, retry
from offloader.engine import OffloadOptions
from offloader.hashers import hash_file
from offloader.history import History
from offloader.models import (
    Destination,
    FileEntry,
    FileStatus,
    Job,
    MediaInfo,
    VerificationMode,
)
from offloader.reports import write_csv, write_mhl

if TYPE_CHECKING:
    from typing import BinaryIO

WINDOWS = sys.platform == "win32"

#: What a parser is allowed to raise. Anything else reaches a caller that has
#: no reason to be catching it. In this codebase, `engine.run`, which catches
#: nothing around `probe()` and so dies mid-job.
PARSER_MAY_RAISE = (OSError,)


# ------------------------------------------------------------------ BRAW atoms


def atom(tag: bytes, payload: bytes = b"", *, declared: int | None = None) -> bytes:
    """One ISO-BMFF atom. `declared` lies about the size on purpose."""
    size = len(payload) + 8 if declared is None else declared
    return struct.pack(">I", size) + tag + payload


#: The container types `braw._descend` will recurse into.
CONTAINERS = [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"meta"]
LEAVES = [b"hdlr", b"mdhd", b"stts", b"keys", b"ilst", b"free", b"ftyp", b"data"]

#: A `hdlr` that makes `_read_timing` accept the enclosing trak as the video
#: track: version+flags(4), pre_defined(4), then the handler type itself.
VIDEO_HANDLER = atom(b"hdlr", b"\x00" * 8 + b"vide")


def braw_file(moov_payload: bytes) -> bytes:
    return atom(b"ftyp", b"braw") + atom(b"moov", moov_payload)


def video_trak(mdhd_payload: bytes, stts_payload: bytes) -> bytes:
    """A trak shaped exactly like the one `_read_timing` goes looking for."""
    mdia = (VIDEO_HANDLER
            + atom(b"mdhd", mdhd_payload)
            + atom(b"minf", atom(b"stbl", atom(b"stts", stts_payload))))
    return atom(b"trak", atom(b"mdia", mdia))


#: A well-formed mdhd body: version+flags(4), created(4), modified(4),
#: timescale(4), duration(4).
GOOD_MDHD = b"\x00" * 12 + struct.pack(">II", 1000, 5000)


def atom_trees(depth: int = 3):
    """Random nested atom trees, the shape a corrupt card actually produces."""
    leaf = st.builds(
        lambda tag, body: atom(tag, body),
        st.sampled_from(LEAVES),
        st.binary(max_size=48),
    )
    if depth <= 0:
        return leaf
    return st.one_of(
        leaf,
        st.builds(
            lambda tag, kids: atom(tag, b"".join(kids)),
            st.sampled_from(CONTAINERS),
            st.lists(atom_trees(depth - 1), max_size=3),
        ),
    )


def write_braw(directory: Path, payload: bytes, name: str = "clip.braw") -> Path:
    target = directory / name
    target.write_bytes(payload)
    return target


# ---------------------------------------------------- BRAW: never crash a job


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(payload=st.binary(max_size=4096))
def test_braw_read_info_survives_arbitrary_bytes(payload: bytes, tmp_path_factory):
    """The cheapest fuzz there is: a .braw that is not BRAW at all."""
    target = write_braw(tmp_path_factory.mktemp("braw"), payload)
    try:
        braw.read_info(target)
    except PARSER_MAY_RAISE:
        pass
    except Exception as exc:                                # noqa: BLE001
        pytest.fail(f"read_info raised {type(exc).__name__}: {exc}")


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(trees=st.lists(atom_trees(), min_size=1, max_size=4))
def test_braw_read_info_survives_random_atom_trees(trees, tmp_path_factory):
    """Random well-formed-looking atom trees. This one holds: `_walk` and
    `_descend` bound-check honestly, so a tree that never resolves to a video
    track is parsed and discarded without complaint."""
    target = write_braw(tmp_path_factory.mktemp("braw"), braw_file(b"".join(trees)))
    try:
        braw.read_info(target)
    except PARSER_MAY_RAISE:
        pass
    except Exception as exc:                                # noqa: BLE001
        pytest.fail(f"read_info raised {type(exc).__name__}: {exc}")


@pytest.mark.xfail(strict=True, reason=(
    "braw.py:333/335/338/344/349 index and unpack at offsets derived from an "
    "atom's *declared* size without checking the buffer actually extends that "
    "far, so a short mdhd or a lying stts entry_count raises struct.error / "
    "IndexError straight out of read_info"))
@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(mdhd=st.binary(max_size=40), stts=st.binary(max_size=40),
       trailing=st.lists(atom_trees(2), max_size=2))
def test_braw_read_info_survives_truncated_video_traks(mdhd, stts, trailing,
                                                       tmp_path_factory):
    """The sweep above never finds the crash because a random tree almost never
    grows the `vide` handler that `_read_timing` requires. Pin that shape and
    fuzz only the two atom bodies it then reads at fixed offsets, which is
    exactly where the bound checks are missing."""
    payload = video_trak(mdhd, stts) + b"".join(trailing)
    target = write_braw(tmp_path_factory.mktemp("braw"), braw_file(payload))
    try:
        braw.read_info(target)
    except PARSER_MAY_RAISE:
        pass
    except Exception as exc:                                # noqa: BLE001
        pytest.fail(f"read_info raised {type(exc).__name__}: {exc}")


@pytest.mark.parametrize("label, mdhd, stts", [
    ("mdhd carries no body at all",
     b"", b"\x00" * 4 + struct.pack(">I", 1)),
    ("mdhd stops before the v0 timescale",
     b"\x00" * 4, b"\x00" * 4 + struct.pack(">I", 1)),
    ("mdhd claims v1 but is only v0-sized",
     b"\x01" + b"\x00" * 19, b"\x00" * 4 + struct.pack(">I", 1)),
    ("stts entry_count promises entries that are not there",
     GOOD_MDHD, b"\x00" * 4 + struct.pack(">I", 4096)),
])
@pytest.mark.xfail(strict=True, reason=(
    "braw._read_timing trusts the declared atom size over the real buffer "
    "length; see braw.py:333-350"))
def test_braw_truncated_atoms_do_not_raise(label, mdhd, stts, tmp_path):
    """Minimised regressions for the structured sweep above."""
    target = write_braw(tmp_path, braw_file(video_trak(mdhd, stts)))
    braw.read_info(target)


@pytest.mark.xfail(strict=True, reason=(
    "braw._descend (braw.py:225) recurses once per nested container with no "
    "depth limit, so ~1000 nested atoms exhaust the interpreter stack"))
def test_braw_deep_nesting_does_not_exhaust_the_stack(tmp_path):
    """8 bytes per level, so a 16 KB file is enough to blow the stack."""
    nest = atom(b"stbl")
    for _ in range(2000):
        nest = atom(b"minf", nest)
    target = write_braw(tmp_path, braw_file(atom(b"trak", atom(b"mdia", nest))))
    braw.read_info(target)


class _RecordingHandle:
    """A file handle that remembers the largest read it was ever asked for.

    Used instead of actually planting a 1 TB atom size, which would prove the
    same point by exhausting the machine's memory.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.largest_read = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._data) - self._pos
        self.largest_read = max(self.largest_read, size)
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def seek(self, pos: int, whence: int = 0) -> int:
        self._pos = pos if whence == 0 else self._pos + pos
        return self._pos

    def tell(self) -> int:
        return self._pos


@pytest.mark.xfail(strict=True, reason=(
    "braw._find_moov (braw.py:198) does `handle.read(size - header_length)` "
    "with a size taken straight from the file (up to 2**64-1 via the "
    "extended-size path), without the `offset + size > file_size` check that "
    "check_container (braw.py:177) does perform"))
@given(claimed=st.integers(min_value=2 ** 32, max_value=2 ** 48))
@settings(deadline=None)
def test_find_moov_never_reads_past_the_end_of_the_file(claimed: int):
    """A moov header may claim any size it likes. The parser must not believe
    it: the read it issues has to be bounded by the file that is actually
    there."""
    data = atom(b"ftyp", b"braw") + atom(b"moov", b"", declared=claimed)
    handle = _RecordingHandle(data)
    braw._find_moov(cast("BinaryIO", handle), len(data))
    assert handle.largest_read <= len(data), (
        f"asked the OS for {handle.largest_read:,} bytes "
        f"from a {len(data):,}-byte file")


# ------------------------------------------- one bad file must not kill a job


def _offload(source: Path, dest: Path, **overrides) -> Job:
    options: dict[str, Any] = dict(
        destinations=[dest], thumbnail_count=0, extra_probe=False,
        verification=VerificationMode.FULL)
    options.update(overrides)
    return engine.run(source, OffloadOptions(**options))


@pytest.mark.xfail(strict=True, reason=(
    "engine.run calls probe_mod.probe(source) with no try/except, and "
    "probe.probe's BRAW branch (probe.py:133-134) has none either, so any "
    "braw.py crash aborts the whole job: the clips after the bad one are "
    "never copied and no report is written"))
def test_one_malformed_clip_does_not_abort_the_offload(tmp_path):
    """The scenario this tool exists for: a card with one bad clip. The bad
    clip may fail. The other two must still land, and the job must still
    return a Job that says so."""
    source, dest = tmp_path / "card", tmp_path / "dst"
    source.mkdir()
    (source / "A001_C001.mov").write_bytes(b"A" * 5000)
    write_braw(source, braw_file(video_trak(b"", b"\x00" * 4 + struct.pack(">I", 1))),
               "A001_C002.braw")
    (source / "A001_C003.mov").write_bytes(b"C" * 5000)

    job = _offload(source, dest, extra_probe=True)

    copied = {p.name for p in dest.rglob("*") if p.is_file()}
    assert {"A001_C001.mov", "A001_C003.mov"} <= copied, (
        f"a malformed clip stopped the good ones landing; got {sorted(copied)}")
    assert len(job.files) == 3


# --------------------------------------------------- destination injectivity


def test_destination_paths_are_injective_when_structure_is_preserved(tmp_path):
    """The control case: with the tree preserved, distinct sources cannot
    collide. This one passes, and pins the behaviour the flat case breaks."""
    source, dest = tmp_path / "card", tmp_path / "dst"
    (source / "A").mkdir(parents=True)
    (source / "B").mkdir(parents=True)
    (source / "A" / "clip.mov").write_bytes(b"A" * 1000)
    (source / "B" / "clip.mov").write_bytes(b"B" * 2000)

    _offload(source, dest)

    landed = sorted(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    assert landed == [1000, 2000]


@pytest.mark.xfail(strict=True, reason=(
    "engine._destination_for (engine.py:232) maps every source onto "
    "`dest_root / source.name` when preserve_structure is False, and nothing "
    "checks two sources for the same target. engine.py:85's `seen` dict "
    "dedupes destination *roots* only. Reachable as `offloader --flat` and as "
    "the GUI's 'Recreate the source folder structure' checkbox"))
@settings(max_examples=30, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture,
                                 HealthCheck.too_slow])
@given(folders=st.lists(st.text(alphabet="ABCDEFG", min_size=1, max_size=3),
                        min_size=2, max_size=5, unique=True),
       basename=st.sampled_from(["clip.mov", "A001_C001.mov", "take.braw"]))
def test_flat_mode_never_silently_drops_a_file(folders, basename, tmp_path_factory):
    """Every byte that leaves the card must arrive, or the job must say it
    didn't. Reporting VERIFIED for a file that is not at the destination is the
    one outcome a verified-copy tool may never produce."""
    root = tmp_path_factory.mktemp("flat")
    source, dest = root / "card", root / "dst"
    for index, folder in enumerate(folders):
        (source / folder).mkdir(parents=True)
        (source / folder / basename).write_bytes(bytes([65 + index]) * (1000 + index))

    job = _offload(source, dest, preserve_structure=False)

    source_bytes = sum(p.stat().st_size for p in source.rglob("*") if p.is_file())
    dest_bytes = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())
    verified = [d for entry in job.files for d in entry.destinations
                if d.status is FileStatus.VERIFIED]

    if dest_bytes < source_bytes:
        pytest.fail(
            f"{len(folders)} sources ({source_bytes} bytes) collapsed to "
            f"{dest_bytes} bytes at the destination, and {len(verified)} of "
            f"{len(job.files)} destinations still report VERIFIED")


@pytest.mark.xfail(strict=True, reason="same collision as the sweep above")
def test_flat_mode_collision_is_reported_not_silent(tmp_path):
    """Minimised regression: two takes, one surviving file, a clean report."""
    source, dest = tmp_path / "card", tmp_path / "dst"
    (source / "A").mkdir(parents=True)
    (source / "B").mkdir(parents=True)
    (source / "A" / "clip.mov").write_bytes(b"A" * 1000)
    (source / "B" / "clip.mov").write_bytes(b"B" * 2000)

    job = _offload(source, dest, preserve_structure=False)

    landed = [p for p in dest.rglob("*") if p.is_file()]
    failures = [d for entry in job.files for d in entry.destinations
                if d.status is not FileStatus.VERIFIED]
    assert len(landed) == 2 or failures, (
        f"{len(job.files)} files became {len(landed)} on disk, "
        f"and not one destination reported a problem")


# ------------------------------------------------------------ scanning cycles


@pytest.mark.skipif(not WINDOWS, reason="directory junctions are Windows-only")
@pytest.mark.xfail(strict=True, reason=(
    "engine.scan (engine.py:215) is a bare os.walk with no cycle guard, and "
    "Path.is_symlink() is False for a junction, so the usual symlink check "
    "would not help either. It terminates only because Windows refuses paths "
    "past MAX_PATH; with long paths enabled it would not"))
def test_scan_does_not_follow_a_directory_junction_into_a_cycle(tmp_path):
    card = tmp_path / "card"
    (card / "sub").mkdir(parents=True)
    (card / "sub" / "clip.mov").write_bytes(b"x" * 10)

    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(card / "sub" / "loop"), str(card)],
        capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip(f"could not create a junction: {made.stderr.strip()}")

    found: list[int] = []
    worker = threading.Thread(target=lambda: found.append(len(engine.scan(card))),
                              daemon=True)
    worker.start()
    worker.join(timeout=30)
    assert not worker.is_alive(), "scan() did not terminate on a junction cycle"
    assert found == [1], (
        f"one real file was scanned {found[0]} times through the junction")


# ----------------------------------------------------------- report totality


def _job_named(name: str, root: Path) -> Job:
    when = _dt.datetime(2026, 8, 4, 12, 0).timestamp()
    entry = FileEntry(
        source=root / name, source_root=root, size=10, created=when,
        modified=when, checksum="abc123", media=MediaInfo(),
        destinations=[Destination(root=root, path=root / name,
                                  status=FileStatus.VERIFIED, checksum="abc123",
                                  created=when, modified=when)],
    )
    return Job(name="A001", source_root=root, destination_roots=[root],
               verification=VerificationMode.SOURCE_ONLY,
               hash_label="XXHash3-64",
               started=_dt.datetime(2026, 8, 4, 5, 0),
               finished=_dt.datetime(2026, 8, 4, 6, 0), files=[entry])


#: Names a real filesystem can hand back. The lone surrogate is not exotic: it
#: is what os.listdir returns via surrogateescape for any POSIX filename that
#: is not valid UTF-8, and NTFS accepts unpaired surrogates outright.
UNENCODABLE_NAMES = ["clip\ud800.mov", "clip\udcff.mov"]
ILLEGAL_XML_NAMES = ["clip\x00.mov", "clip\x0b.mov", "clip\x1f.mov"]


@pytest.mark.parametrize("name", ILLEGAL_XML_NAMES + UNENCODABLE_NAMES)
def test_write_mhl_is_total(name, tmp_path):
    """reports/mhl.py filters to the XML 1.0 character set through `_xml_safe`
    (reports/mhl.py:31-46) before ElementTree sees anything. It holds up: this
    is the behaviour the other two writers are missing."""
    out = write_mhl(_job_named(name, tmp_path), tmp_path / "JobReport.mhl")
    assert ET.parse(out).getroot().tag == "hashlist"


@pytest.mark.parametrize("name", UNENCODABLE_NAMES)
@pytest.mark.xfail(strict=True, reason=(
    "csv_report.py:47 opens with the default errors='strict' and does no "
    "filtering, so writer.writerow (csv_report.py:96) raises "
    "UnicodeEncodeError. write_mhl survives the same name because it has "
    "_xml_safe; write_csv has no equivalent"))
def test_write_csv_is_total(name, tmp_path):
    """A report writer runs *after* the bytes are safely copied. Crashing there
    turns a finished offload into one with no paperwork."""
    write_csv(_job_named(name, tmp_path), tmp_path / "JobReport.csv")


# ------------------------------------------------------- ASC MHL round trip


@pytest.mark.parametrize("name", ["clip.mov", "a b.mov", "café.mov", "клип.mov"])
def test_ascmhl_round_trips_ordinary_names(name, tmp_path):
    """The chain of custody is only worth anything if what was written can be
    read back. This is the control case."""
    manifest = ascmhl.write_manifest(_job_named(name, tmp_path), tmp_path)
    assert ascmhl.read_manifest_hashes(manifest), "wrote a manifest, read back nothing"


@pytest.mark.parametrize("name", ILLEGAL_XML_NAMES + UNENCODABLE_NAMES)
@pytest.mark.xfail(strict=True, reason=(
    "ascmhl.py has no _xml_safe equivalent: the <path> text at ascmhl.py:355 "
    "is written raw. Control characters produce a manifest that will not "
    "reparse, and read_manifest_hashes swallows the ParseError "
    "(ascmhl.py:176) and returns {}, so the file vanishes from the chain of "
    "custody silently. A lone surrogate fails earlier, in the UTF-8 encode at "
    "ascmhl.py:212"))
def test_ascmhl_round_trips_hostile_names(name, tmp_path):
    """Whatever the name, `write_manifest` must either refuse it outright or
    produce a manifest `read_manifest_hashes` can fully recover. Writing a
    manifest that silently reads back empty is the one thing it may not do."""
    manifest = ascmhl.write_manifest(_job_named(name, tmp_path), tmp_path)
    assert ascmhl.read_manifest_hashes(manifest), (
        "manifest was written but reads back empty: the file is now missing "
        "from the chain of custody with nothing to indicate it")


# -------------------------------------------------------------- retry policy


@pytest.mark.xfail(strict=True, reason=(
    "RetryPolicy is a frozen dataclass with no __post_init__ validation, and "
    "presets.py builds one straight from presets.json, so a negative delay "
    "reaches retry.py:114's `if pause: sleep(pause)`, and time.sleep raises "
    "ValueError on a negative argument"))
@given(attempts=st.integers(min_value=-5, max_value=12),
       delay=st.floats(min_value=-10, max_value=10, allow_nan=False,
                       allow_infinity=False),
       backoff=st.floats(min_value=-4, max_value=4, allow_nan=False,
                         allow_infinity=False))
@settings(deadline=None)
def test_retry_never_asks_to_sleep_for_a_negative_time(attempts, delay, backoff):
    """`RetryPolicy` is a frozen dataclass with no validation, and presets.py
    builds one straight from presets.json. A hand-edited negative delay must
    not reach time.sleep, which raises ValueError on a negative argument."""
    policy = retry.RetryPolicy(attempts=attempts, delay=delay, backoff=backoff)
    slept: list[float] = []
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(errno.EIO, "transient")
        return "ok"

    try:
        retry.call(flaky, policy, sleep=slept.append)
    except OSError:
        pass

    assert all(pause >= 0 for pause in slept), (
        f"policy(delay={delay}, backoff={backoff}) asked for sleeps {slept}; "
        "time.sleep raises ValueError on a negative argument")


@pytest.mark.xfail(strict=True, reason=(
    "hashers.hash_file's chunk_size is unvalidated: read(0) returns b'' "
    "immediately, so iter(..., b'') stops before the first byte and a "
    "non-empty file hashes as empty. Not reachable from the CLI today, which "
    "is the only reason this is not a live data-integrity bug"))
def test_hash_file_rejects_a_zero_chunk_size(tmp_path):
    """A checksum that silently describes a different file than the one on
    disk is the worst failure mode this codebase has."""
    target = tmp_path / "clip.mov"
    target.write_bytes(b"X" * 100_000)
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")

    with pytest.raises(ValueError):
        digest = hash_file(target, "xxh3-64", chunk_size=0)
        assert digest != hash_file(empty, "xxh3-64"), (
            "a 100 KB file hashed to the empty-file digest")


# ------------------------------------------------- things that already hold


@given(st.lists(st.text(max_size=12), max_size=6))
@settings(deadline=None)
def test_exclude_patterns_never_raise(patterns):
    """Exclude globs come from `--exclude` and from presets.json, so they are
    user input. fnmatch tolerates unclosed brackets; confirm that stays true."""
    engine.is_excluded(Path("clip.mov"), patterns)


def test_empty_source_tree_produces_an_empty_job(tmp_path):
    """A card that mounted but scanned empty must not divide by a zero total."""
    source, dest = tmp_path / "card", tmp_path / "dst"
    source.mkdir()
    events: list[object] = []
    job = engine.run(source, OffloadOptions(destinations=[dest], thumbnail_count=0,
                                            extra_probe=False), progress=events.append)
    assert job.files == []


def test_zero_byte_file_verifies(tmp_path):
    """Cameras do leave 0-byte files behind after a card pull."""
    source, dest = tmp_path / "card", tmp_path / "dst"
    source.mkdir()
    (source / "empty.mov").write_bytes(b"")
    job = _offload(source, dest)
    assert [d.status for entry in job.files for d in entry.destinations] == \
        [FileStatus.VERIFIED]
    assert (dest / "empty.mov").stat().st_size == 0


# ----------------------------------------------------------- name exhaustion


@pytest.mark.parametrize("template, taken", [
    ("{index}", [f"{i:03d}" for i in range(1, 1000)]),
    ("{card}", ["A001"] + [f"A001-{i}" for i in range(2, 1000)]),
])
@pytest.mark.xfail(strict=True, reason=(
    "naming.build's two exhaustion fallbacks return an unchecked name: "
    "naming.py:82 returns render(..., index=999) and naming.py:91 returns "
    "`base`, neither tested against `used`. Two jobs then share one report "
    "folder, which is the exact outcome the dedup exists to prevent"))
def test_build_never_returns_a_taken_name_even_when_exhausted(template, taken):
    """`test_fuzz.py` already asserts this property, but with `max_size=8` it
    can never reach the 999-name ceiling where the guarantee lapses."""
    name = naming.build(template, Path("A001"), taken=taken)
    assert name.casefold() not in {t.casefold() for t in taken}, (
        f"build() handed back {name!r}, which is already taken")


# ---------------------------------------------------------- corrupt history


CORRUPT_HISTORY = [
    pytest.param("42", id="a bare number"),
    pytest.param('["oops"]', id="a list of strings"),
    pytest.param('[{"file_count": [1, 2]}]', id="file_count is a list"),
    pytest.param('[{"destinations": 7}]', id="destinations is a number"),
]


@pytest.mark.parametrize("body", CORRUPT_HISTORY)
@pytest.mark.xfail(strict=True, reason=(
    "History.__init__ maps HistoryEntry.from_dict over read_json's result with "
    "no try/except, and from_dict itself does bare int()/list() conversions, "
    "so a corrupt history.json raises out of the constructor"))
def test_corrupt_history_does_not_stop_an_offload(body, tmp_path):
    """history.py's own reason for existing is that a damaged history must
    never stop someone offloading a card. An exception from the constructor
    stops exactly that."""
    path = tmp_path / "history.json"
    path.write_text(body, encoding="utf-8")
    History(path)


@pytest.mark.xfail(strict=True, reason=(
    "config.read_json catches OSError and json.JSONDecodeError, but "
    "path.read_text raises UnicodeDecodeError (a ValueError) for invalid "
    "UTF-8, which is not caught"))
def test_history_survives_invalid_utf8(tmp_path):
    path = tmp_path / "history.json"
    path.write_bytes(b'[{"job_name": "\xff\xfe"}]')
    History(path)


def test_valid_history_still_round_trips(tmp_path):
    """The control case for the two above: a well-formed file must load."""
    path = tmp_path / "history.json"
    path.write_text(json.dumps([]), encoding="utf-8")
    assert History(path).entries == []
