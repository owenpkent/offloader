# Offloader

Verified media offload for camera cards, with job reports that match the layout
of [ShotPut Pro][spp]'s `JobReport.pdf`.

Copy a card to one or more destinations, checksum every byte, and produce the
paperwork a post house expects: a PDF contact sheet with per-clip metadata, a
CSV manifest, an MHL for re-verification downstream, and a self-contained HTML
page.

[spp]: https://www.imagineproducts.com/product/shotput-pro/windows

## Status

Milestone 1 — engine, CLI and reports — is complete and tested. The PySide6 GUI
is next; see [Roadmap](#roadmap).

The PDF is built against measurements taken from a real ShotPut Pro 2021.2.6
report, documented in [`docs/report-layout.md`](docs/report-layout.md) and
asserted in `tests/test_reports.py`. Checksums agree digit-for-digit: offloading
the same clips with `--hash xxh64` reproduces the reference report's XXHash-64
values exactly.

## Install

```sh
pip install -e .
```

Python 3.10+. `ffmpeg` and `ffprobe` on `PATH` enable metadata and thumbnails —
without them the offload still runs and verifies, it just reports less. Verdana
(standard on Windows and macOS) makes the PDF metrically identical to the
reference; elsewhere it falls back to DejaVu Sans.

Check what was found:

```sh
offloader info
```

## Use

Offload a card to two destinations, verifying both off the platter:

```sh
offloader offload \
  --source E:\ \
  --dest D:\video\080426\A001 \
  --dest \\nas\archive\080426\A001 \
  --name A001 \
  --hash xxh3-64 \
  --verify full \
  --report pdf,csv,mhl
```

Reports land in `<first destination>/<name>_Reports/`, so the command above
writes `D:\video\080426\A001\A001_Reports\JobReport.pdf`.

Regenerate paperwork for a tree that was offloaded earlier — re-hashes and
re-probes in place, copies nothing:

```sh
offloader report --source D:\video\080426\A001 --report pdf,html
```

### Options

| Flag | Meaning |
| --- | --- |
| `--source PATH` | card or folder to offload |
| `--dest PATH` | destination root; repeat for multiple copies |
| `--hash ALGO` | `xxh3-64` (default), `xxh3-128`, `xxh64`, `xxh64be`, `md5`, `sha1`, `sha256`, `none` |
| `--verify MODE` | `source-only` (default), `full`, `none` |
| `--report FMT[,FMT]` | `pdf` (default), `csv`, `mhl`, `html` |
| `--report-dir PATH` | override the report location |
| `--thumbs N` | frames per clip, 0 to disable (default 4) |
| `--name NAME` | job name; defaults to the source folder name |
| `--logo PATH` | image for the PDF header |
| `--exclude GLOB` | extra filename pattern to skip; repeatable |
| `--flat` | do not recreate the source folder structure |
| `--skip-existing` | skip files already present at matching size |
| `--no-probe` | skip ffprobe metadata and thumbnails |

Exit status is `0` on success, `1` if any file failed verification, `2` on a
usage or I/O error.

### Verification modes

| Mode | What it does | Catches |
| --- | --- | --- |
| `none` | copy only | nothing |
| `source-only` | hashes the source as it is read and the bytes as they are written | corruption in transit |
| `full` | additionally re-opens each destination file and hashes it off disk | the above, plus bad media and lying write caches |

`full` is the honest one: it is the only mode that proves what is actually on
the destination, at the cost of reading everything twice.

## Reports

- **PDF** — the parity target. Header summary, one banded row per clip with a
  four-frame contact sheet and metadata, then a full source/destination listing
  with per-file verdicts.
- **CSV** — one row per source/destination pair, with checksums, media metadata
  and status. For spreadsheets and ingest scripts.
- **MHL** — Media Hash List 1.1, paths relative to the file's own directory so
  it travels with the media. Written per destination.
- **HTML** — self-contained; thumbnails inlined as data URIs, light and dark
  themes, no external requests.

## Library

The CLI is a thin wrapper. The engine is importable:

```python
from pathlib import Path
from offloader import engine
from offloader.models import VerificationMode
from offloader.reports import write_pdf

job = engine.run(
    Path("E:/"),
    engine.OffloadOptions(
        destinations=[Path("D:/video/A001")],
        verification=VerificationMode.FULL,
        algorithm="xxh3-64",
    ),
    progress=lambda e: print(e.stage, e.file_name),
)
write_pdf(job, Path("D:/video/A001/A001_Reports/JobReport.pdf"))
```

`engine.run` returns a `Job`, which is the single input every report writer
takes. `engine.rescan` builds the same structure from an existing tree, which is
what makes the report layer testable without moving bytes.

## Development

```sh
pip install -e ".[dev]"
pytest
```

74 tests cover formatting against the reference's exact strings, checksum
vectors and streaming equivalence, copy/verify behaviour including simulated
destination corruption, PDF geometry read back with PyMuPDF, and the CLI.

## Roadmap

Milestone 1 (done) — engine, CLI, PDF/CSV/MHL/HTML reports.

Next, toward fuller ShotPut Pro parity:

- PySide6 GUI: Simple and Preset modes, job queue, drive panels
- Presets with colour coding and sorting
- Pause/resume, and offload priority sequencing
- Duplicate-offload detection ("human error protection")
- Advanced offload-identification naming schemes
- ASC-MHL sealing alongside classic MHL
- Email/SMS notification on completion
- C4 ID checksums

## Licence

MIT. Not affiliated with or endorsed by Imagine Products, Inc.; ShotPut Pro is
their trademark. This project interoperates with the report format, it contains
none of their code or artwork.
