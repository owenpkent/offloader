"""The drawn icon and the Explorer context-menu entry.

The icon is checked as a file format rather than by eye: an ICO the shell cannot
parse fails silently, showing a generic page instead of the mark, and nothing in
a normal run would say so. The menu entry is checked as a *plan* — the registry
writes are Windows-only, but what gets written is a pure function and should be
wrong in the same way on every platform or not at all.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import pytest

from offloader import shellicon, shellmenu

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _ico_entries(blob: bytes) -> list[tuple[int, int, int, int]]:
    """(width, height, byte length, offset) per image in an icon directory."""
    reserved, kind, count = struct.unpack("<HHH", blob[:6])
    assert reserved == 0 and kind == 1, "not an icon directory"
    out = []
    for index in range(count):
        at = 6 + 16 * index
        width, height, _, _, _, _, length, offset = struct.unpack(
            "<BBBBHHII", blob[at:at + 16])
        out.append((width or 256, height or 256, length, offset))
    return out


# ------------------------------------------------------------------ the icon


def test_the_icon_holds_every_size_it_claims():
    blob = shellicon.ico_bytes()
    entries = _ico_entries(blob)

    assert [w for w, _, _, _ in entries] == list(shellicon.ICO_SIZES)
    for width, height, length, offset in entries:
        assert width == height
        assert blob[offset:offset + 8] == PNG_MAGIC
        assert offset + length <= len(blob), "an entry runs past the file"


def test_each_image_is_a_png_of_the_size_it_is_filed_under():
    """A directory entry saying 48 and a payload that decodes to 32 is the
    failure that makes the shell draw the wrong thing at the wrong scale."""
    blob = shellicon.ico_bytes()
    for width, height, length, offset in _ico_entries(blob):
        image = blob[offset:offset + length]
        # IHDR is the first chunk: 8 bytes of magic, 4 length, 4 tag, then w/h.
        declared_w, declared_h = struct.unpack(">II", image[16:24])
        assert (declared_w, declared_h) == (width, height)


def test_the_png_chunks_carry_sound_checksums():
    """zlib.crc32 is what a decoder checks; a bad one is a corrupt file."""
    image = shellicon.png_at(32)
    assert image[:8] == PNG_MAGIC
    at = 8
    tags = []
    while at < len(image):
        length = struct.unpack(">I", image[at:at + 4])[0]
        tag = image[at + 4:at + 8]
        body = image[at + 8:at + 8 + length]
        stored = struct.unpack(">I", image[at + 8 + length:at + 12 + length])[0]
        assert stored == zlib.crc32(tag + body) & 0xFFFFFFFF, f"{tag!r} crc"
        tags.append(tag)
        at += 12 + length
    assert tags == [b"IHDR", b"IDAT", b"IEND"]


def test_the_mark_is_drawn_not_blank():
    """A silhouette of nothing would pass every structural check above."""
    canvas = shellicon._draw(48)
    opaque = sum(1 for i in range(3, len(canvas.pixels), 4)
                 if canvas.pixels[i] != 0)
    assert opaque > 48 * 48 * 0.5, "most of the tile should be the body"

    colours = {tuple(canvas.pixels[at:at + 4])
               for at in range(0, len(canvas.pixels), 4)}
    assert shellicon.BODY in colours
    assert shellicon.SPROCKET in colours
    assert shellicon.FRAME in colours


def test_the_corners_are_transparent():
    """Rounded, so the icon does not read as a square tile against the shell's
    own background."""
    canvas = shellicon._draw(64)
    assert canvas.pixels[3] == 0, "top-left corner is opaque"


def test_write_puts_an_icon_where_it_says(tmp_path: Path):
    target = shellicon.write(tmp_path / "nested" / "mark.ico")
    assert target.exists()
    assert _ico_entries(target.read_bytes())


# ------------------------------------------------------------- the menu entry


def test_both_verbs_are_planned():
    entries = shellmenu.entries("C:\\icons\\offloader.ico", command='"app.exe"')

    assert [entry.applies_to for entry in entries] == ["Drive", "Directory"]
    for entry in entries:
        assert entry.key.startswith("Software\\Classes\\")
        assert entry.command_key == entry.key + "\\command"
        assert entry.icon == "C:\\icons\\offloader.ico"


def test_the_command_passes_the_clicked_path():
    """%V rather than %1: a drive root is the case the entry exists for, and
    %1 does not carry it."""
    entry = shellmenu.entries("i.ico", command='"app.exe"')[0]
    assert entry.command == '"app.exe" "%V"'


def test_the_launcher_is_quoted_for_a_path_with_spaces():
    """Registry commands are parsed by the shell; an unquoted Program Files
    path becomes two arguments."""
    assert shellmenu.launcher().startswith('"')


def test_nothing_is_claimed_installed_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert shellmenu.installed() is False
    with pytest.raises(OSError, match="Windows"):
        shellmenu.install("i.ico")


@pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")
def test_install_then_uninstall_round_trips(monkeypatch):
    """Against the real registry, under HKCU, which needs no administrator.

    Writes to a key of its own so a developer's actual menu entry is neither
    read nor removed by the test.
    """
    suffix = "\\shell\\OffloaderTest"
    monkeypatch.setattr(shellmenu, "DRIVE_KEY",
                        f"{shellmenu.CLASSES}\\Drive{suffix}")
    monkeypatch.setattr(shellmenu, "DIRECTORY_KEY",
                        f"{shellmenu.CLASSES}\\Directory{suffix}")
    import winreg

    try:
        shellmenu.install("C:\\icons\\offloader.ico")
        assert shellmenu.installed()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            shellmenu.DRIVE_KEY) as key:
            assert winreg.QueryValueEx(key, "Icon")[0] == \
                "C:\\icons\\offloader.ico"
            assert "Offload" in winreg.QueryValue(key, None)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            shellmenu.DRIVE_KEY + "\\command") as key:
            assert "%V" in winreg.QueryValue(key, None)
    finally:
        shellmenu.uninstall()

    assert not shellmenu.installed()


@pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")
def test_uninstalling_what_is_not_there_is_not_an_error(monkeypatch):
    monkeypatch.setattr(shellmenu, "DRIVE_KEY",
                        f"{shellmenu.CLASSES}\\Drive\\shell\\OffloaderAbsent")
    monkeypatch.setattr(
        shellmenu, "DIRECTORY_KEY",
        f"{shellmenu.CLASSES}\\Directory\\shell\\OffloaderAbsent")
    assert shellmenu.uninstall() == []
