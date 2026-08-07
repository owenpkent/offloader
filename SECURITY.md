# Security policy

## What counts as a vulnerability here

This is a data-integrity tool, so the threat model is unusual. The most serious
class of bug is not remote code execution — it is **a wrong verdict**.

Please report privately:

- **Data destruction.** Anything that damages, truncates or deletes a source
  file, or a good file already at a destination.
- **A false "Verified".** Any way to make the tool certify a copy that does not
  match its source, or to make `offloader verify` pass on a tree that has been
  altered.
- **Silent omission.** A file that is copied but missing from the manifest, or a
  failure that produces no warning — the operator's decision to reformat a card
  rests on the report being complete.
- **Manifest forgery.** Producing an MHL or ASC MHL that another tool accepts as
  describing content it does not describe.
- Ordinary security issues: path traversal from a crafted filename, code
  execution from parsing a media file or a config, and so on.

## What is not a vulnerability

- **xxHash is not cryptographic.** That is deliberate and documented. The threat
  model is accidental corruption — bad media, bad cables, bit rot — not an
  adversary constructing a collision. If you need tamper evidence rather than
  integrity, use `--hash sha256`. A demonstrated xxHash collision is interesting
  but is not a bug in this tool.
- **`full` verification cannot prove what is on the platter.** It evicts the
  operating system's page cache before reading back, which removes the OS from
  the path, but a drive or RAID controller with its own volatile cache can still
  serve the read. This limit is stated in
  [`docs/data-safety.md`](docs/data-safety.md).
- **`--skip-existing` compares size, not checksum.** It is a speed option, not a
  safety one, and says so.
- Known gaps already listed under "What is still not protected" in
  [`docs/data-safety.md`](docs/data-safety.md).

## How to report

Email **Owenpkent@gmail.com** with `offloader security` in the subject, or use
GitHub's [private vulnerability reporting][pvr] on this repository.

Please include:

- What you expected the tool to report, and what it reported instead
- The output of `offloader info`
- A reproduction — synthetic files are ideal, and the fixtures in
  `tests/test_data_safety.py` show how to fake failing hardware without any

Expect an acknowledgement within a week. This is a personal project, not a
funded one, so please set expectations accordingly — but data-loss reports get
looked at first, before anything else in the queue.

[pvr]: https://github.com/owenpkent/offloader/security/advisories/new

## Disclosure

Report privately first for anything in the first list. Once a fix is released,
credit goes in the changelog unless you would rather it did not.

## Supported versions

Pre-1.0: only the latest release on `main` is supported. There is no backporting.
