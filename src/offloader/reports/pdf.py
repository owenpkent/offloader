"""The PDF job report.

Reproduces the layout of ShotPut Pro's JobReport.pdf: a landscape-Letter
document with a summary header, one banded row per clip carrying a four-frame
contact sheet, and a flowed "All file details" listing of every source and
destination path with its verification verdict.

Coordinates come from `layout`, which was measured off a reference document.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen.canvas import Canvas

from .. import PRODUCT_NAME, __version__
from ..models import FileEntry, Job
from ..util import (
    channel_layout_name,
    format_elapsed,
    format_file_datetime,
    format_fps,
    format_job_datetime,
    format_size,
)
from . import fonts, icons, layout


@dataclass(frozen=True)
class Run:
    """A styled fragment of a flowed line."""

    text: str
    bold: bool = False
    color: str = layout.COLOR_META_VALUE


def _label(text: str) -> Run:
    return Run(text, bold=True, color=layout.COLOR_META_LABEL)


def _value(text: str) -> Run:
    return Run(text, bold=False, color=layout.COLOR_META_VALUE)


class PdfReport:
    """Renders one Job. Instantiate, call `build()`."""

    def __init__(
        self,
        job: Job,
        path: Path,
        *,
        logo: Path | None = None,
        footer: str | None = None,
        thumbnails: bool = True,
        detail_section: bool = True,
    ) -> None:
        self.job = job
        self.path = Path(path)
        self.logo = Path(logo) if logo else None
        self.footer = footer or f"{PRODUCT_NAME} Version {__version__}"
        self.show_thumbnails = thumbnails
        self.show_detail = detail_section

        fonts.register()
        self.canvas = Canvas(str(self.path), pagesize=layout.PAGE_SIZE)
        self.canvas.setTitle(f"{job.name} Job Report")
        self.canvas.setAuthor(PRODUCT_NAME)
        self.canvas.setSubject("Verified offload report")

        self.page_number = 1
        self.y = layout.CONTENT_TOP_FIRST   # current top-left cursor

    # ------------------------------------------------------------ text
    def _font(self, bold: bool) -> str:
        return fonts.font(layout.FONT_BOLD if bold else layout.FONT_REGULAR)

    def _width(self, runs: list[Run], size: float) -> float:
        return sum(
            pdfmetrics.stringWidth(run.text, self._font(run.bold), size) for run in runs
        )

    def _elide(self, runs: list[Run], size: float, max_width: float) -> list[Run]:
        """Middle-truncate the longest value run until the line fits.

        Labels are never touched — losing "Source:" would cost more meaning
        than losing the middle of the path that follows it.
        """
        runs = list(runs)
        for _ in range(400):
            if self._width(runs, size) <= max_width:
                break
            candidates = [
                index for index, run in enumerate(runs)
                if not run.bold and len(run.text.strip()) > 12
            ]
            if not candidates:
                break
            index = max(
                candidates,
                key=lambda i: pdfmetrics.stringWidth(
                    runs[i].text, self._font(False), size
                ),
            )
            run = runs[index]
            body = run.text.rstrip()
            trailing = run.text[len(body):]
            keep = len(body) - max(2, int(len(body) * 0.12))
            if keep < 8:
                break
            head = keep // 2
            tail = keep - head
            runs[index] = Run(f"{body[:head]}…{body[-tail:]}{trailing}",
                              run.bold, run.color)
        return runs

    def _draw_flow(self, x: float, baseline: float, runs: list[Run], size: float,
                   *, max_width: float, minimum: float) -> None:
        """Draw a flowed line, shrinking then eliding if it would overrun."""
        if self._width(runs, size) > max_width:
            while size > minimum and self._width(runs, size) > max_width:
                size -= 0.1
            size = round(size, 2)
            runs = self._elide(runs, size, max_width)
        self._draw_runs(x, baseline, runs, size)

    def _draw_runs(self, x: float, baseline: float, runs: list[Run],
                   size: float) -> float:
        """Draw fragments left to right. Returns the pen x after the last one."""
        canvas = self.canvas
        y = layout.to_canvas(baseline)
        for run in runs:
            if not run.text:
                continue
            canvas.setFont(self._font(run.bold), size)
            canvas.setFillColor(HexColor(run.color))
            canvas.drawString(x, y, run.text)
            x += pdfmetrics.stringWidth(run.text, self._font(run.bold), size)
        return x

    def _fit_text(self, text: str, size: float, max_width: float,
                  *, bold: bool = False, minimum: float = 5.0) -> float:
        """Largest size at or below `size` that keeps `text` inside its column."""
        if max_width <= 0 or not text:
            return size
        font = self._font(bold)
        while size > minimum and pdfmetrics.stringWidth(text, font, size) > max_width:
            size -= 0.1
        return round(size, 2)

    def _draw_text(self, x: float, baseline: float, text: str, size: float,
                   *, bold: bool = False, color: str = layout.COLOR_META_VALUE,
                   right: bool = False) -> None:
        canvas = self.canvas
        canvas.setFont(self._font(bold), size)
        canvas.setFillColor(HexColor(color))
        y = layout.to_canvas(baseline)
        if right:
            canvas.drawRightString(x, y, text)
        else:
            canvas.drawString(x, y, text)

    # ------------------------------------------------------------ chrome
    def _band(self, y_top: float, height: float) -> None:
        canvas = self.canvas
        canvas.saveState()
        canvas.setFillColorRGB(*layout.BAND_FILL)
        canvas.setFillAlpha(layout.BAND_ALPHA)
        canvas.rect(
            layout.BAND_X0,
            layout.to_canvas(y_top + height),
            layout.BAND_X1 - layout.BAND_X0,
            height,
            stroke=0,
            fill=1,
        )
        canvas.restoreState()

    def _draw_footer(self) -> None:
        self._draw_text(layout.FOOTER_LEFT_X, layout.FOOTER_BASELINE, self.footer,
                        layout.SIZE_FOOTER, color=layout.COLOR_FOOTER)
        self._draw_text(layout.FOOTER_RIGHT_X, layout.FOOTER_BASELINE,
                        f"Page {self.page_number}", layout.SIZE_FOOTER,
                        color=layout.COLOR_FOOTER)

    def _new_page(self) -> None:
        self._draw_footer()
        self.canvas.showPage()
        self.page_number += 1
        self.y = layout.CONTENT_TOP

    def _ensure(self, height: float) -> None:
        """Break to a new page if `height` more points would overflow."""
        if self.y + height > layout.CONTENT_BOTTOM:
            self._new_page()

    # ------------------------------------------------------------ header
    def _draw_header(self) -> None:
        job = self.job
        x, y, w, h = layout.LOGO_RECT
        if self.logo and self.logo.is_file():
            try:
                self.canvas.drawImage(
                    str(self.logo), x, layout.to_canvas(y + h), w, h,
                    preserveAspectRatio=True, anchor="c", mask="auto",
                )
            except Exception:
                icons.draw_default_logo(self.canvas, x, y, w, h)
        else:
            icons.draw_default_logo(self.canvas, x, y, w, h)

        self._draw_text(layout.TITLE_X, layout.TITLE_BASELINE, job.name,
                        layout.SIZE_TITLE, bold=True, color=layout.COLOR_TITLE)

        finished = job.finished or job.started
        columns = [
            [
                ("Final Status: ", job.final_status),
                ("Size of offload", format_size(job.total_bytes)),
                ("Verification Type: ", job.verification_label),
                ("Total Files: ", str(job.total_files)),
            ],
            [
                ("Offload Start Date: ", format_job_datetime(job.started)),
                ("Offload Finish Date: ", format_job_datetime(finished)),
                ("Total Time: ", format_elapsed(job.elapsed_sec)),
                ("Video Files: ", str(job.video_files)),
            ],
            [
                ("", ""),
                ("OS Version: ", job.os_version),
                ("Processors: ", str(job.processors) if job.processors else ""),
                ("System Ram: ", job.system_ram),
            ],
        ]

        for (label_x, value_x), rows, value_width in zip(
            layout.HEADER_COLUMNS, columns, layout.HEADER_VALUE_WIDTHS
        ):
            for baseline, (label, value) in zip(layout.HEADER_BASELINES, rows):
                if not label and not value:
                    continue
                self._draw_text(label_x, baseline, label, layout.SIZE_HEADER,
                                bold=True, color=layout.COLOR_LABEL)
                size = self._fit_text(value, layout.SIZE_HEADER, value_width - 4.0,
                                      minimum=layout.SIZE_HEADER_MIN)
                self._draw_text(value_x, baseline, value, size,
                                color=layout.COLOR_HEADER_VALUE)

        self.canvas.setStrokeColor(HexColor(layout.COLOR_RULE))
        self.canvas.setLineWidth(layout.RULE_WIDTH)
        y_rule = layout.to_canvas(layout.RULE_Y)
        self.canvas.line(0, y_rule, layout.PAGE_WIDTH, y_rule)

    # ------------------------------------------------------------ clip rows
    def _metadata_lines(self, entry: FileEntry) -> list[list[Run]]:
        """The stack of metadata lines under a filename. Lines with nothing to
        say are omitted rather than left blank."""
        media = entry.media
        lines: list[list[Run]] = []

        if entry.checksum:
            lines.append([
                _label(f"{self.job.hash_label} Checksum: "),
                _value(entry.checksum),
            ])

        lines.append([
            _label("Size: "),
            _value(format_size(entry.size) + layout.RUN_SEPARATOR),
            _label("Created: "),
            _value(format_file_datetime(entry.created)),
        ])

        format_bits = [
            bit for bit in (
                media.container,
                f"{media.width} x {media.height}" if media.is_video else None,
                media.video_codec,
                format_fps(media.fps) if media.fps else None,
            ) if bit
        ]
        if format_bits:
            runs: list[Run] = []
            for index, bit in enumerate(format_bits):
                if index:
                    runs.append(_value(layout.RUN_SEPARATOR))
                runs.append(_label(bit))
            lines.append(runs)

        timing: list[Run] = []
        if media.duration_sec:
            from ..util import format_duration
            timing += [_label("Duration: "),
                       _value(format_duration(media.duration_sec) + layout.RUN_SEPARATOR)]
        if media.timecode:
            timing += [_label("TC: "),
                       _value(media.timecode + layout.RUN_SEPARATOR)]
        if media.frame_count:
            timing += [_label("Frames: "), _value(str(media.frame_count))]
        if timing:
            lines.append(timing)

        for track in media.audio_tracks[:1]:
            name = channel_layout_name(track.channels, track.layout)
            detail = [track.codec]
            if track.bit_rate_kbps:
                detail.append(f"{track.bit_rate_kbps:.2f} kb/s")
            if track.sample_rate_hz:
                detail.append(f"{track.sample_rate_hz} hz")
            count = len(media.audio_tracks)
            lines.append([
                _label(f"{count} {name} track" + ("s" if count > 1 else "")),
                _value(layout.RUN_SEPARATOR + layout.RUN_SEPARATOR.join(detail)),
            ])

        return lines[: len(layout.META_BASELINE_OFFSETS)]

    def _draw_clip_row(self, entry: FileEntry, index: int) -> None:
        self._ensure(layout.ROW_PITCH)
        top = self.y

        if index % 2 == 1:
            self._band(top, layout.BAND_HEIGHT)

        has_thumbs = bool(entry.thumbnails) and self.show_thumbnails
        if has_thumbs:
            text_x = layout.TEXT_X
            max_width = layout.TEXT_COLUMN_WIDTH
            for slot, thumb in enumerate(entry.thumbnails[: layout.THUMB_COUNT]):
                x = layout.THUMB_X0 + slot * layout.THUMB_WIDTH
                try:
                    self.canvas.drawImage(
                        str(thumb), x, layout.to_canvas(top + layout.THUMB_HEIGHT),
                        layout.THUMB_WIDTH, layout.THUMB_HEIGHT, mask="auto",
                    )
                except Exception:
                    pass
        else:
            icon_x, icon_w, icon_h = layout.ICON_RECT
            icons.draw_filmstrip(
                self.canvas, icon_x, top, icon_w, icon_h,
                icons.format_label(entry.source.suffix),
            )
            text_x = layout.ICON_TEXT_X
            max_width = layout.BAND_X1 - text_x - 10.0

        self._draw_text(text_x, top + layout.NAME_BASELINE_OFFSET, entry.name,
                        layout.SIZE_NAME, bold=True, color=layout.COLOR_LABEL)

        for runs, offset in zip(self._metadata_lines(entry),
                                layout.META_BASELINE_OFFSETS):
            self._draw_flow(text_x, top + offset, runs, layout.SIZE_META,
                            max_width=max_width, minimum=layout.SIZE_META_MIN)

        self.y = top + layout.ROW_PITCH

    # ------------------------------------------------------------ detail list
    def _detail_lines(self, entry: FileEntry) -> tuple[list[Run], list[list[Run]]]:
        source_runs: list[Run] = [
            Run(entry.name, bold=True, color=layout.COLOR_LABEL),
        ]
        rest: list[Run] = []
        if entry.checksum:
            rest += [
                _label(f"{self.job.hash_label} Checksum: "),
                _value(entry.checksum + layout.RUN_SEPARATOR),
            ]
        rest += [
            _label(format_size(entry.size)),
            _value(f" ({entry.size} bytes)" + layout.RUN_SEPARATOR),
            _label("Source: "),
            _value(str(entry.source) + layout.RUN_SEPARATOR),
            _label("Created: "),
            _value(format_file_datetime(entry.created) + layout.RUN_SEPARATOR),
            _label("Modified: "),
            _value(format_file_datetime(entry.modified)),
        ]

        dest_lines: list[list[Run]] = []
        for number, destination in enumerate(entry.destinations, start=1):
            runs: list[Run] = [
                _label(f"Destination {number}: "),
                _value(str(destination.path) + layout.RUN_SEPARATOR),
                _label("Status: "),
                _value(destination.status.value + layout.RUN_SEPARATOR),
            ]
            if destination.created is not None:
                runs += [_label("Created: "),
                         _value(format_file_datetime(destination.created)
                                + layout.RUN_SEPARATOR)]
            if destination.modified is not None:
                runs += [_label("Modified: "),
                         _value(format_file_datetime(destination.modified))]
            if destination.error:
                runs += [_value(layout.RUN_SEPARATOR), _label("Error: "),
                         _value(destination.error)]
            dest_lines.append(runs)

        return source_runs + rest, dest_lines

    def _draw_detail_header(self) -> None:
        job = self.job
        self._ensure(layout.DETAIL_TITLE_GAP + layout.DETAIL_PATH_GAP
                     + layout.DETAIL_DEST_GAP * len(job.destination_roots)
                     + layout.DETAIL_FIRST_ENTRY_GAP + layout.DETAIL_PITCH)

        baseline = self.y + layout.DETAIL_TITLE_GAP
        self._draw_text(layout.DETAIL_TITLE_X, baseline,
                        f"All file details for root source: {job.name}",
                        layout.SIZE_TITLE, color=layout.COLOR_TITLE)

        path_width = layout.DETAIL_MAX_X - layout.DETAIL_PATH_X
        baseline += layout.DETAIL_PATH_GAP
        self._draw_flow(layout.DETAIL_PATH_X, baseline, [
            Run("Full Path: ", bold=True, color=layout.COLOR_LABEL),
            Run(str(job.source_root), color=layout.COLOR_HEADER_VALUE),
        ], layout.SIZE_HEADER, max_width=path_width, minimum=layout.SIZE_DETAIL)

        for number, root in enumerate(job.destination_roots, start=1):
            baseline += layout.DETAIL_DEST_GAP
            self._draw_flow(layout.DETAIL_PATH_X, baseline, [
                Run(f"Destination {number}: ", bold=True, color=layout.COLOR_LABEL),
                Run(str(root), color=layout.COLOR_HEADER_VALUE),
            ], layout.SIZE_HEADER, max_width=path_width, minimum=layout.SIZE_DETAIL)

        self.y = baseline + layout.DETAIL_FIRST_ENTRY_GAP - layout.DETAIL_BAND_OFFSET

    def _draw_detail_entry(self, entry: FileEntry, index: int) -> None:
        source_runs, dest_lines = self._detail_lines(entry)
        height = layout.DETAIL_LINE_GAP * max(1, len(dest_lines)) + 9.0

        self._ensure(height)
        top = self.y

        if index % 2 == 0:
            self._band(top, height)

        baseline = top + layout.DETAIL_BAND_OFFSET
        self._draw_runs(layout.DETAIL_NAME_X, baseline, source_runs[:1],
                        layout.SIZE_DETAIL_NAME)
        name_width = pdfmetrics.stringWidth(
            entry.name, self._font(True), layout.SIZE_DETAIL_NAME
        )
        gap = pdfmetrics.stringWidth(
            layout.RUN_SEPARATOR, self._font(False), layout.SIZE_DETAIL_NAME
        )
        rest_x = layout.DETAIL_NAME_X + name_width + gap
        self._draw_flow(rest_x, baseline, source_runs[1:], layout.SIZE_DETAIL,
                        max_width=layout.DETAIL_MAX_X - rest_x,
                        minimum=layout.SIZE_DETAIL_MIN)

        for offset, runs in enumerate(dest_lines, start=1):
            self._draw_flow(layout.DETAIL_SUB_X,
                            baseline + layout.DETAIL_LINE_GAP * offset,
                            runs, layout.SIZE_DETAIL,
                            max_width=layout.DETAIL_MAX_X - layout.DETAIL_SUB_X,
                            minimum=layout.SIZE_DETAIL_MIN)

        self.y = top + height

    # ------------------------------------------------------------ build
    def build(self) -> Path:
        self._draw_header()

        for index, entry in enumerate(self.job.files):
            self._draw_clip_row(entry, index)

        if self.show_detail and self.job.files:
            self._draw_detail_header()
            for index, entry in enumerate(self.job.files):
                self._draw_detail_entry(entry, index)

        self._draw_footer()
        self.canvas.save()
        return self.path


def write_pdf(job: Job, path: Path, **options) -> Path:
    """Render `job` to a PDF at `path`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return PdfReport(
        job,
        path,
        logo=options.get("logo"),
        footer=options.get("footer"),
        thumbnails=options.get("thumbnails", True),
        detail_section=options.get("detail_section", True),
    ).build()
