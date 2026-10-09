# Release plan

Plan dated 2026-09-10. Scope: Offloader's first packaged desktop release,
using Alpha-OSK's release process as a reference. This is a proposed sequence,
not a commitment to a launch date. Completed implementation is recorded below.

## Implementation progress

The implementation now has a single version source in `src/offloader/_version.py`,
pinned Windows packaging dependencies, a PyInstaller bundle with GUI, CLI, and
maintenance executables, and source and bundle inventories. The Windows builder
supports the default signed flow plus `--no-sign`, `--skip-build`,
`--verify-only`, and `--no-installer`. It emits signed-build inventories and
SHA-256 checksums, and can assemble the NSIS installer.

The installer uses transactional maintenance operations and a shared installed
GUI/CLI lifetime lock. Every running instance, including an idle one, must
close before maintenance proceeds, and maintenance never force-kills it. The
optional Finish launch runs under the desktop user's token and is being
implemented now. CI includes Windows bundle checks and fresh-environment wheel
installation.
See [build-windows.md](build-windows.md) for commands and validation details.

Hardware-key signing, clean-machine interactive installation and alternate
credential checks, the complete third-party license inventory/SBOM, private
pilot, public release workflow, and release qualification remain pending. The
tables below retain the planned stage gates; implementation does not complete
those gates.

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
| Version | One source in `src/offloader/_version.py` used by package metadata and the Windows bundle | Confirm the frozen release identity across all published assets |
| CI | Windows/macOS/Linux tests on Python 3.13, Linux Python 3.10, ffmpeg job, property-test soak, wheel/sdist build and metadata checks; a tag-triggered candidate workflow that gates the tag against the declared version, builds unsigned, and prepares a draft with no assets | Install built artifacts in fresh environments; attach signed assets from the release workstation |
| Distribution | Frozen bundle, NSIS installer path, source and bundle inventories, and checksums | Hardware-key signing, clean-machine installation, release workflow, and publication documentation |
| Dependencies | Minimum versions and optional extras | Recorded build environment and pinned release dependency sets |
| Media tools | ffmpeg/ffprobe discovered externally; copying works without them | Explicit installer dependency policy and useful missing-tool messaging |
| Integrity | Detailed guarantees and remaining limits in `data-safety.md` | Release-specific regression evidence and operational validation |
| Updates | `offloader update` and the desktop app both check GitHub Releases, verify the signed installer and hand it over, declining while a job is active | Qualify an end-to-end update against a published signed release on a clean machine |

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
| `build/windows/build.py`, `.spec`, `installer.nsh`, `sign.py` | PyInstaller bundle, NSIS installer, transactional maintenance, and explicit signing stage adapted to Offloader's entry points and dependencies |
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
- **Install:** match Alpha-OSK's signed NSIS wizard and default to
  `C:/Program Files/Offloader`, with UAC for installation. Launch the app as
  the original, non-elevated user. Keep configuration/history in that user's
  `%APPDATA%/Offloader`, preserving them on upgrade and ordinary uninstall.
- **Distribution:** GitHub prerelease with signed installer, SHA-256 checksums,
  source commit, release notes, dependency lockfile, SBOM, and license inventory. Keep CI
  wheel/sdist artifacts; defer PyPI publication until it serves an actual need.
- **Media tools:** for the first beta, keep ffmpeg/ffprobe external and clearly
  identify unavailable metadata/thumbnails. A bundled build can follow after
  selecting, documenting, and validating its redistribution arrangement.
- **Timeline support:** include and test OpenTimelineIO and the currently
  declared adapter in the desktop bundle if timeline import is advertised for
  that bundle. Otherwise mark that capability source-only for the beta.
- **Updates:** `offloader update` finds and verifies a release and runs the
  signed installer; the in-app check remains deferred. Refuse replacement
  while the app or CLI has an active job; never force-kill a copy to install
  an update.
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

`.github/workflows/release.yml` now implements the preparation half of this:
it fails on a tag/version mismatch before building, builds unsigned, requires
every artifact the contract names to exist, and prepares a draft pinned to the
tagged commit with `contents: write` held only by the drafting job. It attaches
nothing, because hosted CI cannot sign; signature verification and asset upload
remain release-workstation steps. Alpha-OSK has no release automation to copy
here, so this is new work rather than parity.

The release workflow should prepare a draft with narrowly scoped permissions,
pin the source commit and build environment, and fail on version mismatch,
missing assets, failed checks, or invalid signatures. Inventory bundled native
executables and DLLs and verify their signing coverage. Sign the application
before installer assembly, sign the installer afterward, then calculate the
published hashes. Promote the exact tested artifact; a rebuild needs its own
qualification. Confirm access to the existing signing setup before making it
a build dependency. No new signing purchase or account setup is implied here.

## Installer flow and Alpha-OSK parity

The repository now contains the NSIS installer and transactional maintenance
path. The public Windows download will be `Offloader-Setup-{version}.exe`, a
signed NSIS installer requiring no Python. Clean-machine installation,
alternate credentials, and public release remain qualification gates.

**First install:** open the installer, approve UAC with OK Studio Inc. shown
as publisher, then proceed through Welcome, License, Install Location,
Shortcut Options, Install Progress, and Finish. Finish offers Launch Offloader,
running under the original user's identity rather than the installer's admin
token. Desktop and Start Menu shortcuts default on; Back/Next navigation must
preserve the user's choices. The Start Menu also includes an uninstall entry.

Alpha-OSK's additional research-participation page is product-specific. There
is no corresponding Offloader feature or consent page to add.

| Behavior | Alpha-OSK reference | Planned Offloader behavior |
| --- | --- | --- |
| Packaging | Versioned, branded, signed NSIS setup executable | Same flow with Offloader identity, icon, artwork, and version metadata |
| Install location | Program Files x64 by default; UAC elevation | Program Files/Offloader by default, with a location page and validated target |
| Shortcuts | Desktop and Start Menu choices, checked by default; All Users context | Same choices and defaults; no automatic start-at-login registration |
| Installed components | Complete PyInstaller bundle and uninstaller | Desktop app, CLI, shared runtime, notices, and uninstaller; preserve external ffmpeg policy |
| Windows app listing | Name, version, publisher, location, icon, uninstall command | Matching Installed Apps entry with Offloader-specific keys; validate scope under alternate admin credentials |
| Finish/launch | Launch checkbox uses the original user's shell | Same user-facing launch option; never run Offloader with inherited installer elevation |
| Existing installation | Close app, remove previous installed files, replace with new version | Check GUI and CLI first; block if a transfer is active, then close idle instances gracefully before replacement |
| User state | Silent upgrade preserves learned data and settings | Preserve presets, history, and settings; installation never reads or changes camera media, destinations, or reports |
| Silent installation | `/S` and an explicit computed `/D=` target; user-context relaunch | Support silent operation and explicit validated target; active jobs cause a nonzero exit without modifying the installation |
| Uninstall | Confirmation and progress; optional removal of user data | Confirmation and progress; keep user data by default, with an explicit optional settings/history removal choice |

**Upgrade/reinstall:** identify the existing Offloader installation, check
for active GUI and CLI jobs, and refuse replacement until they finish or the
user cancels them through Offloader. Check again before changing files to
close the race with a newly started job. For idle instances, request a normal
exit. Never use a force-kill fallback. Remove obsolete application-owned files,
install the new signed bundle, refresh shortcuts and the app listing, and
offer relaunch. Check every cleanup/install exit code; failure must not show
a success page or launch a half-installed app. Preserve recoverable prior
application files until replacement succeeds.

**Uninstall:** apply the same active-job guard. Remove installed application
files, shortcuts, and app registration. Preserve per-user configuration unless
the user explicitly chooses its removal. Silent upgrade cleanup always keeps
user data. Remove only inventoried application files, never recursively erase
an arbitrary install directory that might contain user material.

**Updater boundary:** installer parity includes the silent-install contract,
user-context relaunch, and safe settings preservation. A generic unattended
deployment must not launch an app in a missing or unrelated user's session.

The update client is now implemented in `src/offloader/update.py`, wrapped for
the desktop app in `src/offloader/gui/updates.py`, and documented in
[updates.md](updates.md). It meets the conditions this section set: the
signature, publisher and embedded version are all verified before elevation,
and the `/D=` target is computed from the running executable rather than from
the uninstall registry key.

The app never replaces itself under an active transfer. A running or paused job
declines the update with a reason, the queue is rechecked immediately before
the installer is launched, and the app then closes itself deliberately so
maintenance can proceed. What remains is qualification rather than
implementation: an end-to-end update from one signed published release to the
next, on a clean machine, which needs a signed release to exist first.

**Acceptance gates:** exercise first install, custom path, same-version
reinstall, upgrade, failed upgrade recovery, silent install, and uninstall on
a clean Windows account. Include alternate admin credentials, Desktop/Start
Menu choices with Back/Next, settings preservation, and an active GUI or CLI
transfer during upgrade/uninstall. Confirm the signed publisher, app version,
normal-user launch, CLI behavior, and absence of any media/report changes.

Reference code in the Alpha-OSK checkout: `build/windows/build.py` function
`_generate_nsi_script`, `build/windows/installer.nsh`, and `src/updater.py`.
Reuse the intended installer experience while testing Offloader's own identity,
state paths, file ownership, and active-job guarantees.

## Windows signing flow, matching Alpha-OSK

The builder implements this signing flow. Hardware-key signing remains a
release operation and is still pending qualification. `--no-sign` is the
explicit path for development and hosted CI artifacts.

**Signing is mandatory for a Windows release, including a public beta.**
Unsigned CI artifacts are development outputs and must not be promoted to a
release download.

Use Alpha-OSK's existing local build pattern and the same OK Studio Inc.
certificate, SafeNet hardware token, Windows SDK SignTool, and DigiCert
timestamp service. A new signing provider or hosted signing service is not
needed for this release.

Implemented interface for `build/windows/build.py`:

| Mode | Behavior |
| --- | --- |
| Default invocation | Build, sign application binaries, assemble NSIS installer, sign installer, verify signatures |
| `--no-sign` | Explicit unsigned development build; required in hosted PR CI; not eligible for publication |
| `--skip-build` | Repackage and sign the existing bundle after confirming its version and source identity |
| `--verify-only` | Verify existing application/installer signatures without signing or rebuilding |
| `--no-installer` | Produce the portable bundle; signing still defaults on |

These modes are implemented; signed release use still requires the hardware
key, signature qualification, and the release gates below.
The release sequence is:

1. Freeze the source commit and version; pass source checks.
2. Build the application bundle in the recorded packaging environment.
3. Sign the Offloader application binaries and verify native dependency
   signature coverage under the policy below.
4. Assemble the installer from that signed bundle.
5. Sign the installer, then verify trust, intended publisher, timestamps, and
   version metadata for the application and installer.
6. Run artifact smoke tests and clean-machine installation checks against
   those exact signed files.
7. Calculate final checksums and prepare the draft release with its inventory.
8. Publish only after every gate passes and release publication is requested.

The planned normal build command signs by default, as in Alpha-OSK. Any
signing or verification failure blocks promotion to the draft/publish stages.
A failed or cancelled hardware
prompt leaves the candidate unqualified; it must never trigger an unsigned
fallback. A rebuild or any binary change invalidates the prior qualification
and restarts signing and artifact checks.

The workstation has Windows SDK SignTool and the existing OK Studio Inc.
code-signing certificate in the current-user store, with a private-key
association. The certificate expires on 2026-12-31; recheck validity and token
availability at release time. Store visibility does not prove the hardware
key is unlocked.

1. **Implement release signing.** Keep development and hosted PR builds
   unsigned through the explicit `--no-sign` option. Add `sign.py` as the
   shared helper called by the default build for both application binaries
   and the installer. Select the existing OK Studio certificate by its exact
   thumbprint, with a local override for certificate renewal. Never commit a
   PIN or private key. Use the current-user token setup from a normal shell.
2. **Define signature coverage.** Inventory the desktop executable, CLI,
   bundled DLLs/Python extensions, and eventual installer. Decide how to
   preserve valid vendor signatures and verify publisher identity for each
   category before implementing bulk signing. Reject incomplete bundles and
   paths that escape through symlinks or junctions.
3. **Sign and timestamp.** Use SHA-256 file and timestamp digests with RFC 3161.
   The installed SignTool rejected the HTTPS form of DigiCert's timestamp URL;
   Alpha-OSK's HTTP endpoint reached the signing step. Validate the returned
   timestamp cryptographically. Treat missing tools, certificate problems,
   cancelled PIN prompts, timestamp failures, and signing failures as failures
   of the signed build. Never silently emit an unsigned release.
4. **Verify the result.** Require Authenticode trust, the intended signer,
   a valid timestamp, and matching executable version metadata. Add automated
   tests for wrong signer, missing timestamp, cancelled signing, and invalid
   bundle paths without accessing the real hardware key.
5. **Integrate the installer.** Sign application binaries before assembling
   the installer, then sign and verify the installer. Run smoke tests on the
   signed output, calculate final checksums, and retain an artifact/signature
   inventory. This stage depends on the installer implementation.
6. **Qualify and publish later.** On a clean Windows account, check the
   displayed publisher and normal launch/install behavior. Keep hardware-key
   signing on the release workstation initially; hosted PR CI should never
   require the token. Publish only the exact signed artifacts that passed
   qualification, after a separate release instruction.

Implementation reference: Microsoft's
[SignTool documentation](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool).

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
- [ ] Confirm every Windows download passed the mandatory signing flow above;
  block publication if any artifact is unsigned, has the wrong signer, lacks
  a valid timestamp, or differs from the qualified candidate.
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

Version unification, artifact identity and inventory, the NSIS installer path,
transactional maintenance, shared installed-instance locking, signing hooks,
and checksum records are implemented. The clean-account GUI walkthrough,
hardware-key signing, alternate-credential install checks, complete license
inventory/SBOM, private pilot, release workflow, and candidate qualification
remain to be done.
