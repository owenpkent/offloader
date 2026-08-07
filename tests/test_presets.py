from __future__ import annotations

from pathlib import Path

import pytest

from offloader.hashers import ALGORITHMS
from offloader.models import VerificationMode
from offloader.presets import Preset, PresetStore, default_presets


@pytest.fixture
def store(tmp_path: Path) -> PresetStore:
    return PresetStore(tmp_path / "presets.json")


def test_first_run_seeds_defaults(store: PresetStore):
    assert [p.name for p in store.presets] == [p.name for p in default_presets()]
    assert store.path.is_file()


def test_round_trip_survives_reload(tmp_path: Path):
    path = tmp_path / "presets.json"
    original = PresetStore(path)
    original.add(Preset(
        name="Archive",
        destinations=[Path("D:/a"), Path("E:/b")],
        algorithm="md5",
        verification=VerificationMode.FULL,
        reports=["pdf", "mhl"],
        excludes=["*.tmp"],
        color="#3f9e6b",
        naming_template="{card}_{date}",
    ))

    reloaded = PresetStore(path)
    restored = next(p for p in reloaded.presets if p.name == "Archive")
    assert restored.destinations == [Path("D:/a"), Path("E:/b")]
    assert restored.algorithm == "md5"
    assert restored.verification is VerificationMode.FULL
    assert restored.reports == ["pdf", "mhl"]
    assert restored.excludes == ["*.tmp"]
    assert restored.naming_template == "{card}_{date}"


def test_corrupt_file_falls_back_to_defaults(tmp_path: Path):
    # A broken config must never stand between someone and their card.
    path = tmp_path / "presets.json"
    path.write_text("{not json", encoding="utf-8")
    assert PresetStore(path).presets


def test_names_are_kept_unique(store: PresetStore):
    store.add(Preset(name="Dailies"))
    store.add(Preset(name="Dailies"))
    names = store.names()
    assert names.count("Dailies") == 1
    assert "Dailies 2" in names


def test_duplicate_copies_settings_but_resets_usage(store: PresetStore):
    original = store.add(Preset(name="Base", destinations=[Path("D:/x")],
                                algorithm="sha1", use_count=7))
    copy = store.duplicate(store.presets.index(original))

    assert copy.name == "Base copy"
    assert copy.destinations == [Path("D:/x")]
    assert copy.algorithm == "sha1"
    assert copy.use_count == 0
    # The lists must not be shared with the original.
    copy.destinations.append(Path("E:/y"))
    assert original.destinations == [Path("D:/x")]


def test_remove_and_move(store: PresetStore):
    store.presets.clear()
    for name in ("A", "B", "C"):
        store.add(Preset(name=name))

    assert store.move(2, -1) == 1
    assert store.names() == ["A", "C", "B"]
    store.remove(0)
    assert store.names() == ["C", "B"]


def test_move_clamps_at_the_ends(store: PresetStore):
    store.presets.clear()
    store.add(Preset(name="only"))
    assert store.move(0, -5) == 0
    assert store.move(0, 5) == 0


@pytest.mark.parametrize("mode,expected_first", [
    ("name", "Alpha"),
    ("in-use", "Zulu"),
])
def test_sorting(store: PresetStore, mode: str, expected_first: str):
    store.presets.clear()
    store.add(Preset(name="Zulu", use_count=9, color="#b3565b"))
    store.add(Preset(name="Alpha", use_count=0, color="#5577b0"))
    assert store.sorted(mode)[0].name == expected_first


def test_sort_by_color_follows_the_swatch_order(store: PresetStore):
    from offloader.presets import PRESET_COLORS

    store.presets.clear()
    store.add(Preset(name="second", color=PRESET_COLORS[2]))
    store.add(Preset(name="first", color=PRESET_COLORS[0]))
    assert [p.name for p in store.sorted("color")] == ["first", "second"]


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


def test_runnable_requires_a_destination():
    assert not Preset(name="empty").is_runnable
    assert Preset(name="ok", destinations=[Path("D:/x")]).is_runnable


def test_mark_used_records_a_timestamp():
    preset = Preset(name="p")
    preset.mark_used()
    assert preset.use_count == 1
    assert preset.last_used is not None
    assert preset.in_use


# ---------------------------------------------------- null tolerance (CI find)


def test_explicit_nulls_fall_back_to_defaults(tmp_path: Path):
    """REGRESSION, found by the property tests on CI. `dict.get(k, default)`
    returns None when the key is *present* with a null value, which is exactly
    what a hand-edited or version-skewed presets.json produces. That gave a
    Preset with `algorithm=None`, which crashes when the job runs."""
    preset = Preset.from_dict({
        "name": None, "algorithm": None, "verification": None,
        "thumbnail_count": None, "reports": None, "excludes": None,
        "destinations": None, "naming_template": None, "color": None,
        "retry_attempts": None, "retry_wait": None, "use_count": None,
    })

    assert preset.algorithm in ALGORITHMS
    assert preset.name
    assert preset.naming_template
    assert preset.reports == ["pdf"]
    assert preset.destinations == []
    assert isinstance(preset.thumbnail_count, int)
    assert isinstance(preset.retry_wait, float)
    # And it must actually be usable, not merely constructible.
    assert preset.to_options().algorithm in ALGORITHMS


def test_nonsense_types_fall_back_rather_than_raise():
    preset = Preset.from_dict({
        "thumbnail_count": "four", "retry_wait": "soon",
        "retry_attempts": [], "excludes": 7, "reports": 3,
        "algorithm": "not-an-algorithm", "verification": "sideways",
    })
    assert preset.thumbnail_count == 4
    assert preset.retry_wait == 2.0
    assert preset.retry_attempts == 3
    assert preset.excludes == []
    assert preset.algorithm in ALGORITHMS
    assert preset.verification is VerificationMode.SOURCE_ONLY


def test_an_explicitly_empty_report_list_is_respected():
    """Offloading without paperwork is a real choice; only a missing or null
    key should fall back to the default."""
    assert Preset.from_dict({"reports": []}).reports == []
    assert Preset.from_dict({}).reports == ["pdf"]
