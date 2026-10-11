"""Finding, verifying and applying a newer Windows release.

The shape follows Alpha-OSK's `src/updater.py`: GitHub Releases is the feed, so
there is no manifest server to run or keep honest; the downloaded installer is
checked against the same code-signing certificate that produced it; and nothing
is executed until every check has passed.

Two of its decisions are deliberately **not** copied, because Offloader's
guarantees differ:

* **Nothing is force-closed.** Alpha-OSK's installer terminates the running app
  and a helper process restarts it afterwards. Offloader's installer refuses
  maintenance while any Offloader program holds the installation open, and
  never force-kills. That is a data-safety promise in `docs/data-safety.md`,
  not an implementation detail.
  So this module verifies and hands over; it never tries to clear the way.
* **Prereleases are not rejected.** Alpha-OSK's updater only accepts `X.Y.Z`
  tags. Offloader's first packaged release is planned as `0.1.0b1`, so the same
  rule would make the updater blind to the very build it is shipped in. The
  grammar here is the one `build/windows/versioning.py` already enforces, and
  the ordering agrees with the Windows version fields that installer writes.

Everything fails closed. A version that cannot be read, a digest that does not
match, a signature from the wrong certificate: all of them mean "do not
install", never "install it anyway". A feed that cannot be fetched or parsed is
a failure to check (`find_release` raises), never "up to date".
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from ._version import __version__

#: Where releases are published. Pinned deliberately: this URL is compiled into
#: every shipped build, so moving it orphans every install that already exists.
#: Treat a rename or transfer of the repository as a breaking change.
#:
#: The collection rather than `/releases/latest`. GitHub documents that endpoint
#: as returning the newest published *full* release and excluding prereleases,
#: so an installed `0.1.0b1` could never see `0.1.0b2` through it, and a
#: repository holding only betas — which is what the candidate workflow's
#: `--prerelease` produces — would answer with nothing at all. Ordering
#: prereleases is the whole point of the grammar below, so the feed has to be
#: one that carries them.
FEED_URL = ("https://api.github.com/repos/owenpkent/offloader/releases"
            "?per_page=30")

#: The published installer's name. Part of the release contract — the asset has
#: to be identifiable without trusting anything else in the release.
ASSET_TEMPLATE = "Offloader-Setup-{version}.exe"

#: Hosts a download may come from, checked before *and* after redirects. GitHub
#: serves release assets off a separate domain and changes which one, so the
#: post-redirect check is the one that matters.
ALLOWED_HOSTS = frozenset({
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
})

#: A ceiling, not an estimate. Bounds what a compromised or confused feed can
#: make this write to disk.
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024

#: Where an operator is sent when this build cannot update itself.
RELEASES_PAGE = "https://github.com/owenpkent/offloader/releases"

#: The certificates an installer may be signed with, current one first, and
#: the name every one of them must present. The values come from
#: `build/windows/sign.py`, repeated rather than imported because that module
#: is a build script and is not shipped.
#:
#: A tuple so a renewed certificate can be rotated in without stranding the
#: field: ship a release that lists the next thumbprint *after* the current
#: one, wait for installs to take it, then sign with the new certificate and
#: move it to the front. A build that only knows one leaf certificate refuses
#: every release signed by its successor and can only be recovered by a
#: manual reinstall. See docs/updates.md.
EXPECTED_CERT_THUMBPRINTS: tuple[str, ...] = (
    "FC22B5221318F3F3F6B3EB2D969D7F99091557BF",
)
#: The certificate currently used for releases.
EXPECTED_CERT_THUMBPRINT = EXPECTED_CERT_THUMBPRINTS[0]
EXPECTED_SIGNER_NAME = "OK Studio Inc."

#: How long a signature or version probe may take before it is a failure.
PROBE_TIMEOUT = 60

#: Suffix of the download while it is incomplete or unverified. Nothing lands
#: under the installer's own name until the bytes are complete and signed.
PARTIAL_SUFFIX = ".offloader-partial"

#: Environment variable the signature probe reads the installer's path from.
#: The path never appears in the script text, so no character in it can end a
#: string literal or be read as PowerShell.
_PROBE_PATH_VARIABLE = "OFFLOADER_UPDATE_INSTALLER"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
_TAG_RE = re.compile(r"^v?(.+)$")

#: Alpha before beta before release candidate before the final release. Mirrors
#: the disjoint ranges `versioning.windows_version` reserves, so a late alpha
#: never sorts above the first beta here either.
_STAGE_ORDER = {"a": 0, "b": 1, "rc": 2, None: 3}


class UpdateError(RuntimeError):
    """An update was found but could not be trusted or applied."""


@dataclass(frozen=True)
class Release:
    """A candidate release, as far as the feed describes it."""

    version: str
    asset_name: str
    download_url: str
    notes: str = ""


def parse_version(value: str) -> tuple[int, int, int, int, int] | None:
    """`0.2.0b3` as an orderable tuple, or None if it is not a version.

    None rather than an exception: an unreadable version is a reason to decline
    an update, and every caller here treats it that way.
    """
    match = _VERSION_RE.match(value.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.group(1, 2, 3))
    stage = match.group(4)
    return (major, minor, patch, _STAGE_ORDER[stage], int(match.group(5) or 0))


def is_newer(candidate: str, installed: str) -> bool:
    """Whether `candidate` should replace `installed`.

    Fails closed. If either side is unreadable the answer is no, because the
    alternative is deciding an upgrade path from a string nobody can order.
    """
    left, right = parse_version(candidate), parse_version(installed)
    if left is None or right is None:
        return False
    return left > right


def _assert_allowed(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise UpdateError(f"refusing a non-HTTPS download: {parsed.scheme}://")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise UpdateError(f"refusing a download from {parsed.hostname!r}")


def _fetch_json(url: str, *, timeout: int = 30) -> Any:
    _assert_allowed(url)
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": f"Offloader/{__version__}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        _assert_allowed(response.geturl())
        return json.loads(response.read().decode("utf-8"))


def release_from_feed(payload: Any, *, installed: str = __version__) -> Release | None:
    """The best newer release the feed describes, or None.

    Takes the releases collection, or a single release object for a caller that
    already has one. The greatest eligible version wins rather than whichever
    the feed happens to list first: GitHub orders by creation date, and a
    patched `0.1.0b2` published after `0.1.0rc1` would otherwise be offered as
    the upgrade from it.
    """
    if isinstance(payload, list):
        candidates = [_release_entry(item, installed=installed) for item in payload]
        ranked = [(parse_version(found.version), found)
                  for found in candidates if found is not None]
        if not ranked:
            return None
        return max(ranked, key=lambda pair: pair[0])[1]
    return _release_entry(payload, installed=installed)


def _eligible(version: str, installed: str) -> bool:
    """Whether an installation on `installed` should be offered `version`.

    Newer, and not a step off the channel this install is already on. A build
    that is itself a prerelease is testing the prereleases, so it takes the
    next one; a stable install is not volunteered for a beta it did not ask
    for. Neither side being readable means no, as everywhere else here.
    """
    if not is_newer(version, installed):
        return False
    running, offered = parse_version(installed), parse_version(version)
    if running is None or offered is None:
        return False
    stable = _STAGE_ORDER[None]
    return running[3] != stable or offered[3] == stable


def _release_entry(payload: Any, *, installed: str) -> Release | None:
    """One release from the feed, if it is one this install should be offered.

    Reads only what it needs, and requires the asset to be named for the
    version the tag claims: an extra or renamed file in a release cannot then
    be mistaken for the installer.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("draft"):
        # A draft is visible to anyone who can write to the repository and its
        # assets are not published. Offering one would hand an installer to the
        # maintainer's own machine before the release exists for anybody else.
        return None
    tag = payload.get("tag_name")
    if not isinstance(tag, str):
        return None
    tag_match = _TAG_RE.match(tag.strip())
    if tag_match is None:
        return None
    version = tag_match.group(1)
    if parse_version(version) is None or not _eligible(version, installed):
        return None

    expected = ASSET_TEMPLATE.format(version=version)
    assets = payload.get("assets")
    if not isinstance(assets, list):
        return None
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != expected:
            continue
        url = asset.get("browser_download_url")
        if not isinstance(url, str):
            return None
        notes = payload.get("body")
        return Release(version=version, asset_name=expected, download_url=url,
                       notes=notes if isinstance(notes, str) else "")
    return None


def find_release(installed: str = __version__, *, url: str = FEED_URL,
                 fetch: Callable[[str], Any] = _fetch_json) -> Release | None:
    """Ask the feed for a newer release, raising if the feed cannot be read.

    None means the feed was read and holds nothing newer for this install.
    Every failure to get that answer (offline, an HTTP error or rate limit, a
    redirect off the allowlist, a body that is not the expected JSON) raises
    `UpdateError` instead, because reporting it as "up to date" would let a
    broken proxy or a network attacker suppress updates without anyone
    noticing.
    """
    try:
        payload = fetch(url)
    except UpdateError as exc:
        raise UpdateError(f"the release feed was refused: {exc}") from exc
    except urllib.error.HTTPError as exc:
        hint = (" (GitHub's rate limit for unauthenticated requests may have "
                "been reached; try again later)"
                if exc.code in (403, 429) else "")
        raise UpdateError(
            f"the release feed answered HTTP {exc.code}{hint}") from exc
    except urllib.error.URLError as exc:
        raise UpdateError(
            f"could not reach the release feed: {exc.reason}") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError("the release feed was not readable JSON") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise UpdateError(f"could not read the release feed: {exc}") from exc
    if not isinstance(payload, (list, dict)):
        raise UpdateError("the release feed was not a list of releases")
    return release_from_feed(payload, installed=installed)


def check(installed: str = __version__, *, url: str = FEED_URL,
          fetch: Callable[[str], Any] = _fetch_json) -> Release | None:
    """Ask the feed for a newer release. Never raises.

    For a background caller (a timer or a menu item), where the cost of an
    exception is an interrupted app and the cost of returning None is one
    missed check. None here does not mean "up to date": anything that has to
    tell the operator so, like the `update` command, uses `find_release`.
    """
    try:
        return find_release(installed, url=url, fetch=fetch)
    except Exception:
        return None


def is_link(path: Path) -> bool:
    """Whether `path` is a symlink, a junction or any other reparse point.

    False when nothing exists there. A junction is not a symlink to
    `os.path.islink`, so the reparse attribute is checked as well.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def discard(path: Path) -> None:
    """Delete a staging file this module created, never following a link."""
    try:
        if not is_link(path) and path.is_file():
            path.unlink()
    except OSError:
        pass


def file_sha256(path: Path) -> str:
    """SHA-256 of the file as it is on disk now."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _assert_unchanged(path: Path, expected_digest: str, when: str) -> None:
    try:
        actual = file_sha256(path)
    except OSError as exc:
        raise UpdateError(f"could not re-read {path}: {exc}") from exc
    if actual.lower() != expected_digest.lower():
        raise UpdateError(f"the installer changed after it was downloaded "
                          f"({when}): {path}")


def download(release: Release, directory: Path, *,
             opener: Callable[..., Any] = urllib.request.urlopen,
             progress: Callable[[int, int], None] | None = None) -> tuple[Path, str]:
    """Stream the installer into `directory`. Returns the staged path and SHA-256.

    Hashed while streaming rather than re-read afterwards, so the digest
    describes the bytes that were actually written.

    The bytes go to `<asset>.offloader-partial`, created exclusively so an
    existing file or a planted link at that name is never written through, and
    flushed to disk before returning. They stay under that name until
    `promote` is called after verification, so an interrupted or refused
    download never leaves something under the installer's own name that a
    person could double-click. Any failure deletes the partial file.
    """
    _assert_allowed(release.download_url)
    directory = Path(directory)
    if is_link(directory):
        raise UpdateError(f"refusing a download directory that is a link or "
                          f"junction: {directory}")
    if not directory.is_dir():
        raise UpdateError(f"the download directory does not exist: {directory}")
    final = directory / release.asset_name
    staged = directory / (release.asset_name + PARTIAL_SUFFIX)
    for path in (final, staged):
        if is_link(path):
            raise UpdateError(f"refusing to download over a link: {path}")
    # A partial file of ours left by an interrupted earlier run.
    discard(staged)

    digest = hashlib.sha256()
    written = 0
    total = 0
    request = urllib.request.Request(
        release.download_url,
        headers={"User-Agent": f"Offloader/{__version__}"},
    )
    created = False
    try:
        with opener(request, timeout=60) as response:
            # The redirect is where the host can change, so this is the check
            # that counts; the pre-flight above only rejects an obvious feed.
            _assert_allowed(response.geturl())
            declared = response.headers.get("Content-Length")
            total = int(declared) if declared and declared.isdigit() else 0
            if total > MAX_DOWNLOAD_BYTES:
                raise UpdateError(
                    f"refusing a {total} byte download; the ceiling is "
                    f"{MAX_DOWNLOAD_BYTES}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            descriptor = os.open(staged, flags, 0o600)
            created = True
            with os.fdopen(descriptor, "wb") as handle:
                while True:
                    block = response.read(1 << 20)
                    if not block:
                        break
                    written += len(block)
                    if written > MAX_DOWNLOAD_BYTES:
                        raise UpdateError(
                            "download exceeded the size ceiling mid-stream")
                    digest.update(block)
                    handle.write(block)
                    if progress is not None:
                        progress(written, total)
                handle.flush()
                os.fsync(handle.fileno())
        if written == 0:
            raise UpdateError("the installer download was empty")
        if total and written != total:
            raise UpdateError(f"the download stopped at {written} of {total} "
                              f"bytes")
    except BaseException as exc:
        if created:
            discard(staged)
        if isinstance(exc, UpdateError):
            raise
        if isinstance(exc, urllib.error.URLError):
            raise UpdateError(
                f"could not download the installer: {exc}") from exc
        if isinstance(exc, (OSError, http.client.HTTPException)):
            raise UpdateError(
                f"could not download the installer: {exc}") from exc
        raise
    return staged, digest.hexdigest()


def promote(staged: Path) -> Path:
    """Give a verified download its installer name. Returns the final path.

    Called only after `verify` has accepted the staged bytes. Refuses to
    replace a link at the final name; a plain file of that name (an earlier
    download into the same `--dir`) is replaced atomically.
    """
    staged = Path(staged)
    if not staged.name.endswith(PARTIAL_SUFFIX):
        raise UpdateError(f"not a staged download: {staged}")
    final = staged.with_name(staged.name[:-len(PARTIAL_SUFFIX)])
    if is_link(staged) or is_link(final):
        raise UpdateError(f"refusing to promote over a link: {final}")
    try:
        os.replace(staged, final)
    except OSError as exc:
        raise UpdateError(f"could not rename the verified download: {exc}") from exc
    return final


def system_root() -> Path:
    """The Windows directory, from `%SystemRoot%`, which must be absolute."""
    value = os.environ.get("SystemRoot") or ""
    root = Path(value)
    if not value or not root.is_absolute():
        raise UpdateError("cannot locate Windows: %SystemRoot% is not an "
                          "absolute path")
    return root


def powershell_executable() -> Path:
    """Windows PowerShell by absolute path, never by a search of PATH.

    A bare `powershell` is resolved by the Windows search order, which looks
    in the application's directory and the current directory before System32.
    The CLI is routinely run from a card's root, so a `powershell.exe` planted
    there would otherwise both run and decide the signature verdict.
    """
    return (system_root() / "System32" / "WindowsPowerShell" / "v1.0"
            / "powershell.exe")


def _powershell(script: str, *,
                variables: dict[str, str] | None = None,
                run: Callable[..., subprocess.CompletedProcess[str]] | None = None
                ) -> dict[str, Any]:
    runner = run or subprocess.run
    environment = dict(os.environ)
    environment.update(variables or {})
    try:
        result = runner(
            [str(powershell_executable()), "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False,
            # Not the operator's current directory, which may be untrusted
            # removable media.
            cwd=str(system_root()), env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"could not read the installer's signature: {exc}") from exc
    if result.returncode != 0:
        raise UpdateError("could not read the installer's signature")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise UpdateError("unreadable signature output") from exc
    return data if isinstance(data, dict) else {}


#: Constant script text. The path arrives through `_PROBE_PATH_VARIABLE`, and
#: the modules the verdict depends on are loaded from `$PSHOME` explicitly and
#: called by qualified name, so a same-named module earlier on the user's
#: `PSModulePath` is not consulted.
_PROBE_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "foreach($m in 'Microsoft.PowerShell.Security','Microsoft.PowerShell.Utility')"
    "{Import-Module -Name (Join-Path $PSHOME ('Modules\\'+$m+'\\'+$m+'.psd1'))};"
    f"$p=$env:{_PROBE_PATH_VARIABLE};"
    "$s=Microsoft.PowerShell.Security\\Get-AuthenticodeSignature -LiteralPath $p;"
    "$v=[System.Diagnostics.FileVersionInfo]::GetVersionInfo($p);"
    "[pscustomobject]@{"
    "Status=[string]$s.Status;"
    "Thumbprint=[string]$s.SignerCertificate.Thumbprint;"
    "Subject=[string]$s.SignerCertificate.Subject;"
    "TimestampThumbprint=[string]$s.TimeStamperCertificate.Thumbprint;"
    "FileVersion=[string]$v.FileVersion;"
    "ProductVersion=[string]$v.ProductVersion"
    "} | Microsoft.PowerShell.Utility\\ConvertTo-Json -Compress"
)


def inspect_installer(path: Path, *, run: Callable[..., Any] | None = None
                      ) -> dict[str, Any]:
    """The downloaded file's Authenticode status and embedded version.

    The path is handed over in an environment variable rather than written
    into the script, so an apostrophe, `$` or backtick in it is just a
    character of the path.
    """
    absolute = str(Path(path).absolute())
    return _powershell(_PROBE_SCRIPT,
                       variables={_PROBE_PATH_VARIABLE: absolute}, run=run)


def _subject_name(subject: str) -> str:
    for part in subject.split(","):
        key, _, value = part.strip().partition("=")
        if key.upper() == "CN":
            return value.strip().strip('"')
    return ""




def verify(path: Path, release: Release, *,
           run: Callable[..., Any] | None = None,
           expected_digest: str | None = None) -> dict[str, Any]:
    """Refuse the download unless everything about it agrees.

    Four separate questions, because each one fails on its own:

    * the bytes on disk are the ones that were hashed while streaming. The
      file is re-hashed before the signature is read and again afterwards, so
      what Authenticode inspected is what was downloaded;
    * Windows trusts the signature;
    * it is *our* certificate and our name, not merely a valid one;
    * the version compiled into the executable is the version the release
      claimed. Without this last check, anyone able to re-upload an asset could
      re-serve an older, still validly signed installer under a newer name and
      roll every install back onto a build whose bugs are already fixed.
    """
    if expected_digest is not None:
        _assert_unchanged(path, expected_digest, "before its signature was read")

    record = inspect_installer(path, run=run)
    status = str(record.get("Status", ""))
    if status != "Valid":
        raise UpdateError(f"the installer's signature is {status or 'missing'}")

    thumbprint = str(record.get("Thumbprint", "")).replace(" ", "").upper()
    accepted = {value.replace(" ", "").upper()
                for value in EXPECTED_CERT_THUMBPRINTS}
    if not thumbprint or thumbprint not in accepted:
        raise UpdateError("the installer was signed by a different certificate")

    signer = _subject_name(str(record.get("Subject", "")))
    if signer != EXPECTED_SIGNER_NAME:
        raise UpdateError(f"the installer's publisher is {signer!r}")

    embedded = str(record.get("FileVersion", "")).strip()
    if parse_version(release.version) != parse_version(embedded.split("+")[0]):
        raise UpdateError(
            f"the installer reports version {embedded!r} but the release "
            f"claims {release.version!r}")

    if expected_digest is not None:
        _assert_unchanged(path, expected_digest, "while its signature was read")
    return record


def install_target() -> Path:
    """The installation this process belongs to, which an update replaces.

    Only an installed packaged build can update itself: a frozen executable
    whose directory carries the installer's `.offloader-install.json`
    manifest. Anything else is refused with a message naming what was found,
    rather than guessing a directory:

    * a pip install or a source checkout is not what the installer replaces,
      and guessing `Program Files` would install a second copy beside the
      registered one (or into `Program Files (x86)` under a 32-bit Python);
    * a portable bundle has no manifest, so the installer's maintenance step
      would refuse that directory every time.

    The target is the running executable's directory rather than the
    `InstallLocation` registry value: that key is under HKLM and only an
    administrator can write it, but it records where the *last* install went,
    which is not necessarily the copy that is running. Known limit: when this
    directory equals the installer's compiled default, the installer cannot
    tell `/D=` from no `/D=` and uses the registered location instead (see
    `installer.nsi`'s `.onInit`).
    """
    if not getattr(sys, "frozen", False):
        raise UpdateError(
            "this Offloader runs from a Python installation (pip or a source "
            "checkout), not the installed Windows app, so it cannot install "
            "the release itself. Update it the way it was installed, for "
            "example `python -m pip install --upgrade` or `git pull`, or "
            f"download the installer from {RELEASES_PAGE}")
    from .installation import MANIFEST_NAME, InstallationError, _read_manifest

    directory = Path(sys.executable).resolve().parent
    try:
        manifest = _read_manifest(directory)
    except (InstallationError, OSError):
        manifest = None
    if manifest is None:
        raise UpdateError(
            f"this Offloader is a portable bundle in {directory} (there is no "
            f"readable {MANIFEST_NAME} beside it), so the installer cannot "
            f"replace it. Download Offloader-Setup from {RELEASES_PAGE} and "
            "run it, or replace the portable folder by hand")
    return directory


def runs_from(target: Path) -> bool:
    """Whether this process's executable lives inside `target`.

    Such a process cannot wait for the installer: it holds the installation's
    shared lock and its own files open for as long as it runs, so the
    installer's maintenance step would refuse, or fail to remove the old
    executable after committing.
    """
    try:
        executable = Path(sys.executable).resolve()
        executable.relative_to(Path(target).resolve())
    except (OSError, ValueError):
        return False
    return True


def install_command(installer: Path, target: Path) -> tuple[str, str]:
    """The installer path and the argument string to elevate it with.

    `/D=` has two rules NSIS enforces silently: it must be the last argument,
    and it must not be quoted even when the path contains spaces. Quoting it
    installs into a directory with a quote in its name; putting anything after
    it is read as part of the path.
    """
    return str(installer), f"/S /D={target}"


class ShellResult(NamedTuple):
    """What `ShellExecuteExW` reported.

    `error` is the Win32 error when the launch failed, otherwise 0.
    `exit_code` is the installer's exit code, or None when it was not waited
    for or no process handle came back.
    """

    error: int
    exit_code: int | None


#: `ShellExecuteExW` failing because the UAC prompt was declined.
_ERROR_CANCELLED = 1223


def _shell_execute_ex(verb: str, path: str, arguments: str, directory: str,
                      wait: bool) -> ShellResult:  # pragma: no cover - Windows
    """`ShellExecuteExW` with `SEE_MASK_NOCLOSEPROCESS`, optionally waiting."""
    import ctypes
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    see_mask_nocloseprocess, see_mask_noasync = 0x40, 0x100
    wait_object_0, wait_timeout = 0, 0x102
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = see_mask_nocloseprocess | see_mask_noasync
    info.lpVerb, info.lpFile = verb, path
    info.lpParameters, info.lpDirectory = arguments, directory
    info.nShow = 1
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return ShellResult(ctypes.get_last_error() or -1, None)
    if not info.hProcess:
        return ShellResult(0, None)
    try:
        if not wait:
            return ShellResult(0, None)
        while True:
            # Short slices rather than INFINITE so Ctrl+C is still delivered.
            state = kernel32.WaitForSingleObject(info.hProcess, 500)
            if state == wait_object_0:
                break
            if state != wait_timeout:
                return ShellResult(0, None)
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            return ShellResult(0, None)
        return ShellResult(0, int(code.value))
    finally:
        kernel32.CloseHandle(info.hProcess)


def apply(installer: Path, target: Path | None = None, *,
          expected_digest: str,
          wait: bool = True,
          shell_execute: Callable[..., ShellResult] | None = None) -> bool:
    """Run the verified installer elevated, silently.

    Returns True when the installer was waited for and exited 0, False when it
    was started but its outcome could not be observed (`wait=False`, or no
    process handle came back). Raises `UpdateError` when it could not start,
    when the file changed since verification, or when it exited non-zero.

    The file is re-hashed immediately before launch and compared with the
    digest that verification accepted; a mismatch refuses to run it.

    `ShellExecuteExW` with `runas` rather than `subprocess`: the installer's
    manifest asks for elevation, and only the shell honours that with a UAC
    prompt; a plain spawn fails outright. It runs in the installer's own
    directory, not the operator's current one.

    This does not close the running app, and must not. Offloader's installer
    refuses maintenance while any Offloader program holds the installation
    open and exits non-zero instead. Silent mode hides its own message, so
    that exit code is the only report of the refusal, and it is surfaced here.
    """
    installer = Path(installer).absolute()
    destination = install_target() if target is None else Path(target)
    path, arguments = install_command(installer, destination)

    _assert_unchanged(installer, expected_digest, "before it was launched")

    launch = shell_execute or _shell_execute_ex
    result = launch("runas", path, arguments, str(installer.parent), wait)
    if result.error:
        if result.error == _ERROR_CANCELLED:
            raise UpdateError("the update needs administrator approval")
        raise UpdateError(f"could not start the installer (code {result.error})")
    if result.exit_code is None:
        return False
    if result.exit_code != 0:
        raise UpdateError(
            f"the installer refused or failed (exit code {result.exit_code}). "
            "Silent mode hides its own message; the usual cause is an "
            "Offloader window or CLI transfer still running. Nothing was "
            "force-closed; close Offloader and run the update again")
    return True
