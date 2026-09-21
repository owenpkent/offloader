"""The release tag gate.

A tag and the version literal disagreeing is the kind of mistake that does not
look like one: the release publishes, the installer installs, and the fault
appears later as an update every installed copy refuses, because the updater
compares the feed's tag against the version compiled into the installer.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from offloader._version import __version__

REPO = Path(__file__).resolve().parent.parent


def _gate():
    path = REPO / "scripts" / "check_tag.py"
    spec = importlib.util.spec_from_file_location("_check_tag", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("tag,expected", [
    ("v0.1.0", "0.1.0"),
    ("0.1.0", "0.1.0"),
    ("refs/tags/v0.1.0", "0.1.0"),
    ("refs/tags/v0.1.0b1", "0.1.0b1"),
    ("  v0.2.0  ", "0.2.0"),
])
def test_the_version_is_read_out_of_the_tag(tag: str, expected: str):
    assert _gate().version_from_tag(tag) == expected


def test_the_declared_version_passes():
    assert _gate().main([f"v{__version__}"]) == 0


def test_a_tag_naming_another_version_fails(capsys):
    """The whole point. Exit 1 so a workflow stops before building."""
    assert _gate().main(["v99.0.0"]) == 1
    assert "_version.py" in capsys.readouterr().err


@pytest.mark.parametrize("tag", ["latest", "v1.2", "release-1", "v1.2.3-evil",
                                 "v1.2.3.4", ""])
def test_a_tag_that_is_not_a_release_version_fails(tag: str):
    """Rejected by the same grammar the updater and the installer's Windows
    version fields use, so a tag that cannot be published is refused here
    rather than producing an asset nothing can compare."""
    assert _gate().main([tag]) == 2


def test_the_gate_uses_one_grammar_with_the_updater():
    """If these ever diverge, a tag could pass the gate and then be invisible
    to the updater, or vice versa."""
    from offloader import update

    module = _gate()
    for value in ("0.1.0", "0.1.0b1", "latest", "1.2", "1.2.3-evil"):
        publishable = update.parse_version(value) is not None
        assert (module.main([value]) != 2) is publishable, value
