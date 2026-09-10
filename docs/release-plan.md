# Release plan

Plan dated 2026-09-10. Scope: Offloader's first packaged desktop release,
using Alpha-OSK's release process as a reference. This is a proposed sequence,
not a commitment to a launch date. Completed implementation is recorded below.

## Implementation progress

The first slice now has a single version source in `src/offloader/_version.py`,
pinned Windows packaging dependencies, an unsigned PyInstaller directory
bundle with GUI and CLI executables, and automated artifact checks. CI now
includes Windows bundle checks and fresh-environment wheel installation.
See [build-windows.md](build-windows.md) for commands and validation details.

The installer, signing, complete third-party license inventory/SBOM, safe
installation during active jobs, private pilot, and clean-machine qualification
remain pending. The tables below retain the pre-implementation assessment and
the planned stage gates; creating a bundle does not complete those gates.

## Release target

Ship a **Windows x64 public beta** that someone without Python can install,
use to offload a card to two destinations, and independently re-verify from
the resulting manifests. Include the desktop app and a usable CLI.

Keep macOS and Linux available from source and Python distributions initially.
Their existing CI coverage is useful, but does not establish that a packaged
desktop application works on either platform. Native installers follow their
own build and machine-validation gates.

Start with a small private pilot, then publish a GitHub prerelease. Graduate
to a normal release after the acceptance checklist and pilot findings are
closed. A beta label does not relax any data-integrity gate.

## Initial evidence and gaps

Inspected local Offloader checkout at `fb59094`; working tree was clean before
this plan. Remote release state and CI results have not been checked, and no
tests or builds were run for this documentation task.

| Area | Present in the checkout | Work needed for release |
| --- | --- | --- |
| Product | Engine, CLI, Qt desktop app, reports, BRAW/BWF support, optional timeline import | Exercise the frozen application against representative workflows |
| Version | `0.1.0` appears independently in `pyproject.toml` and `src/offloader/__init__.py` | One source used by package metadata, app, installer, reports, and release assets |
| CI | Windows/macOS/Linux tests on Python 3.13, Linux Python 3.10, ffmpeg job, property-test soak, wheel/sdist build and metadata checks | Install built artifacts in fresh environments; build and smoke-test Windows desktop artifacts |
| Distribution | Source installation instructions and CI package artifacts | Frozen bundle, installer, signing, release workflow, installation documentation |
| Dependencies | Minimum versions and optional extras | Recorded build environment and pinned release dependency sets |
| Media tools | ffmpeg/ffprobe discovered externally; copying works without them | Explicit installer dependency policy and useful missing-tool messaging |
| Integrity | Detailed guarantees and remaining limits in `data-safety.md` | Release-specific regression evidence and operational validation |
| Updates | No updater found | Manual updates for the beta; documented safe upgrade and recovery |

Sources: [`pyproject.toml`](../pyproject.toml),
[`CI`](../.github/workflows/ci.yml), [`README`](../README.md),
[`ROADMAP`](../ROADMAP.md), and [`data safety`](data-safety.md).
There are no tags in the inspected local checkout; that is not proof that
nothing has been published remotely.

## What to take from Alpha-OSK

Reference checkout: `C:/Users/owenp/dev/alpha-osk`.

| Reference | Offloader adaptation |
| --- | --- |
| `src/__version__.py` and release rules in `AGENTS.md` | Establish one version source and enforce agreement before building |
| `build/windows/build.py`, `.spec`, `installer.nsh`, `sign.py` | Use a PyInstaller bundle, NSIS installer, and explicit signing stage, adapted to Offloader's entry points and dependencies |
| `docs/build/WINDOWS.md` release checklist | Publish the dependency lockfile and software bill of materials (SBOM) alongside the installer; verify embedded executable versions and signatures |
| Exact versioned installer naming | Define `Offloader-Setup-{version}.exe`; keep the contract stable |
| Dedicated `alpha-osk-releases` repository | Make the publication target explicit. Default to the existing `owenpkent/offloader` repo; a second repo is optional infrastructure |
| Clean-account installer validation | Test the downloaded, signed installer on a machine without the development environment |
| Website reads the releases API | Use GitHub Releases as the beta download page; if a website is added, avoid a second manually maintained version number |
| `docs/roadmap/LAUNCH_PLAN.md` and `launch_tasks.csv` | Keep a launch checklist with dependencies, evidence, and a small outreach phase |

Alpha-OSK's launch plan is dated May 2026 and includes product-specific
telemetry work. Treat its dates and open checkboxes as historical context.
Its updater, elevation behavior, signing configuration, and user settings
migrations need independent design before reuse. Do not copy product IDs,
credentials, update endpoints, or installation paths.

## Proposed scope decisions

- **Channel:** Windows x64 beta first. Record the Windows versions actually
  tested before publishing a support claim.
- **Version:** verify existing remote tags/releases first. If `0.1.0` is
  unused, use `0.1.0b1` for the first public beta and `v0.1.0b1` for its tag.
  Test the mapping to numeric Windows executable/installer version fields.
  Otherwise choose the next unused version before freezing the candidate.
- **Install:** prefer a per-user install that runs without administrator
  rights. Keep configuration/history outside the application directory and
  preserve them on upgrade and ordinary uninstall.
- **Distribution:** GitHub prerelease with signed installer, SHA-256 checksums,
  source commit, release notes, dependency lockfile, SBOM, and license inventory. Keep CI
  wheel/sdist artifacts; defer PyPI publication until it serves an actual need.
- **Media tools:** for the first beta, keep ffmpeg/ffprobe external and clearly
  identify unavailable metadata/thumbnails. A bundled build can follow after
  selecting, documenting, and validating its redistribution arrangement.
- **Timeline support:** include and test OpenTimelineIO and the currently
  declared adapter in the desktop bundle if timeline import is advertised for
  that bundle. Otherwise mark that capability source-only for the beta.
- **Updates:** manual installation initially. Refuse replacement while the app
  or CLI has an active job; never force-kill a copy to install an update.
- **Scope freeze:** defer new media features, cloud services, notifications,
  auto-update, and a marketing website. Fix integrity and packaging blockers
  discovered during qualification.

These are working defaults for implementation, not statements that the
packaging or safeguards already exist.

## Work sequence and exit criteria

Owen owns release decisions and final publishing. Implementation and evidence
collection can be prepared in the repository. Progress is gated by results,
not elapsed time; set a public date after the first clean-machine pilot.

| Stage | Deliverables | Exit criterion |
| --- | --- | --- |
| 1. Release foundation | Single version source; frozen scope; dependency pins; build instructions; chosen artifact names and repository | A clean checkout produces matching package/app/report versions; release identity is unambiguous |
| 2. Windows package | PyInstaller spec and entry points; icon/version metadata; NSIS installer; third-party notices; media-tool status | GUI and CLI work on a clean Windows account without Python; reports render and manifests re-verify |
| 3. Trust and installation | Signing integration; signature verification; safe active-job handling; install/upgrade/uninstall checks | Both executable and installer signatures validate; install lifecycle preserves user data and never interrupts a job |
| 4. Candidate qualification | All existing CI jobs green for the exact source commit; artifact smoke tests; integrity acceptance matrix below | Every gate has recorded evidence; no unexplained skips, false success, data loss, or blocking install defects |
| 5. Private pilot | Same candidate tested by 3 to 5 willing users on disposable copies of real media | At least 3 complete offload/re-verify workflows across 2 independent Windows machines; all blocking findings resolved and retested |
| 6. Public beta | GitHub prerelease; verified asset downloads; quick start; known limits; support instructions; brief demo | A new user can install, run, locate the reports, and report a problem using the published instructions |
| 7. Stabilize | Triage pilot/public reports; publish fixes with fresh versions; retain prior assets | At least one week of observation plus repeat acceptance evidence and no open integrity, installation, or recovery blockers before a normal release |

Suggested implementation files: `build/windows/offloader.spec`,
`build/windows/build.py`, `build/windows/installer.nsi`,
`build/windows/sign.py`, `.github/workflows/release.yml`, and
`docs/build-windows.md`. Follow Alpha-OSK's separation of build, sign, and
publish, rather than assuming its scripts are drop-in compatible.

The release workflow should prepare a draft with narrowly scoped permissions,
pin the source commit and build environment, and fail on version mismatch,
missing assets, failed checks, or invalid signatures. Inventory bundled native
executables and DLLs and verify their signing coverage. Sign the application
before installer assembly, sign the installer afterward, then calculate the
published hashes. Promote the exact tested artifact; a rebuild needs its own
qualification. Confirm access to the existing signing setup before making it
a build dependency. No new signing purchase or account setup is implied here.

## Candidate acceptance matrix

Use dedicated fixture folders and disposable copies, never the sole copy of
production material. Record candidate version, source SHA, artifact SHA-256,
OS/filesystem, result, and evidence location for every row.

| Gate | Required evidence |
| --- | --- |
| Fresh installation | Install and launch without Python or developer PATH entries; exercise GUI, CLI, Unicode/spaced paths, report fonts, and missing media tools |
| Ordinary offload | Copy a representative card to two destinations with full verification; independently re-verify both manifests and inspect all advertised report formats |
| Data profile | Transfer arbitrary binary files with no media tools; verify bytes and reports |
| Media profiles | BRAW with/without proxy, BWF/iXML, and common camera media; confirm unavailable thumbnails are represented honestly |
| Optional timelines | If bundled, exercise supported adapter discovery, missing media, and ambiguous basenames from the frozen application |
| Unsafe destinations | Source/destination overlap, flatten collisions, and replacement of existing good files are refused or handled without destroying the good copy |
| Failure handling | Corruption, full disk, destination disconnect, permission failure, and transient source errors never yield a false Verified verdict; reports distinguish destination outcomes |
| Job control | Pause/resume/cancel in GUI and CLI, including retry waits; finished files survive and cancelled in-flight files never appear complete |
| Re-verification | Alter a copied byte and remove a file; verification fails with actionable output and correct exit status |
| Install lifecycle | Upgrade, reinstall, ordinary uninstall, and fallback to the previous candidate preserve presets/history and leave media/reports untouched; active GUI/CLI jobs prevent replacement |
| Real storage | Removable source and two physical destinations; record devices, filesystems, elapsed time, throughput, memory, and responsiveness with the candidate version |

Keep each existing regression test relevant to these guarantees in the release
gate. Add tests when implementation changes introduce a new failure mode;
do not substitute a source test run for validation of the shipped bundle.

### Known limits that affect the release

- **Concurrent writers:** destination coordination is absent. Before a public
  desktop beta, add a lock respected by both GUI and CLI or an equivalent
  enforced refusal of overlapping Offloader jobs, with stale-lock recovery
  tests. Merely documenting the possibility of a false verdict is insufficient
  for an ordinary multi-instance desktop workflow.
- **Case-insensitive targets off Windows:** defer native macOS/Linux packages
  until destination-aware collision handling is implemented or those targets
  are safely refused. Keep the existing source-use limitation visible.
- **Size-only skip:** expose its actual meaning wherever it is offered; a
  skipped file must not be presented as checksum-verified by this run. Confirm
  this behavior in both UI and reports, and fix any misleading presentation.
- **ASC MHL structure:** file verification does not yet check directory
  structure hashes. Describe the supported verification scope accurately;
  structure verification remains the next trust improvement in the roadmap.
- **Mutable sources and hardware caches:** carry the existing documented
  limits into release notes. Do not promise an independent source reread or
  physical persistence beyond what the implementation establishes.

## Publish and recover

- [ ] Record the final candidate's source SHA, checks, machine results, and
  pilot findings in a versioned release record.
- [ ] Update `CHANGELOG.md`, installation instructions, supported platforms,
  dependencies, and known limits to match the final bundle.
- [ ] Prepare release notes explaining what ships, how to install, how to
  verify a transfer, and how to report a problem. Include a screenshot and a
  short card-to-two-destinations demo using non-sensitive sample material.
- [ ] Verify signing and checksums on assets downloaded from the draft.
- [ ] Review the concrete draft, then publish the prerelease and source tag
  against the tested commit. Keep repository targets explicit.
- [ ] Validate the public download links, perform one installed smoke test,
  and watch incoming issues closely during the first 48 hours.
- [ ] Prepare outreach for existing relevant contacts/communities after the
  download works. Sending messages and posting announcements are separate
  actions from preparing this release plan.

If a candidate reports false success, damages data, or cannot safely complete
installation, halt promotion immediately. Mark the affected release clearly,
direct users to the previous tested installer where one exists, and publish
a corrected candidate under a new version. Never silently replace an existing
version's executable. Keep prior assets and checksums available for diagnosis.

For the first beta, there may be no previous packaged release: withdrawal and
an explicit known-issue notice are the recovery path until a fix qualifies.
Any downgrade must be tested against the user's current configuration/history
format; do not ask users to delete state as the default recovery procedure.

## First implementation slice

Version unification and the unsigned local Windows bundle are implemented.
Headless artifact checks pass; the clean-account GUI walkthrough remains to
be done. Next add the installer, safe job handling, signing, and candidate
qualification in the order above.
