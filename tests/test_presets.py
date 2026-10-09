from __future__ import annotations

from pathlib import Path

from offloader.models import VerificationMode
from offloader.naming import DEFAULT_TEMPLATE
from offloader.presets import Preset


def test_to_options_carries_settings_through(tmp_path: Path):
    preset = Preset(
        name="p",
        destinations=[tmp_path / "d"],
        algorithm="xxh64",
        verification=VerificationMode.FULL,
        thumbnail_count=2,
        excludes=["*.tmp"],
        preserve_structure=False,
        skip_existing=True,
    )
    options = preset.to_options(job_name="A001")

    assert options.destinations == [tmp_path / "d"]
    assert options.algorithm == "xxh64"
    assert options.verification is VerificationMode.FULL
    assert options.thumbnail_count == 2
    assert options.job_name == "A001"
    assert options.preserve_structure is False
    assert options.skip_existing is True
    assert "*.tmp" in options.excludes
    assert ".DS_Store" in options.excludes    # defaults still applied


def test_blank_name_and_template_fall_back():
    preset = Preset(name="  ", naming_template="")
    assert preset.name == "Untitled"
    assert preset.naming_template == DEFAULT_TEMPLATE
