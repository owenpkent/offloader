"""Regression tests for ways a "Verified" verdict, or a passing `verify`, could
be wrong about what is at the destination.

Each test reproduces one finding from a private review of the fuzz-edges
branch, on synthetic data only, and fails against the code before its fix.
The bar is the one in AGENTS.md: someone may reformat a card because this tool
said Verified, so a failed, missing or unchecked file must never disappear
into a success verdict, and an existing good copy must never be replaced by
something else.
"""

from __future__ import annotations

import os
import platform
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from offloader import ascmhl, cli, engine, longpath, verify
from offloader import reports as reports_mod
from offloader.models import FileStatus, MediaInfo, VerificationMode
from offloader.reports.mhl import write_mhl

WINDOWS = platform.system() == "Windows"


def _options(dest: Path | list[Path], **overrides) -> engine.OffloadOptions:
    defaults = dict(
        destinations=dest if isinstance(dest, list) else [dest],
        algorithm="xxh3-64",
        verification=VerificationMode.FULL,
        thumbnail_count=0,
        extra_probe=False,
    )
    defaults.update(overrides)
    return engine.OffloadOptions(**defaults)


def _card(tmp_path: Path, files: dict[str, bytes], name: str = "A001") -> Path:
    root = tmp_path / name
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _offload(*args: str) -> int:
    """Exit codes are compared as literals on purpose: they are a documented
    contract (README), and a format script keys off the numbers."""
    return cli.main(["offload", *args, "--no-probe", "--quiet", "--retries", "1"])


def _flip_one_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))


def _fail_reads_of(monkeypatch, name: str) -> None:
    """Make every read of the source file called `name` fail, as a card with
    an unreadable clip does. Partials and destinations are unaffected."""
    real = longpath.open_binary

    def failing(path, mode="rb"):
        if "r" in mode and str(path).endswith(name):
            raise PermissionError(13, "Access is denied", str(path))
        return real(path, mode)

    monkeypatch.setattr(longpath, "open_binary", failing)


# ------------------------------------------------- P1: a failed file in the MHL


@pytest.mark.parametrize("report", ["mhl", "ascmhl"])
def test_a_failed_copy_stays_in_the_manifest_so_verify_fails(
        tmp_path, monkeypatch, capsys, report):
    """The manifest used to list only the files that copied, so `verify` on a
    destination missing a clip said "VERIFIED - safe to erase the source"."""
    card = _card(tmp_path, {"Clips/A002_C001.mov": os.urandom(5000),
                            "Clips/BAD_C002.mov": os.urandom(6000)})
    dest = tmp_path / "dst"
    with monkeypatch.context() as patch:
        _fail_reads_of(patch, "BAD_C002.mov")
        assert _offload("--source", str(card), "--dest", str(dest),
                        "--report", report) == 1
    capsys.readouterr()

    assert cli.main(["verify", str(dest), "--quiet"]) == 1
    out = capsys.readouterr().out
    assert "safe to erase" not in out.replace("do not erase", "")
    assert "BAD_C002.mov" in out


def test_a_cancelled_job_manifest_lists_what_it_never_reached(tmp_path):
    card = _card(tmp_path, {f"C{n:03d}.mov": os.urandom(3000) for n in range(1, 4)})
    dest = tmp_path / "dst"
    control = engine.JobControl()

    def cancel_after_first(event: engine.ProgressEvent) -> None:
        if event.file_index >= 1:
            control.cancel()

    job = engine.run(card, _options(dest), cancel_after_first, control)
    assert job.cancelled
    manifest = write_mhl(job, dest / "A001_Reports" / "JobReport.mhl")
    report = verify.verify_manifest(manifest)
    assert not report.passed, report.summary()


# ----------------------------------------- P1: skip-existing keeps the manifest


def test_a_skip_existing_rerun_never_overwrites_the_earlier_manifest(
        tmp_path, capsys):
    """A rerun used to overwrite JobReport.mhl with one listing only what it
    copied, so a corrupt same-size copy it skipped verified clean."""
    card = _card(tmp_path, {"Clips/C001.mov": os.urandom(5000),
                            "Clips/C002.mov": os.urandom(5000)})
    dest = tmp_path / "dst"
    assert _offload("--source", str(card), "--dest", str(dest),
                    "--report", "mhl") == 0
    first = dest / "A001_Reports" / "JobReport.mhl"
    original = first.read_bytes()

    _flip_one_byte(dest / "Clips" / "C001.mov")          # same size, wrong bytes
    (card / "Clips" / "C003.mov").write_bytes(os.urandom(4000))
    rerun = _offload("--source", str(card), "--dest", str(dest), "--report", "mhl",
                     "--skip-existing")
    capsys.readouterr()

    assert first.read_bytes() == original
    assert len(verify.find_manifests(dest)) == 2
    assert cli.main(["verify", str(dest), "--quiet"]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "C001.mov" in out
    assert rerun == 4      # skipped files were not verified: Incomplete


def test_a_reused_report_dir_gets_a_numbered_manifest(tmp_path, capsys):
    card = _card(tmp_path, {"C001.mov": os.urandom(3000)})
    dest = tmp_path / "dst"
    paperwork = dest / "paperwork"
    for _ in range(2):
        _offload("--source", str(card), "--dest", str(dest), "--report", "mhl",
                 "--report-dir", str(paperwork))
    assert (paperwork / "JobReport.mhl").is_file()
    assert (paperwork / "JobReport-2.mhl").is_file()
    assert "JobReport-2.mhl" in capsys.readouterr().err


# --------------------------------------------- P1: unlistable directories fail


def test_a_directory_that_cannot_be_listed_fails_the_job(tmp_path, monkeypatch):
    card = _card(tmp_path, {"Clips/C001.mov": b"a" * 100,
                            "Locked/C002.mov": b"b" * 100})
    locked = card / "Locked"
    real = os.scandir

    def refusing(path=".", *args):
        if os.path.normcase(str(path)).endswith(os.path.normcase("Locked")):
            raise PermissionError(13, "Access is denied", str(path))
        return real(path, *args)

    monkeypatch.setattr(os, "scandir", refusing)
    job = engine.run(card, _options(tmp_path / "dst"))
    monkeypatch.undo()

    assert job.final_status == "Failed"
    assert [p.path for p in job.scan_errors] == [locked]
    assert "Locked" in job.notes


def test_a_listing_that_fails_part_way_fails_the_job(tmp_path, monkeypatch):
    card = _card(tmp_path, {"Clips/C001.mov": b"a" * 100,
                            "Bad/C002.mov": b"b" * 100})
    real = os.scandir

    class Breaks:
        def __init__(self, path):
            self._path = path

        def __iter__(self):
            return self

        def __next__(self):
            raise OSError(5, "I/O error", str(self._path))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def close(self):
            return None

    def flaky(path=".", *args):
        if os.path.normcase(str(path)).endswith(os.path.normcase("Bad")):
            return Breaks(path)
        return real(path, *args)

    monkeypatch.setattr(os, "scandir", flaky)
    job = engine.run(card, _options(tmp_path / "dst"))
    monkeypatch.undo()
    assert job.final_status == "Failed"
    assert job.scan_errors and job.scan_errors[0].path.name == "Bad"


@pytest.mark.skipif(not WINDOWS, reason="uses an NTFS deny ACE")
def test_an_acl_locked_directory_fails_the_offload_and_verify(tmp_path, capsys):
    card = _card(tmp_path, {"Clips/A001_C001.mov": os.urandom(5000),
                            "Locked/A001_C002.mov": os.urandom(7000)})
    locked = card / "Locked"
    user = os.environ.get("USERNAME", "")
    denied = subprocess.run(["icacls", str(locked), "/deny", f"{user}:(RD)"],
                            capture_output=True, text=True)
    try:
        if denied.returncode != 0:
            pytest.skip(f"icacls failed: {denied.stdout} {denied.stderr}")
        try:
            os.listdir(locked)
            pytest.skip("the deny ACE did not stop listing (elevated session?)")
        except PermissionError:
            pass
        dest = tmp_path / "dst"
        code = _offload("--source", str(card), "--dest", str(dest), "--report", "mhl")
        err = capsys.readouterr().err
        assert code == 1
        assert "Locked" in err
        assert cli.main(["verify", str(dest), "--quiet"]) == 1
    finally:
        subprocess.run(["icacls", str(locked), "/remove:d", user],
                       capture_output=True, text=True)


# ------------------------------------- P2: names Win32 rewrites are literal


@pytest.mark.skipif(not WINDOWS, reason="Win32 name rewriting is Windows-only")
def test_names_win32_would_rewrite_are_copied_as_themselves(tmp_path, capsys):
    """`clip.mov.` used to open `clip.mov`, so the wrong bytes were read and
    certified; `NUL` read as an empty device and left a partial behind."""
    card = _card(tmp_path, {"B/clip.mov": b"A" * 1000})
    odd = {"clip.mov.": b"Z" * 3000, "clip.mov ": b"S" * 2000, "NUL": b"N" * 500}
    for name, data in odd.items():
        with open("\\\\?\\" + str(card / "B" / name), "wb") as handle:
            handle.write(data)
    dest = tmp_path / "dst"
    try:
        assert _offload("--source", str(card), "--dest", str(dest),
                        "--report", "mhl") == 0
        listing = os.listdir("\\\\?\\" + str(dest / "B"))
        assert sorted(listing) == sorted(["clip.mov", *odd]), listing
        for name, data in {**odd, "clip.mov": b"A" * 1000}.items():
            with open("\\\\?\\" + str(dest / "B" / name), "rb") as handle:
                assert handle.read() == data, name
        assert cli.main(["verify", str(dest), "--quiet"]) == 0
    finally:
        for root in (card, dest):
            for name in odd:
                try:
                    os.remove("\\\\?\\" + str(root / "B" / name))
                except OSError:
                    pass


# ------------------------------------------ P2: junctions are never followed


@pytest.mark.skipif(not WINDOWS, reason="directory junctions are Windows-only")
def test_a_junction_listed_before_its_target_does_not_claim_it(tmp_path):
    card = _card(tmp_path, {"Z_real/C001.mov": os.urandom(4000)})
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(card / "A_alias"), str(card / "Z_real")],
        capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip(f"could not create a junction: {made.stderr.strip()}")

    dest = tmp_path / "dst"
    job = engine.run(card, _options(dest))

    assert job.final_status == "Verified"
    assert (dest / "Z_real" / "C001.mov").read_bytes() == \
        (card / "Z_real" / "C001.mov").read_bytes()
    assert not (dest / "A_alias").exists()
    assert any("A_alias" in warning for warning in job.warnings)


# ------------------------- P2: a source holding <job>_Reports is not overwritten


def test_reports_never_land_on_a_copied_reports_folder(tmp_path, capsys):
    card = _card(tmp_path, {
        "Clips/C001.mov": os.urandom(3000),
        "A001_Reports/clip.txt": b"shuttle notes",
        "A001_Reports/JobReport.mhl": b"<hashlist>the shuttle's manifest</hashlist>",
        "A001_Reports/JobReport.csv": b"shuttle,csv\n",
    })
    dest = tmp_path / "dst"
    assert _offload("--source", str(card), "--dest", str(dest),
                    "--report", "mhl,csv") == 0
    for name in ("clip.txt", "JobReport.mhl", "JobReport.csv"):
        assert (dest / "A001_Reports" / name).read_bytes() == \
            (card / "A001_Reports" / name).read_bytes(), name
    assert (dest / "A001_Reports-2" / "JobReport.mhl").is_file()
    assert "A001_Reports-2" in capsys.readouterr().err
    assert cli.main(["verify", str(dest / "A001_Reports-2" / "JobReport.mhl"),
                     "--quiet"]) == 0


# ---------------------------------------- P2: report failures are not success


def test_a_report_failure_exits_non_zero_and_is_on_the_job(tmp_path, monkeypatch,
                                                          capsys):
    card = _card(tmp_path, {"C001.mov": os.urandom(3000)})

    def broken(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setitem(reports_mod.WRITERS, "mhl", broken)
    code = _offload("--source", str(card), "--dest", str(tmp_path / "dst"),
                    "--report", "mhl,csv")
    captured = capsys.readouterr()
    assert code == 5
    assert "REPORT(S) FAILED" in captured.err
    assert "reports failed" in captured.out

    job = engine.run(card, _options(tmp_path / "dst2"))
    reports_mod.paperwork.write_reports(job, ["mhl"])
    assert job.report_failures
    assert job.final_status == "Verified, reports failed"


def test_the_gui_runner_records_report_failures(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6", reason="GUI extra not installed")
    from PySide6.QtWidgets import QApplication

    from offloader.gui import worker
    from offloader.presets import Preset

    QApplication.instance() or QApplication([])
    card = _card(tmp_path, {"C001.mov": os.urandom(3000)})
    dest = tmp_path / "dst"

    def broken(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setitem(reports_mod.WRITERS, "mhl", broken)
    preset = Preset(name="t", destinations=[str(dest)], reports=["mhl"],
                    thumbnail_count=0)
    item = worker.QueueItem(identifier=1, source=card, name="A001", preset=preset)
    job = engine.run(card, _options(dest, job_name="A001"))
    runner = worker._Runner(item)
    runner._write_reports(job, preset)
    item.job, item.state = job, worker.JobState.DONE

    assert "reports failed" in item.status_text
    assert job.report_failures


# ------------------------------- P2: never replace a differing existing file


def test_a_different_card_never_replaces_an_existing_verified_file(tmp_path):
    relative = "DCIM/100CANON/IMG_0001.CR3"
    card_a = _card(tmp_path, {relative: b"A" * 4096}, name="cardA")
    card_b = _card(tmp_path, {relative: b"B" * 4096}, name="cardB")
    dest = tmp_path / "dst"
    target = dest / relative

    assert engine.run(card_a, _options(dest)).final_status == "Verified"
    second = engine.run(card_b, _options(dest))

    assert second.final_status == "Failed"
    error = second.files[0].destinations[0].error
    assert "different content" in error and "--replace-existing" in error
    assert target.read_bytes() == b"A" * 4096
    assert not list(dest.rglob("*.offloader-partial"))

    # The same card again is harmless and still verifies.
    assert engine.run(card_a, _options(dest)).final_status == "Verified"
    # Asked for explicitly, the replacement happens.
    forced = engine.run(card_b, _options(dest, replace_existing=True))
    assert forced.final_status == "Verified"
    assert target.read_bytes() == b"B" * 4096


def test_the_cli_takes_replace_existing(tmp_path):
    args = cli.build_parser().parse_args(
        ["offload", "--source", "s", "--dest", "d", "--replace-existing"])
    assert cli._options_from(args, [Path("d")]).replace_existing is True


# ------------------------------- P2: every destination has its own manifest


def test_each_destination_verifies_on_its_own_with_report_dir_in_the_second(
        tmp_path, capsys):
    card = _card(tmp_path, {"C001.mov": os.urandom(5000)})
    d0, d1 = tmp_path / "d0", tmp_path / "d1"
    assert _offload("--source", str(card), "--dest", str(d0), "--dest", str(d1),
                    "--report", "mhl,csv",
                    "--report-dir", str(d1 / "A001_Reports")) == 0
    assert (d0 / "A001_Reports" / "JobReport.mhl").is_file()
    listed = [node.text for node in
              ET.parse(d1 / "A001_Reports" / "JobReport.mhl").getroot().iter("file")]
    assert listed == ["../C001.mov"], listed

    assert cli.main(["verify", str(d0), "--quiet"]) == 0
    assert cli.main(["verify", str(d1), "--quiet"]) == 0
    _flip_one_byte(d1 / "C001.mov")
    capsys.readouterr()
    assert cli.main(["verify", str(d1), "--quiet"]) == 1
    assert cli.main(["verify", str(d0), "--quiet"]) == 0


# ------------------------------------------------------- P3: exit codes


def test_an_empty_source_exits_incomplete(tmp_path):
    card = _card(tmp_path, {})
    assert _offload("--source", str(card), "--dest", str(tmp_path / "dst"),
                    "--report", "csv") == 4


def test_an_all_skipped_rerun_exits_incomplete(tmp_path):
    card = _card(tmp_path, {"C001.mov": os.urandom(3000)})
    dest = tmp_path / "dst"
    assert _offload("--source", str(card), "--dest", str(dest), "--report", "csv") == 0
    assert _offload("--source", str(card), "--dest", str(dest), "--report", "csv",
                    "--skip-existing") == 4


# ---------------------------------------- P3: verify scans the managed root


def test_verify_reports_files_beside_a_one_entry_manifest(tmp_path):
    card = _card(tmp_path, {"Clips/C001.mov": os.urandom(2000)})
    dest = tmp_path / "dst"
    job = engine.run(card, _options(dest))
    manifest = write_mhl(job, dest / "A001_Reports" / "JobReport.mhl")
    (dest / "Clips" / "C002.mov").write_bytes(b"not in the manifest")
    (dest / "Other").mkdir()
    (dest / "Other" / "C003.mov").write_bytes(b"nor this")

    report = verify.verify_manifest(manifest)
    names = sorted(p.name for p in report.unlisted)
    assert names == ["C002.mov", "C003.mov"], names
    assert report.passed          # unlisted files are diagnostic, not failures


# ------------------------------------- P3: a file vanishing mid-job is one file


def test_a_file_that_vanishes_after_the_scan_fails_alone(tmp_path):
    card = _card(tmp_path, {f"C{n:03d}.mov": os.urandom(3000) for n in range(1, 4)})
    doomed = card / "C002.mov"

    def delete_during_first(event: engine.ProgressEvent) -> None:
        if event.file_index == 0 and doomed.exists():
            doomed.unlink()

    job = engine.run(card, _options(tmp_path / "dst"), delete_during_first)

    by_name = {entry.name: entry for entry in job.files}
    assert by_name["C002.mov"].status is FileStatus.FAILED
    assert "vanished" in by_name["C002.mov"].destinations[0].error
    assert by_name["C003.mov"].status is FileStatus.VERIFIED
    assert job.final_status == "Failed"


# --------------------------------------- P3: thumbnails per clip, not per stem


def test_same_named_clips_in_different_folders_keep_their_own_thumbnails(
        tmp_path, monkeypatch):
    from offloader import probe as probe_mod
    from offloader import thumbs

    card = _card(tmp_path, {"A/C001.mov": b"first take" * 100,
                            "B/C001.mov": b"second take" * 100})
    video = MediaInfo(width=1920, height=1080, duration_sec=10.0)
    monkeypatch.setattr(probe_mod, "probe", lambda _path: video)
    monkeypatch.setattr(thumbs, "ffmpeg_path", lambda: "ffmpeg")

    def fake_ffmpeg(command, **_kwargs):
        source, target = command[command.index("-i") + 1], command[-1]
        Path(target).write_text(source, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(thumbs.subprocess, "run", fake_ffmpeg)
    job = engine.run(card, _options(tmp_path / "dst", extra_probe=True,
                                    thumbnail_count=2))

    first, second = job.files
    assert first.thumbnails and second.thumbnails
    assert not set(first.thumbnails) & set(second.thumbnails)
    for entry in job.files:
        for picture in entry.thumbnails:
            drawn_from = Path(picture.read_text(encoding="utf-8"))
            assert drawn_from.parent.name == entry.source.parent.name


# ------------------------------- P3: ASC MHL previous digest case-insensitive


def test_an_uppercase_previous_digest_still_labels_the_file_verified(tmp_path):
    card = _card(tmp_path, {"C001.mov": os.urandom(2000)})
    dest = tmp_path / "dst"
    job = engine.run(card, _options(dest, algorithm="xxh64"))
    first = ascmhl.write_manifest(job, dest)
    namespace = f"{{{ascmhl.NAMESPACE}}}"
    tree = ET.parse(first)
    for element in tree.getroot().iter(f"{namespace}xxh64"):
        if element.get("action"):
            element.text = element.text.upper()
    text = ET.tostring(tree.getroot(), encoding="unicode")
    first.write_text(text, encoding="utf-8")

    second = ascmhl.write_manifest(job, dest)
    actions = [element.get("action")
               for element in ET.parse(second).getroot().iter(f"{namespace}xxh64")
               if element.get("action")]
    assert actions == ["verified"]
