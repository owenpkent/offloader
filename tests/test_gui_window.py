"""Main-window logic: the guards that run before anything is queued.

These are the checks that stop an operator wiping a card or filling a disk, so
they are worth testing directly rather than only through the interface.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="GUI extra not installed")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from offloader.gui import main_window as mw  # noqa: E402
from offloader.presets import Preset  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    """A MainWindow whose presets, history and settings live in the temp dir.

    Each module imported `config_file` by name, so patch it where it is used.
    """
    from offloader import history as history_module
    from offloader import presets as presets_module

    monkeypatch.setattr(presets_module, "config_file", lambda n: tmp_path / n)
    monkeypatch.setattr(history_module, "config_file", lambda n: tmp_path / n)
    monkeypatch.setattr(mw, "config_file", lambda n: tmp_path / n)

    window = mw.MainWindow()
    window.drives.stop()          # no volume polling during tests
    yield window
    window.controller.shutdown(2000)
    window.close()


@pytest.fixture
def prompts(monkeypatch):
    """Capture message boxes instead of showing them."""
    seen: list[tuple[str, str]] = []

    def warning(_parent, title, text, *args, **kwargs):
        seen.append(("warning", f"{title}: {text}"))
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(warning))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(warning))

    def answer(value):
        def question(_parent, title, text, *args, **kwargs):
            seen.append(("question", f"{title}: {text}"))
            return value
        monkeypatch.setattr(QMessageBox, "question", staticmethod(question))

    answer(QMessageBox.No)
    return type("Prompts", (), {"seen": seen, "answer": staticmethod(answer)})


def _card(tmp_path: Path) -> Path:
    root = tmp_path / "card"
    root.mkdir(parents=True, exist_ok=True)
    (root / "clip.mov").write_bytes(b"x" * 4096)
    return root


# ------------------------------------------------------------------ guards


def test_preset_without_destinations_is_refused(window, prompts, tmp_path):
    source = _card(tmp_path)
    assert not window._check_destinations(source, Preset(name="empty"))
    assert any("No destinations" in message for _, message in prompts.seen)


def test_destination_inside_the_source_is_refused(window, prompts, tmp_path):
    """Copying a tree into itself would recurse; refuse before queueing."""
    source = _card(tmp_path)
    preset = Preset(name="p", destinations=[source / "inner"])
    assert not window._check_destinations(source, preset)
    assert any("inside" in message.lower() for _, message in prompts.seen)


def test_source_equal_to_destination_is_refused(window, prompts, tmp_path):
    source = _card(tmp_path)
    preset = Preset(name="p", destinations=[source])
    assert not window._check_destinations(source, preset)


def test_empty_source_is_refused(window, prompts, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    preset = Preset(name="p", destinations=[tmp_path / "dest"])
    assert not window._check_destinations(empty, preset)
    assert any("Nothing to offload" in message for _, message in prompts.seen)


def test_a_valid_pair_passes(window, prompts, tmp_path):
    source = _card(tmp_path)
    preset = Preset(name="p", destinations=[tmp_path / "dest"])
    assert window._check_destinations(source, preset)
    assert prompts.seen == []


def test_insufficient_space_prompts_and_can_be_declined(window, prompts, tmp_path,
                                                        monkeypatch):
    import shutil as shutil_module

    class Usage:
        total, used, free = 10, 9, 1      # one byte free

    monkeypatch.setattr(shutil_module, "disk_usage", lambda _p: Usage)
    source = _card(tmp_path)
    preset = Preset(name="p", destinations=[tmp_path / "dest"])

    assert not window._check_destinations(source, preset)
    assert any("Not enough space" in message for _, message in prompts.seen)


def test_insufficient_space_can_be_overridden(window, prompts, tmp_path, monkeypatch):
    import shutil as shutil_module

    class Usage:
        total, used, free = 10, 9, 1

    monkeypatch.setattr(shutil_module, "disk_usage", lambda _p: Usage)
    prompts.answer(QMessageBox.Yes)
    source = _card(tmp_path)
    preset = Preset(name="p", destinations=[tmp_path / "dest"])

    assert window._check_destinations(source, preset)


# ------------------------------------------------------------------ duplicates


def test_a_fresh_card_is_not_a_duplicate(window, prompts, tmp_path):
    assert window._check_duplicate(_card(tmp_path))
    assert prompts.seen == []


def test_a_previously_offloaded_card_warns(window, prompts, tmp_path):
    from offloader import engine, history

    source = _card(tmp_path)
    job = engine.run(source, engine.OffloadOptions(
        destinations=[tmp_path / "dest"], thumbnail_count=0, extra_probe=False,
        job_name="earlier"))
    window.controller.history.record(
        job, history.fingerprint(engine.scan(source), source))

    assert not window._check_duplicate(source)          # declined
    assert any("earlier" in message for _, message in prompts.seen)

    prompts.answer(QMessageBox.Yes)
    assert window._check_duplicate(source)             # accepted


def test_duplicate_warning_can_be_switched_off(window, prompts, tmp_path):
    from offloader import engine, history

    source = _card(tmp_path)
    job = engine.run(source, engine.OffloadOptions(
        destinations=[tmp_path / "dest"], thumbnail_count=0, extra_probe=False))
    window.controller.history.record(
        job, history.fingerprint(engine.scan(source), source))

    window.settings["warn_on_duplicate"] = False
    assert window._check_duplicate(source)
    assert prompts.seen == []


def test_changing_the_card_clears_the_duplicate_match(window, prompts, tmp_path):
    from offloader import engine, history

    source = _card(tmp_path)
    job = engine.run(source, engine.OffloadOptions(
        destinations=[tmp_path / "dest"], thumbnail_count=0, extra_probe=False))
    window.controller.history.record(
        job, history.fingerprint(engine.scan(source), source))

    (source / "another.mov").write_bytes(b"y" * 100)
    assert window._check_duplicate(source)             # different card now
    assert prompts.seen == []


# ------------------------------------------------------------------ wiring


def test_enqueue_is_blocked_by_a_failing_guard(window, prompts, tmp_path):
    window._enqueue(_card(tmp_path), Preset(name="empty"), None)
    assert window.controller.items == []


def test_enqueue_adds_a_job_when_the_guards_pass(window, prompts, tmp_path):
    window.controller._auto_start = False
    window._enqueue(_card(tmp_path), Preset(name="p", destinations=[tmp_path / "d"]),
                    "A001")
    assert [i.name for i in window.controller.items] == ["A001"]


def test_mode_switch_persists(window, tmp_path):
    window._set_mode(1)
    assert window.stack.currentIndex() == 1
    assert window._simple_button.isChecked()
    assert window.settings["mode"] == "simple"

    window._set_mode(0)
    assert window.settings["mode"] == "preset"
    assert window._preset_button.isChecked()


def test_choosing_a_source_reaches_both_panels(window, tmp_path):
    source = _card(tmp_path)
    window._set_source(source)
    assert window.simple.drop_zone.path == source
    assert window.presets.drop_zone.path == source


def test_settings_survive_a_reload(window, tmp_path):
    window._save_setting("sound_on_completion", False)
    from offloader.config import read_json
    assert read_json(tmp_path / mw.SETTINGS_FILE, {})["sound_on_completion"] is False


def test_environment_summary_reports_missing_tools(window, monkeypatch):
    monkeypatch.setattr(mw.probe, "ffprobe_path", lambda: None)
    monkeypatch.setattr(mw.thumbs, "ffmpeg_path", lambda: None)
    summary = window._environment_summary()
    assert "ffprobe" in summary and "ffmpeg" in summary
