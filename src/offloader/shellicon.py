"""The product mark, rendered to a Windows `.ico`.

Drawn rather than shipped, for the same reason `reports.icons` draws the PDF's
glyphs: the repository carries no binary artwork, and the shell entry, the
window and the report header then take their identity from one filmstrip rather
than three files that drift apart.

Encoded with `zlib` and `struct` because the base install has neither Pillow
nor any other raster library, and reportlab writes PDFs. An ICO is a short
header, one directory entry per size and a PNG per size, all of which the
standard library can produce.
"""

from __future__ import annotations

import os
import struct
import sys
import zlib
from pathlib import Path

#: The filmstrip palette, matching `reports.icons.draw_filmstrip`. Defined here
#: rather than imported so a shell icon does not drag in the report layer.
BODY = (0x1C, 0x1C, 0x1C, 0xFF)
SPROCKET = (0xF2, 0xF2, 0xF2, 0xFF)
FRAME = (0xF0, 0xA9, 0x2B, 0xFF)

#: Sizes the shell chooses between: 16 for the context menu and small views,
#: 32 and 48 for the medium ones, 256 for the large. Windows scales from the
#: nearest, so supplying the exact sizes avoids it resampling 256 down to 16 and
#: turning the sprocket holes to mush.
ICO_SIZES = (16, 32, 48, 64, 128, 256)


class _Canvas:
    """A little RGBA raster. Transparent until something is drawn on it."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.pixels = bytearray(size * size * 4)

    def _put(self, x: int, y: int, colour: tuple[int, int, int, int]) -> None:
        if 0 <= x < self.size and 0 <= y < self.size:
            at = (y * self.size + x) * 4
            self.pixels[at:at + 4] = bytes(colour)

    def rect(self, x: int, y: int, width: int, height: int,
             colour: tuple[int, int, int, int]) -> None:
        for row in range(y, y + height):
            for col in range(x, x + width):
                self._put(col, row, colour)

    def round_rect(self, x: int, y: int, width: int, height: int, radius: int,
                   colour: tuple[int, int, int, int]) -> None:
        """A filled rectangle with the corners taken off.

        No anti-aliasing: at 16 px a soft edge reads as a smudge, and the shell
        composites the icon over backgrounds of either polarity.
        """
        radius = max(0, min(radius, width // 2, height // 2))
        for row in range(y, y + height):
            for col in range(x, x + width):
                dx = min(col - x, x + width - 1 - col)
                dy = min(row - y, y + height - 1 - row)
                if dx < radius and dy < radius:
                    off_x = radius - 1 - dx
                    off_y = radius - 1 - dy
                    if off_x * off_x + off_y * off_y > radius * radius:
                        continue
                self._put(col, row, colour)


def _draw(size: int) -> _Canvas:
    """The mark at one size, simplified as it gets smaller.

    Six sprocket holes and three outlined frames are legible at 48 px and
    above. Below that they collapse into each other, so the count drops and the
    frames fill rather than outline — the silhouette survives, which is all a
    16 px icon can carry.
    """
    canvas = _Canvas(size)

    margin = max(1, round(size * 0.055))
    body = size - 2 * margin
    canvas.round_rect(margin, margin, body, body,
                      max(1, round(size * 0.16)), BODY)

    hole_w = max(1, round(size * 0.10))
    hole_h = max(1, round(size * 0.105))
    inset = margin + max(1, round(size * 0.055))
    rows = 6 if size >= 48 else 4
    gap = (body - rows * hole_h) / (rows + 1)
    for row in range(rows):
        top = margin + round(gap + row * (hole_h + gap))
        canvas.rect(inset, top, hole_w, hole_h, SPROCKET)
        canvas.rect(size - inset - hole_w, top, hole_w, hole_h, SPROCKET)

    frames = 3 if size >= 48 else (2 if size >= 24 else 1)
    frame_w = max(2, round(size * 0.34))
    frame_h = max(1, round(size * 0.13))
    frame_gap = max(1, round(size * 0.07))
    left = (size - frame_w) // 2
    stack = frames * frame_h + (frames - 1) * frame_gap
    top = (size - stack) // 2
    # Stroke scales with the icon: a hairline that reads as a crisp edge at
    # 48 px is a thread at 256, and one fixed width cannot be both.
    stroke = max(1, round(size * 0.012))
    outline = size >= 48 and frame_h > 2 * stroke + 1
    for index in range(frames):
        at = top + index * (frame_h + frame_gap)
        canvas.rect(left, at, frame_w, frame_h, FRAME)
        if outline:
            canvas.rect(left + stroke, at + stroke,
                        frame_w - 2 * stroke, frame_h - 2 * stroke, BODY)
    return canvas


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_bytes(canvas: _Canvas) -> bytes:
    """`canvas` as a PNG: 8-bit RGBA, no interlacing, filter 0 on every row."""
    stride = canvas.size * 4
    raw = bytearray()
    for row in range(canvas.size):
        raw.append(0)                      # filter: none
        raw += canvas.pixels[row * stride:(row + 1) * stride]
    header = struct.pack(">IIBBBBB", canvas.size, canvas.size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def png_at(size: int) -> bytes:
    """The mark at one size, as a PNG. For callers that want a raster rather
    than an icon file — the desktop app's window icon, for instance."""
    return png_bytes(_draw(size))


def ico_bytes(sizes: tuple[int, ...] = ICO_SIZES) -> bytes:
    """A multi-resolution icon holding the mark at each of `sizes`."""
    images = [png_bytes(_draw(size)) for size in sizes]
    offset = 6 + 16 * len(images)
    entries = b""
    for size, image in zip(sizes, images, strict=True):
        # 0 means 256 in an icon directory: the field is one byte.
        dimension = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32,
                               len(image), offset)
        offset += len(image)
    return (struct.pack("<HHH", 0, 1, len(images)) + entries
            + b"".join(images))


def default_path() -> Path:
    """Where the icon is kept for the shell to read it.

    Not inside the package: a registry value points at this path for as long as
    the menu entry exists, and a path under `site-packages` dies with the next
    reinstall or virtual environment.
    """
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA")
                    or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME")
                    or Path.home() / ".cache")
    return base / "Offloader" / "offloader.ico"


def write(path: Path | None = None,
          sizes: tuple[int, ...] = ICO_SIZES) -> Path:
    """Render the icon to `path` (default `default_path()`) and return it."""
    target = Path(path) if path is not None else default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ico_bytes(sizes))
    return target
