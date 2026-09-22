"""Finding, verifying and applying a newer Windows release.

The shape follows Alpha-OSK's `src/updater.py`: GitHub Releases is the feed, so
there is no manifest server to run or keep honest; the downloaded installer is
checked against the same code-signing certificate that produced it; and nothing
is executed until every check has passed.

Two of its decisions are deliberately **not** copied, because Offloader's
guarantees differ:

* **Nothing is force-closed.** Alpha-OSK's installer terminates the running app
  and a helper process restarts it afterwards. Offloader's installer refuses
  maintenance while a copy is in flight and never force-kills — that is a
  data-safety promise in `docs/data-safety.md`, not an implementation detail.
  So this module verifies and hands over; it never tries to clear the way.
* **Prereleases are not rejected.** Alpha-OSK's updater only accepts `X.Y.Z`
  tags. Offloader's first packaged release is planned as `0.1.0b1`, so the same
  rule would make the updater blind to the very build it is shipped in. The
  grammar here is the one `build/windows/versioning.py` already enforces, and
  the ordering agrees with the Windows version fields that installer writes.

Everything fails closed. A feed that cannot be parsed, a version that cannot be
read, a digest that does not match, a signature from the wrong certificate: all
of them mean "no update", never "install it anyway".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

#: The certificate the installer is signed with, and the name it must present.
#: The same values as `build/windows/sign.py`, repeated rather than imported
#: because that module is a build script and is not shipped.
EXPECTED_CERT_THUMBPRINT = "FC22B5221318F3F3F6B3EB2D969D7F99091557BF"
EXPECTED_SIGNER_NAME = "OK Studio Inc."

#: How long a signature or version probe may take before it is a failure.
PROBE_TIMEOUT = 60

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


def check(installed: str = __version__, *, url: str = FEED_URL,
          fetch: Callable[[str], Any] = _fetch_json) -> Release | None:
    """Ask the feed for a newer release. Never raises.

    Called on a timer and from a menu item, where the cost of an exception is
    an interrupted app and the cost of returning None is one missed check.
    """
    try:
        return release_from_feed(fetch(url), installed=installed)
    except Exception:
        return None


def download(release: Release, directory: Path, *,
             opener: Callable[..., Any] = urllib.request.urlopen,
             progress: Callable[[int, int], None] | None = None) -> tuple[Path, str]:
    """Stream the installer into `directory`. Returns its path and SHA-256.

    Hashed while streaming rather than re-read afterwards, so the digest
    describes the bytes that were actually written.
    """
    _assert_allowed(release.download_url)
    target = Path(directory) / release.asset_name
    digest = hashlib.sha256()
    written = 0

    request = urllib.request.Request(
        release.download_url,
        headers={"User-Agent": f"Offloader/{__version__}"},
    )
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
            with open(target, "wb") as handle:
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
    except urllib.error.URLError as exc:
        raise UpdateError(f"could not download the installer: {exc}") from exc

    if written == 0:
        raise UpdateError("the installer download was empty")
    return target, digest.hexdigest()


def _powershell(script: str, *,
                run: Callable[..., subprocess.CompletedProcess[str]] | None = None
                ) -> dict[str, Any]:
    runner = run or subprocess.run
    result = runner(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False,
    )
    if result.returncode != 0:
        raise UpdateError("could not read the installer's signature")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise UpdateError("unreadable signature output") from exc
    return data if isinstance(data, dict) else {}


def inspect_installer(path: Path, *, run: Callable[..., Any] | None = None
                      ) -> dict[str, Any]:
    """The downloaded file's Authenticode status and embedded version."""
    literal = str(Path(path))
    script = (
        "$ErrorActionPreference='Stop';"
        f"$p='{literal}';"
        "$s=Get-AuthenticodeSignature -LiteralPath $p;"
        "$v=(Get-Item -LiteralPath $p).VersionInfo;"
        "[pscustomobject]@{"
        "Status=[string]$s.Status;"
        "Thumbprint=[string]$s.SignerCertificate.Thumbprint;"
        "Subject=[string]$s.SignerCertificate.Subject;"
        "TimestampThumbprint=[string]$s.TimeStamperCertificate.Thumbprint;"
        "FileVersion=[string]$v.FileVersion;"
        "ProductVersion=[string]$v.ProductVersion"
        "} | ConvertTo-Json -Compress"
    )
    return _powershell(script, run=run)


def _subject_name(subject: str) -> str:
    for part in subject.split(","):
        key, _, value = part.strip().partition("=")
        if key.upper() == "CN":
            return value.strip().strip('"')
    return ""


def verify(path: Path, release: Release, *,
           run: Callable[..., Any] | None = None,
           expected_digest: str | None = None,
           actual_digest: str | None = None) -> dict[str, Any]:
    """Refuse the download unless everything about it agrees.

    Four separate questions, because each one fails on its own:

    * the bytes are the ones that were hashed while streaming — closing the
      window between writing the file and reading it back;
    * Windows trusts the signature;
    * it is *our* certificate and our name, not merely a valid one;
    * the version compiled into the executable is the version the release
      claimed. Without this last check, anyone able to re-upload an asset could
      re-serve an older, still validly signed installer under a newer name and
      roll every install back onto a build whose bugs are already fixed.
    """
    if expected_digest is not None and actual_digest is not None:
        if expected_digest.lower() != actual_digest.lower():
            raise UpdateError("the installer changed after it was downloaded")

    record = inspect_installer(path, run=run)
    status = str(record.get("Status", ""))
    if status != "Valid":
        raise UpdateError(f"the installer's signature is {status or 'missing'}")

    thumbprint = str(record.get("Thumbprint", "")).replace(" ", "").upper()
    if thumbprint != EXPECTED_CERT_THUMBPRINT:
        raise UpdateError("the installer was signed by a different certificate")

    signer = _subject_name(str(record.get("Subject", "")))
    if signer != EXPECTED_SIGNER_NAME:
        raise UpdateError(f"the installer's publisher is {signer!r}")

    embedded = str(record.get("FileVersion", "")).strip()
    if parse_version(release.version) != parse_version(embedded.split("+")[0]):
        raise UpdateError(
            f"the installer reports version {embedded!r} but the release "
            f"claims {release.version!r}")
    return record


def install_target() -> Path:
    """Where an update should be installed.

    Computed from the running executable, never read from the registry. The
    registry value is writable by anything running as the user, so trusting it
    would let a planted key redirect an elevated silent install into a
    directory of someone else's choosing.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    base = os.environ.get("ProgramFiles") or r"C:\Program Files"
    return Path(base) / "Offloader"


def install_command(installer: Path, target: Path) -> tuple[str, str]:
    """The installer path and the argument string to elevate it with.

    `/D=` has two rules NSIS enforces silently: it must be the last argument,
    and it must not be quoted even when the path contains spaces. Quoting it
    installs into a directory with a quote in its name; putting anything after
    it is read as part of the path.
    """
    return str(installer), f"/S /D={target}"


def apply(installer: Path, target: Path | None = None, *,
          shell_execute: Callable[..., int] | None = None) -> None:
    """Run the verified installer elevated, silently.

    `ShellExecuteW` with `runas` rather than `subprocess`: the installer's
    manifest asks for elevation, and only the shell honours that with a UAC
    prompt — a plain spawn fails outright.

    This does not close the running app, and must not. Offloader's installer
    refuses maintenance while a transfer is in flight and exits non-zero
    instead; that refusal is the guarantee, so the caller's job is to ask the
    operator to finish, not to clear the way for it.
    """
    destination = install_target() if target is None else Path(target)
    path, arguments = install_command(Path(installer), destination)

    if shell_execute is None:                      # pragma: no cover - Windows
        import ctypes

        shell_execute = ctypes.windll.shell32.ShellExecuteW

    result = shell_execute(None, "runas", path, arguments, None, 1)
    # ShellExecuteW returns a fake HINSTANCE; anything of 32 or less is an
    # error code, and 5 is the one that matters — the UAC prompt was declined.
    if int(result) <= 32:
        if int(result) == 5:
            raise UpdateError("the update needs administrator approval")
        raise UpdateError(f"could not start the installer (code {int(result)})")
