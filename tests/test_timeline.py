"""Resolving an edit timeline to the media it references.

The resolver tests need no OpenTimelineIO: they build `Reference` objects
directly, because what is being tested is the decision, not the parse. Only
the reader tests need the adapter, and they skip without it.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest

from offloader import cli, engine, timeline
from offloader.models import FileStatus, VerificationMode
from offloader.timeline import Reference, Status

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """A drive with media already on it, in two different folders."""
    root = tmp_path / "library"
    _write(root / "package" / "A001_C001.mp4", b"picture one" * 100)
    _write(root / "audio" / "260822_001.wav", b"sound one" * 100)
    return root


def _ref(name: str, target: Path, uses: int = 1) -> Reference:
    return Reference(name=name, target=target, uses=uses)


#: A path on a volume that is not attached, which is how an editor's export
#: addresses everything. Nothing here should ever resolve by walking it.
def _elsewhere(name: str) -> Path:
    return Path(os.sep, "Volumes", "Edit Operational 14TB", "media", name)


# --------------------------------------------------------------------------
# The resolver
# --------------------------------------------------------------------------


def test_present_when_the_name_is_under_a_root(library: Path):
    """The editor's path is unreachable; the file is here under another one."""
    report = timeline.resolve([_ref("A001_C001.mp4", _elsewhere("A001_C001.mp4"))],
                              [library])
    (resolution,) = report.resolutions
    assert resolution.status is Status.PRESENT
    assert resolution.location == library / "package" / "A001_C001.mp4"


def test_gap_when_reachable_but_not_here(library: Path, tmp_path: Path):
    other = _write(tmp_path / "elsewhere" / "B002.mov", b"needs copying")
    report = timeline.resolve([_ref("B002.mov", other)], [library])
    (resolution,) = report.resolutions
    assert resolution.status is Status.GAP
    assert resolution.location == other
    assert report.gap_bytes == other.stat().st_size


def test_missing_when_nowhere_at_all(library: Path):
    report = timeline.resolve([_ref("gone.mov", _elsewhere("gone.mov"))], [library])
    (resolution,) = report.resolutions
    assert resolution.status is Status.MISSING
    assert resolution.location is None


def test_identical_duplicates_resolve_and_are_not_a_gap(library: Path):
    """The 32-file case: the same bytes filed twice under the search root.

    Copying these again is not merely wasted transfer, it puts a third file of
    that name on the drive and makes a name-only relink ambiguous where it was
    not. So they must read as PRESENT, and the duplicates must be reported.
    """
    same = (library / "package" / "A001_C001.mp4").read_bytes()
    _write(library / "backup" / "A001_C001.mp4", same)

    report = timeline.resolve([_ref("A001_C001.mp4", _elsewhere("A001_C001.mp4"))],
                              [library])
    (resolution,) = report.resolutions
    assert resolution.status is Status.PRESENT
    assert len(resolution.alternatives) == 1
    assert "byte-identical" in resolution.note
    assert report.gaps == []


def test_differing_duplicates_are_ambiguous_and_nothing_is_chosen(library: Path):
    """The purple-slate case, and the whole reason this module exists.

    An earlier conform left a stand-in slate of the same name as the real
    archival footage that arrived later. Both are under the search root. A
    tool that picks one puts a placeholder in a finished film and reports it
    as online.
    """
    _write(library / "stand-ins" / "88 Paralympics.mp4", b"purple slate")
    _write(library / "archival" / "88 Paralympics.mp4", b"the real footage" * 50)

    wanted = Path(library, "archival", "88 Paralympics.mp4")
    report = timeline.resolve([_ref("88 Paralympics.mp4", wanted)], [library])
    (resolution,) = report.resolutions

    assert resolution.status is Status.AMBIGUOUS
    assert resolution.location is None, "must not choose between different files"
    assert len(resolution.alternatives) == 2
    # The closest-path guess is offered, and labelled as a guess in the note.
    assert resolution.alternatives[0] == wanted
    assert "different files share this name" in resolution.note
    assert report.unresolved == [resolution]


def test_generated_reference_is_reported_not_dropped():
    report = timeline.resolve(
        [Reference(name="3 timeline-native item(s)", target=None, uses=3,
                   generated=True)],
        [],
        table={},
    )
    (resolution,) = report.resolutions
    assert resolution.status is Status.GENERATED
    assert report.gaps == []


# --------------------------------------------------------------------------
# Proxy substitution
# --------------------------------------------------------------------------


def test_camera_original_is_satisfied_by_a_proxy_already_here(
        library: Path, tmp_path: Path, monkeypatch):
    """55 GB of BRAW need not move when a frame-matched proxy is on the drive."""
    original = _write(tmp_path / "camera" / "A006_C121.braw", b"raw" * 1000)
    proxy = _write(library / "package" / "A006_C121.mp4", b"proxy")
    monkeypatch.setattr(timeline, "_frames", lambda path: 1065)

    report = timeline.resolve([_ref("A006_C121.braw", original)], [library])
    (resolution,) = report.resolutions

    assert resolution.status is Status.SUBSTITUTED
    assert resolution.location == proxy
    assert "1065 frames" in resolution.note
    assert report.gaps == [], "the original must not also be copied"


def test_proxy_of_the_wrong_length_is_refused(library: Path, tmp_path: Path,
                                              monkeypatch):
    """A shorter proxy moves every edit point after it. Never substitute it."""
    original = _write(tmp_path / "camera" / "A006_C121.braw", b"raw" * 1000)
    _write(library / "package" / "A006_C121.mp4", b"proxy")
    lengths = {"braw": 1065, "mp4": 900}
    monkeypatch.setattr(timeline, "_frames",
                        lambda path: lengths[Path(path).suffix.lstrip(".")])

    report = timeline.resolve([_ref("A006_C121.braw", original)], [library])
    (resolution,) = report.resolutions

    assert resolution.status is Status.AMBIGUOUS
    assert resolution.location is None
    assert "does not match" in resolution.note


def test_unverifiable_frame_count_says_so_rather_than_implying_agreement(
        library: Path, tmp_path: Path, monkeypatch):
    original = _write(tmp_path / "camera" / "A006_C121.braw", b"raw" * 1000)
    _write(library / "package" / "A006_C121.mp4", b"proxy")
    monkeypatch.setattr(timeline, "_frames", lambda path: None)

    report = timeline.resolve([_ref("A006_C121.braw", original)], [library])
    (resolution,) = report.resolutions
    assert resolution.status is Status.SUBSTITUTED
    assert "unverified" in resolution.note


def test_substitution_can_be_switched_off(library: Path, tmp_path: Path):
    original = _write(tmp_path / "camera" / "A006_C121.braw", b"raw" * 1000)
    _write(library / "package" / "A006_C121.mp4", b"proxy")

    report = timeline.resolve([_ref("A006_C121.braw", original)], [library],
                              substitute_proxies=False)
    (resolution,) = report.resolutions
    assert resolution.status is Status.GAP


# --------------------------------------------------------------------------
# Turning a report into a file set
# --------------------------------------------------------------------------


def test_selection_carries_only_the_gaps(library: Path, tmp_path: Path):
    gap = _write(tmp_path / "elsewhere" / "B002.mov", b"needs copying")
    references = [
        _ref("A001_C001.mp4", _elsewhere("A001_C001.mp4")),   # present
        _ref("B002.mov", gap),                                 # gap
        _ref("gone.mov", _elsewhere("gone.mov")),              # missing
    ]
    selection = timeline.selection(timeline.resolve(references, [library]))
    assert [f.source for f in selection] == [gap]


def test_mirror_layout_keeps_the_path_below_the_volume(library: Path,
                                                       tmp_path: Path):
    gap = _write(tmp_path / "elsewhere" / "deep" / "B002.mov", b"x")
    (entry,) = timeline.selection(
        timeline.resolve([_ref("B002.mov", gap)], [library]))
    assert entry.relative == Path(*gap.parts[1:])
    assert not entry.relative.is_absolute()


def test_flat_layout_is_the_basename(library: Path, tmp_path: Path):
    gap = _write(tmp_path / "elsewhere" / "deep" / "B002.mov", b"x")
    (entry,) = timeline.selection(
        timeline.resolve([_ref("B002.mov", gap)], [library]), flat=True)
    assert entry.relative == Path("B002.mov")


# --------------------------------------------------------------------------
# The engine, offloading a selection
# --------------------------------------------------------------------------


def _options(tmp_path: Path, **overrides) -> engine.OffloadOptions:
    defaults = dict(
        destinations=[tmp_path / "dest"],
        algorithm="xxh3-64",
        verification=VerificationMode.FULL,
        thumbnail_count=0,
        extra_probe=False,
    )
    defaults.update(overrides)
    return engine.OffloadOptions(**defaults)


def test_selection_offloads_files_from_several_volumes(tmp_path: Path):
    """The change this whole feature rests on: one job, many source trees.

    Without the selection path this cannot be expressed at all -- `run` scans
    a single root, so the second file could only be reached by a second job
    with its own report.
    """
    one = _write(tmp_path / "treeA" / "clips" / "A.mov", b"a" * 500)
    two = _write(tmp_path / "treeB" / "sound" / "B.wav", b"b" * 500)
    selection = [
        engine.SelectedFile(one, Path("treeA", "clips", "A.mov"), tmp_path),
        engine.SelectedFile(two, Path("treeB", "sound", "B.wav"), tmp_path),
    ]

    job = engine.run(tmp_path / "treeA", _options(tmp_path, selection=selection))

    assert job.total_files == 2
    assert all(f.status is FileStatus.VERIFIED for f in job.files)
    assert (tmp_path / "dest" / "treeA" / "clips" / "A.mov").read_bytes() == b"a" * 500
    assert (tmp_path / "dest" / "treeB" / "sound" / "B.wav").read_bytes() == b"b" * 500


def test_selection_ignores_the_scan_and_its_excludes(tmp_path: Path):
    """Only the selected file moves, not everything beside it."""
    one = _write(tmp_path / "tree" / "A.mov", b"a" * 100)
    _write(tmp_path / "tree" / "B.mov", b"b" * 100)
    selection = [engine.SelectedFile(one, Path("A.mov"), tmp_path / "tree")]

    job = engine.run(tmp_path / "tree", _options(tmp_path, selection=selection))

    assert job.total_files == 1
    assert not (tmp_path / "dest" / "B.mov").exists()


def test_a_destination_inside_a_search_root_is_allowed(tmp_path: Path):
    """The ordinary case for this feature, and the one the card rule refuses.

    Collecting a cut's gaps into a new folder on the same drive the cut was
    resolved against is normal. `assert_safe_destinations` would reject it,
    because for a card "destination inside source" means you are about to eat
    the card.
    """
    library = tmp_path / "library"
    source = _write(tmp_path / "other" / "A.mov", b"a" * 100)
    dest = library / "collected"
    dest.mkdir(parents=True)
    selection = [engine.SelectedFile(source, Path("A.mov"), tmp_path / "other")]

    job = engine.run(library, _options(tmp_path, destinations=[dest],
                                       selection=selection))
    assert job.total_files == 1
    assert (dest / "A.mov").exists()


def test_a_source_inside_the_destination_is_refused(tmp_path: Path):
    """The narrower rule that replaces it: never read a file you are writing."""
    dest = tmp_path / "dest"
    inside = _write(dest / "already" / "A.mov", b"a" * 100)
    selection = [engine.SelectedFile(inside, Path("already", "A.mov"), dest)]

    with pytest.raises(engine.UnsafeDestination, match="inside the destination"):
        engine.run(tmp_path, _options(tmp_path, selection=selection))


def test_a_relative_path_that_climbs_out_is_refused(tmp_path: Path):
    source = _write(tmp_path / "tree" / "A.mov", b"a" * 100)
    selection = [engine.SelectedFile(source, Path("..", "escaped.mov"),
                                     tmp_path / "tree")]

    with pytest.raises(engine.UnsafeDestination, match="escapes the destination"):
        engine.run(tmp_path, _options(tmp_path, selection=selection))
    assert not (tmp_path / "escaped.mov").exists()


def test_an_absolute_relative_path_is_refused(tmp_path: Path):
    source = _write(tmp_path / "tree" / "A.mov", b"a" * 100)
    elsewhere = tmp_path / "not-the-destination" / "A.mov"
    selection = [engine.SelectedFile(source, elsewhere, tmp_path / "tree")]

    with pytest.raises(engine.UnsafeDestination, match="absolute destination"):
        engine.run(tmp_path, _options(tmp_path, selection=selection))
    assert not elsewhere.exists()


def test_two_selected_files_landing_on_one_path_are_refused(tmp_path: Path):
    """Flattening two trees is exactly how a selection loses a file.

    Both would be copied, both verified as they landed, and the report would
    attest to two files where the drive holds one.
    """
    one = _write(tmp_path / "treeA" / "A.mov", b"a" * 100)
    two = _write(tmp_path / "treeB" / "A.mov", b"b" * 100)
    selection = [
        engine.SelectedFile(one, Path("A.mov"), tmp_path / "treeA"),
        engine.SelectedFile(two, Path("A.mov"), tmp_path / "treeB"),
    ]

    with pytest.raises(engine.UnsafeDestination, match="silently overwrite"):
        engine.run(tmp_path, _options(tmp_path, selection=selection))


def test_the_same_file_twice_in_a_selection_is_refused(tmp_path: Path):
    one = _write(tmp_path / "tree" / "A.mov", b"a" * 100)
    selection = [
        engine.SelectedFile(one, Path("A.mov"), tmp_path / "tree"),
        engine.SelectedFile(one, Path("copy", "A.mov"), tmp_path / "tree"),
    ]
    with pytest.raises(ValueError, match="same source file twice"):
        engine.run(tmp_path, _options(tmp_path, selection=selection))


def test_report_shows_a_placeable_path_for_each_tree(tmp_path: Path):
    one = _write(tmp_path / "treeA" / "clips" / "A.mov", b"a" * 100)
    selection = [engine.SelectedFile(one, Path("treeA", "clips", "A.mov"),
                                     tmp_path)]
    job = engine.run(tmp_path, _options(tmp_path, selection=selection))
    assert job.files[0].relative == Path("treeA", "clips", "A.mov")


def test_a_job_named_after_a_timeline_drops_the_suffix(tmp_path: Path):
    """The job name reaches a report header, where ".xml" helps nobody."""
    source = _write(tmp_path / "tree" / "A.mov", b"a" * 100)
    cut = _timeline_file(tmp_path, "file://localhost/X/A.mov",
                         "file://localhost/X/B.wav")
    selection = [engine.SelectedFile(source, Path("A.mov"), tmp_path / "tree")]

    job = engine.run(cut, _options(tmp_path, selection=selection))
    assert job.name == "cut"


# --------------------------------------------------------------------------
# Reading a real timeline
# --------------------------------------------------------------------------

FCP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence id="sequence-1">
    <name>Test Sequence</name>
    <duration>240</duration>
    <rate><timebase>24</timebase><ntsc>FALSE</ntsc></rate>
    <media>
      <video>
        <track>
          <clipitem id="clipitem-1">
            <name>A001_C001.mp4</name>
            <duration>120</duration>
            <rate><timebase>24</timebase><ntsc>FALSE</ntsc></rate>
            <start>0</start><end>60</end><in>0</in><out>60</out>
            <file id="file-1">
              <name>A001_C001.mp4</name>
              <pathurl>{first}</pathurl>
              <rate><timebase>24</timebase><ntsc>FALSE</ntsc></rate>
              <duration>120</duration>
            </file>
          </clipitem>
          <clipitem id="clipitem-2">
            <name>A001_C001.mp4</name>
            <duration>120</duration>
            <rate><timebase>24</timebase><ntsc>FALSE</ntsc></rate>
            <start>60</start><end>90</end><in>60</in><out>90</out>
            <file id="file-1"/>
          </clipitem>
          <clipitem id="clipitem-3">
            <name>260822_001.wav</name>
            <duration>120</duration>
            <rate><timebase>24</timebase><ntsc>FALSE</ntsc></rate>
            <start>90</start><end>120</end><in>0</in><out>30</out>
            <file id="file-2">
              <name>260822_001.wav</name>
              <pathurl>{second}</pathurl>
              <rate><timebase>24</timebase><ntsc>FALSE</ntsc></rate>
              <duration>120</duration>
            </file>
          </clipitem>
        </track>
      </video>
    </media>
  </sequence>
</xmeml>
"""


def _timeline_file(tmp_path: Path, first: str, second: str) -> Path:
    path = tmp_path / "cut.xml"
    path.write_text(FCP_XML.format(first=first, second=second), encoding="utf-8")
    return path


def _has_adapter() -> bool:
    try:
        import opentimelineio as otio
        return "fcp_xml" in set(otio.adapters.available_adapter_names())
    except ImportError:
        return False


needs_otio = pytest.mark.skipif(
    not _has_adapter(), reason='needs pip install "offloader[timeline]"')


@needs_otio
def test_reads_every_reference_including_id_only_clipitems(tmp_path: Path):
    """A clipitem may name its file by id and carry no path of its own.

    Both of the first clip's uses must be counted against one reference. A
    sweep that only reads `pathurl` finds a source's first use and silently
    misses every later one, which is how a file gets left out of a package.
    """
    path = _timeline_file(tmp_path,
                          "file://localhost/X/media/A001_C001.mp4",
                          "file://localhost/X/audio/260822_001.wav")
    references = timeline.read_references(path)

    by_name = {r.name: r for r in references}
    assert set(by_name) == {"A001_C001.mp4", "260822_001.wav"}
    assert by_name["A001_C001.mp4"].uses == 2
    assert by_name["260822_001.wav"].uses == 1


@needs_otio
def test_percent_escapes_in_a_path_are_decoded(tmp_path: Path):
    path = _timeline_file(tmp_path,
                          "file://localhost/X/media/Shop%20Machines.mp4",
                          "file://localhost/X/audio/260822_001.wav")
    names = {r.name for r in timeline.read_references(path)}
    assert "Shop Machines.mp4" in names


@needs_otio
def test_read_then_resolve_end_to_end(tmp_path: Path, library: Path):
    """The whole path: an unreachable export, resolved against a real drive."""
    path = _timeline_file(tmp_path,
                          "file://localhost/Volumes/Edit%2014TB/A001_C001.mp4",
                          "file://localhost/Volumes/Edit%2014TB/260822_001.wav")
    report = timeline.resolve(timeline.read_references(path), [library],
                              timeline=path)
    assert report.counts[Status.PRESENT] == 2
    assert report.gaps == []


@pytest.mark.parametrize("url, expected", [
    # Premiere on Windows, and Resolve's variant of the same.
    ("file://localhost/E:/Media/a.mov", "E:/Media/a.mov"),
    ("file:///E:/Media/a.mov", "E:/Media/a.mov"),
    # An editor cutting on a Mac. The leading slash is part of the path and
    # must survive: dropping it yields a *relative* path that still resolves
    # by basename, so nothing looks broken while every "is it where the
    # timeline says?" test quietly answers no.
    ("file:///Volumes/Edit/a.mov", "/Volumes/Edit/a.mov"),
    ("file://localhost/Volumes/Edit/a.mov", "/Volumes/Edit/a.mov"),
    # Percent-escapes, because clip names contain spaces.
    ("file:///Volumes/Edit%2014TB/Shop%20Machines.mp4",
     "/Volumes/Edit 14TB/Shop Machines.mp4"),
    # A bare path, from a hand-edited file.
    ("E:/Media/a.mov", "E:/Media/a.mov"),
])
def test_file_urls_survive_both_platforms(url: str, expected: str):
    """Pure function, so it runs identically on every runner."""
    got = timeline._url_to_path(url)
    assert got is not None
    assert got.as_posix() == expected


def test_a_unc_url_keeps_its_leading_pair():
    got = timeline._url_to_path("file://server/share/a.mov")
    assert got is not None
    assert got.as_posix().startswith("//server/share")


def test_unknown_suffix_is_refused_with_the_formats_it_knows(tmp_path: Path):
    path = tmp_path / "cut.premiereproj"
    path.write_text("not interchange", encoding="utf-8")
    with pytest.raises(timeline.TimelineSupportMissing, match="no timeline adapter"):
        timeline.read_references(path)


def test_a_missing_timeline_says_so(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        timeline.read_references(tmp_path / "nope.xml")


# --------------------------------------------------------------------------
# Through the command line
# --------------------------------------------------------------------------


def test_resolve_without_a_search_root_is_a_usage_error(tmp_path: Path, capsys):
    """Without one, every reference is a gap and the whole cut gets copied."""
    cut = _timeline_file(tmp_path, "file://localhost/X/A.mov",
                         "file://localhost/X/B.wav")
    code = cli.main(["resolve", "--timeline", str(cut)])
    assert code == 2
    assert "--search-root" in capsys.readouterr().err


@needs_otio
def test_resolve_exits_non_zero_when_something_is_unsettled(
        tmp_path: Path, library: Path, capsys):
    _write(library / "stand-ins" / "A001_C001.mp4", b"a different file")
    cut = _timeline_file(tmp_path,
                         "file://localhost/X/A001_C001.mp4",
                         "file://localhost/X/260822_001.wav")

    code = cli.main(["resolve", "--timeline", str(cut),
                     "--search-root", str(library)])
    assert code == 1
    assert "MORE THAN ONE CANDIDATE" in capsys.readouterr().out


@needs_otio
def test_resolve_is_clean_when_everything_is_here(tmp_path: Path, library: Path):
    cut = _timeline_file(tmp_path,
                         "file://localhost/X/A001_C001.mp4",
                         "file://localhost/X/260822_001.wav")
    assert cli.main(["resolve", "--timeline", str(cut),
                     "--search-root", str(library)]) == 0


@needs_otio
def test_resolve_writes_a_row_per_reference(tmp_path: Path, library: Path):
    cut = _timeline_file(tmp_path,
                         "file://localhost/X/A001_C001.mp4",
                         "file://localhost/X/260822_001.wav")
    out = tmp_path / "resolved.csv"
    cli.main(["resolve", "--timeline", str(cut), "--search-root", str(library),
              "--csv", str(out), "--quiet"])

    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert {r["file"] for r in rows} == {"A001_C001.mp4", "260822_001.wav"}
    assert all(r["status"] == "present" for r in rows)


@needs_otio
def test_offload_from_a_timeline_copies_only_the_gap(tmp_path: Path,
                                                     library: Path):
    gap = _write(tmp_path / "elsewhere" / "260822_001.wav", b"not on the drive")
    (library / "audio" / "260822_001.wav").unlink()
    cut = _timeline_file(tmp_path,
                         "file://localhost/X/A001_C001.mp4",
                         gap.as_uri())
    dest = tmp_path / "dest"

    code = cli.main(["offload", "--timeline", str(cut),
                     "--search-root", str(library), "--dest", str(dest),
                     "--report", "csv", "--no-probe", "--quiet"])

    assert code == 0
    landed = list(dest.rglob("260822_001.wav"))
    assert len(landed) == 1
    assert landed[0].read_bytes() == b"not on the drive"
    # The file already under the search root is not copied again.
    assert not list(dest.rglob("A001_C001.mp4"))
