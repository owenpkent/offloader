# Windows bundle

The first packaging slice produces an **unsigned directory bundle**, containing
`Offloader.exe` (desktop) and `offloader-cli.exe` (console). Both executables
need the adjacent `_internal` directory. Copy or archive the entire Offloader
directory, not either executable alone.

This is a development artifact. An installer, signing, active-job installation
protection, and clean-machine qualification remain in the
[release plan](release-plan.md). Nothing is automatically published.

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

Build from the repository root:

```powershell
.\.venv-build\Scripts\python.exe build\windows\build.py --clean
```

The script also accepts invocation by absolute path from another directory.
Output goes to `dist/windows/Offloader/`, with intermediates in
`.pyinstaller/windows/`. `--clean` clears PyInstaller's cache; it does not
delete the tracked `build/windows/` sources. Building replaces the previous
bundle, so do not build over executables currently in use.

`src/offloader/_version.py` is the version source. The Python distribution
reads its literal through setuptools dynamic metadata; the runtime imports it;
the spec reads it for Windows FileVersion and ProductVersion strings. The
build refuses stale installed Offloader metadata. Reinstall the editable
project after changing the version. No version was bumped for this slice.

Numeric Windows versions reserve the fourth field for prerelease ordering:
alpha, beta, release candidate, then stable. Prerelease sequence numbers are
limited to 0 through 999, and every numeric field must fit in 16 bits. Other
PEP 440 forms are rejected rather than silently truncated.

The spec excludes optional timeline import, disables UPX, and includes the
project license and distribution metadata. ffmpeg and ffprobe remain external.
Missing media tools reduce metadata/thumbnails, not copy verification. A
release-ready third-party license inventory and SBOM remain separate work.

## Check the artifact

Local validation on 2026-09-10 (Windows x64, Python 3.12.10): the directory
bundle built successfully and passed the smoke checks below. It contains
143,937,338 bytes before archiving. The development test environment passed
661 tests with 3 skips and 87% line coverage. Wheel/sdist builds, isolated
wheel installation, lint, and diff checks also passed. The packaging environment
uses the exact versions in `requirements-build.txt`; the source test run used
the existing development dependencies.

[Hosted CI for 5963d04](https://github.com/owenpkent/offloader/actions/runs/34530344380)
passed on Windows, Linux, and macOS, including the Windows bundle, package
build, and property-test soak jobs. Independent clean-machine testing remains
pending. The future signed release flow is defined in the
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

These checks do not validate installation, signing, a visible interactive
desktop, or independent hardware. The release still needs the plan's
clean-machine and real-storage tests.

CI builds and checks this bundle on Windows and uploads it as
`offloader-windows-unsigned`. The existing Python distribution job additionally
installs the wheel in a fresh environment outside the checkout, comparing
package metadata, runtime version, and CLI version. That check is implemented
in `scripts/check_wheel.py`.

For changes to the build helpers, include them in linting:

```powershell
python -m ruff check src tests scripts build/windows
```
