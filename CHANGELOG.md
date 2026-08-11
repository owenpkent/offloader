# Changelog

Notable changes to this project. Format follows [Keep a Changelog][kac]; this
project uses [semantic versioning][semver].

[kac]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html

## [Unreleased]

### Added

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

### Changed

- **Transient read failures now retry at the failing chunk, not the whole
  file.** The reader reopens the source and resumes from the last chunk it
  delivered, so recovering a few bytes on a marginal card no longer costs a
  re-read of an entire clip. No hasher rewind is needed: the checksums only
  ever see chunks that were read successfully, so the running state is already
  at the resume point. Restarting the whole file remains the fallback for
  failures the chunk retry cannot reach (opening a target, a write to a
  blipping network destination, a chunk that never reads good), and a recovery
  is still reported, because a card that needs retries today is a card to stop
  using.

### Fixed

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
