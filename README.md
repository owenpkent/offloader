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

## Blackmagic RAW

ffprobe returns an empty document for `.braw` — not an error, nothing — so a
general-purpose tool reports a filename, a size, and a placeholder icon.
[`docs/braw.md`](docs/braw.md) covers what this one does instead:

- **Metadata straight from the container.** Camera model and firmware, lens,
  reel/scene/take, good-take flag, resolution, compression ratio and bitrate,
  colour science generation and embedded LUT — 44 keys in all. Only the `moov`
  is read, so a 28 GB clip costs the same as a 5 MB one.
- **Thumbnails from the matching proxy.** Nothing but Blackmagic's SDK decodes
  BRAW, so the contact sheet comes from the proxy the camera wrote beside it
  (matched by stem). The report says so explicitly, because frames from a proxy
  are not evidence the original decoded.
- **A structural check checksums cannot do.** A clip whose recording was
  interrupted has no `moov` atom. It copies perfectly, verifies perfectly, and
  will not play. Every `.braw` is checked during the offload and a failure
  becomes a job warning — while the card is still in your hand.

## Data safety

The tool is held to one standard: someone reformats a card because it said
"Verified". [`docs/data-safety.md`](docs/data-safety.md) is the threat model —
what is guaranteed, what is not, and two real bugs that were found and fixed
(the engine could destroy the card it was copying, and a failed copy could
destroy the good archive copy it was replacing).

The short version:

- A destination equal to, inside, or duplicating another destination is refused
  by the engine, so the CLI, GUI and library callers cannot disagree about it.
- Files are written under a `.offloader-partial` name and only moved into place
  once complete and verified. A failed or interrupted copy never damages what
  was already there and never leaves a plausible-looking filename.
- `--verify full` evicts each file from the page cache before reading it back,
  because a read straight after a write is otherwise served from memory and
  proves nothing about the device.
- Empty files, and verifications that may have been served from cache, are
  reported as warnings rather than folded into a "Verified" verdict.

Before you erase a card:

```sh
offloader verify D:\video\080426\A001
```

Re-hashes everything the MHL lists, catches a single flipped bit in a file whose
size never changed, and exits non-zero so a format script can gate on it. Run it
again on the archive months later to catch bit rot. An MHL is written beside
*each* destination, with relative paths, so every copy can be checked on its own
wherever it ends up mounted.

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
usage or I/O error, `3` if a destination was refused as unsafe.

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
pytest                      # 324 tests, ~26s
pytest --fuzz               # same suite, 3000 examples per property (~2 min)
ruff check src tests
pytest --cov=offloader --cov-report=term-missing
```

324 tests at 82% line coverage. They cover formatting against the reference's
exact strings, checksum vectors and streaming equivalence, copy/verify
behaviour including simulated destination corruption, pause/resume/cancel
concurrency, ffprobe parsing, preset and history persistence, card detection,
PDF geometry read back with PyMuPDF, the CLI, and the GUI.

The GUI tests run on Qt's offscreen platform and drive the real queue
controller — the worker thread actually copies files — so they cover the wiring
between interface and engine, not just that the modules import.

### Property-based testing

`tests/test_fuzz.py` uses [Hypothesis][hyp] to assert invariants over generated
input rather than over a handful of fixtures. Filenames come off camera cards,
which in practice means any Unicode at all — accented takes, CJK slates, emoji
from a naming macro, and the occasional control character from a corrupt
directory entry.

The properties worth knowing about:

- Every report writer survives arbitrary filenames. The PDF must never draw
  outside the page; the CSV must keep its column count whatever commas, quotes
  or newlines a name contains; the MHL must stay parseable; the HTML must never
  let input become markup.
- Chunked hashing equals whole-buffer hashing for every algorithm at arbitrary
  chunk boundaries — the engine's boundaries fall wherever a read lands.
- `sanitize()` always returns a legal filename, and `build()` never collides
  with a name already taken.
- Presets survive a JSON round trip, and load from arbitrary garbage without
  raising — a hand-edited or version-skewed config must not brick the app.

This found a real bug: XML 1.0 cannot represent most C0 control characters even
as character references, so a control byte in one filename produced an MHL that
no parser would read — stranding verification of the entire delivery, not just
that file. Names are now sanitised into the XML character range.

[hyp]: https://hypothesis.readthedocs.io/

### Copy performance

[`docs/performance.md`](docs/performance.md) covers why the engine does not
shell out to robocopy, with measurements: robocopy copies ~1.9x faster but emits
no checksums, so the verified workflow it implies makes two extra passes over
the data. Hashing in flight beats a faster copy primitive. The one thing worth
borrowing — overlapping reads with writes — is now in the engine.

## Roadmap

Done — engine, CLI, PDF/CSV/MHL/HTML reports, and the desktop app (Simple and
Preset modes, job queue with pause/resume/priority, drive panel with card
detection, preset colour coding and sorting, auto-naming, duplicate-offload
protection).

Remaining, toward fuller ShotPut Pro parity:

- Long-path support on Windows (the `\\?\` prefix) — a deep destination tree can
  still fail today, where robocopy handles it natively
- Retry on transient read errors, for marginal cards and readers
- ASC-MHL sealing alongside classic MHL
- Email/SMS notification on completion
- C4 ID checksums
- Per-job report templates and custom branding presets
- Windows installer and code signing

## Licence

MIT. Not affiliated with or endorsed by Imagine Products, Inc.; ShotPut Pro is
their trademark. This project interoperates with the report format, it contains
none of their code or artwork.
