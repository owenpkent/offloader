"""Geometry of the PDF report, measured from the reference JobReport.pdf.

Every constant here was read directly out of the reference document's content
streams (span origins, fill rectangles, image bounding boxes), so the generated
report lands on the same coordinates rather than merely looking similar.

Coordinates are **top-left origin, in points**, matching how the values were
measured. `to_canvas()` flips them for ReportLab, whose origin is bottom-left.
"""

from __future__ import annotations

# ---------------------------------------------------------------- page
PAGE_WIDTH = 792.0      # US Letter, landscape
PAGE_HEIGHT = 612.0
PAGE_SIZE = (PAGE_WIDTH, PAGE_HEIGHT)


def to_canvas(y_top: float) -> float:
    """Convert a top-left y to a ReportLab bottom-left y."""
    return PAGE_HEIGHT - y_top


# ---------------------------------------------------------------- colors
COLOR_TITLE = "#666666"
COLOR_LABEL = "#4c4c4c"        # bold field labels in the header and filenames
COLOR_HEADER_VALUE = "#666666"
COLOR_META_LABEL = "#666666"   # bold labels inside a clip's metadata block
COLOR_META_VALUE = "#7f7f7f"
COLOR_FOOTER = "#000000"
COLOR_RULE = "#c0c0c0"

#: The alternating row wash. Stored as the raw fill plus its alpha because the
#: reference paints a saturated blue at 15% rather than a pre-blended grey.
BAND_FILL = (0.33332, 0.46667, 0.69140)
BAND_ALPHA = 0.15

# ---------------------------------------------------------------- fonts
FONT_REGULAR = "Verdana"
FONT_BOLD = "Verdana-Bold"

SIZE_TITLE = 20.0
SIZE_HEADER = 8.0
SIZE_NAME = 8.0
SIZE_META = 6.0
SIZE_DETAIL_NAME = 6.0
SIZE_DETAIL = 5.0
SIZE_FOOTER = 6.0
#: Metadata lines shrink to fit the column beside the thumbnails; the reference
#: drops to 5.7pt on long duration/timecode lines.
SIZE_META_MIN = 5.0

# ---------------------------------------------------------------- header
LOGO_RECT = (15.0, 15.0, 70.0, 70.0)     # x, y, w, h
TITLE_X = 105.0
TITLE_BASELINE = 35.0

#: (label_x, value_x) for each of the three header columns.
HEADER_COLUMNS = ((125.0, 210.0), (361.0, 455.0), (575.0, 639.0))
HEADER_BASELINES = (51.0, 62.0, 73.0, 84.0)

#: Room a value has before it collides with the next column. Values that do not
#: fit shrink instead of overprinting: the reference never had to, but its
#: verification labels are shorter than the ones we can produce.
HEADER_VALUE_WIDTHS = (
    HEADER_COLUMNS[1][0] - HEADER_COLUMNS[0][1],   # 151
    HEADER_COLUMNS[2][0] - HEADER_COLUMNS[1][1],   # 120
    779.0 - HEADER_COLUMNS[2][1],                  # 140
)
SIZE_HEADER_MIN = 5.5

RULE_Y = 90.0
RULE_WIDTH = 2.0

# ---------------------------------------------------------------- body flow
CONTENT_TOP_FIRST = 95.0    # below the header rule, page 1 only
CONTENT_TOP = 15.0          # continuation pages
CONTENT_BOTTOM = 588.0      # last baseline may not cross this

# ---------------------------------------------------------------- clip rows
ROW_PITCH = 80.95
THUMB_HEIGHT = 78.0
BAND_X0 = 13.0
BAND_X1 = 779.0
BAND_HEIGHT = 80.9

TEXT_X = 15.0
NAME_BASELINE_OFFSET = 8.0
#: Baselines of the metadata lines, relative to the row top. The reference
#: report used five; formats that carry camera metadata (BRAW) need two more,
#: and the row is 80.95pt tall so they fit without disturbing anything above.
META_BASELINE_OFFSETS = (19.0, 28.0, 37.0, 46.0, 55.0, 64.0, 73.0)

THUMB_X0 = 232.0
THUMB_X1 = 782.3
THUMB_COUNT = 4
THUMB_WIDTH = (THUMB_X1 - THUMB_X0) / THUMB_COUNT   # 137.575

#: Files without a video stream get a placeholder icon where the strip goes,
#: and their text block shifts right to clear it.
ICON_RECT = (15.0, 78.0, 78.0)    # x, w, h
ICON_TEXT_X = 95.95

#: Width available to metadata text before it would collide with the strip.
TEXT_COLUMN_WIDTH = THUMB_X0 - TEXT_X - 10.0

# ---------------------------------------------------------------- detail list
DETAIL_TITLE_GAP = 50.0        # from the end of the clip rows to the title baseline
DETAIL_TITLE_X = 15.0
DETAIL_PATH_X = 21.0
DETAIL_PATH_GAP = 16.0         # title baseline -> "Full Path:" baseline
DETAIL_DEST_GAP = 13.0         # each subsequent destination line
DETAIL_FIRST_ENTRY_GAP = 20.0  # last path line -> first entry baseline

DETAIL_PITCH = 17.0            # per file: one source line + one destination line
DETAIL_NAME_X = 15.0
DETAIL_SUB_X = 25.0            # indent of the "Destination N:" line
DETAIL_LINE_GAP = 8.0          # source baseline -> destination baseline
DETAIL_BAND_OFFSET = 6.0       # source baseline -> band top
DETAIL_BAND_HEIGHT = 17.0

#: Three spaces separate flowed runs on a detail line, as in the reference.
RUN_SEPARATOR = "   "

#: Detail lines never wrap. When the paths are long enough to overrun the page
#: the line shrinks, then the longest path is elided in the middle — the head
#: and tail of a path are what identify it, the middle rarely is.
SIZE_DETAIL_MIN = 4.0
DETAIL_MAX_X = BAND_X1 - 4.0

# ---------------------------------------------------------------- footer
FOOTER_BASELINE = 596.0
FOOTER_LEFT_X = 15.0
FOOTER_RIGHT_X = 702.0
