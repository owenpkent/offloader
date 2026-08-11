# Roadmap

## How this is prioritised

One question decides the order: **does this make the "Verified" verdict more
trustworthy?**

1. **Trust** — anything that closes a gap between what the tool reports and what
   is actually on disk.
2. **Parity** — what a working DIT expects from an offload tool and would miss.
3. **Polish** — packaging, presentation, convenience.

Every item below comes from a limit already documented somewhere in `docs/`,
not from a wishlist. Where a section says "not planned", that is a decision, not
an oversight.

## Done

Engine, CLI, five report formats (PDF, CSV, MHL 1.1, ASC MHL v2.0, HTML), the
desktop app, and cross-platform CI.

The PDF matches a real ShotPut Pro report's geometry, measured from its content
streams. ASC MHL is diffed against the reference implementation's own worked
example. BRAW metadata comes out of the container because ffprobe cannot read
the format at all, and has been run over 510 real clips from two camera bodies.

Transient read failures retry at the failing chunk rather than restarting the
file: the reader resumes from the last chunk it delivered, so one marginal
sector costs a re-read of 8 MiB, not of a 79 GB clip. Whole-file restart
remains the fallback for everything the chunk retry cannot reach.

## Next

### Verify what is already written

`offloader verify` checks file hashes. ASC MHL also records **directory content
and structure hashes**, and those are written but never re-checked. The
structure hash exists precisely to catch a rename or a moved file — a change no
file hash can see, because every file is individually fine.

Today a rename shows up only as a "not in manifest" line. It should be a
structure-hash mismatch, which is a much stronger statement.

*Where:* `verify.py`, using the directory-hash code already in `ascmhl.py`.

### Read the source twice, optionally

A source read that returns wrong bytes without raising is rare, but nothing
currently catches it: the checksum is computed from what was read, so a bad read
produces a destination that faithfully matches a corrupted source and verifies
clean.

A `--paranoid` mode reading the source twice and comparing would close it, at
the cost of a second pass. Worth having as an option for irreplaceable material,
not as a default.

*Where:* `engine.py`, alongside the existing verification modes.

### `.sidecar` and companion grouping

BRAW `.sidecar` files carry colour metadata. They are copied like any other
file, but nothing links them to their clip, so a missing one is not flagged and
a report does not show them together. The proxy pairing in `companions.py`
already does the stem-matching this needs.

## Later

### BRAW

- **Start timecode.** The `tmcd` track is located; only its sample payload is
  unread. Currently reported as `00:00:00:00` derived from the frame rate.
- **Audio tracks.** The `soun` track is found and deliberately skipped. Its
  sample count is one per audio sample, which is the bug that cost a fix — the
  data is a parse away.
- **Decoding via the Blackmagic RAW SDK**, so a clip without a proxy still gets
  a contact sheet. A separate, platform-specific dependency, which is why it is
  here and not above.
- **Spanned clips** treated as one take rather than several files.

### ASC MHL

- **Nested histories** — an `ascmhl` folder further down the tree with its own
  chain, and a parent taking a child's root hash as its directory hash.
- **`previousPath`** so a rename is tracked across generations rather than
  reading as a new `original` plus a missing path.
- **The flatten operation**, consolidating a history into one manifest.
- **Several hash formats per manifest**, which the format allows.

### Operational

- **Email or SMS on completion.** ShotPut Pro has it; a DIT running a long
  offload wants to leave the cart.
- **Coordination between instances.** One app instance serialises its queue; two
  pointed at the same destination do not know about each other. A lock file in
  the destination would do it.
- **`--skip-existing` by checksum**, not size. Today it is explicitly a speed
  option and says so; a checksum variant would make it a safe one.
- **Windows installer and code signing**, so it can be handed to someone who
  does not have Python.
- **Per-job report templates and custom branding.**

## Not planned

- **A general-purpose file *sync* tool.** The `data` profile generalises the
  engine to any large *one-way* transfer — written once, never modified,
  verified once, archived — because that assumption is exactly what makes the
  "Verified" verdict meaningful, and it holds for a dataset or a disk image as
  well as a camera card. What stays out is everything that *breaks* it:
  two-way reconciliation, conflict resolution and partial-file updates. Those
  turn a copy you can prove into a merge you have to trust.
- **Metadata fidelity beyond timestamps** — ACLs, alternate data streams,
  junctions. robocopy does these well and they do not apply to camera media.
- **Replacing the copy loop with robocopy.** Measured and rejected; see
  [`docs/performance.md`](docs/performance.md). It copies faster and emits no
  checksums, so the verified workflow it implies makes two extra passes over the
  data.
- **Cryptographic tamper evidence by default.** xxHash is chosen for accidental
  corruption, which is the actual threat. `--hash sha256` is there for anyone
  who needs the other thing.

## Known limits

The honest list of what is *not* guaranteed today lives in
[`docs/data-safety.md`](docs/data-safety.md) under "What is still not
protected". The most important one will not be fixed by any item above: **read-
back verification proves the operating system is not lying, not that the drive
is not.** A controller with its own volatile cache can still serve a read from
memory. Eviction removes the OS from the path; it does not remove the hardware.
