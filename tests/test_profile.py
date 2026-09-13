"""The `data` profile: a generic large-data transfer.

The verified copy engine is the same for both profiles; `data` only switches
off the media-specific work (ffprobe, thumbnails, the BRAW check). These tests
pin that contract at every layer it passes through — OffloadOptions, the engine,
the CLI and presets — so a future refactor cannot quietly start probing a data
transfer or stop probing a media one.
"""

from __future__ import annotations

from pathlib import Path

from offloader import cli, engine
from offloader.models import FileStatus, Profile, VerificationMode
from offloader.presets import Preset


def _options(tmp_path: Path, **overrides) -> engine.OffloadOptions:
    defaults = dict(
        destinations=[tmp_path / "dest"],
        algorithm="xxh3-64",
        verification=VerificationMode.FULL,
    )
    defaults.update(overrides)
    return engine.OffloadOptions(**defaults)


def test_data_profile_forces_media_work_off(tmp_path: Path):
    # A caller that sets only the profile still gets a clean generic transfer:
    # the media knobs are zeroed regardless of what was passed alongside them.
    options = _options(tmp_path, profile=Profile.DATA,
                       extra_probe=True, thumbnail_count=4)
    assert options.extra_probe is False
    assert options.thumbnail_count == 0


def test_media_profile_leaves_media_work_alone(tmp_path: Path):
    options = _options(tmp_path, profile=Profile.MEDIA,
                       extra_probe=True, thumbnail_count=4)
    assert options.extra_probe is True
    assert options.thumbnail_count == 4


def test_data_profile_still_copies_and_verifies(source_tree: Path, tmp_path: Path):
    job = engine.run(source_tree, _options(tmp_path, profile=Profile.DATA))

    assert job.profile is Profile.DATA
    assert job.total_files == 3
    assert job.final_status == "Verified"
    assert all(f.status is FileStatus.VERIFIED for f in job.files)

    copied = tmp_path / "dest" / "Clips" / "A001_C001.mov"
    assert copied.read_bytes() == (source_tree / "Clips" / "A001_C001.mov").read_bytes()


def test_data_profile_never_probes(source_tree: Path, tmp_path: Path, monkeypatch):
    # Probing a data transfer is the exact thing the profile exists to prevent,
    # so make it fail loudly if the engine ever reaches for ffprobe.
    def boom(*_args, **_kwargs):
        raise AssertionError("probe() must not run under the data profile")

    monkeypatch.setattr("offloader.engine.probe_mod.probe", boom)
    job = engine.run(source_tree, _options(tmp_path, profile=Profile.DATA))

    assert job.video_files == 0
    assert all(not f.media.is_video for f in job.files)


def test_cli_generic_flag_selects_the_data_profile(source_tree: Path, tmp_path: Path,
                                                    capsys):
    dest = tmp_path / "dest"
    code = cli.main([
        "offload", "--source", str(source_tree), "--dest", str(dest),
        "--name", "DATASET", "--generic", "--quiet",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "DATASET: Verified" in out
    # The video count is media-only noise on a generic transfer.
    assert "video)" not in out
    assert (dest / "DATASET_Reports" / "JobReport.pdf").is_file()


def test_cli_profile_data_is_equivalent_to_generic(tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args([
        "offload", "--source", str(tmp_path), "--dest", str(tmp_path / "d"),
        "--profile", "data",
    ])
    options = cli._options_from(args, [tmp_path / "d"])
    assert options.profile is Profile.DATA


def test_cli_defaults_to_the_media_profile(tmp_path):
    parser = cli.build_parser()
    args = parser.parse_args([
        "offload", "--source", str(tmp_path), "--dest", str(tmp_path / "d"),
    ])
    options = cli._options_from(args, [tmp_path / "d"])
    assert options.profile is Profile.MEDIA


def test_preset_round_trips_the_profile():
    preset = Preset(name="Archive", profile=Profile.DATA)
    restored = Preset.from_dict(preset.to_dict())
    assert restored.profile is Profile.DATA
    assert restored.to_options().profile is Profile.DATA


def test_preset_defaults_to_media_and_tolerates_missing_key():
    assert Preset(name="x").profile is Profile.MEDIA
    # A config written before profiles existed has no key at all.
    assert Preset.from_dict({"name": "legacy"}).profile is Profile.MEDIA
    # A garbage value must never brick a load.
    assert Preset.from_dict({"name": "bad", "profile": "nonsense"}).profile is Profile.MEDIA


def test_the_data_profile_does_not_invent_companions(tmp_path: Path):
    """Stem-matching says a `.sidecar` belongs to a clip. Under the data
    profile nothing is a clip, so a dataset that happens to share a stem with
    its metadata file would be linked on no evidence but the name."""
    card = tmp_path / "run_1440"
    card.mkdir()
    (card / "capture.h5").write_bytes(b"instrument data " * 200)
    (card / "capture.xmp").write_bytes(b"<x:xmpmeta/>")

    job = engine.run(card, _options(tmp_path, profile=Profile.DATA))

    assert all(f.companion_of is None for f in job.files)
    assert all(f.companions == [] for f in job.files)


def test_the_media_profile_still_groups_them(tmp_path: Path):
    card = tmp_path / "A001"
    card.mkdir()
    (card / "A001_C001.braw").write_bytes(b"a clip " * 400)
    (card / "A001_C001.sidecar").write_bytes(b"the grade")

    job = engine.run(card, _options(tmp_path, profile=Profile.MEDIA,
                                    extra_probe=False))

    sidecar = next(f for f in job.files if f.name == "A001_C001.sidecar")
    assert sidecar.companion_of is not None
