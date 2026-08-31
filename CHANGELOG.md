# Changelog

Notable changes to this project. Format follows [Keep a Changelog][kac]; this
project uses [semantic versioning][semver].

[kac]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html

## [Unreleased]

### Added

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
  the new default.

- **Transient read failures now retry at the failing chunk, not the whole
  file.** The reader reopens the source and resumes from the last chunk it
  delivered, so recovering a few bytes on a marginal card no longer costs a
  re-read of an entire clip. No hasher rewind is needed: the checksums only
  ever see chunks that were read successfully, so the running state is already
  at the resume point. Restarting the whole file remains the fallback for
  failures the chunk retry cannot reach (opening a target, a write to a
  blipping network destination, a chunk that never reads good), and a recovery
  is still reported, because a card that needs retries today is a card to stop
  using. The retry budget is per chunk, deliberately: a card with many marginal
  sectors gets its full set of attempts at each one, the way a recovery tool
  would, at a cost in time rather than integrity.

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
  case to carry information.
- **A directory junction sent the scanner round in circles.** `scan` was a bare
  `os.walk` with no cycle guard, and `Path.is_symlink()` is False for a
  junction, so the usual check would not have helped. It terminated only
  because Windows refuses paths past MAX_PATH, having by then returned the same
  file dozens of times. Directories are now visited at most once each.
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
