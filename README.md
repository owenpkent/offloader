# Offloader

[![CI](https://github.com/owenpkent/offloader/actions/workflows/ci.yml/badge.svg)](https://github.com/owenpkent/offloader/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![Licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)

Verified media offload for camera cards, with job reports that match the layout
of [ShotPut Pro][spp]'s `JobReport.pdf`.

Copy a card to one or more destinations, checksum every byte, and produce the
paperwork a post house expects: a PDF contact sheet with per-clip metadata, a
CSV manifest, MHL and ASC MHL manifests for re-verification downstream, and a
self-contained HTML page.

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
pip install -e .            # engine + CLI
pip install -e ".[gui]"     # and the desktop app
```

Python 3.10+. `ffmpeg` and `ffprobe` on `PATH` enable metadata and thumbnails —
without them the offload still runs and verifies, it just reports less. Verdana
(standard on Windows and macOS) makes the PDF metrically identical to the
reference; elsewhere it falls back to DejaVu Sans.

Check what was found on this machine:

```sh
offloader info
```

## Quick start

Offload a card to two destinations, verifying both off the platter:

```sh
offloader offload \
  --source E:\ \
  --dest D:\video\080426\A001 \
  --dest \\nas\archive\080426\A001 \
  --name A001 \
  --hash xxh3-64 \
  --verify full \
  --report pdf,csv,ascmhl
```

Reports land in `<first destination>/<name>_Reports/`, so the command above
writes `D:\video\080426\A001\A001_Reports\JobReport.pdf`. Manifests are written
beside *every* copy, since a manifest that lives only with the first one cannot
re-verify the second.

Then, before the card is reformatted:

```sh
offloader verify D:\video\080426\A001
```

## Commands

| Command | What it does |
| --- | --- |
| `offload` | copy and verify a source to one or more destinations |
| `verify` | re-check an offloaded tree against its manifests |
| `report` | regenerate paperwork for an existing tree, copying nothing |
| `info` | show tool and environment status |
| `gui` | launch the desktop app (also `offloader-gui`) |

### `offload` and `report`

| Flag | Meaning |
| --- | --- |
| `--source PATH` | card or folder to offload |
| `--dest PATH` | destination root; repeat for multiple copies |
| `--hash ALGO` | `xxh3-64` (default), `xxh3-128`, `xxh64`, `xxh64be`, `md5`, `sha1`, `sha256`, `c4`, `none` |
| `--verify MODE` | `source-only` (default), `full`, `none` |
| `--report FMT[,FMT]` | `pdf` (default), `csv`, `mhl`, `ascmhl`, `html` |
| `--report-dir PATH` | override the report location |
| `--thumbs N` | frames per clip, 0 to disable (default 4) |
| `--name NAME` | job name; defaults to the source folder name |
| `--logo PATH` | image for the PDF header |
| `--footer TEXT` | footer line for the PDF |
| `--exclude GLOB` | extra filename pattern to skip; repeatable |
| `--flat` | do not recreate the source folder structure |
| `--skip-existing` | skip files already present at matching size |
| `--retries N` | attempts per file on a transient read failure (default 3, 1 disables) |
| `--retry-wait SECONDS` | pause before the first retry, backing off after (default 2) |
| `--no-probe` | skip ffprobe metadata and thumbnails |
| `--quiet` | suppress progress |

Exit status is `0` on success, `1` if any file failed verification, `2` on a
usage or I/O error, `3` if a destination was refused as unsafe.

### `verify`

```sh
offloader verify PATH [--allow-cache] [--quiet]
```

`PATH` is an `.mhl` file or a folder to search for them. Re-hashes everything the
manifest lists and exits non-zero if anything is off, so a format script can gate
on it. `--allow-cache` skips the page-cache eviction — faster, and may verify
memory rather than the device.

### Verification modes

| Mode | What it does | Catches |
| --- | --- | --- |
| `none` | copy only | nothing |
| `source-only` | hashes the source as it is read and the bytes as they are written | corruption in transit |
| `full` | additionally re-reads each destination file off disk and hashes it | the above, plus bad media and lying write caches |

`full` is the honest one: it is the only mode that proves what is actually on
the destination, at the cost of reading everything twice.

## Reports

- **PDF** — the parity target. Header summary, one banded row per clip with a
  four-frame contact sheet and metadata, then a full source/destination listing
  with per-file verdicts.
- **CSV** — one row per source/destination pair, with checksums, media and
  camera metadata, and status. For spreadsheets and ingest scripts.
- **MHL** — Media Hash List 1.1, paths relative to the file's own directory so
  it travels with the media. Written per destination.
- **ASC MHL** — the format the ASC publishes and ARRI recommends. A numbered
  history in an `ascmhl/` folder with a C4-identified chain file, directory and
  root hashes, and every hash labelled `original`, `verified` or `failed`, so a
  delivery shows *where* in the chain a file stopped matching. Validated
  byte-for-byte against the reference implementation's worked example — see
  [`docs/ascmhl.md`](docs/ascmhl.md).
- **HTML** — self-contained; thumbnails inlined as data URIs, light and dark
  themes, no external requests.

## The desktop app

```sh
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
- Reads that fail for a transient reason are retried, and a file that only
  succeeded on a later attempt is reported — a card that needs retries today is
  a card to stop using.
- Destinations past Windows' 260-character limit use the extended-length path
  prefix. `offloader info` reports whether your machine needs it.

`offloader verify` catches a single flipped bit in a file whose size never
changed. Run it before erasing a card, and again on the archive months later to
catch bit rot.

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

## Documentation

| Document | What is in it |
| --- | --- |
| [`docs/data-safety.md`](docs/data-safety.md) | Threat model: what is guaranteed, what is not, and the bugs behind each guarantee |
| [`docs/report-layout.md`](docs/report-layout.md) | Every coordinate of the PDF, measured off the reference report |
| [`docs/performance.md`](docs/performance.md) | Why not robocopy, with benchmarks and the confounds that made the first run worthless |
| [`docs/braw.md`](docs/braw.md) | Blackmagic RAW container parsing, proxy pairing, and the interrupted-recording check |
| [`docs/ascmhl.md`](docs/ascmhl.md) | ASC MHL v2.0, and how it was validated against the reference implementation |

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
pytest                      # 400 tests, ~33s
pytest --fuzz               # same suite, 3000 examples per property (~2 min)
ruff check src tests
pytest --cov=offloader --cov-report=term-missing
```

400 tests at 83% line coverage. They cover formatting against the reference's
exact strings, checksum vectors and streaming equivalence, copy/verify
behaviour including simulated destination corruption, pause/resume/cancel
concurrency, retry discrimination, BRAW container parsing, ffprobe parsing,
preset and history persistence, card detection, PDF geometry read back with
PyMuPDF, the CLI, and the GUI.

The GUI tests run on Qt's offscreen platform and drive the real queue
controller — the worker thread actually copies files — so they cover the wiring
between interface and engine, not just that the modules import.

Where a format has a reference implementation, the tests are pinned to *its*
output rather than to a reading of the spec: ASC MHL manifests are diffed
against the ones `ascmitc/mhl` ships, and the BRAW parser is checked against a
real camera file when one is present.

### Property-based testing

`tests/test_fuzz.py` uses [Hypothesis][hyp] to assert invariants over generated
input rather than over a handful of fixtures. Filenames come off camera cards,
which in practice means any Unicode at all — accented takes, CJK slates, emoji
from a naming macro, and the occasional control character from a corrupt
directory entry.

The properties worth knowing about:

- Every report writer survives arbitrary filenames. The PDF must never draw
  outside the page; the CSV must keep its column count whatever commas, quotes
  or newlines a name contains; the MHL must stay parseable; the HTML's element
  set must not change with input.
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

## Roadmap

Done — engine, CLI, all five report formats, and the desktop app (Simple and
Preset modes, job queue with pause/resume/priority, drive panel with card
detection, preset colour coding and sorting, auto-naming, duplicate-offload
protection).

Remaining, toward fuller ShotPut Pro parity:

- Email/SMS notification on completion
- BRAW decoding via the Blackmagic RAW SDK, for thumbnails without a proxy
- Per-job report templates and custom branding presets
- Windows installer and code signing

## Contributing

Issues and pull requests are welcome. [`CONTRIBUTING.md`](CONTRIBUTING.md)
covers the setup, how to fake camera hardware in tests, and the one rule that
shapes everything else: **someone reformats a card because this tool said
"Verified"**, so anything touching the copy or verification path needs a test
that fails against the old code.

Found data loss or a wrong verdict? Please read [`SECURITY.md`](SECURITY.md) and
report it privately first.

- [Code of conduct](CODE_OF_CONDUCT.md)
- [Changelog](CHANGELOG.md)

## Licence

MIT. Not affiliated with or endorsed by Imagine Products, Inc.; ShotPut Pro is
their trademark. This project interoperates with the report format, it contains
none of their code or artwork.
