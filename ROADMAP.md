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

`offloader verify` re-checks the ASC MHL directory content and structure hashes,
not only the file hashes — so a rename or a moved file, which every file hash
agrees is fine, is reported as the structure-hash mismatch it is. See
[`docs/ascmhl.md`](docs/ascmhl.md#directory-hashes).

`--paranoid` reads every source file a second time and compares, which is the
only thing that catches a read returning wrong bytes without reporting an error.
Retry works at the chunk that failed rather than restarting the file. Sidecars
and proxies are grouped with the clip they belong to, so a clip separated from
its grade is a warning rather than two unrelated rows.

## Next

### `--skip-existing` by checksum, not size

Today it is explicitly a speed option and says so: a destination file of the
right length is assumed to be the right file. That is the one place the tool
takes something on trust, and it is listed under "What is still not protected"
in [`docs/data-safety.md`](docs/data-safety.md) for that reason. A checksum
variant would make it a safe option rather than a fast one.

*Where:* `engine.py`, in the `skip_existing` branch.

### `previousPath`, so a rename survives a generation

Verifying directory hashes made a rename visible; it did not make it
*explicable*. A renamed directory reports as one `MISSING` line and one
`RENAMED` parent, with nothing saying the first became the second. The format
has `previousPath` for exactly this and it is not written.

*Where:* `ascmhl.py`, and `verify.py` to read it back.

### Coordination between instances

One app instance serialises its queue. Two pointed at the same destination do
not know about each other, which is a documented gap in
[`docs/data-safety.md`](docs/data-safety.md). A lock file in the destination
would close it.

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
- **The flatten operation**, consolidating a history into one manifest.
- **Several hash formats per manifest**, which the format allows.

### Operational

- **Email or SMS on completion.** ShotPut Pro has it; a DIT running a long
  offload wants to leave the cart.
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
