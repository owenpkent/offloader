# ASC MHL v2.0

The format the ASC publishes and ARRI recommends. Classic MHL 1.1 records one
flat list of checksums; ASC MHL records a **history** — a numbered series of
manifests in an `ascmhl/` folder at the root of the managed data, tied together
by a chain file that identifies each manifest by its C4 ID.

```sh
offloader offload --source E:\ --dest D:\video\A001 --report pdf,ascmhl
```

```
D:\video\A001\
  Clips\...
  ascmhl\
    0001_A001_2026-08-07_231400Z.mhl
    0002_A001_2026-08-07_231401Z.mhl
    ascmhl_chain.xml
```

## Why it is worth the extra format

Every hash carries an `action` saying what it *means*:

| action | meaning |
| --- | --- |
| `original` | the first hash for this file anywhere in the history |
| `verified` | recomputed from the current copy and matched against a previous generation |
| `failed` | recomputed and **did not** match a previous generation |

So a delivery carries the evidence of *where in the chain* a file stopped
matching, not merely that it does not match now. Re-offload the same card to the
same archive and generation 2 records `verified` throughout. Let a file change
in between and that one file is recorded `failed` while its neighbours stay
`verified`.

A `failed` hash is recorded but never reused: the spec is explicit that only
`original` and `verified` hashes may serve as a verification reference, and a
failed file is excluded from the directory hashes so a directory hash can never
certify a tree known to contain a bad file.

## Directory hashes

Each directory gets a `content` and a `structure` hash, and the root gets both
in `processinfo/roothash`.

- **content** — hash of the list of child hashes (file hashes for files, child
  content hashes for directories). Changes when any file's bytes change.
- **structure** — hash of the list of `hash(name || hash)` for each child.
  Changes when anything is renamed or moved, even if every byte is identical.

Both are built by Appendix G: sort the hashes lexicographically, write their raw
bytes into a fresh generator, digest.

`offloader verify` recomputes both from what is on disk and compares. The two
answers separate two different failures:

| content | structure | verdict | what happened |
| --- | --- | --- | --- |
| matches | matches | `ok` | — |
| matches | differs | `RENAMED` | every byte is intact; a name changed or a file moved |
| differs | differs | `CHANGED` | the bytes under this directory are not what was recorded |

This is the only check that can see a rename. Every file involved still hashes
correctly, so no file hash — and no amount of re-reading — will ever object.

Recomputing means hashing the files the manifest does *not* list, since a
renamed file is unlisted under its new name and its hash is what proves the
rename is all that happened. Files matching a recorded `ignore` pattern are left
out, exactly as the writer left them out.

That is why the writer records where the job's own paperwork went. The PDF, CSV
and thumbnails are written into the destination *after* the manifest, so they
are on disk when a verifier recomputes but were never in what it recomputes
against — and folding them in reports the tool's own output as a change to the
tree. The manifest carries the report directory as an `ignore` pattern for the
same reason it carries `ascmhl`: neither is managed data. A path rather than an
assumed name, because `--report-dir` moves it. A history written before this was
recorded is read with the conventional `*_Reports` allowed for, which is a name
and not a fact — a current manifest states its own layout.

A directory whose mismatch is already accounted for by a file that failed on its
own hash says so, rather than reporting a fresh problem for every directory
between that file and the root. A directory that gained an unexpected file is
never counted as accounted for — no file verdict can report an arrival.

## C4

The chain file identifies each manifest by its C4 ID (SMPTE ST 2114): a SHA-512
digest in base58, prefixed `c4`, always 90 characters. It is also available as a
file checksum in its own right — `--hash c4`.

## Verification

`offloader verify` reads ASC MHL histories as well as classic MHL:

```sh
offloader verify D:\video\A001
```

Only the newest generation of a history is checked. Every generation covers the
same files, so verifying all of them would hash the media once per generation
for no additional evidence.

## Validation

Implementing a standard from prose is how you end up with something that only
your own reader accepts, so this was checked against the reference
implementation's output rather than against my reading of the spec.
`ascmitc/mhl` ships a worked example (scenario 02) with known-good values. Every
one reproduces exactly:

| | |
| --- | --- |
| File hashes (xxh64) | match |
| `Clips` content / structure hashes | `4c226b42e27d7af3` / `906faa843d591a9f` |
| Root content / structure hashes | `8d02114c32e28cbe` / `f557f8ca8e5a88ef` |
| C4 of the manifest, vs the chain file | match, all 90 characters |

And the end-to-end check: rebuilding that scenario and diffing the manifest
against the one the reference project shipped produces **no differences** beyond
hostname, tool name and file modification times. That test is in
`tests/test_ascmhl.py` and skips cleanly when the reference tree is not present.

Getting there took two corrections worth recording:

- ElementTree writes its XML declaration with single quotes; the reference and
  the spec's examples use double. Now matched, so a manifest can be diffed
  against another tool's output directly.
- `directoryhash/path` carries a `lastmodificationdate` attribute, which was
  missing at first.

And one real bug the round trip caught: failed entries were being dropped from
the manifest entirely rather than recorded with `action="failed"`. Excluding
them from directory hashes is correct; excluding them from the document destroys
the evidence the format exists to carry.

## Limits

- **A renamed directory is reported as two facts, not one.** The old name reads
  as `MISSING` and its parent as `RENAMED`; nothing states that the one became
  the other. `previousPath` is what the format has for that, and it is not
  written.
- **No nested histories.** The spec allows an `ascmhl` folder further down the
  tree with its own history, and permits a parent to take a child's root hash as
  its directory hash. One history per destination root is written here.
- **No `previousPath`.** Renames are not tracked across generations; a renamed
  file reads as a new `original` plus a missing old path.
- **No flatten operation.** `process` is recorded as `transfer` for an offload;
  `in-place` and `flatten` are defined but only `transfer` is produced.
- **One hash format per manifest.** The format allows several per file; this
  writes the job's algorithm.

## Sources

- [ASC MHL specification v1.0](https://cdn.theasc.com/ASCMHL_Specification_v1.0.pdf)
- [ascmitc/mhl-specification](https://github.com/ascmitc/mhl-specification)
- [ascmitc/mhl — reference implementation and worked examples](https://github.com/ascmitc/mhl)
- [ARRI — Data Transfer and File Handling](https://www.arri.com/en/learn-help/learn-help-camera-system/pre-postproduction/data-transfer)
