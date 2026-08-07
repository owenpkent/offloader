# Changelog

Notable changes to this project. Format follows [Keep a Changelog][kac]; this
project uses [semantic versioning][semver].

[kac]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html

## [Unreleased]

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
