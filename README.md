# Offloader

[![CI](https://github.com/owenpkent/offloader/actions/workflows/ci.yml/badge.svg)](https://github.com/owenpkent/offloader/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![Licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)

Verified copy for large data transfers. Read every byte once, checksum it, fan
it out to one or more destinations, and read it back to prove what landed —
then produce the paperwork: a CSV manifest, MHL and ASC MHL manifests for
re-verification downstream, and a self-contained HTML page.

The flagship use is camera-card offload, with job reports that match the layout
of [ShotPut Pro][spp]'s `JobReport.pdf` — a PDF contact sheet with per-clip
metadata, ffprobe media details and Blackmagic RAW container checks. The same
profile covers the sound cart: a card of broadcast WAVs reports its slate,
tracks and timecode rather than a picture report with the fields blank, see
[Sound recorder cards](#sound-recorder-cards). But that media layer is a
profile, not the engine: `--profile data` (shorthand
`--generic`) offloads any large one-way transfer — datasets, disk images,
render output, backups — with the same verified copy and manifests, and nothing
depending on ffmpeg. See [Generic data transfers](#generic-data-transfers).

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

Or skip the install and run the checkout directly. `run.py` puts `src/` at the
front of the import path, so it always runs the code next to it rather than
whatever pip last installed:

```sh
python run.py                     # launch the desktop app
python run.py info                # anything the CLI takes, forwarded untouched
python run.py offload --source E:\ --dest D:\video\A001 --name A001
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
| `resolve` | report which of an edit timeline's media is already here, copying nothing |
| `control` | pause, resume or cancel a running offload from another terminal |
| `verify` | re-check an offloaded tree against its manifests |
| `report` | regenerate paperwork for an existing tree, copying nothing |
| `info` | show tool and environment status |
| `gui` | launch the desktop app (also `offloader-gui`) |
| `update` | check for a newer release, and install it (Windows) |

### `offload` and `report`

| Flag | Meaning |
| --- | --- |
| `--source PATH` | card or folder to offload |
| `--timeline PATH` | offload an edit timeline's media instead of a folder; see [From an edit timeline](#from-an-edit-timeline) |
| `--dest PATH` | destination root; repeat for multiple copies |
| `--hash ALGO` | `xxh3-64` (default), `xxh3-128`, `xxh64`, `xxh64be`, `md5`, `sha1`, `sha256`, `c4`, `none` |
| `--verify MODE` | `source-only` (default), `full`, `none` |
| `--profile P` | `media` (default: ffprobe, thumbnails, BRAW) or `data` (generic transfer, no media probing) |
| `--generic` | shorthand for `--profile data` |
| `--report FMT[,FMT]` | `pdf` (default), `csv`, `mhl`, `ascmhl`, `html` |
| `--report-dir PATH` | override the report location |
| `--thumbs N` | frames per clip, 0 to disable (default 4) |
| `--name NAME` | job name; defaults to the source folder name |
| `--logo PATH` | image for the PDF header |
| `--footer TEXT` | footer line for the PDF |
| `--exclude GLOB` | extra filename pattern to skip; repeatable |
| `--flat` | do not recreate the source folder structure; refused if two files would land on one path |
| `--skip-existing` | skip files already present at matching size |
| `--control-file PATH` | make the job pausable from another terminal; see [Pausing a running job](#pausing-a-running-job) |
| `--proxies-first` | copy the camera's proxy folders before the originals (default) |
| `--originals-first` | copy in plain tree order instead |
| `--retries N` | attempts per failing read on a transient error (default 3, 1 disables) |
| `--retry-wait SECONDS` | pause before the first retry, backing off after (default 2) |
| `--no-probe` | skip ffprobe metadata and thumbnails |
| `--quiet` | suppress progress |

Exit status is `0` on success, `1` if any file failed verification, `2` on a
usage or I/O error, `3` if a destination was refused as unsafe, `4` if a
timeline could not be read for want of an adapter.

### Pausing a running job

A long offload gets started detached, over ssh, or by a scheduler, and the
person who wants it paused is rarely sitting at that terminal. Start it with a
control file and it can be driven from anywhere:

```sh
offloader offload --source E:\ --dest D:\A001 --control-file D:\A001\job.control

# from any other terminal
offloader control D:\A001\job.control --pause
offloader control D:\A001\job.control --resume
offloader control D:\A001\job.control --cancel
offloader control D:\A001\job.control            # what state is it in?
```

The job reads the file **once per 8 MiB chunk**, so a pause takes effect inside
a second even in the middle of a 14 GB clip. Paused, it holds its place with
the file still in flight; resumed, it carries on from that chunk rather than
restarting the file. A cancel keeps every finished file and discards the one in
flight, which never had its real name.

The file holds one word: `run`, `pause` or `cancel`. Deleting it releases the
job. **Anything else is "no opinion" and leaves the job exactly as it is** —
empty, garbled, half-written, or momentarily unreadable because another process
has it open. That asymmetry is deliberate: inferring `cancel` from a damaged
control file would let a stray byte stop an offload that is nine hours in, and
a small text file is precisely what a sync client rewrites in two steps.
`offloader control` writes through a staging file and renames it into place, so
a job polling between chunks can never read a half-written instruction.

Starting a job also *claims* the path by writing `run` to it. A control file
left saying `pause` by a previous job would otherwise stop the next one before
it copied a byte, with nothing on screen to explain why.

Why a file rather than a signal or a keypress: Windows has almost no signal
support beyond SIGINT, a detached job has no console to press a key in, and a
file needs no port and no daemon, survives the terminal going away, and can be
read to see what a job is doing. The desktop app drives the same `JobControl`
through its transport buttons.

### From an edit timeline

A card offload knows its source. This is the other job: a cut comes back from
an editor, and the question is *which files does this timeline need, and are
they all here?*

```sh
pip install "offloader[timeline]"

offloader resolve --timeline "01 Chairs Row V6.xml" --search-root E:\ChairsDoc
offloader offload --timeline "01 Chairs Row V6.xml" --search-root E:\ChairsDoc \
                  --dest E:\ChairsDoc\RowV6_Media
```

`resolve` copies nothing and prints the answer. `offload` copies only the files
that are not already under a `--search-root`, with the same verified copy,
checksums and reports as a card.

| Flag | Meaning |
| --- | --- |
| `--timeline PATH` | the cut to read: `.xml` (FCP 7), `.fcpxml`, `.edl`, `.aaf`, `.otio` |
| `--search-root PATH` | where the media already lives; repeatable, and required |
| `--adapter NAME` | override the OpenTimelineIO adapter chosen from the suffix |
| `--no-proxy-substitution` | do not let a proxy on disk stand in for a camera original |
| `--layout mirror\|flat` | keep each file's path below its volume root (default), or put everything in one folder |
| `--csv PATH` | (`resolve`) write the full per-reference table |

Every reference gets one status: `present`, `substituted`, `gap`, `ambiguous`,
`missing` or `generated`. Only `gap` is copied. `resolve` exits non-zero if
anything is `ambiguous` or `missing`.

The part worth knowing before you trust it: **it refuses to choose between two
files that share a name.** On the conform it was built against, 20 basenames
had more than one copy under the search root. Seven were byte-identical and
harmless. Thirteen were different files -- eleven of them "MISSING MEDIA"
stand-in slates from an earlier pass, sitting beside the real archival footage
that arrived later. Relinking by filename picks one at random, and when it
picks a slate the clip **reports as online**. So the tool reports every
candidate, offers the closest-path match as an explicitly labelled guess, and
copies nothing.

OpenTimelineIO is trusted with media references and nothing else. It recovered
all 334 of that file's references exactly, including the clipitems that name
their file by id and carry no path; on the same file it reported 29.97 fps and
12 audio tracks for a sequence declaring 24 fps and 23 tracks. Full account and
the adapter table in [`docs/timeline.md`](docs/timeline.md).

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
- **CSV** — one row per source/destination pair, with checksums, media
  metadata, the slate from whichever department wrote it (camera or sound), and
  status. For spreadsheets and ingest scripts.
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

## Proxies first

Camera proxies move before the originals by default. A 27-clip BRAW card is
around 110 GB of original against 0.4 GB of H.264, so the proxies land in the
first few seconds of a job that runs for the better part of an hour, and an
edit can start cutting while the originals are still copying. The cost is well
under a percent of the runtime.

It also improves the contact sheet. Thumbnails for an original ffmpeg cannot
decode are borrowed from the matching proxy, and the proxy is looked for at the
destination before the source — so with the proxies already down, that read
comes off the destination disk instead of competing with the copy for the card.

**This is ordering only.** The same files are copied either way, and the report
is sorted back into tree order before it is written, so the paperwork is
identical whichever way the job ran — a contact sheet that opened with the
proxy folder and buried the clips would be a worse report bought with a faster
transfer.

```sh
offloader offload --source E:\ --dest D:\video\A001 --originals-first
```

`--originals-first` restores plain tree order, and both GUI modes have a
checkbox. Presets saved before the option existed inherit the new default.
A card with no proxy directory is unaffected.

## Sound recorder cards

The `media` profile covers production sound as well as picture. A card of
broadcast WAVs from a field recorder offloads and verifies like any other, and
the paperwork reads as a sound report rather than a picture report with the
interesting fields blank:

```sh
offloader offload --source E:\ --dest D:\audio\082226\SOUND_A --name SOUND_A
```

```
  SOUND_A: Verified
  48 files, 2.1 GB in 0:01:12  (48 audio)
```

- **The file counts stay disjoint.** A clip with dialogue is a video file, not
  both, so the two numbers still add up to something a reader can check. A card
  with picture and sound reports `(54 video, 12 audio)`.
- **The header cell adapts rather than grows.** The reference layout gives the
  summary grid exactly four rows, so on a card with no picture the `Video
  Files` cell becomes `Audio Files`. `Video Files: 0` is the one number on such
  a page that tells the reader nothing.
- **Format reads as sound.** `WAVE · 48 kHz · 24-bit · 2 ch` instead of a
  resolution and frame rate. A clip's audio line is left exactly as the
  reference renders it, so picture reports still match ShotPut digit for digit.
- **The slate comes off the iXML.** Scene, take, sound roll, the mixer's note,
  the circled-take flag and what each track was are read straight out of the
  `iXML` chunk, which ffprobe does not surface at all. The report reads
  `Roll SR082226 · Scene 12A · Take 3   CIRCLED` over
  `Boom, Lav 1   LINEAR PCM   48 kHz   24-bit`, naming the channels rather than
  describing their shape.
- **Timecode is real frame timecode when the card says enough for one.** A
  broadcast WAV stores its origin as a sample count since midnight, in `bext`
  and again in iXML. Dividing it by the sample rate gives the clock; turning
  the remainder into frames needs the rate in `SPEED/TIMECODE_RATE`, which only
  iXML carries. With iXML the report shows `10:00:00:00 NDF`. Without it,
  `10:00:00.000` -- milliseconds, because a frame count there would mean
  picking a rate at random and printing a guess in the field the report exists
  for. See [`docs/ixml.md`](docs/ixml.md).
- **No thumbnails are attempted.** A file with no picture already renders with
  the filmstrip glyph in place of the contact sheet.

The CSV gains `Audio Codec`, `Audio Channels`, `Sample Rate (Hz)`, `Bit Depth`,
`Recorder`, `Project`, `Track Names` and `Note` columns, blank for files that
carry none of it. The existing `Reel`, `Scene`, `Take` and `Good Take` columns
are filled from whichever department wrote the slate: a sound roll lands in
`Reel`, and a circled take reads as a good take, so one column means one thing
whichever cart the card came off.

## Generic data transfers

The copy engine has never been camera-specific: it streams the source once,
checksums it, writes N destinations in the same pass, evicts the page cache and
reads each copy back off the platter. Everything that made this a *camera* tool
— ffprobe metadata, contact-sheet thumbnails, the BRAW container check — sits in
a layer above it.

`--profile data` (or `--generic`) switches that layer off:

```sh
offloader offload \
  --source /mnt/instrument/run_1440 \
  --dest /archive/2026/run_1440 \
  --dest /nas/cold/run_1440 \
  --generic \
  --verify full \
  --hash sha256 \
  --report csv,ascmhl
```

Nothing is treated as a clip, ffmpeg is never invoked, and the run does not need
it installed. What you still get is the whole point of the tool: every byte
read once and fanned out, both copies verified off disk, a checksum manifest
beside each one, and `offloader verify` to re-check the archive months later for
bit rot. The PDF, CSV, MHL, ASC MHL and HTML reports all render a plain file
listing — the per-clip metadata block simply does not appear.

This is a **one-way, write-once** transfer: the same model the tool has always
assumed, now stated for any large data rather than only camera originals. It is
deliberately not a sync tool — no two-way reconciliation, conflict resolution or
partial-file updates. See [`ROADMAP.md`](ROADMAP.md).

## Compared with robocopy

robocopy moves bytes fast; Offloader proves the bytes arrived, and gives you
the paperwork to prove it again later. The overlap is "copy a tree to another
drive", but each is the wrong tool for the other's job.

What Offloader does that robocopy cannot:

- **Verification.** robocopy has no integrity checking (`/V` is verbose
  logging, not verification): it trusts the OS write path. Offloader checksums
  every byte as it is read and, with `--verify full`, evicts the page cache and
  reads each copy back off the platter. A flaky USB bridge or failing cable
  that corrupts data in transit passes robocopy and fails Offloader.
- **Manifests.** CSV, MHL and ASC MHL are written beside every copy, so anyone
  can re-verify the tree months later without the source. robocopy leaves
  nothing behind but a log.
- **One read, many destinations.** `--dest` repeats, so a slow card is read
  once and fanned out. robocopy reads the source again for every destination.
- **An answer to the real question.** "Is it safe to erase the source?"
  robocopy can only say it issued the writes.

What robocopy does that Offloader deliberately will not (these are decisions,
recorded under "Not planned" in [`ROADMAP.md`](ROADMAP.md)):

- **Mirroring and sync.** `/MIR`, deleting extras from the destination,
  incremental reconciliation. Offloader is one-way and write-once because that
  assumption is exactly what makes the "Verified" verdict meaningful, and a
  tool that can delete from a destination is the wrong shape for one whose
  verdict authorises erasing the source.
- **Metadata fidelity beyond timestamps.** NTFS ACLs, alternate data streams,
  junctions (`/COPYALL`, `/SEC`, backup mode). Camera cards have none of these.
  Replicating a server share with permissions intact is robocopy's job, and it
  does it well.

So the rule of thumb: replication or sync on trusted hardware, use robocopy. A
one-way transfer of data you cannot get back, use Offloader. (Using robocopy
*inside* Offloader as the copy loop was measured and rejected: it copies faster
but emits no checksums, so the verified workflow it implies costs two extra
passes over the data. See [`docs/performance.md`](docs/performance.md).)

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

Both carry a **Copy proxies before the originals** checkbox, on by default. It
changes only what moves first, never what is copied or how the report reads —
see [Proxies first](#proxies-first).

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

- **Duplicate offload protection.** The source's name and its file listing (names
  and sizes, never contents) are fingerprinted and checked against past offloads.
  Re-pulling a card you already have gets a warning naming the earlier job and
  when it ran. Only successful offloads count; a cancelled attempt is a reason to
  run again.
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
- Reads that fail for a transient reason are retried at the failing chunk, so
  one marginal sector costs a re-read of 8 MiB rather than of the whole clip.
  A file that only succeeded on a later attempt is reported, because a card
  that needs retries today is a card to stop using.
- Destinations past Windows' 260-character limit use the extended-length path
  prefix. `offloader info` reports whether your machine needs it.

`offloader verify` catches a single flipped bit in a file whose size never
changed. Run it before erasing a card, and again on the archive months later to
catch bit rot. It reads manifests other tools wrote, and compares digests per
algorithm: hex case-insensitively, C4 exactly. Another tool's choice of casing
is not corruption, and saying it is would be a costly thing to get wrong.

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
| [`ROADMAP.md`](ROADMAP.md) | What is next, why, and what this will not become |
| [`docs/release-plan.md`](docs/release-plan.md) | Windows beta release sequence, packaging, signing, acceptance gates, and recovery |
| [`docs/build-windows.md`](docs/build-windows.md) | Build, sign, and check Windows desktop bundles and installers |
| [`docs/data-safety.md`](docs/data-safety.md) | Threat model: what is guaranteed, what is not, and the bugs behind each guarantee |
| [`docs/report-layout.md`](docs/report-layout.md) | Every coordinate of the PDF, measured off the reference report |
| [`docs/performance.md`](docs/performance.md) | Why not robocopy, with benchmarks and the confounds that made the first run worthless |
| [`docs/braw.md`](docs/braw.md) | Blackmagic RAW container parsing, proxy pairing, and the interrupted-recording check |
| [`docs/ascmhl.md`](docs/ascmhl.md) | ASC MHL v2.0, and how it was validated against the reference implementation |
| [`docs/ixml.md`](docs/ixml.md) | Broadcast WAV chunk walking, the iXML slate, and where sound timecode comes from |
| [`docs/timeline.md`](docs/timeline.md) | Resolving an edit timeline to its media, what OpenTimelineIO is and is not trusted with, and why ambiguity refuses |

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
pytest                      # run the full suite
pytest --fuzz               # same suite, 3000 examples per property (~3 min)
ruff check src tests
pytest --cov=offloader --cov-report=term-missing
```

727 tests passed with 5 skipped and 85% line coverage on Windows/Python 3.12
in the latest local run. They cover formatting against the reference's
exact strings, checksum vectors and streaming equivalence, copy/verify
behaviour including simulated destination corruption, pause/resume/cancel
concurrency, retry discrimination, BRAW container parsing, ffprobe parsing,
preset and history persistence, card detection, PDF geometry read back with
PyMuPDF, the CLI, the GUI, and Windows installation ownership, rollback,
locking, build provenance, and signing failure handling.

The GUI tests run on Qt's offscreen platform and drive the real queue
controller — the worker thread actually copies files — so they cover the wiring
between interface and engine, not just that the modules import.

Where a format has a reference implementation, the tests are pinned to *its*
output rather than to a reading of the spec: ASC MHL manifests are diffed
against the ones `ascmitc/mhl` ships, and the BRAW parser is checked against a
real camera file when one is present.

### Property-based testing

Three modules use [Hypothesis][hyp] to assert invariants over generated input
rather than over a handful of fixtures, each aimed at a different layer:

- **`tests/test_fuzz.py`: what a filename can contain.** Names come off camera
  cards, which in practice means any Unicode at all: accented takes, CJK
  slates, emoji from a naming macro, and the occasional control character from
  a corrupt directory entry.
- **`tests/test_fuzz_edges.py`: what a card can contain.** A BRAW clip whose
  atom headers lie, a tree that maps two sources onto one destination, a
  directory junction pointing at its own parent, a retry policy someone
  hand-edited into a preset.
- **`tests/test_edge_cases.py`: the layers between the bytes and the
  paperwork.** What ffprobe hands back, what a number formats to, what a digest
  compares equal to, what a name renders to.

The properties worth knowing about:

- Every report writer survives arbitrary filenames. The PDF must never draw
  outside the page; the CSV must keep its column count whatever commas, quotes
  or newlines a name contains; the MHL must stay parseable; the HTML's element
  set must not change with input.
- Chunked hashing equals whole-buffer hashing for every algorithm at arbitrary
  chunk boundaries — the engine's boundaries fall wherever a read lands.
- `sanitize()` always returns a legal filename, and `build()` never collides
  with a name already taken, including once the numeric space is exhausted.
- Presets survive a JSON round trip, and load from arbitrary garbage without
  raising — a hand-edited or version-skewed config must not brick the app.
- No input makes an offload lose a file without saying so, no single malformed
  file aborts a job, and a parser fed hostile bytes may return nothing but may
  not raise something its caller has no reason to catch.

These have earned their keep. The first found that XML 1.0 cannot represent
most C0 control characters even as character references, so a control byte in
one filename produced an MHL no parser would read, stranding verification of
the entire delivery rather than one file. The later two found 24 more, the
worst of which certified a file it had just overwritten: flattening a tree
mapped two clips onto one destination path, and each was verified as it landed,
before the next replaced it. Both were reported `Verified`. That check now runs
against the scan, before a byte moves. Every one of those 24 has a test here
that fails against the code as it was.

[hyp]: https://hypothesis.readthedocs.io/

## Roadmap

[`ROADMAP.md`](ROADMAP.md) is prioritised by one question — does this make the
"Verified" verdict more trustworthy? — and every item on it comes from a limit
already documented in `docs/`, not from a wishlist. It also says what this
deliberately will **not** become.

Nearest up: verifying the ASC MHL directory hashes that are already written (so
a rename is a mismatch rather than a footnote), an optional second read of the
source, and grouping BRAW `.sidecar` files with their clips.

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
