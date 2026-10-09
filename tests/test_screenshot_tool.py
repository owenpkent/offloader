"""The screenshot tool's guard against rendering real volumes.

`tools/screenshots.py` is not shipped, but it writes the PNGs in a public
README, and the thing it is guarding against already happened once: it patched
a function the drive panel had stopped calling, so the patch became a no-op,
the real scan ran, and whether a real drive label reached the images came down
to which write landed last. A guard that can rot silently is worth a test.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="GUI extra not installed")

TOOL = Path(__file__).resolve().parent.parent / "tools" / "screenshots.py"


@pytest.fixture(scope="module")
def tool():
    """The tool, imported with its environment put back afterwards.

    Importing it redirects `APPDATA` and `XDG_CONFIG_HOME` to a sandbox at
    module scope — deliberate in the tool, and not something to leave behind
    for the rest of the suite, which reads those to find the config directory.
    """
    saved = {name: os.environ.get(name)
             for name in ("APPDATA", "XDG_CONFIG_HOME")}
    try:
        spec = importlib.util.spec_from_file_location("_screenshots", TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _window(*labels: str):
    """The shape `assert_no_real_volumes` reads: rows carrying volumes."""
    rows = [SimpleNamespace(volume=SimpleNamespace(label=label))
            for label in labels]
    return SimpleNamespace(drives=SimpleNamespace(_rows=rows))


# ------------------------------------------------------------------- the stub


def test_the_stub_replaces_the_scan_the_panel_calls(tool, monkeypatch):
    """Not a name it used to call. The previous version assigned
    `drives.list_volumes`, which no longer exists, so nothing was overridden
    and the real scan kept running."""
    monkeypatch.setattr(tool.drives, "scan_batches",
                        lambda: iter([("real volumes", True)]))
    tool.stub_volume_scan()

    batches = list(tool.drives.scan_batches())
    assert [volumes for volumes, _ in batches] == [tool.VOLUMES]


def test_the_stub_yields_one_final_batch(tool, monkeypatch):
    """A non-final batch tells the panel to keep network shares from the
    previous scan, and a second delivery is what raced the invented list in the
    first place. One batch, marked final, leaves neither opening."""
    monkeypatch.setattr(tool.drives, "scan_batches", lambda: iter([]))
    tool.stub_volume_scan()

    batches = list(tool.drives.scan_batches())
    assert len(batches) == 1
    assert batches[0][1] is True


def test_a_missing_scan_function_stops_the_run(tool, monkeypatch):
    """The regression this guard exists for: the tool must not fall back to
    letting the panel scan the machine."""
    monkeypatch.delattr(tool.drives, "scan_batches")

    with pytest.raises(SystemExit, match="scan_batches"):
        tool.stub_volume_scan()


def test_a_non_callable_scan_attribute_is_not_good_enough(tool, monkeypatch):
    """`hasattr` would have accepted this, and a stub assigned over a
    non-function is the same silent no-op in a different disguise."""
    monkeypatch.setattr(tool.drives, "scan_batches", "not a function")

    with pytest.raises(SystemExit, match="scan_batches"):
        tool.stub_volume_scan()


# ------------------------------------------------------------------ the check


def test_invented_volumes_pass_the_check(tool):
    tool.assert_no_real_volumes(_window(*[v.label for v in tool.VOLUMES]))


def test_a_real_volume_refuses_to_render(tool):
    """What a leak looks like: a network share's own label on screen."""
    with pytest.raises(SystemExit, match="NAS_1"):
        tool.assert_no_real_volumes(_window("A001 (PYXIS)", "NAS_1"))


def test_the_refusal_names_every_leaked_volume(tool):
    """So the person running it can see it was the whole scan that got
    through, not one stray row."""
    with pytest.raises(SystemExit) as raised:
        tool.assert_no_real_volumes(_window("home", "Marketing"))

    message = str(raised.value)
    assert "home" in message and "Marketing" in message


def test_an_empty_panel_is_not_treated_as_a_leak(tool):
    """The panel is empty until the first batch arrives, and a guard that
    failed the run for that would fire on timing rather than on a leak."""
    tool.assert_no_real_volumes(_window())
