from __future__ import annotations

from pathlib import Path

import pytest

from offloader import cli


def test_info_reports_the_environment(capsys):
    assert cli.main(["info"]) == 0
    out = capsys.readouterr().out
    assert "Offloader" in out
    assert "checksums:" in out
    assert "reports:" in out


def test_offload_writes_the_default_report(source_tree: Path, tmp_path: Path, capsys):
    dest = tmp_path / "dest"
    code = cli.main([
        "offload", "--source", str(source_tree), "--dest", str(dest),
        "--name", "A001", "--no-probe", "--quiet",
    ])
    assert code == 0
    assert (dest / "A001_Reports" / "JobReport.pdf").is_file()
    assert "A001: Verified" in capsys.readouterr().out


def test_offload_writes_every_requested_format(source_tree: Path, tmp_path: Path):
    dest = tmp_path / "dest"
    cli.main([
        "offload", "--source", str(source_tree), "--dest", str(dest),
        "--name", "A001", "--report", "pdf,csv,mhl,html", "--no-probe", "--quiet",
    ])
    reports = dest / "A001_Reports"
    assert {p.name for p in reports.iterdir()} == {
        "JobReport.pdf", "JobReport.csv", "JobReport.mhl", "JobReport.html"
    }


def test_report_dir_can_be_redirected(source_tree: Path, tmp_path: Path):
    elsewhere = tmp_path / "paperwork"
    cli.main([
        "offload", "--source", str(source_tree), "--dest", str(tmp_path / "dest"),
        "--report-dir", str(elsewhere), "--no-probe", "--quiet",
    ])
    assert (elsewhere / "JobReport.pdf").is_file()


def test_report_command_does_not_copy(source_tree: Path, tmp_path: Path):
    out = tmp_path / "paperwork"
    assert cli.main([
        "report", "--source", str(source_tree), "--report-dir", str(out),
        "--no-probe", "--quiet",
    ]) == 0
    assert (out / "JobReport.pdf").is_file()
    assert not (tmp_path / "dest").exists()


def test_unknown_report_format_is_rejected(source_tree: Path, tmp_path: Path):
    with pytest.raises(SystemExit):
        cli.main([
            "offload", "--source", str(source_tree), "--dest", str(tmp_path / "d"),
            "--report", "pdf,docx",
        ])


def test_missing_source_exits_nonzero(tmp_path: Path, capsys):
    code = cli.main([
        "offload", "--source", str(tmp_path / "gone"), "--dest", str(tmp_path / "d"),
        "--quiet",
    ])
    assert code == 2
    assert "error:" in capsys.readouterr().err


def test_failed_verification_exits_nonzero(source_tree: Path, tmp_path: Path,
                                           monkeypatch, capsys):
    from offloader import engine

    monkeypatch.setattr(engine, "hash_file", lambda *a, **k: "0" * 16)
    code = cli.main([
        "offload", "--source", str(source_tree), "--dest", str(tmp_path / "dest"),
        "--verify", "full", "--no-probe", "--quiet",
    ])
    assert code == 1
    assert "FAILED" in capsys.readouterr().err


def test_multiple_destinations(source_tree: Path, tmp_path: Path):
    cli.main([
        "offload", "--source", str(source_tree),
        "--dest", str(tmp_path / "d1"), "--dest", str(tmp_path / "d2"),
        "--no-probe", "--quiet",
    ])
    assert (tmp_path / "d1" / "notes.txt").is_file()
    assert (tmp_path / "d2" / "notes.txt").is_file()


# ------------------------------------------------------------------ progress


def test_progress_is_silent_when_stderr_is_not_a_tty(capsys):
    """Piped output must stay clean for logs and CI."""
    progress = cli._Progress(enabled=True)
    assert not progress.enabled
    progress(cli.engine.ProgressEvent(0, 1, "clip.mov", "copy", 0, 10, 5, 10))
    progress.done()
    assert capsys.readouterr().err == ""


def test_progress_renders_a_throttled_single_line(monkeypatch):
    written: list[str] = []

    class FakeStderr:
        @staticmethod
        def isatty():
            return True

        @staticmethod
        def write(text):
            written.append(text)

        @staticmethod
        def flush():
            pass

    monkeypatch.setattr(cli.sys, "stderr", FakeStderr)
    progress = cli._Progress(enabled=True)
    assert progress.enabled

    progress(cli.engine.ProgressEvent(0, 2, "clip.mov", "copy", 0, 10, 5, 10))
    rendered = "".join(written)
    assert "clip.mov" in rendered
    assert "50.0%" in rendered
    assert "1/2" in rendered
    assert rendered.startswith("\r")

    # A second event inside the throttle window is dropped.
    before = len(written)
    progress(cli.engine.ProgressEvent(0, 2, "clip.mov", "copy", 0, 10, 6, 10))
    assert len(written) == before

    # Completion always renders, throttle or not.
    progress(cli.engine.ProgressEvent(1, 2, "clip.mov", "copy", 0, 10, 10, 10))
    assert "100.0%" in "".join(written)

    progress.done()
    assert "".join(written).endswith("\r")


def test_progress_handles_a_zero_byte_job(monkeypatch):
    class FakeStderr:
        @staticmethod
        def isatty():
            return True

        write = staticmethod(lambda text: None)
        flush = staticmethod(lambda: None)

    monkeypatch.setattr(cli.sys, "stderr", FakeStderr)
    progress = cli._Progress(enabled=True)
    progress(cli.engine.ProgressEvent(0, 1, "empty", "copy", 0, 0, 0, 0))


# ------------------------------------------------------------------ safety


def test_offloading_a_card_onto_itself_is_refused(source_tree: Path, capsys):
    code = cli.main(["offload", "--source", str(source_tree),
                     "--dest", str(source_tree), "--no-probe", "--quiet"])
    assert code == 3
    assert "refused" in capsys.readouterr().err
    assert (source_tree / "notes.txt").read_text(encoding="utf-8") == "slate notes"


def test_warnings_are_printed(tmp_path: Path, capsys):
    card = tmp_path / "card"
    card.mkdir()
    (card / "empty.mov").write_bytes(b"")
    (card / "real.mov").write_bytes(b"x" * 100)

    cli.main(["offload", "--source", str(card), "--dest", str(tmp_path / "d"),
              "--verify", "full", "--no-probe", "--quiet"])
    err = capsys.readouterr().err
    assert "warning(s)" in err
    assert "empty.mov" in err


def test_an_mhl_is_written_beside_every_copy(source_tree: Path, tmp_path: Path):
    cli.main(["offload", "--source", str(source_tree),
              "--dest", str(tmp_path / "d1"), "--dest", str(tmp_path / "d2"),
              "--name", "A001", "--report", "mhl", "--no-probe", "--quiet"])
    assert (tmp_path / "d1" / "A001_Reports" / "JobReport.mhl").is_file()
    assert (tmp_path / "d2" / "A001_Reports" / "JobReport.mhl").is_file()


# ------------------------------------------------------------------ verify


def test_verify_passes_on_a_clean_tree(source_tree: Path, tmp_path: Path, capsys):
    destination = tmp_path / "d1"
    cli.main(["offload", "--source", str(source_tree), "--dest", str(destination),
              "--name", "A001", "--verify", "full", "--report", "mhl",
              "--no-probe", "--quiet"])
    capsys.readouterr()

    assert cli.main(["verify", str(destination), "--quiet"]) == 0
    out = capsys.readouterr().out
    assert "safe to erase" in out


def test_verify_fails_on_a_flipped_bit(source_tree: Path, tmp_path: Path, capsys):
    destination = tmp_path / "d1"
    cli.main(["offload", "--source", str(source_tree), "--dest", str(destination),
              "--name", "A001", "--verify", "full", "--report", "mhl",
              "--no-probe", "--quiet"])
    capsys.readouterr()

    victim = destination / "Clips" / "A001_C001.mov"
    payload = bytearray(victim.read_bytes())
    payload[10] ^= 0x01
    victim.write_bytes(bytes(payload))

    assert cli.main(["verify", str(destination), "--quiet"]) == 1
    out = capsys.readouterr().out
    assert "MISMATCH" in out
    assert "do not erase" in out


def test_verify_fails_on_a_missing_file(source_tree: Path, tmp_path: Path, capsys):
    destination = tmp_path / "d1"
    cli.main(["offload", "--source", str(source_tree), "--dest", str(destination),
              "--name", "A001", "--verify", "full", "--report", "mhl",
              "--no-probe", "--quiet"])
    capsys.readouterr()
    (destination / "Clips" / "A001_C002.mov").unlink()

    assert cli.main(["verify", str(destination), "--quiet"]) == 1
    assert "MISSING" in capsys.readouterr().out


def test_verify_reports_a_missing_manifest(tmp_path: Path, capsys):
    assert cli.main(["verify", str(tmp_path), "--quiet"]) == 2
    assert "no .mhl manifest" in capsys.readouterr().err
