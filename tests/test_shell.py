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


def test_256_is_stored_as_zero_in_the_directory():
    """The width and height fields are one byte each, so 256 cannot be written
    literally. A 256 there truncates to 0 by accident and happens to be right;
    writing it deliberately is the difference between that and a 255-pixel
    icon nobody asked for."""
    blob = shellicon.ico_bytes((256,))
    assert blob[6] == 0 and blob[7] == 0


def test_a_custom_size_list_is_honoured():
    blob = shellicon.ico_bytes((32, 48))
    assert [w for w, _, _, _ in _ico_entries(blob)] == [32, 48]


@pytest.mark.parametrize("size", shellicon.ICO_SIZES)
def test_every_size_draws_inside_its_own_canvas(size: int):
    """The mark is drawn from proportions of `size`, and rounding each of them
    independently is how a rectangle ends up one pixel past the edge. The
    canvas would not complain — `_put` drops out-of-range pixels — so the only
    symptom would be a clipped mark at one size."""
    canvas = shellicon._draw(size)
    assert len(canvas.pixels) == size * size * 4
    opaque = sum(1 for i in range(3, len(canvas.pixels), 4)
                 if canvas.pixels[i] != 0)
    assert opaque > 0, "nothing was drawn"
    assert opaque < size * size, "the mark fills the tile, so it is not rounded"


def _frame_bands(canvas) -> int:
    """How many separate amber frames the mark has, by counting runs of rows
    that contain any frame pixel.

    Deliberately not a scan down one column: above 48 px the frames are drawn
    as outlines, so a single column crosses each one's top and bottom edge and
    counts it twice. Every row within a frame's height has frame pixels
    somewhere — its left and right edges if nothing else — which makes this
    measurement indifferent to whether they are filled or outlined.
    """
    bands = 0
    inside = False
    for row in range(canvas.size):
        start = row * canvas.size * 4
        row_pixels = canvas.pixels[start:start + canvas.size * 4]
        has_frame = any(
            tuple(row_pixels[at:at + 4]) == shellicon.FRAME
            for at in range(0, len(row_pixels), 4)
        )
        if has_frame and not inside:
            bands += 1
        inside = has_frame
    return bands


@pytest.mark.parametrize("size,frames", [(16, 1), (32, 2), (48, 3), (256, 3)])
def test_the_mark_simplifies_as_it_shrinks(size: int, frames: int):
    """Six sprocket holes and three outlined frames are mush at 16 px, so the
    detail is meant to drop rather than shrink. Counting the frames is the
    cheapest way to hold that intent still."""
    assert _frame_bands(shellicon._draw(size)) == frames


def test_a_radius_larger_than_the_box_does_not_invert_it():
    """`round_rect` is called with a proportion of the size, so a small enough
    tile asks for a radius wider than the rectangle. Clamped, it rounds fully;
    unclamped, the corner test runs on negative offsets."""
    canvas = shellicon._Canvas(4)
    canvas.round_rect(0, 0, 4, 4, radius=99, colour=shellicon.BODY)
    opaque = sum(1 for i in range(3, len(canvas.pixels), 4)
                 if canvas.pixels[i] != 0)
    assert 0 < opaque <= 16


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


# ----------------------------------------------------- finding the launcher


def test_the_user_scripts_directory_is_searched():
    """The bug this had. A `pip install --user` puts entry points under the
    user scheme, nowhere near `sys.executable`, and a first version guessed
    only beside the interpreter — so it never found the installed GUI script
    and silently fell back to `pythonw -m`."""
    import sysconfig

    scheme = "nt_user" if sys.platform == "win32" else "posix_user"
    if scheme not in sysconfig.get_scheme_names():
        pytest.skip(f"{scheme} is not a scheme on this platform")

    expected = Path(sysconfig.get_path("scripts", scheme=scheme))
    assert expected in shellmenu.script_dirs()


def test_the_interpreters_own_scripts_directory_is_searched_first():
    """A virtual environment's script must win over a stale user-scheme copy
    of the same name, or the entry launches the wrong installation."""
    import sysconfig

    assert shellmenu.script_dirs()[0] == Path(sysconfig.get_path("scripts"))


def test_script_directories_are_not_repeated():
    """The default scheme is often one of the fallbacks too, and a duplicate
    means the same directory is stat'd twice on every install."""
    found = shellmenu.script_dirs()
    assert len(found) == len(set(found))


def test_the_gui_script_is_preferred_when_it_exists(tmp_path, monkeypatch):
    """So clicking the entry opens the app rather than flashing a console
    window, which is what `python.exe` would do."""
    name = "offloader-gui.exe" if sys.platform == "win32" else "offloader-gui"
    script = tmp_path / name
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(shellmenu, "script_dirs", lambda: [tmp_path])

    assert shellmenu.launcher() == f'"{script}"'


def test_the_module_is_the_fallback_when_no_script_is_installed(monkeypatch,
                                                               tmp_path):
    """A checkout that was never installed still gets a working entry."""
    monkeypatch.setattr(shellmenu, "script_dirs", lambda: [tmp_path])

    command = shellmenu.launcher()
    assert "-m offloader.gui.app" in command
    assert command.startswith('"')


def test_the_fallback_runs_the_windowed_interpreter_if_there_is_one(
        monkeypatch, tmp_path):
    """`python.exe` would leave a console window behind the app for as long as
    it runs; `pythonw.exe` beside it is the same interpreter without one."""
    if sys.platform != "win32":
        pytest.skip("pythonw is a Windows interpreter")

    fake_root = tmp_path / "Python"
    fake_root.mkdir()
    (fake_root / "pythonw.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(shellmenu, "script_dirs", lambda: [tmp_path])
    monkeypatch.setattr(sys, "executable", str(fake_root / "python.exe"))

    assert "pythonw.exe" in shellmenu.launcher()


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
def test_uninstall_leaves_no_orphaned_command_key(monkeypatch):
    """`DeleteKey` refuses a key that still has children, so the command
    subkey has to go first. Wrong order leaves the verb's parent behind with
    its command intact — a half-removed entry, which is worse than none,
    because the menu item survives and `installed()` keeps saying yes."""
    suffix = "\\shell\\OffloaderOrphanTest"
    monkeypatch.setattr(shellmenu, "DRIVE_KEY",
                        f"{shellmenu.CLASSES}\\Drive{suffix}")
    monkeypatch.setattr(shellmenu, "DIRECTORY_KEY",
                        f"{shellmenu.CLASSES}\\Directory{suffix}")
    import winreg

    shellmenu.install("C:\\icons\\offloader.ico")
    shellmenu.uninstall()

    for key in (shellmenu.DRIVE_KEY + "\\command", shellmenu.DRIVE_KEY,
                shellmenu.DIRECTORY_KEY + "\\command",
                shellmenu.DIRECTORY_KEY):
        with pytest.raises(OSError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, key).Close()


@pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")
def test_installing_twice_replaces_rather_than_duplicates(monkeypatch):
    """Re-running `shell --install` after an upgrade must repoint the command
    at the new location, not fail and not leave the old one."""
    suffix = "\\shell\\OffloaderTwiceTest"
    monkeypatch.setattr(shellmenu, "DRIVE_KEY",
                        f"{shellmenu.CLASSES}\\Drive{suffix}")
    monkeypatch.setattr(shellmenu, "DIRECTORY_KEY",
                        f"{shellmenu.CLASSES}\\Directory{suffix}")
    import winreg

    try:
        shellmenu.install("one.ico", command='"first.exe"')
        shellmenu.install("two.ico", command='"second.exe"')

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            shellmenu.DRIVE_KEY) as key:
            assert winreg.QueryValueEx(key, "Icon")[0] == "two.ico"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            shellmenu.DRIVE_KEY + "\\command") as key:
            assert winreg.QueryValue(key, None) == '"second.exe" "%V"'
    finally:
        shellmenu.uninstall()


@pytest.mark.skipif(sys.platform != "win32", reason="registry is Windows-only")
def test_uninstalling_what_is_not_there_is_not_an_error(monkeypatch):
    monkeypatch.setattr(shellmenu, "DRIVE_KEY",
                        f"{shellmenu.CLASSES}\\Drive\\shell\\OffloaderAbsent")
    monkeypatch.setattr(
        shellmenu, "DIRECTORY_KEY",
        f"{shellmenu.CLASSES}\\Directory\\shell\\OffloaderAbsent")
    assert shellmenu.uninstall() == []
