# Windows builds

The Windows builder produces a directory bundle, a portable ZIP, and an NSIS
installer named `Offloader-Setup-{version}.exe`. `Offloader.exe` (desktop) and
`offloader-cli.exe` (console) need the adjacent `_internal` directory and
`.offloader-install.lock`. Copy the entire bundle. The standalone
`offloader-maintenance.exe` manages installation and removal.

Signing defaults on. Use `--no-sign` for development and hosted CI. Unsigned
artifacts are not release downloads. The signing and installer code is
implemented; hardware-token signing and independent clean-machine
qualification remain gates in the [release plan](release-plan.md).
Nothing is automatically published.

## Build

Use Windows x64 and Python 3.12. Create a clean packaging environment so an
unrelated globally installed module cannot become an accidental dependency:

```powershell
python -m venv .venv-build
```

Install the project and the pinned runtime/build dependency set:

```powershell
.\.venv-build\Scripts\python.exe -m pip install -e ".[gui]" -r requirements-build.txt
```

Install [NSIS 3.12](https://nsis.sourceforge.io/Download) to build an installer.
The compiler is discovered on PATH or in its standard Program Files location.
Build development artifacts from the repository root:

```powershell
.\.venv-build\Scripts\python.exe build\windows\build.py --clean --no-sign
```

The script also accepts invocation by absolute path from another directory.
Output goes to `dist/windows/Offloader/`, with intermediates in
`.pyinstaller/windows/`. `--clean` clears PyInstaller's cache; it does not
delete the tracked `build/windows/` sources. Building replaces the previous
bundle, so do not build over executables currently in use.

| Option | Behavior |
| --- | --- |
| Default | Require clean sources, sign the bundle, assemble/sign the installer, verify, smoke-test, and write inventories/checksums |
| `--no-sign` | Explicit unsigned development output; never accesses the signing key |
| `--no-installer` | Build only the portable bundle; signing still defaults on |
| `--skip-build` | Reuse only a bundle with matching source commit, source digest, version, file set, and hashes |
| `--verify-only` | Check signed artifacts, source identity, versions, and final checksums without rebuilding or accessing the key |

The builder writes `.offloader-build.json` inside the bundle, an external
`Offloader-{version}-inventory.json` with dependency versions and signature
coverage, and `SHA256SUMS.txt` for the final installer, ZIP, and inventory.
A failed build leaves `.offloader-build-incomplete`; it must not be promoted.
Source changes during a build invalidate the candidate. These inventories are
provenance and tamper checks, not a complete third-party license inventory or SBOM.

## Signing

The default certificate is the existing OK Studio Inc. certificate identified
by its exact public thumbprint. `OFFLOADER_SIGN_CERT_SHA1` selects a renewed
certificate with the same company identity. `OFFLOADER_SIGNTOOL` can select a
specific Windows SDK tool; otherwise the newest installed x64 SDK is used.
Preflight checks certificate dates, code-signing usage, and private-key
association without unlocking the hardware token. No PIN or private key is
stored in the repository.

Own executables and unsigned native dependencies receive SHA-256 Authenticode
signatures and RFC 3161 timestamps. Valid, timestamped DLL/PYD signatures from
Microsoft, Python Software Foundation, and The Qt Company are retained under
an explicit publisher allowlist. Invalid or unexpected signatures stop the
build. The generated uninstaller is signed during NSIS compilation, followed
by the setup executable. Verification requires trust, the selected signer or
approved dependency publisher, timestamps, and matching application versions.
Cancelled signing, timestamp errors, and verification warnings are failures.

Verification needs SignTool but does not require the private key or a local
copy of the signing certificate. It can verify timestamped artifacts after
the signing certificate expires. The timestamp endpoint and verification
flags follow [SignTool's documented contract](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool).

## Installation safety

The installer defaults to Program Files/Offloader with administrator approval.
Desktop and Start Menu shortcuts default on. Silent `/S` installation accepts
the standard [NSIS `/D=` contract](https://nsis.sourceforge.io/Docs/Chapter3.html):
the absolute target is last and unquoted, including when it contains spaces.

Every installed GUI and CLI process holds a shared installation lock for its
whole lifetime. Installation and uninstall require exclusive access and fail
while any instance remains open, including an idle one. No process is killed.
New launches cannot start a job while files are being changed. The persistent
lock file remains after uninstall to avoid creating competing lock identities.
This lock protects program files; destination coordination between concurrent
offloads remains unimplemented.

Maintenance rejects nonempty unowned targets, link/junction paths, unsafe
inventories, changed managed files, and collisions with unrelated files.
It stages and checks new files, retains old owned files for rollback, and
records interrupted work for recovery. Only inventoried application files
are removed. Per-user configuration/history and unrelated files are retained.
An incomplete-installation marker blocks application startup until recovery.

Interactive Finish offers to launch Offloader using the non-elevated desktop
shell user's token and environment. If that identity cannot be obtained, it
asks the user to launch from Start Menu. There is no elevated fallback or
automatic launch during silent installation.

The GUI wizard, alternate administrator credentials, shortcut behavior, and
real install/upgrade/uninstall still need clean-machine qualification. Automated
tests and temporary-directory maintenance checks do not replace those gates.

`src/offloader/_version.py` is the version source. The Python distribution
reads its literal through setuptools dynamic metadata; the runtime imports it;
the spec reads it for Windows FileVersion and ProductVersion strings. The
build refuses stale installed Offloader metadata. Reinstall the editable
project after changing the version. No version was bumped for this slice.

Setuptools uses `.python-build/` for its generated files, configured in
`setup.cfg`, so its source-archive cleanup does not discard the tracked
`build/windows/` scripts or NSIS template. CI checks their presence in the sdist.

Numeric Windows versions reserve the fourth field for prerelease ordering:
alpha, beta, release candidate, then stable. Prerelease sequence numbers are
limited to 0 through 999, and every numeric field must fit in 16 bits. Other
PEP 440 forms are rejected rather than silently truncated.

The spec excludes optional timeline import, disables UPX, and includes the
project license and distribution metadata. ffmpeg and ffprobe remain external.
Missing media tools reduce metadata/thumbnails, not copy verification. A
release-ready third-party license inventory and SBOM remain separate work.

## Check the artifact

Installer implementation validation on 2026-09-10 (Windows x64, Python 3.12.10,
NSIS 3.12): 727 tests passed with 5 skips and 85% line coverage. Lint passed.
The unsigned installer and portable bundle built, and the frozen smoke checks
passed, including temporary installation, reinstall, removal, lock contention,
and unrelated-file preservation. Wheel/sdist builds and isolated wheel
installation also passed. Signing preflight and read-only verification of
existing Microsoft, Python, and Qt dependency signatures passed. No hardware
signing, visible wizard, or desktop-user launch was performed.

Historical baseline on 2026-09-10 before installer implementation (Windows x64,
Python 3.12.10): the directory
bundle built successfully and passed the smoke checks below. It contains
143,937,338 bytes before archiving. The development test environment passed
661 tests with 3 skips and 87% line coverage. Wheel/sdist builds, isolated
wheel installation, lint, and diff checks also passed. The packaging environment
uses the exact versions in `requirements-build.txt`; the source test run used
the existing development dependencies.

[Hosted CI for 5963d04](https://github.com/owenpkent/offloader/actions/runs/34530344380)
passed on Windows, Linux, and macOS, including the Windows bundle, package
build, and property-test soak jobs. Independent clean-machine testing remains
pending. The required signed release flow is defined in the
[release plan](release-plan.md#windows-signing-flow-matching-alpha-osk).

Run the headless smoke checks:

```powershell
.\.venv-build\Scripts\python.exe build\windows\smoke.py
```

The runner checks CLI and embedded executable versions, starts the GUI with
Qt's offscreen platform, and isolates configuration in a temporary directory.
It removes development Python/Qt environment variables and media tools from
PATH. It then copies disposable data to two destinations, requests all five
report formats, re-verifies the copies, and checks that a flipped byte fails
verification. Re-verification uses `--allow-cache` so this check exercises
packaging and checksum behavior without claiming physical-drive qualification.

The smoke runner also uses the frozen standalone maintenance helper to install,
reinstall, and uninstall in a temporary directory. It checks the installed CLI,
conflicting application/maintenance locks, and preservation of an unrelated
file. It does not run the NSIS wizard, change the registry, or create shortcuts.

These checks do not validate installation, signing, a visible interactive
desktop, or independent hardware. The release still needs the plan's
clean-machine and real-storage tests.

CI builds and checks unsigned installers and portable bundles on Windows and
uploads them with their inventories/checksums as `offloader-windows-unsigned`.
It never invokes the hardware key. The Python distribution job additionally
installs the wheel in a fresh environment outside the checkout, comparing
package metadata, runtime version, and CLI version. That check is implemented
in `scripts/check_wheel.py`.

For changes to the build helpers, include them in linting:

```powershell
python -m ruff check src tests scripts build/windows
```
