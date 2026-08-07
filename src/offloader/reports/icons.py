"""Vector placeholders drawn directly on the canvas.

Files with no video stream get a filmstrip glyph where the contact sheet would
go, and jobs with no supplied logo get a neutral disc. Both are drawn rather
than shipped as bitmaps, so the repo carries no third-party artwork.
"""

from __future__ import annotations

from reportlab.lib.colors import HexColor
from reportlab.pdfgen.canvas import Canvas

from . import fonts, layout


def draw_filmstrip(canvas: Canvas, x: float, y_top: float, width: float,
                   height: float, label: str = "RAW") -> None:
    """A filmstrip tile: dark body, sprocket holes down both edges, and a
    short label naming the format."""
    y = layout.to_canvas(y_top + height)

    canvas.saveState()
    canvas.setFillColor(HexColor("#1c1c1c"))
    canvas.roundRect(x, y, width, height, radius=3.0, stroke=0, fill=1)

    hole_w = width * 0.085
    hole_h = height * 0.075
    margin = width * 0.045
    rows = 6
    gap = (height - rows * hole_h) / (rows + 1)

    canvas.setFillColor(HexColor("#f2f2f2"))
    for row in range(rows):
        hy = y + gap + row * (hole_h + gap)
        canvas.rect(x + margin, hy, hole_w, hole_h, stroke=0, fill=1)
        canvas.rect(x + width - margin - hole_w, hy, hole_w, hole_h, stroke=0, fill=1)

    # Three stacked frames in the centre channel, echoing the reference icon.
    frame_w = width * 0.34
    frame_h = height * 0.15
    fx = x + (width - frame_w) / 2
    canvas.setStrokeColor(HexColor("#f0a92b"))
    canvas.setLineWidth(1.2)
    for i in range(3):
        fy = y + height * 0.46 + i * (frame_h + height * 0.055)
        canvas.roundRect(fx, fy, frame_w, frame_h, radius=1.5, stroke=1, fill=0)

    canvas.setFillColor(HexColor("#f2f2f2"))
    size = max(5.0, height * 0.16)
    canvas.setFont(fonts.font(layout.FONT_BOLD), size)
    canvas.drawCentredString(x + width / 2, y + height * 0.18, label)
    canvas.restoreState()


def draw_default_logo(canvas: Canvas, x: float, y_top: float, width: float,
                      height: float) -> None:
    """Neutral disc used when the job supplies no logo image."""
    y = layout.to_canvas(y_top + height)
    radius = min(width, height) / 2

    canvas.saveState()
    canvas.setFillColor(HexColor("#5577b0"))
    canvas.circle(x + width / 2, y + height / 2, radius, stroke=0, fill=1)
    canvas.setFillColor(HexColor("#ffffff"))
    canvas.setFillAlpha(0.28)
    canvas.circle(x + width * 0.38, y + height * 0.64, radius * 0.52, stroke=0, fill=1)
    canvas.restoreState()


def format_label(suffix: str) -> str:
    """Short badge text for a non-video file, drawn inside the filmstrip."""
    clean = suffix.lstrip(".").upper()
    return clean[:5] if clean else "FILE"
