"""A timeline offload must never call a partial cut complete.

Every test here is a way the cut could be short of media while the terminal,
the exit code or the paperwork said otherwise: references that are missing,
ambiguous, offline, unsupported or remote; two different recordings sharing a
name; a stand-in slate where the real file was reachable; a proxy nobody
could check; a cancel; a relative path read against the wrong directory.

Timeline files are written with OpenTimelineIO's own `otio_json` adapter, so
the reader under test is the real one.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from offloader import cli, engine, timeline
from offloader.models import (
    Destination,
    FileEntry,
    FileStatus,
    Job,
    VerificationMode,
)
from offloader.reports import write_csv, write_html, write_pdf
from offloader.timeline import Reference, Status

try:
    import opentimelineio as otio
except ImportError:                       # pragma: no cover - optional extra
    otio = None

needs_otio = pytest.mark.skipif(otio is None, reason="needs opentimelineio")

#: The heading every report gives what a timeline job did not supply. Spelled
#: out rather than imported: it is what a reader of the paperwork sees.
UNRESOLVED_TITLE = "Unresolved timeline references, NOT supplied by this job"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _otio(path: Path, refs: list, clip_names: list[str] | None = None) -> Path:
    """An .otio cut with one clip per entry of `refs`.

    A str or Path entry becomes an ExternalReference: an absolute path is
    written as a file URL, anything else verbatim (a relative path, an
    https URL). Any other entry is used as the media reference itself.
    """
    cut = otio.schema.Timeline(name="cut")
    track = otio.schema.Track(kind=otio.schema.TrackKind.Video)
    cut.tracks.append(track)
    span = otio.opentime.TimeRange(otio.opentime.RationalTime(0, 24),
                                   otio.opentime.RationalTime(24, 24))
    for index, ref in enumerate(refs):
        if isinstance(ref, (str, Path)):
            url = Path(ref).as_uri() if Path(ref).is_absolute() else str(ref)
            media = otio.schema.ExternalReference(target_url=url)
        else:
            media = ref
        name = clip_names[index] if clip_names else f"clip{index}"
        track.append(otio.schema.Clip(name=name, media_reference=media,
                                      source_range=span))
    path.parent.mkdir(parents=True, exist_ok=True)
    otio.adapters.write_to_file(cut, str(path))
    return path


def _offload(cut: Path, root: Path, dest: Path, *extra: str) -> int:
    return cli.main(["offload", "--timeline", str(cut), "--search-root", str(root),
                     "--dest", str(dest), "--report", "csv", "--no-probe",
                     "--quiet", "--retries", "1", *extra])


def _csv_status(dest: Path) -> str:
    (report,) = list(dest.rglob("*.csv"))
    header = report.read_text(encoding="utf-8").splitlines()[1]
    return next(cell for cell in header.split(",") if cell.startswith("Status:"))


@pytest.fixture
def s1(tmp_path: Path):
    """present.mov here, missing.mov nowhere, amb.mov twice and different."""
    root = tmp_path / "drive"
    _write(root / "present.mov", b"P" * 1000)
    _write(root / "a" / "amb.mov", b"A" * 1000)
    _write(root / "b" / "amb.mov", b"B" * 1000)
    editor = tmp_path / "editor"
    return root, [editor / "present.mov", editor / "missing.mov", editor / "amb.mov"]


# --------------------------------------------------------------------------
# 1. "nothing to copy" and "Verified" with unresolved references
# --------------------------------------------------------------------------


@needs_otio
def test_nothing_to_copy_with_unresolved_references_fails_and_names_them(
        tmp_path: Path, s1, capsys):
    root, refs = s1
    cut = _otio(tmp_path / "cut.otio", refs)

    code = _offload(cut, root, tmp_path / "dest")

    out = capsys.readouterr().out
    assert code == 4
    assert "every reference is already here" not in out
    assert "NOT complete" in out
    assert "[missing] missing.mov" in out
    assert "[ambiguous] amb.mov" in out


@needs_otio
def test_nothing_to_copy_when_everything_is_present_still_exits_zero(
        tmp_path: Path, capsys):
    root = tmp_path / "drive"
    _write(root / "present.mov", b"P" * 1000)
    cut = _otio(tmp_path / "cut.otio", [tmp_path / "editor" / "present.mov"])

    assert _offload(cut, root, tmp_path / "dest") == 0
    assert "every reference is already here" in capsys.readouterr().out


@needs_otio
def test_offload_copies_the_gap_lists_the_unresolved_and_is_not_verified(
        tmp_path: Path, s1, capsys):
    root, refs = s1
    gap = _write(tmp_path / "elsewhere" / "gap.mov", b"G" * 5000)
    cut = _otio(tmp_path / "cut.otio", [gap, *refs[1:]])
    dest = tmp_path / "dest"

    code = _offload(cut, root, dest)

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert code == 4
    landed = list(dest.rglob("gap.mov"))
    assert len(landed) == 1 and landed[0].read_bytes() == b"G" * 5000
    assert ": Incomplete" in captured.out
    assert "2 timeline reference(s) NOT supplied" in text
    assert "missing: missing.mov" in text and "ambiguous: amb.mov" in text
    assert _csv_status(dest) == "Status: Incomplete"


def test_a_job_with_unresolved_references_is_never_verified(tmp_path: Path):
    entry = FileEntry(source=tmp_path / "a.mov", source_root=tmp_path, size=1,
                      created=0.0, modified=0.0)
    entry.destinations.append(Destination(root=tmp_path / "d",
                                          path=tmp_path / "d" / "a.mov",
                                          status=FileStatus.VERIFIED))
    job = Job(name="cut", source_root=tmp_path, destination_roots=[tmp_path / "d"],
              files=[entry])
    assert job.final_status == "Verified"
    job.unresolved = ["missing: b.mov (used 1x)"]
    assert job.final_status == "Incomplete"
    job.verification = VerificationMode.NONE
    assert job.final_status == "Incomplete"


# --------------------------------------------------------------------------
# 2. Reports list what was not supplied
# --------------------------------------------------------------------------

_LINES = ["missing: missing.mov (used 2x); not under any search root",
          "ambiguous: amb.mov (used 1x); 2 different files share this name"]


def test_csv_report_lists_unresolved_references(sample_job: Job, tmp_path: Path):
    sample_job.unresolved = list(_LINES)
    text = write_csv(sample_job, tmp_path / "r.csv").read_text(encoding="utf-8")
    assert f"# {UNRESOLVED_TITLE} (2)" in text
    for line in _LINES:
        assert line in text
    assert "Status: Incomplete" in text
    # The file table above is untouched: the header row is still row 3.
    rows = list(csv.reader((tmp_path / "r.csv").open(encoding="utf-8")))
    assert rows[2][0] == "File Name"


def test_html_report_lists_unresolved_references(sample_job: Job, tmp_path: Path):
    sample_job.unresolved = [*_LINES, "offline: <script>.mov (used 1x)"]
    text = write_html(sample_job, tmp_path / "r.html").read_text(encoding="utf-8")
    assert f"{UNRESOLVED_TITLE} (3)" in text
    for line in _LINES:
        assert line in text
    assert "&lt;script&gt;.mov" in text and "<script>.mov" not in text


def test_reports_without_unresolved_references_have_no_section(
        sample_job: Job, tmp_path: Path):
    assert UNRESOLVED_TITLE not in write_csv(
        sample_job, tmp_path / "r.csv").read_text(encoding="utf-8")
    assert UNRESOLVED_TITLE not in write_html(
        sample_job, tmp_path / "r.html").read_text(encoding="utf-8")


def test_pdf_report_lists_unresolved_references(sample_job: Job, tmp_path: Path):
    fitz = pytest.importorskip("fitz", reason="PyMuPDF needed to read the PDF")
    long_path = "/Volumes/" + "/".join(f"folder{i:02d}" for i in range(40))
    sample_job.unresolved = [*_LINES,
                             *(f"missing: clip{i}.mov (used 1x), timeline path "
                               f"{long_path}/clip{i}.mov" for i in range(60))]
    path = write_pdf(sample_job, tmp_path / "r.pdf")
    with fitz.open(path) as document:
        text = "".join(page.get_text() for page in document).replace("\n", "")
        pages = document.page_count
    assert f"{UNRESOLVED_TITLE} (62)" in text
    assert "missing.mov" in text and "amb.mov" in text
    # Wrapped, not elided: the last reference's whole path survives.
    assert f"{long_path}/clip59.mov".replace(" ", "") in text.replace(" ", "")
    assert pages >= 2


# --------------------------------------------------------------------------
# 3. One file cannot satisfy several distinct references
# --------------------------------------------------------------------------


def test_docs_wav_example_two_recordings_one_file_is_ambiguous(tmp_path: Path):
    """docs/timeline.md: f2/260223_009.WAV and f3/260223_009.WAV are two
    different recordings. With one file of that name on the drive and the
    editor's volume unreachable, neither may silently become that file."""
    root = tmp_path / "drive"
    _write(root / "audio" / "260223_009.WAV", b"RECORDING-F2" * 100)
    editor = Path("/Volumes/Edit/cards")
    refs = [Reference("260223_009.WAV", editor / "f2" / "260223_009.WAV"),
            Reference("260223_009.WAV", editor / "f3" / "260223_009.WAV")]

    report = timeline.resolve(refs, [root])

    assert [r.status for r in report.resolutions] == [Status.AMBIGUOUS] * 2
    assert all(r.location is None for r in report.resolutions)
    assert "2 different timeline references" in report.resolutions[0].note
    assert len(report.incomplete) == 2


def test_readable_recordings_sharing_a_name_resolve_to_distinct_files(
        tmp_path: Path):
    """Repro S3: the drive holds a copy of f2 only. f2 is PRESENT, f3 is not
    mapped to f2's copy but copied from its own readable path."""
    root = tmp_path / "drive"
    f2 = _write(tmp_path / "cards" / "f2" / "260223_009.WAV", b"RECORDING-F2" * 100)
    f3 = _write(tmp_path / "cards" / "f3" / "260223_009.WAV",
                b"RECORDING-F3-DIFFERENT" * 200)
    copy = _write(root / "audio" / "260223_009.WAV", f2.read_bytes())

    report = timeline.resolve([Reference(f2.name, f2), Reference(f3.name, f3)], [root])
    first, second = report.resolutions

    assert first.status is Status.PRESENT and first.location == copy
    assert second.status is Status.GAP and second.location == f3
    assert second.alternatives == [copy]
    assert report.differing == [second]


@needs_otio
def test_offload_copies_the_second_recording_instead_of_calling_it_present(
        tmp_path: Path, capsys):
    root = tmp_path / "drive"
    f2 = _write(tmp_path / "cards" / "f2" / "260223_009.WAV", b"RECORDING-F2" * 100)
    f3 = _write(tmp_path / "cards" / "f3" / "260223_009.WAV",
                b"RECORDING-F3-DIFFERENT" * 200)
    _write(root / "audio" / "260223_009.WAV", f2.read_bytes())
    cut = _otio(tmp_path / "cut.otio", [f2, f3])
    dest = tmp_path / "dest"

    code = _offload(cut, root, dest)

    captured = capsys.readouterr()
    assert "nothing to copy" not in captured.out
    landed = list(dest.rglob("260223_009.WAV"))
    assert [p.read_bytes() for p in landed] == [f3.read_bytes()]
    assert "a different file of this name is under the search roots" in captured.err
    assert code == 0


def test_missing_media_slate_does_not_stand_in_for_a_readable_original(
        tmp_path: Path):
    """Repro S3b: the real file is reachable; the only file of that name under
    the root is a 50-byte stand-in slate."""
    root = tmp_path / "drive"
    real = _write(tmp_path / "archive" / "Archival_017.mov", b"REAL" * 5000)
    slate = _write(root / "MISSING MEDIA" / "Archival_017.mov", b"SLATE" * 10)

    (resolution,) = timeline.resolve([Reference(real.name, real)], [root]).resolutions

    assert resolution.status is Status.GAP
    assert resolution.location == real
    assert resolution.alternatives == [slate]
    assert "different file" in resolution.note


def test_same_size_different_bytes_is_caught_by_the_hash(tmp_path: Path):
    root = tmp_path / "drive"
    real = _write(tmp_path / "archive" / "x.mov", b"A" * 4096)
    _write(root / "x.mov", b"B" * 4096)
    (resolution,) = timeline.resolve([Reference(real.name, real)], [root]).resolutions
    assert resolution.status is Status.GAP


def test_readable_original_with_the_same_bytes_here_is_present(tmp_path: Path):
    root = tmp_path / "drive"
    real = _write(tmp_path / "archive" / "x.mov", b"same" * 1000)
    here = _write(root / "filed" / "x.mov", b"same" * 1000)
    (resolution,) = timeline.resolve([Reference(real.name, real)], [root]).resolutions
    assert resolution.status is Status.PRESENT
    assert resolution.location == here


def test_names_differing_only_in_case_compete_for_one_file(tmp_path: Path):
    root = tmp_path / "drive"
    _write(root / "a.mov", b"one file")
    # Different folders, so the two are distinct on every platform; the index
    # is keyed by lowercased name, so both compete for the one a.mov.
    refs = [Reference("A.MOV", Path("/Volumes/Edit/cam1/A.MOV")),
            Reference("a.mov", Path("/Volumes/Edit/cam2/a.mov"))]
    report = timeline.resolve(refs, [root])
    assert [r.status for r in report.resolutions] == [Status.AMBIGUOUS] * 2


# --------------------------------------------------------------------------
# 4. Offline media and image sequences are not "generated"
# --------------------------------------------------------------------------


@needs_otio
def test_missing_reference_is_offline_by_name_and_fails(tmp_path: Path, capsys):
    root = tmp_path / "drive"
    _write(root / "A001_C002.mov", b"a file of that name")
    cut = _otio(tmp_path / "cut.otio",
                [otio.schema.MissingReference(name="A001_C002.mov")])

    (reference,) = timeline.read_references(cut)
    assert reference.name == "A001_C002.mov" and not reference.generated
    (resolution,) = timeline.resolve([reference], [root]).resolutions
    assert resolution.status is Status.OFFLINE
    assert resolution.alternatives == [root / "A001_C002.mov"]

    assert cli.main(["resolve", "--timeline", str(cut),
                     "--search-root", str(root)]) == 4
    assert "[offline] A001_C002.mov" in capsys.readouterr().out
    assert _offload(cut, root, tmp_path / "dest") == 4
    assert "every reference is already here" not in capsys.readouterr().out


@needs_otio
def test_nameless_missing_reference_takes_the_clip_name(tmp_path: Path):
    cut = _otio(tmp_path / "cut.otio", [otio.schema.MissingReference()],
                clip_names=["B003_C010"])
    (reference,) = timeline.read_references(cut)
    assert reference.kind == timeline.KIND_OFFLINE
    assert reference.name == "B003_C010"


@needs_otio
def test_image_sequence_is_unsupported_by_name_and_fails(tmp_path: Path, capsys):
    root = tmp_path / "drive"
    root.mkdir()
    plates = tmp_path / "plates"
    for frame in range(1001, 1004):
        _write(plates / f"sh010.{frame}.exr", b"E" * 100)
    sequence = otio.schema.ImageSequenceReference(
        target_url_base=plates.as_uri() + "/", name_prefix="sh010.",
        name_suffix=".exr", start_frame=1001, frame_zero_padding=4, rate=24)
    cut = _otio(tmp_path / "cut.otio", [sequence])

    (resolution,) = timeline.resolve(timeline.read_references(cut), [root]).resolutions
    assert resolution.status is Status.UNSUPPORTED
    assert resolution.name == "sh010.####.exr"
    assert plates.as_uri() in resolution.note

    assert cli.main(["resolve", "--timeline", str(cut),
                     "--search-root", str(root)]) == 4
    assert "[unsupported] sh010.####.exr" in capsys.readouterr().out


@needs_otio
def test_a_generator_is_still_generated_and_does_not_fail(tmp_path: Path):
    root = tmp_path / "drive"
    root.mkdir()
    cut = _otio(tmp_path / "cut.otio",
                [otio.schema.GeneratorReference(name="Bars", generator_kind="SMPTEBars")])
    (resolution,) = timeline.resolve(timeline.read_references(cut), [root]).resolutions
    assert resolution.status is Status.GENERATED
    assert cli.main(["resolve", "--timeline", str(cut),
                     "--search-root", str(root)]) == 0


def _has_fcp_adapter() -> bool:
    return otio is not None and "fcp_xml" in set(otio.adapters.available_adapter_names())


_FCP_NO_PATHURL = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="5"><sequence id="s1"><name>seq</name><duration>48</duration>
<rate><timebase>24</timebase><ntsc>FALSE</ntsc></rate><media><video><track>
<clipitem id="c2"><name>B002_C007.mov</name><duration>24</duration>
<rate><timebase>24</timebase><ntsc>FALSE</ntsc></rate>
<start>0</start><end>24</end><in>0</in><out>24</out>
<file id="f2"><name>B002_C007.mov</name>
<rate><timebase>24</timebase><ntsc>FALSE</ntsc></rate><duration>24</duration></file>
</clipitem>
</track></video></media></sequence></xmeml>
"""


@pytest.mark.skipif(not _has_fcp_adapter(), reason="needs the fcp_xml adapter")
def test_fcp_clip_with_no_pathurl_is_offline_by_name(tmp_path: Path, capsys):
    root = tmp_path / "drive"
    root.mkdir()
    cut = tmp_path / "cut.xml"
    cut.write_text(_FCP_NO_PATHURL, encoding="utf-8")

    (resolution,) = timeline.resolve(timeline.read_references(cut), [root]).resolutions
    assert resolution.status is Status.OFFLINE
    assert resolution.name == "B002_C007.mov"
    assert cli.main(["resolve", "--timeline", str(cut),
                     "--search-root", str(root)]) == 4


# --------------------------------------------------------------------------
# 5. A control-file cancel is not a success
# --------------------------------------------------------------------------


def test_control_file_cancel_exits_non_zero_and_counts_what_was_skipped(
        tmp_path: Path, monkeypatch, capsys):
    """`offloader control PATH --cancel` written as the job starts."""
    source = tmp_path / "card"
    for index in range(3):
        _write(source / f"clip{index}.mov", b"\x5a" * 100_000)
    control = tmp_path / "job.control"
    claim = engine.FileControl.claim

    def claim_then_cancel(self):
        claim(self)
        self.path.write_text("cancel\n", encoding="utf-8")

    monkeypatch.setattr(engine.FileControl, "claim", claim_then_cancel)

    code = cli.main(["offload", "--source", str(source),
                     "--dest", str(tmp_path / "dest"),
                     "--control-file", str(control), "--no-probe",
                     "--report", "csv", "--quiet"])

    out = capsys.readouterr().out
    assert code == 130, "the same code as Ctrl+C"
    assert ": Cancelled" in out
    assert "3 file(s) not attempted" in out


# --------------------------------------------------------------------------
# 6. An unverified proxy is not proof
# --------------------------------------------------------------------------


@needs_otio
def test_unverified_proxy_is_shown_warned_and_not_verified(
        tmp_path: Path, monkeypatch, capsys):
    root = tmp_path / "drive"
    _write(root / "Proxies" / "A001_C001.mov", b"proxy")
    gap = _write(tmp_path / "elsewhere" / "gap.wav", b"G" * 2000)
    # The proxy reads as media; the original is unreachable, so the counts
    # cannot be compared.
    monkeypatch.setattr(timeline, "_frames",
                        lambda path: 240 if Path(path).suffix == ".mov" else None)
    cut = _otio(tmp_path / "cut.otio",
                ["file:///Z:/CardA/A001_C001.braw", gap])
    dest = tmp_path / "dest"

    code = _offload(cut, root, dest)

    captured = capsys.readouterr()
    assert "[unverified proxy] A001_C001.braw" in captured.out
    assert "frames unverified" in captured.out
    assert "A001_C001.braw: treated as satisfied by proxy" in captured.err
    assert ": Incomplete" in captured.out
    assert _csv_status(dest) == "Status: Incomplete"
    assert code == 4
    assert list(dest.rglob("gap.wav"))


@needs_otio
def test_unverified_proxy_alone_is_not_nothing_to_copy(tmp_path: Path, monkeypatch,
                                                       capsys):
    root = tmp_path / "drive"
    _write(root / "Proxies" / "A001_C001.mov", b"proxy")
    monkeypatch.setattr(timeline, "_frames",
                        lambda path: 240 if Path(path).suffix == ".mov" else None)
    cut = _otio(tmp_path / "cut.otio", ["file:///Z:/CardA/A001_C001.braw"])

    assert _offload(cut, root, tmp_path / "dest") == 4
    assert "every reference is already here" not in capsys.readouterr().out


def test_a_stand_in_that_is_not_media_does_not_satisfy_the_original(tmp_path: Path):
    """Repro S7: 35 unrelated bytes named like the proxy, original unreachable.
    The real `_frames` cannot read it, so it must not stand in."""
    root = tmp_path / "drive"
    junk = _write(root / "Proxies" / "A001_C001.mov",
                  b"not really a proxy, unrelated bytes")
    refs = [Reference("A001_C001.braw",
                      Path("/no-such-volume/CardA/A001_C001.braw"))]

    report = timeline.resolve(refs, [root])
    (resolution,) = report.resolutions

    assert resolution.status is Status.MISSING
    assert resolution.location is None
    assert str(junk) in resolution.note
    assert report.incomplete == [resolution]


def test_a_stand_in_that_is_not_media_leaves_a_readable_original_a_gap(
        tmp_path: Path, monkeypatch):
    root = tmp_path / "drive"
    _write(root / "Proxies" / "A001_C001.mov", b"junk")
    original = _write(tmp_path / "card" / "A001_C001.braw", b"raw" * 100)
    monkeypatch.setattr(timeline, "_frames",
                        lambda path: 240 if Path(path).suffix == ".braw" else None)

    (resolution,) = timeline.resolve([Reference(original.name, original)],
                                     [root]).resolutions
    assert resolution.status is Status.GAP
    assert resolution.location == original


# --------------------------------------------------------------------------
# 7. Relative paths and non-file URLs
# --------------------------------------------------------------------------


@needs_otio
def test_a_relative_path_resolves_beside_the_timeline_not_the_cwd(
        tmp_path: Path, monkeypatch):
    """Repro S5: run from a directory holding an unrelated media/clip.mov."""
    root = tmp_path / "drive"
    root.mkdir()
    intended = _write(tmp_path / "project" / "media" / "clip.mov", b"INTENDED" * 100)
    _write(tmp_path / "unrelated_cwd" / "media" / "clip.mov", b"WRONG")
    cut = _otio(tmp_path / "project" / "cut.otio", ["media/clip.mov"])
    monkeypatch.chdir(tmp_path / "unrelated_cwd")

    (resolution,) = timeline.resolve(timeline.read_references(cut), [root],
                                     timeline=cut).resolutions

    assert resolution.status is Status.GAP
    assert resolution.location == intended
    assert resolution.location.is_absolute()


def test_resolve_anchors_a_relative_reference_to_the_timeline(tmp_path: Path,
                                                              monkeypatch):
    root = tmp_path / "drive"
    root.mkdir()
    intended = _write(tmp_path / "project" / "media" / "clip.mov", b"INTENDED")
    _write(tmp_path / "cwd" / "media" / "clip.mov", b"WRONG")
    monkeypatch.chdir(tmp_path / "cwd")

    (resolution,) = timeline.resolve(
        [Reference("clip.mov", Path("media/clip.mov"))], [root],
        timeline=tmp_path / "project" / "cut.otio").resolutions
    assert resolution.location == intended


@needs_otio
@pytest.mark.parametrize("url", ["https://example.com/media/clip.mov",
                                 "s3://bucket/media/clip.mov"])
def test_a_non_file_url_is_external_and_never_matched_locally(
        tmp_path: Path, url: str, capsys):
    root = tmp_path / "drive"
    _write(root / "clip.mov", b"an unrelated local clip")
    cut = _otio(tmp_path / "cut.otio", [url])

    (resolution,) = timeline.resolve(timeline.read_references(cut), [root]).resolutions
    assert resolution.status is Status.EXTERNAL
    assert resolution.location is None
    assert url in resolution.note

    assert cli.main(["resolve", "--timeline", str(cut),
                     "--search-root", str(root)]) == 4
    out = capsys.readouterr().out
    assert "[external] clip.mov" in out and url in out
    assert _offload(cut, root, tmp_path / "dest") == 4
    assert not (tmp_path / "dest").exists()


def test_a_drive_letter_is_not_a_url_scheme():
    assert timeline._url_scheme("E:/Media/a.mov") == ""
    assert timeline._url_scheme("file:///E:/Media/a.mov") == "file"
    assert timeline._url_scheme("HTTPS://host/a.mov") == "https"
