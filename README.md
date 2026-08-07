# Offloader

Verified media offload for camera cards, with job reports that match the layout
of [ShotPut Pro][spp]'s `JobReport.pdf`.

Copy a card to one or more destinations, checksum every byte, and produce the
paperwork a post house expects: a PDF contact sheet with per-clip metadata, a
CSV manifest, an MHL for re-verification downstream, and a self-contained HTML
page.

[spp]: https://www.imagineproducts.com/product/shotput-pro/windows

## Status

Engine, CLI, reports and the desktop interface are complete and tested.

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

## The desktop app

```sh
pip install -e ".[gui]"
offloader-gui          # or: offloader gui
```

Two modes, switched from the header:

- **Preset mode** — saved workflows, each with its own destinations, checksum,
  verification depth, reports and colour. Drop a card straight onto a preset row
  to queue it, or pick both and press **Add to queue**. Sort by name, colour or
  how often a preset gets used.
- **Simple mode** — source, destinations and options on one screen, for a
  one-off where building a preset would be more work than the job.

Down the left is the **drive panel**: every mounted volume with a capacity bar
(amber past 80 %, red past 95 %) and one-click *Source* / *Destination* buttons.
Volumes that look like camera media are badged `CARD` and sorted to the top —
detected by the marker directories cameras write (`DCIM`, `PRIVATE`, `XDROOT`
and friends) or by a root full of camera originals, since a Blackmagic card
writes clips straight to the root and a reader in a dock reports as a fixed
disk.

Along the bottom is the **queue**. Jobs run one at a time — offloads are I/O
bound, and running two at once against the same bus makes both slower and the
progress readout meaningless. Each row shows live throughput and ETA, and the
transport controls pause, resume, cancel, reprioritise, and open the reports
folder. Pause takes effect within one 8 MiB chunk; cancel deletes the partial
destination file rather than leaving something that looks complete.

Two guards run before anything is queued:

- **Duplicate offload protection.** The source's file listing — names and sizes,
  never contents — is fingerprinted and checked against past offloads. Re-pulling
  a card you already have gets a warning naming the earlier job and when it ran.
  Only successful offloads count; a cancelled attempt is a reason to run again.
- **Space and containment checks.** A destination inside the source is refused
  outright; one without room prompts before queueing.

Presets, history and settings live in `%APPDATA%\Offloader` (or
`~/.config/offloader`). A corrupt config file is treated as an empty one — it
must never stand between someone and their card.

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
ruff check src tests
```

140 tests cover formatting against the reference's exact strings, checksum
vectors and streaming equivalence, copy/verify behaviour including simulated
destination corruption, pause/resume/cancel concurrency, preset and history
persistence, card detection, PDF geometry read back with PyMuPDF, the CLI, and
the GUI. The GUI tests run on Qt's offscreen platform and drive the real queue
controller — the worker thread actually copies files — so they cover the wiring
between interface and engine, not just that the modules import.

## Roadmap

Done — engine, CLI, PDF/CSV/MHL/HTML reports, and the desktop app (Simple and
Preset modes, job queue with pause/resume/priority, drive panel with card
detection, preset colour coding and sorting, auto-naming, duplicate-offload
protection).

Remaining, toward fuller ShotPut Pro parity:

- ASC-MHL sealing alongside classic MHL
- Email/SMS notification on completion
- C4 ID checksums
- Per-job report templates and custom branding presets
- Windows installer and code signing

## Licence

MIT. Not affiliated with or endorsed by Imagine Products, Inc.; ShotPut Pro is
their trademark. This project interoperates with the report format, it contains
none of their code or artwork.
