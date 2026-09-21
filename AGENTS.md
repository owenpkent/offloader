# Offloader agent guide

Offloader is a Python verified-copy tool for large one-way transfers, with a
CLI and a PySide6 desktop app. Camera-card offload is its primary workflow;
the data profile supports arbitrary files without media dependencies.

**Someone may reformat a camera card because Offloader said "Verified".**
Protecting the source, existing good copies, and the accuracy of that verdict
takes priority over features, throughput, and presentation.

## Start here

- Read [CONTRIBUTING.md](CONTRIBUTING.md) for development conventions.
- Before changing copy, verify, cleanup, or destination handling, read
  [docs/data-safety.md](docs/data-safety.md). Its known limits are part of the
  product's contract, not guarantees that have already been implemented.
- Use [ROADMAP.md](ROADMAP.md) for feature priorities and
  [docs/release-plan.md](docs/release-plan.md) for the proposed Windows beta.
  The release plan describes future work; verify the checkout before claiming
  an installer, signing pipeline, or release gate exists.
- Packaging, signing, the bill of materials and the tag-triggered candidate
  workflow are in [docs/build-windows.md](docs/build-windows.md); what an
  update checks before it runs anything is in
  [docs/updates.md](docs/updates.md). Hardware-key signing and clean-machine
  qualification are still gates, so a build being implemented is not a
  release being possible.
- Inspect the working tree before edits. Preserve unrelated user changes.
  Keep this guide concise and link to detailed documentation rather than
  copying it wholesale.

## Working with Owen

- Minimize typing and manual effort. Complete authorized, reversible work
  without repeatedly asking for confirmation. Ask only when a decision
  materially changes the result; prefer clickable choices when available.
- Keep replies concise. Never use em dashes in written output.
- Put text intended for copying into fenced code blocks, one per paste target,
  with labels and commentary outside the block.
- Use `rg` for content searches and `rg --files` for file discovery. Do not
  use `grep`, `findstr`, or `Select-String`.
- Delegate bounded searches or implementation only when it saves net effort;
  prefer an appropriate cheaper model and concise reports. Keep design
  decisions and final diff review in the main agent. Pass search rules on.
- Do not steal focus, move the cursor, or launch interactive GUI verification
  without authorization. Prefer headless checks. Never terminate user apps by
  image name; target only a specific process launched for the task.
- Never add AI authorship, co-author trailers, or AI-session links to commits,
  PRs, issues, documentation, or code comments.

## Code map

All module paths below are relative to `src/offloader/`.

| Area | Files and responsibility |
| --- | --- |
| Entry points | `cli.py`, `__main__.py`, `gui/app.py`; root `run.py` prefers this checkout's `src/`, launching the GUI without arguments and forwarding arguments to the CLI |
| Copy and control | `engine.py`: scanning, destination validation, fan-out, staging, verification, progress, `JobControl`, and CLI control-file support |
| Integrity | `hashers.py`, `integrity.py`, `verify.py`, `retry.py`, `longpath.py` |
| Shared data | `models.py`; keep engine and model code independent of Qt |
| Reports | `reports/` for PDF, CSV, MHL 1.1, and HTML; `ascmhl.py` for ASC MHL histories and directory hashes |
| Media | `probe.py`, `thumbs.py`, `braw.py`, `ixml.py`, `companions.py` |
| Timeline import | `timeline.py`: optional OpenTimelineIO integration, media resolution, and ambiguity handling |
| Desktop | `gui/main_window.py`, `gui/worker.py`, `gui/queue_view.py`, mode/editor widgets, and `gui/drives.py` |
| Persistent state | `config.py`, `presets.py`, `history.py`; `volumes.py` discovers storage and `naming.py` handles naming |
| Installation and updates | `installation.py` and `installation_lock.py` for transactional maintenance and the shared installed-instance lock; `update.py` finds, verifies and hands over a release, wrapped for the app by `gui/updates.py`. Neither may force-close a running transfer; see [docs/updates.md](docs/updates.md) |

Python 3.10+ is supported. Core dependencies are xxhash and ReportLab; PySide6
is the GUI extra. Timeline dependencies are separate extras. ffmpeg/ffprobe
on PATH enable media metadata and thumbnails but are not required for copying
and verification. Do not make a missing media tool block a valid transfer.

## Safety invariants

- Keep destination validation in the engine so CLI, GUI, and library callers
  share it. Never weaken source-overlap, duplicate-target, self-copy, or
  flattened-name collision checks to make a workflow pass.
- Write to staged `.offloader-partial` files. Preserve verification before
  promotion in verified modes and atomic replacement of final paths. Failure
  or cancellation must not truncate an existing good copy or leave incomplete
  data under a plausible final name. Cleanup must target only owned staging
  files, never source media or an unrelated existing destination.
- Preserve the distinction between `none`, `source-only`, and `full`
  verification. Full verification rereads the destination with cache-eviction
  handling; failed eviction must remain visible. Do not claim this proves
  physical persistence past a drive/controller cache.
- A skipped file is not newly checksum-verified. `skip_existing` currently
  compares size. Warnings, missing files, failed destinations, and report
  failures must not disappear into a blanket success verdict.
- Keep manifests portable and independently usable at each destination.
  Preserve relative paths and per-destination results. Hex digests compare
  case-insensitively; C4 identifiers compare exactly.
- Keep pause, resume, and cancellation responsive during copying and retry
  waits. Malformed or temporarily unreadable control-file contents mean no
  change of state, not cancellation. Use the existing `JobControl` machinery.
- Treat filenames, removable-media metadata, manifests, and configuration as
  untrusted input. Keep parsers bounded, reject unsafe XML constructs, escape
  HTML output, and guard against traversal and collisions.
- Timeline resolution must refuse ambiguous matches rather than choosing an
  arbitrary same-named file. Read [docs/timeline.md](docs/timeline.md) before
  changing its source/destination rules, which differ from card offload.
- Keep configuration under `config.py`'s per-user directory. Preserve atomic
  writes and unreadable-config fallback. Tests must use temporary state rather
  than modifying the user's presets or history.
- Do not represent known gaps as implemented protections: concurrent instances
  are not coordinated, size-only skip is not a checksum check, and directory
  structure hashes are not yet rechecked. Consult the current safety document
  for the full list before changing claims.

## Development and validation

Install development dependencies, preferably in an existing project virtual
environment or a new local one:

```powershell
python -m pip install -e ".[dev]"
```

Inspect environment capabilities without launching the GUI:

```powershell
python run.py info
```

Lint:

```powershell
python -m ruff check src tests
```

Run the suite:

```powershell
python -m pytest -q
```

Run the deeper property checks when relevant to safety or parser changes:

```powershell
python -m pytest tests/test_fuzz.py tests/test_fuzz_edges.py tests/test_edge_cases.py --fuzz -q
```

Build Python distributions when packaging changes:

```powershell
python -m build
```

The build command requires the `build` package, which CI installs separately.
GUI tests set Qt's offscreen platform before importing PySide6; CI also sets
`QT_QPA_PLATFORM=offscreen`. Do not launch the desktop just to run tests.

Changes to copy, verify, or delete paths require a regression test that fails
without the fix. Start with affected tests, then run lint and the full suite
for code changes. Use temporary fixtures, injected failures, and disposable
media copies. Never test destructive behavior against original footage.
Documentation-only changes need link/content and diff checks, not a test run.

Relevant suites include `test_data_safety.py`, `test_engine.py`,
`test_verify.py`, `test_retry.py`, and `test_control.py`. Parser, report,
timeline, and GUI suites live alongside them in `tests/`. Prefer independent
reference outputs for formats and checksums over tests that mirror the writer.
See CONTRIBUTING for existing synthetic BRAW/BWF fixtures and failure helpers.

CI configuration is in [ci.yml](.github/workflows/ci.yml): cross-platform
tests, a Python minimum-version job, ffmpeg coverage, property-test soak, and
wheel/sdist validation. A configured job is not evidence of a passing run.
Report what was actually executed and any skips or environment limitations.

## Implementation, documentation, and releases

- Match surrounding code, use type hints on new public functions, and explain
  non-obvious constraints in comments. Keep Qt imports inside the GUI layer
  and long-running work outside the GUI thread.
- Update the relevant `docs/` file when behavior changes. Report geometry,
  media parsing, ASC MHL, timeline resolution, and performance each have their
  own reference documents linked from README. Update published test counts
  only from actual results, not estimates.
- Measure performance before claiming improvement; follow
  [docs/performance.md](docs/performance.md) and state cache/durability effects.
- `src/offloader/_version.py` is the version source. Setuptools reads its
  literal through dynamic metadata; the package re-exports it and the Windows
  bundle uses it for executable metadata. Do not add another version literal
  or bump the version merely for documentation work.
- Alpha-OSK is a release-process reference, not a runtime dependency. Do not
  copy its product IDs, signer configuration, update endpoint, elevation
  behavior, or repository targets into Offloader.
- Follow the release plan's artifact and clean-machine gates when packaging
  lands. Installation or updates must not force-stop an active transfer.
  Preparing a release is distinct from publishing or sending announcements;
  perform external actions only within the user's authorized scope.
- Treat data loss, false verification, and silent omissions as security issues.
  Follow [SECURITY.md](SECURITY.md) for private reporting; do not publish
  sensitive reproductions or contact anyone without authorization.
