"""Report output tests.

The PDF assertions read the generated document back with PyMuPDF and check that
text lands on the coordinates measured from the reference JobReport.pdf. That
makes layout drift a test failure rather than something you notice months later
in a delivery.
"""

from __future__ import annotations

import csv
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from offloader.models import Job
from offloader.reports import layout, write_csv, write_html, write_mhl, write_pdf

fitz = pytest.importorskip("fitz", reason="PyMuPDF needed to inspect the PDF")


def _spans(page) -> list[dict]:
    return [
        span
        for block in page.get_text("dict")["blocks"] if block["type"] == 0
        for line in block["lines"]
        for span in line["spans"]
    ]


def _find(spans: list[dict], text: str) -> dict:
    for span in spans:
        if span["text"].strip() == text.strip():
            return span
    raise AssertionError(f"no span matching {text!r}")


@pytest.fixture
def rendered(sample_job: Job, tmp_path: Path):
    path = write_pdf(sample_job, tmp_path / "JobReport.pdf")
    with fitz.open(path) as document:
        yield document


def test_page_is_landscape_letter(rendered):
    assert (round(rendered[0].rect.width), round(rendered[0].rect.height)) == (792, 612)


def test_title_lands_on_reference_coordinates(rendered):
    title = _find(_spans(rendered[0]), "A001")
    assert title["origin"] == pytest.approx((layout.TITLE_X, layout.TITLE_BASELINE), abs=0.5)
    assert title["size"] == pytest.approx(layout.SIZE_TITLE, abs=0.01)


@pytest.mark.parametrize(
    "label,column,row",
    [
        ("Final Status:", 0, 0),
        ("Verification Type:", 0, 2),
        ("Total Files:", 0, 3),
        ("Offload Start Date:", 1, 0),
        ("Video Files:", 1, 3),
        ("OS Version:", 2, 1),
        ("System Ram:", 2, 3),
    ],
)
def test_header_grid_matches_reference(rendered, label, column, row):
    span = _find(_spans(rendered[0]), label)
    assert span["origin"][0] == pytest.approx(layout.HEADER_COLUMNS[column][0], abs=0.5)
    assert span["origin"][1] == pytest.approx(layout.HEADER_BASELINES[row], abs=0.5)
    assert span["size"] == pytest.approx(layout.SIZE_HEADER, abs=0.01)


def test_header_values_never_cross_into_the_next_column(rendered):
    spans = _spans(rendered[0])
    for column, (_, value_x) in enumerate(layout.HEADER_COLUMNS[:-1]):
        limit = layout.HEADER_COLUMNS[column + 1][0]
        for span in spans:
            if (abs(span["origin"][0] - value_x) < 0.5
                    and span["origin"][1] <= layout.HEADER_BASELINES[-1]):
                assert span["bbox"][2] <= limit, f"{span['text']!r} overruns column"


def test_first_clip_row_sits_below_the_header_rule(rendered):
    name = _find(_spans(rendered[0]), "A001_C001.mov")
    expected = layout.CONTENT_TOP_FIRST + layout.NAME_BASELINE_OFFSET
    assert name["origin"] == pytest.approx((layout.TEXT_X, expected), abs=0.5)
    assert name["size"] == pytest.approx(layout.SIZE_NAME, abs=0.01)


def test_contact_sheet_fills_the_reference_cells(rendered):
    images = sorted(rendered[0].get_image_info(), key=lambda i: i["bbox"][0])
    strip = [i for i in images if i["bbox"][0] >= layout.THUMB_X0 - 0.5]
    assert len(strip) == layout.THUMB_COUNT

    for slot, image in enumerate(strip):
        x0, y0, x1, y1 = image["bbox"]
        assert x0 == pytest.approx(
            layout.THUMB_X0 + slot * layout.THUMB_WIDTH, abs=0.5)
        assert x1 - x0 == pytest.approx(layout.THUMB_WIDTH, abs=0.5)
        assert y0 == pytest.approx(layout.CONTENT_TOP_FIRST, abs=0.5)
        assert y1 - y0 == pytest.approx(layout.THUMB_HEIGHT, abs=0.5)
    assert strip[-1]["bbox"][2] == pytest.approx(layout.THUMB_X1, abs=0.5)


def test_a_file_without_thumbnails_shifts_its_text_clear_of_the_icon(rendered):
    name = _find(_spans(rendered[0]), "notes.txt")
    assert name["origin"][0] == pytest.approx(layout.ICON_TEXT_X, abs=0.5)


def test_metadata_lines_use_the_reference_leading(rendered):
    spans = _spans(rendered[0])
    checksum = _find(spans, "XXHash-64 Checksum:")
    size = _find(spans, "Size:")
    assert checksum["origin"][1] == pytest.approx(
        layout.CONTENT_TOP_FIRST + layout.META_BASELINE_OFFSETS[0], abs=0.5)
    assert size["origin"][1] == pytest.approx(
        layout.CONTENT_TOP_FIRST + layout.META_BASELINE_OFFSETS[1], abs=0.5)


def test_alternating_band_uses_the_reference_wash(rendered):
    bands = [
        d for d in rendered[0].get_drawings()
        if d["type"] == "f" and d["rect"].width > 700
    ]
    assert bands, "expected at least one banded row"
    band = bands[0]
    assert band["fill"] == pytest.approx(layout.BAND_FILL, abs=0.001)
    assert band["fill_opacity"] == pytest.approx(layout.BAND_ALPHA, abs=0.01)
    assert band["rect"].x0 == pytest.approx(layout.BAND_X0, abs=0.5)
    assert band["rect"].x1 == pytest.approx(layout.BAND_X1, abs=0.5)


def test_footer_appears_on_every_page(rendered):
    for number, page in enumerate(rendered, start=1):
        spans = _spans(page)
        page_label = _find(spans, f"Page {number}")
        assert page_label["origin"][1] == pytest.approx(layout.FOOTER_BASELINE, abs=0.5)


def test_detail_section_header_and_paths_are_present(rendered):
    text = "\n".join(page.get_text() for page in rendered)
    assert "All file details for root source: A001" in text
    assert "Full Path:" in text
    assert "Destination 1:" in text


def test_every_file_appears_in_the_detail_listing(sample_job: Job, rendered):
    text = "\n".join(page.get_text() for page in rendered)
    for entry in sample_job.files:
        assert entry.name in text
        assert str(entry.size) in text        # exact byte count, as in the reference


def test_nothing_is_drawn_outside_the_page(rendered):
    for page in rendered:
        for span in _spans(page):
            assert span["bbox"][0] >= -0.5
            assert span["bbox"][2] <= layout.PAGE_WIDTH + 0.5
            assert span["bbox"][3] <= layout.PAGE_HEIGHT + 0.5


def test_long_paths_are_elided_rather_than_overrun(sample_job: Job, tmp_path: Path):
    deep = tmp_path.joinpath(*[f"a-very-long-directory-name-{i:02d}" for i in range(12)])
    sample_job.files[0].destinations[0].path = deep / "clip.mov"
    path = write_pdf(sample_job, tmp_path / "long.pdf")
    with fitz.open(path) as document:
        for page in document:
            for span in _spans(page):
                assert span["bbox"][2] <= layout.PAGE_WIDTH + 0.5


def test_pagination_spills_to_more_pages(sample_job: Job, tmp_path: Path):
    sample_job.files = sample_job.files * 12
    path = write_pdf(sample_job, tmp_path / "many.pdf")
    with fitz.open(path) as document:
        assert document.page_count > 2
        assert f"Page {document.page_count}" in document[-1].get_text()


# ------------------------------------------------------------------ CSV


def test_csv_has_a_row_per_destination(sample_job: Job, tmp_path: Path):
    path = write_csv(sample_job, tmp_path / "JobReport.csv")
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    header = next(r for r in rows if r and r[0] == "File Name")
    body = rows[rows.index(header) + 1:]

    assert len(body) == sum(len(f.destinations) for f in sample_job.files)
    record = dict(zip(header, body[0], strict=True))
    assert record["File Name"] == "A001_C001.mov"
    assert record["Source Checksum"] == "cd4990759f33f032"
    assert record["Status"] == "Verified"
    assert record["Size (bytes)"] == "258758961"


# ------------------------------------------------------------------ MHL


def test_mhl_is_valid_and_relative(sample_job: Job, tmp_path: Path):
    out = tmp_path / "backup" / "JobReport.mhl"
    write_mhl(sample_job, out)
    root = ET.parse(out).getroot()

    assert root.tag == "hashlist"
    assert root.get("version") == "1.1"
    hashes = root.findall("hash")
    assert len(hashes) == len(sample_job.files)

    first = hashes[0]
    assert first.findtext("file") == "Clips/A001_C001.mov"   # relative to the MHL
    assert first.findtext("size") == "258758961"
    assert first.findtext("xxh64") == "cd4990759f33f032"


def test_mhl_omits_files_without_a_checksum(sample_job: Job, tmp_path: Path):
    sample_job.files[1].checksum = None
    sample_job.files[1].destinations[0].checksum = None
    root = ET.parse(write_mhl(sample_job, tmp_path / "j.mhl")).getroot()
    assert len(root.findall("hash")) == 1


# ------------------------------------------------------------------ HTML


def test_html_is_self_contained(sample_job: Job, tmp_path: Path):
    path = write_html(sample_job, tmp_path / "JobReport.html")
    document = path.read_text(encoding="utf-8")

    assert document.startswith("<!doctype html>")
    assert "All file details for root source: A001" in document
    assert "cd4990759f33f032" in document
    # No external requests: the page must render offline.
    assert "http://" not in document
    assert "https://" not in document


def test_html_escapes_hostile_filenames(sample_job: Job, tmp_path: Path):
    sample_job.files[0].source = sample_job.source_root / "<script>alert(1)</script>.mov"
    document = write_html(sample_job, tmp_path / "x.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in document
    assert "&lt;script&gt;" in document


def test_mhl_survives_a_control_character_in_a_filename(sample_job: Job, tmp_path: Path):
    """XML 1.0 has no way to represent most C0 controls, so ElementTree would
    emit a document no parser can read — stranding verification of the whole
    delivery over one bad filename. Found by property-based testing."""
    sample_job.files[0].destinations[0].path = (
        sample_job.destination_roots[0] / "clip\x1f\x00.mov"
    )
    out = write_mhl(sample_job, tmp_path / "JobReport.mhl")

    root = ET.parse(out).getroot()            # must not raise
    names = [node.findtext("file") for node in root.findall("hash")]
    assert any("�" in (name or "") for name in names)
    assert not any("\x1f" in (name or "") or "\x00" in (name or "") for name in names)


def test_mhl_preserves_ordinary_unicode(sample_job: Job, tmp_path: Path):
    sample_job.files[0].destinations[0].path = (
        sample_job.destination_roots[0] / "A001_café_日本.mov"
    )
    root = ET.parse(write_mhl(sample_job, tmp_path / "j.mhl")).getroot()
    assert any("café_日本" in (n.findtext("file") or "") for n in root.findall("hash"))
