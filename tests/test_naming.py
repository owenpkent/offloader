from __future__ import annotations

import datetime as _dt
from pathlib import Path

from offloader import naming

WHEN = _dt.datetime(2026, 8, 7, 16, 41, 26)


def test_default_template_uses_the_card_name(tmp_path: Path):
    card = tmp_path / "A001"
    assert naming.build("{card}", card, when=WHEN) == "A001"


def test_tokens_render():
    card = Path("/Volumes/A001")
    assert naming.build("{card}_{date}", card, when=WHEN) == "A001_20260807"
    assert naming.build("{card}-{time}", card, when=WHEN) == "A001-164126"
    assert naming.build("{year}/{month}", card, when=WHEN) == "2026_08"  # / sanitised


def test_unknown_tokens_are_left_alone_not_raised():
    # A typo in a preset must not stop an offload.
    assert naming.build("{card}_{nope}", Path("A001"), when=WHEN) == "A001_{nope}"


def test_illegal_characters_are_replaced():
    assert naming.sanitize('bad:name*here?') == "bad_name_here_"
    assert naming.sanitize("trailing. ") == "trailing"
    assert naming.sanitize("   ") == "Offload"


def test_index_token_advances_past_taken_names():
    card = Path("A001")
    taken = ["Roll_001", "Roll_002"]
    assert naming.build("Roll_{index}", card, when=WHEN, taken=taken) == "Roll_003"


def test_collision_without_index_gets_a_suffix():
    card = Path("A001")
    assert naming.build("{card}", card, when=WHEN, taken=["A001"]) == "A001-2"
    assert naming.build("{card}", card, when=WHEN, taken=["A001", "A001-2"]) == "A001-3"


def test_collision_check_is_case_insensitive():
    # Windows and exFAT both fold case; two jobs must not share a report folder.
    assert naming.build("{card}", Path("A001"), when=WHEN, taken=["a001"]) == "A001-2"


def test_volume_label_falls_back_to_folder_name():
    values = naming.context(Path("/Volumes/A001"), volume_label="CARD_A", when=WHEN)
    assert values["volume"] == "CARD_A"
    assert naming.context(Path("/Volumes/A001"), when=WHEN)["volume"] == "A001"


def test_card_token_prefers_the_volume_label_for_a_bare_root():
    """A card offloaded from its root has no folder name; what the operator
    calls it is the volume label — A003, not E."""
    from offloader import naming

    values = naming.context(Path("E:/"), volume_label="A003")
    assert values["card"] == "A003"
    # A real folder name still wins; the label describes the volume, the
    # folder describes the selection.
    values = naming.context(Path("E:/DCIM"), volume_label="A003")
    assert values["card"] == "DCIM"
    # No label falls back to the drive letter, never to nothing.
    values = naming.context(Path("E:/"))
    assert values["card"] == "E"
