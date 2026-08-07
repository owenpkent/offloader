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
