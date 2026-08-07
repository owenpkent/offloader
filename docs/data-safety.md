# Not losing footage

The standard this tool is held to: **someone reformats a card because it said
"Verified".** Everything below follows from that. A tool that merely copies
files can be wrong and waste an hour. A tool that certifies copies can be wrong
and destroy the only take.

ARRI puts the failure mode plainly — the way productions lose data is that
"missing or corrupt files weren't noticed before the original recording media
was erased."

## Two bugs that were really there

Both were found by attacking this codebase rather than by reasoning about it,
and both are now regression tests in `tests/test_data_safety.py`.

### 1. The tool destroyed the card it was copying

Pointing a destination at the source — `--source E:\ --dest E:\ --flat`, or a
mis-clicked folder — produced this:

```
source clip before: 110,000 bytes
source clip after :       0 bytes
checksum recorded : 2d06800538d394c2      <- xxh3-64 of nothing at all
```

The copy opened every destination with `"wb"`, which truncates. When the
destination *was* the source, the original was gone before a single byte had
been read. The reader then read an empty file, and the empty-file checksum was
dutifully recorded as the clip's.

It ran under `--verify full`, the strictest setting available.

**Fix.** `engine.assert_safe_destinations()` refuses a destination equal to or
inside the source, and refuses two destinations that resolve to the same
directory. `_copy_fanout` independently refuses to write to a path that is its
own source. The check lives in the engine, not the interface, so the CLI, the
GUI and library callers all get it — previously only the GUI checked, and the
CLI had nothing.

### 2. A failed copy destroyed the good copy it was replacing

Re-running an offload over an existing archive, with the card failing partway:

```
existing archive copy: 66,000 bytes
after failed copy    :      0 bytes
job reports          : Failed
```

The destination was truncated up front, so by the time the read failed, the
previous good copy was already gone. The job correctly reported failure — after
destroying the data.

**Fix.** Files are written to `<name>.offloader-partial`, verified there, and
only then moved into place with `os.replace` (atomic within a filesystem). A
failed, cancelled or interrupted copy deletes its partial and never touches what
was already there.

This closes a second hazard too: an interrupted copy used to leave a file with
the *right name* and the wrong length. That is the most dangerous artefact an
offload tool can produce — it survives a visual check, and it survives a
size-only comparison on the next run.

## What "verified" actually means

Three modes, and it is worth being precise about what each proves.

| Mode | Reads | Proves |
| --- | --- | --- |
| `none` | source once | nothing |
| `source-only` | source once | the bytes written matched the bytes read |
| `full` | source once, destination again | the bytes **on the destination** match the source |

`source-only` hashes the source as it is read and hashes each buffer as it is
handed to `write()`. It catches corruption in transit. It cannot catch anything
that happens after the write call returns.

### The page-cache problem

`full` mode re-reads the destination — but a file read straight after being
written is normally served from the page cache. That compares memory with
memory and proves nothing about the device. Microsoft's own guidance is explicit:
data read back "may end up being read out of the disk cache, in which case
you're not actually verifying physical media."

So the read-back is now preceded by `integrity.evict_from_cache()`, which opens
the file briefly with `FILE_FLAG_NO_BUFFERING` on Windows (whose documented side
effect is evicting that file) or `posix_fadvise(DONTNEED)` elsewhere. If the
eviction fails, the job records a warning rather than quietly claiming a
guarantee it did not deliver.

**Honest limit:** this removes the operating system from the path. A drive or
RAID controller with its own volatile cache can still serve the read from there.
Eviction is a large improvement, not a proof of what is on the platter.

### Checksums

xxHash is not cryptographic. That is the right trade here — the threat is
accidental corruption, not a forger — and it is fast enough to be free next to
the I/O. If the requirement is tamper evidence rather than integrity, use
`--hash sha256`.

## Re-verifying later

An offload verifies at the moment it happens. Bit rot, a failing drive, and a
bad cable on the way to the archive all happen afterwards. That is what the MHL
manifest is for, and why `offloader verify` exists:

```sh
offloader verify D:\video\080426\A001
```

It re-hashes every file the manifest lists, compares, and exits non-zero if
anything is off — designed to be the gate a format script checks:

```
  3 checked: 1 mismatch, 1 missing, 1 ok
  MISMATCH  ...\Clips\A001_C001.mov
            expected 41e3b7200d21e67c
            actual   ea585f5aa8bb784c
  MISSING   ...\Clips\A001_C002.mov

NOT VERIFIED — do not erase the source
```

A single flipped bit, with the file size unchanged, is caught. Nothing but a
checksum finds that.

It also reports files present on disk that the manifest does not list, and it
evicts each file before reading so a freshly written tree is read off the device.

### The manifest has to travel

An MHL that records absolute paths is useless the moment the drive gets a
different letter or the tree moves. This was broken here: reports live in
`<name>_Reports/` while the media sits one level up, so `Path.relative_to`
failed and silently fell back to absolute paths. Now `os.path.relpath` is used,
so entries look like `../Clips/A001_C001.mov` and the manifest works wherever the
tree is mounted.

A manifest is also written **per destination**. One that lives only with the
first copy cannot re-verify the second — and the second copy is the one that
exists precisely so it can be checked independently.

## Other things that are guarded

| Hazard | Handling |
| --- | --- |
| Empty (0-byte) source file | Warned. It hashes and verifies perfectly, and is still a lost take. |
| Destination without room | Checked before queueing; prompts. |
| Re-offloading a card already pulled | Fingerprinted by name and size, warned with the earlier job's name and date. Only successful offloads count. |
| Card pulled mid-copy | `OSError` fails that file; the partial is deleted; other files are unaffected. |
| Quit during an offload | Confirmed, then cancelled cleanly, partials removed. |
| Cancel | Deletes the in-flight partial rather than leaving plausible-looking media. |
| Report writer crashing | Caught; a bad PDF never invalidates a good copy. |
| Corrupt config file | Treated as empty. Config must never stand between someone and their card. |

## What is still not protected

Stated plainly, because a list of guarantees is only useful if its edges are
known.

- **Controller and drive caches.** As above: `full` verification proves the
  operating system is not lying. It cannot prove the drive is not.
- **Long paths on Windows.** Paths beyond `MAX_PATH` are not yet handled with
  the `\\?\` prefix, so a very deep destination tree can fail. It fails loudly
  rather than silently, but it fails.
- **No retry on transient read errors.** A marginal card or reader that would
  succeed on a second attempt currently fails the file. robocopy's `/R` is the
  model to copy here.
- **Source read errors that return garbage instead of raising.** Very rare, and
  only a second independent read of the source would catch it. Not implemented.
- **`skip_existing` compares size, not checksum.** It is a speed option, not a
  safety one, and should not be used on a tree whose integrity is in question.
- **Concurrent instances.** One app instance serialises its queue. Two instances
  pointed at the same destination are not coordinated.

## Working practice

The tool cannot enforce this, but it is what the tool is built to support:

1. Offload to **two** destinations, in one pass, with `--verify full`.
2. Write an MHL with the media (`--report pdf,mhl`).
3. Run `offloader verify` against each destination **before** reformatting.
4. Keep the MHLs. Re-run `offloader verify` on the archive periodically.

Do not erase the card until step 3 has passed on more than one copy.

## Sources

- [ARRI — Data Transfer and File Handling](https://www.arri.com/en/learn-help/learn-help-camera-system/pre-postproduction/data-transfer)
- [ASC Media Hash List specification](https://cdn.theasc.com/ASCMHL_Specification_v1.0.pdf)
- [MHL: Media Hash List](https://mediahashlist.org/mhl-specification/)
- [Does CopyFile verify that the data reached its final destination? — The Old New Thing](https://devblogs.microsoft.com/oldnewthing/20120919-00/?p=6563)
- [FILE_FLAG_NO_BUFFERING and FILE_FLAG_WRITE_THROUGH — The Old New Thing](https://devblogs.microsoft.com/oldnewthing/20210729-00/?p=105494)
- [Media Workflow Tips: Offloading and Verification — Glyph](https://glyphtech.com/a/blog/media-workflow-tips-offloading-and-verification)
