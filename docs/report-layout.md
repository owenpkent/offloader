# PDF report layout reference

Every number here was measured out of a ShotPut Pro 2021.2.6 `JobReport.pdf` by
reading its content streams directly — span origins, fill rectangles and image
bounding boxes — rather than eyeballing a render. They live in code at
`src/offloader/reports/layout.py`, and `tests/test_reports.py` asserts the
generated document still lands on them.

Coordinates are **points, top-left origin**. ReportLab's origin is bottom-left,
so `layout.to_canvas(y)` returns `612 - y`.

## Page

| Property | Value |
| --- | --- |
| Size | 792 × 612 pt (US Letter, landscape) |
| Fonts | Verdana, Verdana-Bold (MacRomanEncoding in the reference) |

## Header (page 1 only)

| Element | Position | Type |
| --- | --- | --- |
| Logo | (15, 15), 70 × 70 | image |
| Job title | x 105, baseline 35 | 20 pt bold, `#666666` |
| Column 1 | labels x 125, values x 210 | 8 pt |
| Column 2 | labels x 361, values x 455 | 8 pt |
| Column 3 | labels x 575, values x 639 | 8 pt |
| Row baselines | 51, 62, 73, 84 | 11 pt leading |
| Rule | y 90, full width, 2 pt | `#c0c0c0` |

Labels are bold `#4c4c4c`; values are regular `#666666`.

Field order, reading down each column:

1. Final Status · Size of offload · Verification Type · Total Files
2. Offload Start Date · Offload Finish Date · Total Time · Video Files
3. *(blank)* · OS Version · Processors · System Ram

`Size of offload` carries no colon in the reference. That is reproduced.

## Clip rows

Content starts at y 95 on page 1 and y 15 on continuation pages; a row may not
extend past y 588. That yields 6 rows on page 1 and 7 thereafter, matching the
reference exactly.

| Property | Value |
| --- | --- |
| Row pitch | 80.95 |
| Band | x 13 → 779, height 80.9, on odd-indexed rows |
| Band fill | rgb(0.3333, 0.4667, 0.6914) at 15 % alpha |
| Filename | x 15, baseline `top + 8`, 8 pt bold `#4c4c4c` |
| Metadata baselines | `top +` 19, 28, 37, 46, 55 |
| Metadata type | 6 pt; labels bold `#666666`, values `#7f7f7f` |
| Contact sheet | x 232.0 → 782.3, 4 cells of 137.575 × 78 at `top` |

The metadata block holds, in order: checksum · size and created · container,
resolution, codec and frame rate · duration, timecode and frame count · audio
tracks. Lines with nothing to report are omitted rather than left blank.

Long lines shrink to fit the 217 pt column beside the strip — the reference
drops its duration/timecode line to 5.7 pt for exactly this reason.

### Files with no video stream

The contact sheet is replaced by a 78 × 78 filmstrip glyph at x 15, and the text
block shifts to x 95.95.

## Detail listing

Introduced by a section header, then two lines per file. Offsets are cumulative
from the end of the clip rows:

| Element | Offset | Type |
| --- | --- | --- |
| `All file details for root source: <name>` | +50 | 20 pt regular `#666666`, x 15 |
| `Full Path:` | +16 | 8 pt, x 21 |
| `Destination N:` | +13 each | 8 pt, x 21 |
| First entry baseline | +20 | — |

| Property | Value |
| --- | --- |
| Entry pitch | 17 (9 + 8 per destination line) |
| Band | x 13 → 779, height 17, top = baseline − 6, on even-indexed entries |
| Source line | x 15 filename 6 pt bold, remainder 5 pt |
| Destination line | x 25, 5 pt, baseline + 8 |

Both lines are **flowed**, not columnar: runs are drawn left to right separated
by three spaces, so field positions shift with content. This is why `258.8 MB`
and `100.7 MB` start at different x values in the reference.

Source-line runs: filename · checksum · size and byte count · `Source:` path ·
`Created:` · `Modified:`.
Destination-line runs: `Destination N:` path · `Status:` · `Created:` ·
`Modified:`.

### Overflow

The reference never overruns because its paths are short (`/Volumes/A001/…`).
Windows paths are not, so a detail line that would cross x 775 first shrinks
toward 4 pt, then middle-elides its longest path run. Head and tail identify a
path; the middle usually does not.

## Footer

Baseline 596 on every page: product and version at x 15, `Page N` at x 702,
6 pt regular black.

## Known deviations from the reference

| Item | Reference | Here | Why |
| --- | --- | --- | --- |
| Resolution | `1620 x 1080` | `1920 x 1080` | The source file's track header and sample aspect ratio both say 1920 × 1080. The reference value appears to be a ShotPut bug; correctness wins. |
| Footer text | `ShotPutPro Version … \| Imagine Products, Inc.` | `Offloader Version …` | Own branding. Override with `--footer`-equivalent `footer=` on `write_pdf`. |
| Logo | Imagine Products mark | generated disc | No third-party artwork in the repo. Supply your own with `--logo`. |
| File order | groups proxies before camera originals | sorted directory walk | The reference's ordering rule is not documented; a sorted walk is deterministic. |
