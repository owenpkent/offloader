# Changelog

Notable changes to this project. Format follows [Keep a Changelog][kac]; this
project uses [semantic versioning][semver].

[kac]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html

## [Unreleased]

### Changed

- **Full verification is the default.** The read-back is the only mode that
  proves what is on the destination device, and the default should be the one
  whose "Verified" means the most. Applies to the engine's options, new
  presets, Simple mode and the CLI's `--verify`; `source-only` remains one
  flag or one dropdown away for the run that is racing a deadline. The cost
  is one extra read of each copy at the destination's own read speed.
- **Jobs offloaded from a card's root are named after the volume label.** A
  root has no folder name, so the job — and every report named after it —
  was called "Offload". It is now called what the operator calls the card:
  the label, e.g. "A003". Applies to the engine, the queue's naming
  templates (`{card}`), and Simple mode's placeholder.
- **The PDF's document title carries the route and the date.** A stack of
  reports in a file manager all read "Offload Job Report"; the title is now
  "A003 Job Report — E:\ → D:\skate video — 2026-08-09".

### Fixed

- **A report directory more than one level down failed its own verification.**
  The writer records a relocated report directory as a relative path, so
  `--report-dir` two levels below the destination was recorded as
  `delivery/reports`. That matched neither the whole path nor any single
  component of `delivery/reports/JobReport.pdf`, so the verifier folded the
  report it had just written into the recomputed directory hashes and an
  unchanged delivery failed. Recorded exclusions now carry down to everything
  beneath them, which also repairs manifests already on disk. A file arriving
  anywhere else is still caught.
- **A reopen that failed spent the offload rather than an attempt.** Recovery
  ran where the retry loop invokes it outside the clause that catches `OSError`,
  so a source that did not come back on the first reopen escaped with most of
  its budget unspent, and the failure closed the whole-file retry as well. A
  reader that is briefly off the bus now costs one attempt of the chunk's own
  budget, and the chunk resumes at its own offset.
- **A cancel during a chunk's retry backoff waited the backoff out.** The wait
  between chunk attempts was one uninterruptible sleep, so on a card failing
  over a stretch a cancel or pause landed only after it. The wait is now taken
  in short slices that check for both.
- **Originals filed under a folder called `Proxy` were reported as belonging to
  another clip.** The classification read the folder name alone, so a card that
  happens to file camera originals there had every one of them treated as a
  companion of whatever shared its stem. A proxy now has to be a proxy container
  as well, which is all the proxy search ever looks for.
- **A decoder ffmpeg lacks is probed once per job, not per clip.** Extracting
  thumbnails from BRAW with a stock ffmpeg fails identically for every clip;
  each one still paid four doomed process spawns. The first clip of a suffix
  that produces no frames now marks that suffix dead for the rest of the job
  (camera proxies, being ordinary MP4/MOV, are unaffected).

- **The queue's throughput and ETA measure the last five seconds, not the life
  of the job.** The old figure was `bytes / total elapsed`, which folds the
  pre-copy card scan and every between-file probe stall into the number
  forever — a real offload read 3.5 MB/s while clips were demonstrably flying
  past, and the ETA was wrong in the same direction. The rate now comes from a
  trailing window, decays visibly during a stall instead of freezing, and
  survives the copy→verify counter reset.

- **The drive panel no longer waits on the slowest network share.** Volume
  probes run concurrently instead of serially — the refresh costs the slowest
  probe, not the sum — and local drives are delivered before network shares,
  so the card reader next to the machine never queues behind an SMB
  round-trip. While the shares are still answering, the rows from the last
  scan stay up rather than flickering out, and the Refresh button says
  "Scanning…" instead of looking like a button that does nothing.
- **A running job is visible as one.** The queue panel now carries a summary
  line — stage, current file, percent, live rate and ETA — instead of leaving
  the evidence in a thin strip of 30 px rows. The progress bar gained a
  percent label and colours that survive the row being selected (the running
  row is auto-selected, and an accent bar on the accent selection was
  invisible on exactly the row that mattered). The rate and progress columns
  are fixed-width, so updating values no longer shove the numbers being read.
  A once-a-second repaint lets the displayed rate visibly decay during a
  stall instead of freezing at its last healthy value.
- **Simple mode's form rows no longer clip.** Inputs and checkboxes declare
  the height their styling actually needs; at fractional display scales
  (125%) the computed hint fell short and every field's text was sliced at
  the bottom.

### Added

- **A tag-triggered release candidate workflow.** Pushing `v*` gates the tag
  against `src/offloader/_version.py` before spending a packaging run on it,
  builds the unsigned bundle and installer on `windows-latest`, confirms every
  artifact the release contract names exists, uploads them for inspection, and
  prepares a draft pinned to the tagged commit. It attaches no
  assets: signing needs the hardware token that only the release workstation
  has, and the release plan requires every Windows download to be signed, so
  the signed installer is uploaded separately. `contents: write` is held only
  by the drafting job, and a test asserts no job in the workflow can attach
  what it built to a release. The tag gate is `scripts/check_tag.py`, sharing
  one version grammar with the updater and the installer's Windows fields, so
  a tag that cannot be published is refused rather than producing an asset
  nothing can compare.

  Whether the draft is marked a prerelease comes from the version the gate
  validated, and is set on both the create and the refresh path, so a rerun
  corrects an existing draft rather than inheriting the first run's choice.
  The updater does not read the flag (it reads the releases collection, skips
  drafts, and takes the channel from the tag's version), but GitHub does: a
  stable release created as a prerelease never becomes the "Latest" release,
  so `/releases/latest` and the releases page keep pointing anyone who
  downloads by hand at the one before it.

  `workflow_dispatch` rehearses the checks against a tag that does not exist
  yet: the proposed tag is a version to gate, not a ref to fetch, so the run
  checks out whatever commit it was started from. Only a *pushed* tag may touch
  a release — a dispatch can be started against an existing tag, and the ref
  test alone let a rehearsal take the write token and edit the release,
  including passing `--draft` to one already published. A rerun of the
  original tag push is still a push, so the drafting step also asks whether
  the release is a draft before editing it: a draft is refreshed, and a
  release that has been published is left untouched and the step fails with
  a diagnostic, rather than being withdrawn and given candidate notes.

- **`offloader update` finds, verifies and applies a newer release.** GitHub
  Releases is the feed, so there is no manifest server and no second place a
  version is written down. Before anything runs: HTTPS with a host allowlist
  re-checked after redirects, a size ceiling held against `Content-Length` and
  again mid-stream, a SHA-256 taken while streaming and compared before
  launch, and an Authenticode check that requires a valid status, this
  project's certificate thumbprint, its publisher name, and an embedded
  `FileVersion` matching the release. That last check is what stops a rollback:
  re-serving an older, still validly signed installer under a newer asset name
  would otherwise move every install back onto a build whose faults are fixed.
  The install target is computed from the running executable rather than read
  from the uninstall registry key, which anything running as the user can
  write.

  Prereleases are ordered rather than rejected, matching the grammar
  `build/windows/versioning.py` already enforces, and a test asserts the two
  orderings agree. The feed is the releases collection rather than
  `/releases/latest`, which GitHub documents as excluding prereleases: on that
  endpoint an installed `0.1.0b1` could never see `0.1.0b2`, and a repository
  holding only the betas the candidate workflow publishes would answer with
  nothing at all. The collection is ordered by creation date rather than by
  version, so every entry is read and the greatest eligible one wins; drafts
  are skipped, since their assets are not published.

  **An install is offered what is newer on the channel it is already on.** A
  build that is itself a prerelease is testing the prereleases and takes the
  next one, and takes the stable release when it arrives, because `0.1.0b2` is
  older than `0.1.0`. An install on a stable release is offered only stable
  releases: `0.1.0b1` finding `0.1.0b2` must not also mean `1.0.0` finding
  `1.0.1b1`.

  The updater never closes a running copy: the installer's
  refusal while a transfer is in flight is the guarantee, so the command says
  so before the elevation prompt appears. See
  [`docs/updates.md`](docs/updates.md); the in-app check is still deferred.

- Windows desktop/CLI bundles and an NSIS installer, with pinned dependencies,
  embedded version metadata, signing by default, explicit unsigned CI builds,
  source/file inventories, checksums, and headless artifact checks. Installation
  uses an application lifetime lock and inventoried files for replacement,
  rollback, and uninstall; configuration/history are preserved. Real signing
  and clean-machine installation qualification remain pending.

  **A rolled-back install can be retried.** Rollback removes the files it
  promoted, and now also the directories it created to promote them into:
  recorded as they are made, pruned deepest first, and only while still empty,
  so a directory that was already there or that holds a file from somewhere
  else is left alone. Without that, a failed first install left an empty
  `_internal` behind, which the next attempt read as a nonempty unowned target
  and refused — a transient failure recovery reported as fully resolved could
  not be retried through the installer at all. The manifest write is what
  commits an update and it is atomic, so both outcomes of a failed one, the new
  inventory absent and the previous one still present, are recovered as "did
  not commit" and roll back. Treating the second as a mismatch left an
  installation no later install or uninstall could get past, since both recover
  first.
- One release version source for Python package metadata, the application,
  reports, and Windows executable metadata. CI also installs the built wheel
  outside the checkout to check its CLI and version consistency.

- **Pause, resume and cancel from the command line.** `JobControl` has existed
  since the desktop app needed transport buttons, and is checked once per 8 MiB
  chunk, but the CLI never passed one — so a job started in a terminal could
  only be killed. `--control-file PATH` wires one to a file, and
  `offloader control PATH --pause|--resume|--cancel` drives it from anywhere.
  A file rather than a signal or a keypress because Windows has almost no
  signal support, a detached job has no console to type into, and a file needs
  no port or daemon, survives the terminal closing, and can be read to see what
  a job is doing.

  The failure modes are the interesting part. The file holds one word;
  **anything else — empty, garbled, half-written, momentarily locked — is "no
  opinion" and leaves the job in the state it is already in**, because
  inferring `cancel` from a damaged control file would let a stray byte stop an
  offload that is nine hours in. `offloader control` writes through a staging
  file and renames it into place, so a job polling between chunks cannot read a
  truncated instruction. Starting a job writes `run` to the path first, so a
  stale `pause` from a previous job cannot silently stop the next one.

  "One word" means the whole value, not its first token: `cancel pending
  upload`, the kind of thing an editor or a sync client leaves behind, is
  damaged content and not an instruction to stop. Contents that are not valid
  UTF-8 are damage too — that raises a `UnicodeDecodeError` rather than an
  `OSError`, so it used to abort the checkpoint instead of being read as no
  opinion.

- **Offloading from an edit timeline.** `offloader resolve --timeline cut.xml
  --search-root E:\Media` reports which of a cut's media is already on the
  drive and which is not; `offloader offload --timeline ...` copies the
  difference, with the same verified copy, checksums and reports as a card.
  Timelines are read with OpenTimelineIO (`pip install "offloader[timeline]"`),
  covering FCP 7 XML, FCPXML, EDL, AAF and `.otio` — but trusted with media
  references only, never with structure: on the file this was measured
  against, the adapter recovered all 334 references exactly while reporting
  29.97 fps and 12 audio tracks for a sequence that declares 24 and 23. See
  [`docs/timeline.md`](docs/timeline.md).

  The resolver **refuses to choose between two files that share a name.** On
  that conform, 20 basenames had more than one copy under the search root:
  seven byte-identical and harmless, thirteen genuinely different files,
  eleven of those being "MISSING MEDIA" stand-in slates from an earlier pass
  sitting beside the real archival footage. A relink by filename picks one at
  random and the clip reports as online either way. Byte-identical duplicates
  resolve normally and are not copied again, which on that job avoided 1.4 GB
  of transfer and, more to the point, avoided manufacturing 32 filename
  collisions on a drive that had none. A camera original may be satisfied by a
  proxy already on the drive, but only where the frame counts agree — a proxy
  one frame short moves every edit point after it.

  A `file:` URL keeps its leading slash unless a drive letter follows it.
  Stripping it unconditionally turns `/Volumes/Edit/a.mov` into the *relative*
  path `Volumes/Edit/a.mov`, which still resolves by basename and so looks
  correct on Windows, while every "is it where the timeline says?" test quietly
  answers no. An editor cutting on a Mac addresses every clip that way, so it
  is the common case rather than an edge; CI on macOS and Linux is what caught
  it.

- **`OffloadOptions.selection`**, an explicit file set for `engine.run` in
  place of scanning one source root. Files may come from any number of volumes
  and each carries its own destination-relative path, so the engine infers no
  layout. A selection is held to a narrower safety rule than a card's: a card
  refuses a destination inside the source, which would wrongly refuse the
  ordinary timeline case of collecting a cut's gaps into a folder on the drive
  they were resolved against. Instead, no file being read may sit at or beneath
  somewhere being written, and a destination-relative path that is absolute or
  climbs out with `..` is refused before the job starts.

- **"Start offload" says "Add to queue" when that is what it does.** Jobs run
  one at a time; while one is running the button enqueues, and the ready line
  says the job runs after the current one.
- **Checksum pickers say what the choice costs.** MD5 sat in the same list as
  XXHash3-64 looking like an equal choice; on the copy path, where every byte
  is hashed once per stream, it is ~40x slower and can cap copy speed. The
  desktop pickers, `--hash` help and `offloader info` now carry a speed note
  per algorithm ("fastest", "~40x slower, legacy compatibility only", …).

- **A `data` profile for generic large-data transfers.** The verified copy
  engine was never camera-specific — it reads every byte once, checksums it,
  fans it out to N destinations and reads it back — but the metadata layer
  assumed camera originals. `--profile data` (or the shorthand `--generic`)
  turns that layer off: no ffprobe, no thumbnails, no BRAW check, so a dataset,
  disk image, render output or backup is copied, checksummed, verified and
  documented (CSV, MHL, ASC MHL, PDF, HTML) with nothing depending on ffmpeg.
  The default stays `media`, so the camera-card workflow is unchanged. The
  profile is a first-class field on `OffloadOptions`, `Job` and saved presets,
  and is selectable in the desktop app's Simple mode and preset editor. This is
  a one-way verified transfer, not two-way sync — see `ROADMAP.md`.
- **`--paranoid` reads every source file twice and compares.** The gap it
  closes: a read that returns wrong bytes *without raising*. The checksum is
  computed from whatever came back, so the destination faithfully matches a
  corrupted source and verifies clean at every level — file hashes, directory
  hashes, the lot. Nothing but reading twice can see it. A disagreement is
  retried rather than adjudicated, because there is no basis for deciding which
  read was the true one; a source that will not read the same twice fails the
  file and leaves nothing behind. The page cache is dropped before the second
  read, and the job says so when it could not be, since a re-read served from
  memory compares the first read against itself. Costs a full second pass, which
  is why it is opt-in.
- **Sidecars and proxies are grouped with the clip they belong to.** A
  `.sidecar` carries a BRAW's grade; delivered without its clip it is nothing,
  and a clip delivered without it has silently lost the grade. Matching is by
  stem, reusing what proxy pairing already did, and an ambiguous stem is left
  unlinked rather than guessed at. A clip that copies while a file belonging to
  it does not is now a job warning instead of two rows twenty lines apart. The
  HTML report shows them together and the CSV gains a `Companion Of` column.
  Media profile only: a companion is a file belonging to a *clip*, and under
  `--profile data` nothing is a clip, so a dataset is not told that
  `capture.xmp` belongs to `capture.h5` on the strength of a shared stem.
- **`offloader verify` now re-checks the ASC MHL directory hashes**, which were
  written from the start and never read back. A rename or a moved file leaves
  every individual file hashing exactly as recorded, so no file-level check can
  object to it; the structure hash exists precisely to catch that, and now does.
  Content matching while structure does not is reported as `RENAMED`, which is a
  much stronger statement than the "not in manifest" line it used to produce.

  Verifying this way means hashing files the manifest does not list — that is
  what proves a rename is only a rename — while honouring the manifest's own
  `ignore` patterns. Directory hashes that a failed file already accounts for
  say so rather than repeating themselves up to the root.

  A manifest now records where the job's reports went, alongside `ascmhl`. They
  are written into the destination after it, so they are on disk when a verifier
  recomputes but were never in what it recomputes against — without the pattern,
  a card that had just been copied reported its own `JobReport.pdf` as a change
  to the tree. The path is recorded rather than the conventional name, since
  `--report-dir` moves it; histories written before it was recorded are read with
  `*_Reports` allowed for.

- **The sound slate, read from the iXML chunk.** ffprobe does not surface iXML
  at all, so every field a sound report is read for was invisible: scene, take,
  sound roll, the mixer's note, the circled-take flag, and what each track was.
  `offloader.ixml` reads the RIFF chunks directly, the way `braw` reads the
  `moov` atom, for a few seeks and a few KB. The PDF now reads
  `Roll SR082226 / Scene 12A / Take 3   CIRCLED` over
  `Boom, Lav 1   LINEAR PCM   48 kHz   24-bit`, naming the channels rather than
  describing their shape, and the CSV gains `Recorder`, `Project`,
  `Track Names` and `Note` while filling the existing `Reel`, `Scene`, `Take`
  and `Good Take` columns from whichever department wrote the slate.

  It also settles the timecode. `SPEED/TIMECODE_RATE` is the frame rate the
  `bext` sample count needed and could not supply, so a slated take now renders
  as `10:00:00:00 NDF` instead of the millisecond clock. Without iXML the
  milliseconds stay, for the same reason as before: a frame count with no rate
  behind it is a guess.

  **Drop frame is a renumbering, not a label.** At `30000/1001` an hour of
  recording is 107,892 elapsed frames, and counting those at a whole 30 reads
  `00:59:56:12` — about 3.6 seconds an hour behind the clock on the wall, which
  is exactly the drift the 1000/1001 rates have and drop frame exists to hide.
  It hides it by never using the labels `00` and `01` at the top of a minute
  that is not a tenth one, per SMPTE ST 12-1 §5.2.2, so the same hour renders
  `01:00:00:00`. That conversion is applied before formatting, against the rate
  the file states rather than the rounded one: `29.97` and `30` both round to
  30 and only one of them drops frames, and renumbering a true 30 would
  introduce the error drop frame removes. A file claiming `DF` at a rate that
  has none is labelled with the numbering actually used rather than having its
  claim repeated. The start timecode is the number an assistant types into an
  edit to line sound up with picture.

  A card is untrusted input, so the walk is bounded at every step, the payload
  is capped, RF64's `ds64` sizes are honoured so a trailing chunk past 4 GB is
  still reachable, Wave64 is declined rather than misread, and a doctype or
  entity declaration is refused outright -- which closes billion-laughs and XXE
  without taking on `defusedxml`. See [`docs/ixml.md`](docs/ixml.md).

- **Sound recorder cards are a first-class offload.** The `media` profile
  already probed `.wav/.aif/.bwf`, but a card of production sound reported
  `(0 video)`, rendered a picture report with the interesting fields blank, and
  dropped the one field a sound report is read for. Now: `Job.audio_files` and
  `MediaInfo.is_audio` alongside the video count, kept disjoint so a clip with
  dialogue counts once and the numbers still add up; the summary grid's `Video
  Files` cell reads `Audio Files` on a card with no picture, borrowing the cell
  rather than growing the four-row reference layout; bit depth captured from
  ffprobe; the format line reading `48 kHz / 24-bit`; and start timecode
  recovered from the broadcast WAV's `time_reference` sample count. Where that
  count is all the file gives up, it renders as the millisecond clock
  `10:00:00.000` rather than an invented frame count -- the entry above turns it
  into real frame timecode whenever iXML supplies the rate. A clip's own
  audio line is left exactly as the reference renders it, so picture reports
  still match ShotPut digit for digit. The CSV gains `Audio Codec`,
  `Audio Channels`, `Sample Rate (Hz)` and `Bit Depth`.

- **`run.py`, a launcher that needs no install.** `python run.py` opens the
  desktop app and `python run.py <anything>` forwards to the CLI untouched,
  exit codes included. It prepends `src/` to the import path, so a fresh clone
  or a branch checked out beside an older `pip install offloader` runs the code
  you are actually looking at rather than site-packages.

### Changed

- **Proxies now copy before the originals.** Camera proxies are a rounding
  error next to the originals (a 27-clip BRAW card is ~110 GB of original
  against ~0.4 GB of H.264), so moving them first costs well under a percent of
  the job's runtime and hands the edit something to cut with minutes in, rather
  than after the last original has landed. It also improves the contact sheet:
  thumbnails for an original ffmpeg cannot decode are borrowed from the
  matching proxy, and that read now comes off the destination disk instead of
  competing with the copy for the card. This is ordering only: the same files
  are copied, and the report still reads in tree order, so the paperwork is
  byte-identical whichever way the job ran. Use `--originals-first` (or the
  "Copy proxies before the originals" checkbox in Simple mode and the preset
  editor) for the old order. Presets saved before this option existed inherit
  the new default. A proxy is a proxy container (MP4, MOV, M4V or MXF) in a
  proxy directory, the same rule the report's clip grouping uses, so a BRAW or
  R3D original filed under a folder called `Proxy` is not hoisted ahead of the
  other originals.

- **The preset editor is grouped into Preset, Copying and Reports.** Sixteen
  fields in one flat column read as a wall, and the two or three bearing on any
  given change were never next to each other. Checkboxes now sit together under
  one label instead of each taking a blank one, `Job name` is called `Job name
  template` to distinguish it from Simple mode's literal job name, and
  `Skip files already present at matching size` carries a tooltip saying what it
  does not compare.
- **A transient read failure is retried at the chunk that failed, not by
  restarting the file.** Recovering a bad sector near the end of a 79 GB clip
  used to mean re-reading all 79 GB; it now costs one 8 MiB re-read. This turned
  out not to need the hasher rewind it looked like it would: a chunk is only
  hashed once it has been delivered whole, so a failed read has produced no
  state to unwind. The source is reopened and sought back to the failed offset,
  since a reader that dropped off the bus needs its handle re-established.
  Writes still restart the whole file — a write that fails part-way leaves the
  destination at a length the copy loop does not know. Once a chunk has had
  every attempt the policy allows, the whole-file retry no longer repeats them
  against the same fault.
- **A verify report that failed only on its directory hashes says so first.**
  It used to open with the file tally — `3 checked: 3 ok` — on a report that did
  not pass, which reads as a pass to anyone scanning. That combination is now
  stated as what it is: the bytes are intact and the tree is not. Reports with
  file failures are unchanged; they already led with them.
- **Recovered reads are reported once per file, not once per chunk.** A card
  failing over a contiguous stretch produced one warning every 8 MiB, burying
  every other warning in the job. A single bad sector still names its offset
  exactly, because there the byte is the useful fact; a run of them is bounded
  by the first and the last, because there it is not.

### Fixed

Found by fuzzing the edges: `tests/test_fuzz_edges.py` for what a card can
contain, `tests/test_edge_cases.py` for the layers between the bytes and the
paperwork. Each fix has the reproduction that found it.

- **Flattening could silently destroy a file and certify it.** With
  `--flat` (and the desktop app's "Recreate the source folder structure"
  unticked), every source mapped to `destination / name`, and nothing checked
  two sources for one target. Two clips of the same name in different card
  folders left one file on disk and *both* rows reading VERIFIED, because each
  was verified as it landed, before the next overwrote it. Full verification
  passed them too. The collision is now detected against the scan, before a
  byte moves, and the job refuses with both paths named. Paths compare through
  `os.path.normcase`, so on Windows this also catches two names differing only
  in case: distinct on the case-sensitive volume they came from, one file on
  the volume they are going to.
- **One malformed clip aborted the whole offload.** `engine.run` called
  `probe()` with no guard, and `probe()` did not guard its own parsers, so a
  BRAW whose atom headers lied, or an ordinary clip whose audio stream ffprobe
  described as `"N/A"`, raised out of the middle of a job. The clips after it
  were never copied and no report was written. Metadata is now contained: the
  bytes are already copied and verified by that point, so an unreadable
  container costs its own metadata and a line in the report, not the rest of
  the card.
- **The BRAW parser trusted the file's own size fields.** `_read_timing`
  indexed and unpacked at offsets derived from an atom's *declared* size
  without checking the buffer reached that far; `_descend` recursed once per
  nested container with no depth limit, so a 16 KB file exhausted the stack;
  and `_find_moov` issued a read for whatever size the header claimed, up to
  2^64. Offsets are now bounded by each atom's real end, depth is capped, and
  the read is clamped to the bytes actually present.
- **A filename could stop the paperwork.** `write_csv` wrote to a strict UTF-8
  stream with no filtering, so a name carrying a lone surrogate (what
  `os.listdir` returns for any POSIX name that is not valid UTF-8, and what
  NTFS accepts outright) raised mid-row after the copy had succeeded. ASC MHL
  had the same gap in a worse place: control characters produced a manifest
  that would not reparse, and `read_manifest_hashes` answers a parse failure
  with an empty dict, so the file left the chain of custody with nothing to
  show for it. The XML 1.0 character filter that `reports/mhl.py` always had
  now lives in `util` and covers all three writers.
- **Verify called good footage corrupt.** Digests were compared with a plain
  case-sensitive `==`, so a manifest from a tool that emits uppercase hex
  reported every byte-identical file as a mismatch. Comparison is now per
  algorithm: case-folded for hex, exact for C4, whose base58 alphabet uses
  case to carry information. ASC MHL directory hashes are compared the same
  way.
- **A directory junction sent the scanner round in circles.** `scan` was a bare
  `os.walk` with no cycle guard, and `Path.is_symlink()` is False for a
  junction, so the usual check would not have helped. It terminated only
  because Windows refuses paths past MAX_PATH, having by then returned the same
  file dozens of times. Directories are now visited at most once each.
- **That cycle guard could hide a real directory behind a symlink to it.** It
  recorded every directory's resolved path, including directory symlinks that
  `os.walk` never enters. With `a -> z` listed before `z`, the guard marked `z`
  as seen, dropped the real `z`, and the walk skipped `a` as a link, so `z`'s
  files were never copied and the job still reported VERIFIED. Only directories
  the walk actually enters are recorded now; junctions keep the guard.
- **A corrupt history blocked offloading**, which is precisely what it exists
  not to do. `History()` mapped `from_dict` over the file with no guard, and
  `from_dict` did bare `int()`/`list()` conversions, so a hand-edited or
  half-written `history.json` raised out of the constructor. A record that will
  not parse is now dropped. `read_json` also treats a file that is not valid
  UTF-8 as unreadable rather than letting `UnicodeDecodeError`, which is a
  ValueError and not an OSError, escape.
- **Job names could collide after 999.** `naming.build`'s two exhaustion
  fallbacks returned a name without checking it was free, so two jobs would
  share one report folder: the exact outcome the deduplication exists to
  prevent. Both searches are now bounded by the number of taken names, and the
  suffix search always produces one more candidate than there are names to
  avoid.
- **Byte counts printed a mantissa of 1000** at every decade boundary
  (`999_999` rendered as `1000.0 KB`), and negative counts never promoted out
  of bytes at all, because the unit test came before the rounding and ignored
  the sign. `nan` rendered as `nan TB`.
- **A frame rate of `inf` crashed the report.** `_parse_rate` accepted `"inf"`
  and `"nan"` because `float()` does, and every fps formatter then called
  `round()` on the result, which does not. The parser now rejects a rate that
  is not finite and positive, and the formatters degrade instead of raising.
- **Smaller ones, same sweep.** `hash_file` accepted a `chunk_size` of 0 and
  returned the empty-file digest for a file that was not empty. `RetryPolicy`
  validated nothing, so a hand-edited negative delay reached `time.sleep`,
  which raises on one. `naming.render` substituted into values it had already
  placed, so a card folder genuinely named `{index}` had its name overwritten
  by the sequence number. `history.fingerprint` hashed the file listing and
  nothing else, so every empty source shared one digest. `config_file` joined
  with pathlib's `/`, which discards the left operand when the right is
  absolute.

Found by adding CI on Linux and macOS — the suite had only ever run on Windows.

- **A preset with explicit nulls loaded unusable.** `dict.get(key, default)`
  returns None when the key is present with a null value, so a hand-edited or
  version-skewed `presets.json` produced a preset whose algorithm was None,
  which crashed when the job ran. Every field now falls back on missing *or*
  null. Caught by the property tests.
- **macOS badged the boot drive as a camera card.** The system volume also
  appears as `/Volumes/Macintosh HD`, a firmlink to `/`, so the system-volume
  guard missed it by string comparison — and macOS has a `/private` directory,
  which is an AVCHD marker. Volumes are now compared resolved and deduplicated.
- **The page-cache warning repeated once per file on macOS**, which has no
  `posix_fadvise`, burying the warnings that were about actual media. Now said
  once per job.
- **BRAW timing could have been read off the audio track.** `_read_timing` took
  the first track carrying samples, which worked only because every file to hand
  listed `vide` first. A real clip also has a `soun` track whose sample count is
  one per *audio* sample — 34,242,000 for an 11-minute take — so an audio-first
  file would have reported 34 million "frames" and a duration to match. The
  video track is now chosen by handler type. Found by running the parser over
  510 real clips, 2.84 TB, from two camera bodies.

### Changed

- ASC MHL v2.0 output, C4 checksums, retry on transient read failures, and
  Windows long-path support all landed after 0.1.0 was tagged and will ship in
  the next release.

## [0.1.0] — 2026-08-07

First release. Engine, CLI, five report formats, and the desktop app.

### Added

- **Offload engine.** Reads source bytes once and fans them out to every
  destination in the same pass, hashing source and each write in flight.
  Overlapped read-ahead so reads and writes do not serialise. Cooperative
  pause, resume and cancel.
- **Three verification depths.** `none`, `source-only`, and `full`, which
  re-reads each destination off disk after evicting it from the page cache.
- **Checksums.** xxh3-64 (default), xxh3-128, xxh64, xxh64be, MD5, SHA-1,
  SHA-256, and C4 (SMPTE ST 2114).
- **Reports.** PDF laid out to match ShotPut Pro's `JobReport.pdf` — geometry
  measured from a reference document and asserted in tests — plus CSV, MHL 1.1,
  ASC MHL v2.0, and self-contained HTML.
- **`offloader verify`.** Re-checks a tree against its manifests and exits
  non-zero, so a format script can gate on it. Catches a flipped bit in a file
  whose size never changed.
- **Blackmagic RAW.** ffprobe returns nothing for `.braw`, so metadata is read
  from the container: camera, lens, reel/scene/take, compression, colour
  science, and 40 more keys, reading only the `moov`. Thumbnails come from the
  matching proxy. Every clip is checked for the missing `moov` atom that an
  interrupted recording leaves behind.
- **Desktop app** (PySide6). Preset and Simple modes, job queue with
  pause/resume/priority, drive panel with camera-card detection, preset colour
  coding, auto-naming, and duplicate-offload protection.
- **Retry on transient read failures**, restricted to errors with a plausible
  transient cause. A file that only succeeded on a later attempt is reported.
- **Long-path support** on Windows for destinations past 260 characters.

### Fixed

These were found by attacking the code rather than by reasoning about it, and
each is a regression test now.

- **The engine could destroy the card it was copying.** A destination equal to
  the source truncated each source file before reading it, then recorded the
  checksum of the resulting empty file. It ran under the strictest verification
  setting.
- **A failed copy destroyed the good copy it was replacing.** Destinations were
  truncated up front, so a read failure afterwards had already taken out the
  previous archive copy.
- **An interrupted copy left a file with the right name and the wrong length** —
  the artefact that survives both a visual check and a size-only comparison.
- **`full` verification compared memory with memory.** A read straight after a
  write is served from the page cache.
- **A control character in a filename produced an unparseable MHL**, stranding
  verification of an entire delivery over one bad name. XML 1.0 cannot represent
  most C0 controls even as character references.
- **MHL recorded absolute paths**, so a manifest broke as soon as the tree moved
  or the drive changed letter.
- **A manifest was written only beside the first copy**, leaving the second with
  nothing to re-verify itself against.
- **ASC MHL dropped `failed` entries** instead of recording them — destroying
  the evidence the format exists to carry.
- **A deadlock in the copy read-ahead**, where a full queue at end-of-file
  dropped the sentinel and the consumer blocked forever.
- **A lifetime race in the drive panel**, where a scan task's signals object
  could be collected while the pool thread still held it.

### Known limits

Stated in full under "What is still not protected" in
[`docs/data-safety.md`](docs/data-safety.md). In brief: drive and controller
caches can still defeat read-back verification; `--skip-existing` compares size
rather than checksum; concurrent instances are not coordinated; ASC MHL
directory hashes are written but not re-verified; and BRAW decoding needs
Blackmagic's SDK, so thumbnails require a proxy.

[Unreleased]: https://github.com/owenpkent/offloader/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/owenpkent/offloader/releases/tag/v0.1.0
