# Copy performance, and why not robocopy

Short answer: **robocopy is faster at copying and useless for verifying**, and a
verified offload is one job, not two. Shelling out to it would make the product's
actual workflow slower. What robocopy does well — overlapping reads with writes —
was worth stealing, and has been.

## Measuring it honestly

The first run of this benchmark reported robocopy at **2465 MB/s off an exFAT
volume**, which is not a real number. robocopy never calls `fsync`, so its writes
landed in the page cache and the next iteration deleted them before they ever
reached the disk. It was being timed as a memcpy.

Any comparison has to end with the bytes durably on disk in *every* case. The
figures below force an `fsync` over the whole destination before stopping the
clock.

Hardware: 4.01 GB / 49 files, `D:` exFAT → `C:` NVMe, best of 3 alternating runs.
All three cases ran in one process under identical conditions, which is what
makes them comparable to each other.

| | time | throughput |
| --- | ---: | ---: |
| robocopy + fsync | 10.8 s | 372 MB/s |
| sequential read-then-write | 20.7 s | 194 MB/s |
| overlapped read-ahead | **15.9 s** | **253 MB/s** |

robocopy is roughly 1.9× faster at a plain copy. That gap is real, and worth
understanding rather than dismissing.

**Read these as a comparison, not as absolute throughput.** Re-measuring the
full engine on its own later gave 160–171 MB/s for the same work, and reported
full verification as *faster* than source-only — which is impossible, since full
verification does strictly more. Page-cache state and write-back from the
previous iteration dominate at this data size relative to 32 GB of RAM. The
table above is trustworthy for the A/B it was built to answer (the three cases
were measured against each other, alternating, in one run); any single absolute
number here is not worth quoting on its own. A dataset several times larger than
RAM would be needed for that.

## Why the gap exists

A naive loop — read a chunk, write it, read the next — never overlaps the two
operations, so it settles near the *harmonic* mean of read and write speed rather
than the slower of the two. With reads at ~500 MB/s and writes faster, that
predicts roughly what we measured.

The fix is one thread of read-ahead over a bounded queue: the read of chunk N+1
overlaps the write of chunk N. That is `READ_AHEAD` in `engine.py`, and it
recovered +30% (194 → 253 MB/s) without changing the memory profile —
`READ_AHEAD × CHUNK_SIZE`, currently 24 MiB per file, whatever the hardware does.

Two things that turned out **not** to matter, both worth knowing so nobody
re-optimises them:

- **Hashing is free.** xxh3-64 in flight cost nothing measurable (16.4 s with
  hashing versus 16.9 s without — noise). The copy is I/O bound; the CPU is idle.
- **Chunk size barely matters** between 1 MiB and 8 MiB. 32 MiB was *worse*
  (176 MB/s), so bigger is not better.

One thing that does:

- **`fsync` costs ~21%** (20.8 s versus 16.4 s). It is kept anyway. Without it
  the bytes may still be in the page cache when the job reports "Verified", and
  full verification would re-read what it just wrote instead of what landed.
  That is the difference between a verification and a formality.

## Why robocopy still loses the real job

The product does not copy files. It produces a *verified* copy with a checksum
manifest. robocopy emits no checksums, so the equivalent workflow is copy, then
read the source back to hash it, then read the destination back to hash it:

| verified workflow | passes over the data |
| --- | --- |
| robocopy, then hash source, then hash destination | 2 source reads, 1 write, 1 destination read |
| this engine, `--verify full` | 1 source read, 1 write, 1 destination read |
| this engine, `--verify source-only` | 1 source read, 1 write |

Measured end to end, robocopy + hashing both sides came to ~30 s against the
engine's 22 s for full verification and 19 s for source-only — and that was
*before* the read-ahead change. Hashing in flight is worth more than a faster
copy primitive, because it removes an entire pass over the data.

Adopting robocopy would also cost the things a shell-out cannot give back:
per-file progress granularity, pause and resume mid-file, cancelling without
leaving a partial file that looks complete, and per-destination checksums
computed from the bytes actually written.

## What robocopy was genuinely better at

Two of the three have since been adopted; both are covered in
[`data-safety.md`](data-safety.md).

- **Long paths.** It handles paths beyond `MAX_PATH` natively. *Since adopted* —
  file operations add the extended-length prefix when a path approaches the
  limit, with the caveat that the original failure could not be reproduced on a
  machine that has `LongPathsEnabled` set.
- **Retry on flaky media.** `/R` and `/W` retry a failing read, which matters
  with a marginal card or reader. *Since adopted* — `--retries` and
  `--retry-wait`, restricted to errors with a plausible transient cause, and
  retried at the failing chunk rather than the whole file, so one marginal
  sector does not cost a re-read of the clip.
- **Metadata fidelity.** ACLs, alternate data streams, junctions. Still not
  *reproduced* at the destination, which is not relevant to camera originals
  but is if anyone points this at a general file tree. A junction in the source
  is at least no longer a hazard: the scan visits each directory once, so one
  pointing back at its own parent cannot send it round in circles. That guard
  cannot lean on `Path.is_symlink()`, which is False for a junction.

## Reproducing

The benchmark scripts are not committed — they hard-code local paths and need
several GB of real media. The method, if you want to repeat it:

1. Copy to a fresh destination directory each run.
2. `fsync` every destination file before stopping the clock, in every case,
   including robocopy's. On Windows open with `O_RDWR`; `fsync` on a read-only
   descriptor fails with `EBADF`.
3. Alternate the case order between rounds and take the best of three, so
   write-back from the previous case cannot be charged to the next one.
4. Sleep a second or two between cases to let write-back drain.
