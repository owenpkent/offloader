"""Build and verify Windows artifacts; use --no-sign for development and CI."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
# The sibling build scripts are imported by name from inside functions. Running
# this file directly puts its directory on the path; importing it as a module,
# which the tests do, does not.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
SPEC = HERE / "offloader.spec"
DIST = REPO / "dist" / "windows"
WORK = REPO / ".pyinstaller" / "windows"
OWN_EXECUTABLES = {"Offloader.exe", "offloader-cli.exe", "offloader-maintenance.exe"}
NATIVE_SUFFIXES = {".exe", ".dll", ".pyd"}


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=REPO, check=True)


def check_versions(bundle: Path, version: str) -> None:
    from smoke import file_version

    for name in sorted(OWN_EXECUTABLES):
        if file_version(bundle / name) != version:
            raise RuntimeError(f"Wrong embedded version on {name}")


def check_signatures(bundle: Path, *, signing: bool, version: str) -> list[dict]:
    import sign

    records = []
    for path in sorted(bundle.rglob("*")):
        if path.suffix.lower() not in NATIVE_SUFFIXES or not path.is_file():
            continue
        relative = path.relative_to(bundle).as_posix()
        own = relative in OWN_EXECUTABLES
        if signing:
            status = sign.inspect_file(path)
            if status["signature_status"] == "NotSigned":
                sign.sign_file(path)
        # Executables must belong to Offloader. Approved vendor DLL/PYD
        # signatures are preserved, with trust and timestamps still required.
        record = sign.verify_file(
            path, expected_version=version if own else None,
            allow_vendor=path.suffix.lower() in {".dll", ".pyd"},
        )
        records.append({**record, "path": relative})
    return records


def sbom_names(version: str) -> set[str]:
    """The bill-of-materials files a release carries.

    Named in `artifacts` because four places have to agree about them:
    `save_outputs` checksums them, `validate_outputs` refuses anything it did
    not expect, and `scripts/release_notes.py` writes the upload command that
    publishes them.
    """
    from artifacts import sbom_names as names

    return set(names(version))


def save_outputs(bundle: Path, setup: Path | None, identity: dict,
                 signatures: list[dict], signed: bool) -> None:
    import sbom
    from artifacts import bundle_inventory

    version = identity["version"]
    inventory = DIST / f"Offloader-{version}-inventory.json"
    inventory.write_text(json.dumps({
        "schema": 1, **identity, "signed": signed,
        "python": platform.python_version(),
        "packages": sorted(
            f"{distribution.metadata['Name']}=={distribution.version}"
            for distribution in importlib.metadata.distributions()
        ),
        "files": bundle_inventory(bundle), "signatures": signatures,
    }, indent=2) + "\n", encoding="utf-8")
    archive = DIST / f"Offloader-{version}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                output.write(path, f"Offloader/{path.relative_to(bundle).as_posix()}")
    outputs = [archive, inventory, *sbom.write_all(DIST, identity)]
    if setup is not None:
        outputs.append(setup)
    lines = []
    for path in outputs:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        lines.append(f"{digest.hexdigest()}  {path.name}")
    (DIST / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_outputs(bundle: Path, setup: Path | None, identity: dict) -> None:
    """Require checksummed release outputs from the same completed build."""
    from artifacts import bundle_inventory

    version = identity["version"]
    inventory = DIST / f"Offloader-{version}-inventory.json"
    record = json.loads(inventory.read_text(encoding="utf-8"))
    if not record.get("signed") or any(record.get(key) != value for key, value in identity.items()):
        raise RuntimeError("Output inventory is unsigned or belongs to different sources")
    if record.get("files") != bundle_inventory(bundle):
        raise RuntimeError("Output inventory no longer matches the bundle")
    expected = {inventory.name, f"Offloader-{version}-windows-x64.zip",
                *sbom_names(version)}
    if setup is not None:
        expected.add(setup.name)
    checksums = {}
    for line in (DIST / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if name not in expected or name in checksums:
            raise RuntimeError("Unexpected or duplicate output in checksum inventory")
        checksums[name] = digest
    if set(checksums) != expected:
        raise RuntimeError("Checksum inventory is incomplete")
    for name, expected_digest in checksums.items():
        digest = hashlib.sha256()
        with (DIST / name).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_digest:
            raise RuntimeError(f"Release output changed after the build: {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clean", action="store_true", help="clear the PyInstaller cache")
    parser.add_argument("--no-sign", action="store_true", help="unsigned development build")
    parser.add_argument("--skip-build", action="store_true", help="validate and reuse the bundle")
    parser.add_argument("--verify-only", action="store_true", help="check existing signed artifacts")
    parser.add_argument("--no-installer", action="store_true", help="produce only the portable bundle")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        parser.error("Windows artifacts must be built and verified on Windows")
    if args.verify_only and (args.no_sign or args.skip_build or args.clean):
        parser.error("--verify-only cannot be combined with --no-sign, --skip-build, or --clean")
    if args.skip_build and args.clean:
        parser.error("--clean cannot be combined with --skip-build")

    import artifacts
    import installer
    import sign

    bundle = DIST / "Offloader"
    incomplete = DIST / ".offloader-build-incomplete"
    try:
        identity = artifacts.source_identity(REPO)
        version = identity["version"]
        setup = None if args.no_installer else DIST / f"Offloader-Setup-{version}.exe"
        if args.verify_only:
            if incomplete.exists():
                raise RuntimeError("The last build did not finish successfully")
            artifacts.validate_build_record(bundle, identity)
            validate_outputs(bundle, setup, identity)
            check_versions(bundle, version)
            check_signatures(bundle, signing=False, version=version)
            if setup is not None:
                sign.verify_file(setup, expected_version=version)
            print("Artifact signatures, source identity, file hashes, and versions verified.")
            return 0

        if not args.no_sign:
            # An exact commit is needed for a release. Development builds may
            # use dirty sources, recorded by their content fingerprint.
            dirty = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=REPO, text=True,
            ).strip()
            if dirty:
                raise RuntimeError("Signed builds require a clean checkout; use --no-sign for development")
            sign.preflight()
        if setup is not None:
            installer.find_makensis()
        if args.skip_build:
            artifacts.validate_build_record(bundle, identity)
        DIST.mkdir(parents=True, exist_ok=True)
        incomplete.write_text("Build has not completed verification.\n", encoding="utf-8")
        if not args.skip_build:
            command = [sys.executable, "-m", "PyInstaller", "--noconfirm",
                       "--distpath", str(DIST), "--workpath", str(WORK)]
            if args.clean:
                command.append("--clean")
            run([*command, str(SPEC)])
            (bundle / ".offloader-install.lock").touch()
            shutil.copyfile(REPO / "LICENSE", bundle / "LICENSE")
            artifacts.write_build_record(bundle, identity)
        check_versions(bundle, version)
        signatures = []
        if not args.no_sign:
            signatures = check_signatures(bundle, signing=True, version=version)
        # The record describes the actual bytes that enter the installer,
        # including any new Authenticode signatures.
        artifacts.write_build_record(bundle, identity)
        if setup is not None:
            uninstaller_record = DIST / ".offloader-uninstaller-signature.json"
            if not args.no_sign:
                uninstaller_record.unlink(missing_ok=True)
            installer.build_installer(
                bundle, setup, version,
                sign_command=None if args.no_sign else [
                    sys.executable, str(HERE / "sign.py"), "sign", "--version", version,
                    "--record", str(uninstaller_record),
                ],
            )
            if not args.no_sign:
                record = json.loads(uninstaller_record.read_text(encoding="utf-8"))
                signatures.append({**record, "path": "Uninstall.exe"})
                sign.sign_file(setup)
                signatures.append({**sign.verify_file(setup, expected_version=version),
                                   "path": setup.name})
        run([sys.executable, str(HERE / "smoke.py"), str(bundle)])
        if artifacts.source_identity(REPO) != identity:
            raise RuntimeError("Sources changed while building; rebuild the candidate")
        artifacts.validate_build_record(bundle, identity)
        save_outputs(bundle, setup, identity, signatures, signed=not args.no_sign)
        incomplete.unlink()
        print(f"{'Unsigned development' if args.no_sign else 'Signed'} artifacts: {DIST}")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Windows build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
