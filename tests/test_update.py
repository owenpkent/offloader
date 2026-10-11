"""Finding, verifying and applying a newer release.

Every test here is about refusing something. An updater is a mechanism for
running code that arrived over the network on the operator's machine, with
elevation, so the interesting behaviour is not the happy path but each way the
happy path is declined: an unreadable version, an asset that is not the one the
release claims, a host the redirect moved to, a signature from somebody else,
and an installer whose embedded version does not match the release it came
from.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from offloader import cli, update
from offloader.update import Release, ShellResult, UpdateError

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _system_root(monkeypatch):
    """An absolute `%SystemRoot%` on every platform. Nothing is ever run from
    it: every probe in this file goes through an injected or patched seam."""
    monkeypatch.setenv("SystemRoot", str(REPO))


def _versioning():
    """`build/windows/versioning.py`, which is a build script, not a module."""
    path = REPO / "build" / "windows" / "versioning.py"
    spec = importlib.util.spec_from_file_location("_versioning", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _signing():
    """`build/windows/sign.py`, loaded the same way."""
    path = REPO / "build" / "windows" / "sign.py"
    spec = importlib.util.spec_from_file_location("_signing_for_update", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def _feed(tag: str, *, assets: list[dict] | None = None, body: str = "") -> dict:
    version = tag.lstrip("v")
    if assets is None:
        assets = [{
            "name": f"Offloader-Setup-{version}.exe",
            "browser_download_url":
                f"https://github.com/owenpkent/offloader/releases/download/"
                f"{tag}/Offloader-Setup-{version}.exe",
        }]
    return {"tag_name": tag, "assets": assets, "body": body}


class _Response:
    """The parts of an HTTP response the download path touches."""

    def __init__(self, body: bytes, *, url: str, length: str | None = None):
        self._body = body
        self._at = 0
        self._url = url
        self.headers = {} if length is None else {"Content-Length": length}

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._body) - self._at
        block = self._body[self._at:self._at + size]
        self._at += len(block)
        return block

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _opener(body: bytes, *, url: str, length: str | None = None):
    def open_it(_request, timeout=None):
        return _Response(body, url=url, length=length)
    return open_it


def _signature(**overrides) -> dict:
    record = {
        "Status": "Valid",
        "Thumbprint": update.EXPECTED_CERT_THUMBPRINT,
        "Subject": f"CN={update.EXPECTED_SIGNER_NAME}, O=OK Studio Inc., C=US",
        "TimestampThumbprint": "B" * 40,
        "FileVersion": "0.2.0",
        "ProductVersion": "0.2.0",
    }
    record.update(overrides)
    return record


def _runner(record: dict, *, returncode: int = 0):
    def run(_command, **_kwargs):
        return subprocess.CompletedProcess(
            _command, returncode, stdout=json.dumps(record), stderr="")
    return run


# ------------------------------------------------------------------- versions


@pytest.mark.parametrize("value", [
    "0.1.0", "1.2.3", "10.0.0", "0.1.0a1", "0.1.0b2", "0.1.0rc9",
])
def test_release_versions_parse(value: str):
    assert update.parse_version(value) is not None


@pytest.mark.parametrize("value", [
    "", "1.2", "1.2.3.4", "v1.2.3", "1.2.3-beta", "1.2.3dev1", "latest",
    "1.2.3+local", "one.two.three",
])
def test_anything_else_does_not_parse(value: str):
    """Fails closed. A version nobody can order is not a version to upgrade
    to, and string comparison on these is how `1.0.10` ends up older than
    `1.0.9`."""
    assert update.parse_version(value) is None


def test_prereleases_are_ordered_before_the_release():
    """The deliberate difference from the reference implementation, which
    rejects any tag that is not X.Y.Z. Offloader's first packaged release is
    planned as a beta, so that rule would make this updater blind to it."""
    assert update.is_newer("0.1.0", "0.1.0rc1")
    assert update.is_newer("0.1.0rc1", "0.1.0b1")
    assert update.is_newer("0.1.0b1", "0.1.0a1")
    assert not update.is_newer("0.1.0a9", "0.1.0b1")


def test_a_late_alpha_does_not_outrank_a_first_beta():
    """The invariant `versioning.py` reserves disjoint numeric ranges for. It
    has to hold in both places or the updater and the installer disagree about
    which build is newer."""
    assert not update.is_newer("0.1.0a999", "0.1.0b1")


def test_the_ordering_agrees_with_the_windows_version_fields():
    """Two implementations of the same ordering: this module compares tuples,
    and the installer writes four 16-bit fields. If they ever disagree, an
    update can install a build that Windows then considers older than the one
    it replaced."""
    windows_version = _versioning().windows_version
    ordered = ["0.1.0a1", "0.1.0a2", "0.1.0b1", "0.1.0rc1", "0.1.0",
               "0.1.1a1", "0.1.1", "0.2.0", "1.0.0"]
    for lower, higher in zip(ordered, ordered[1:], strict=False):
        assert update.is_newer(higher, lower), f"{higher} !> {lower}"
        assert windows_version(higher) > windows_version(lower), \
            f"windows fields disagree: {higher} !> {lower}"


def test_an_unreadable_version_on_either_side_declines():
    assert not update.is_newer("nonsense", "0.1.0")
    assert not update.is_newer("0.2.0", "nonsense")


# ---------------------------------------------------------------------- feed


def test_a_newer_release_is_reported():
    release = update.release_from_feed(_feed("v0.2.0"), installed="0.1.0")
    assert release is not None
    assert release.version == "0.2.0"
    assert release.asset_name == "Offloader-Setup-0.2.0.exe"


def test_a_tag_without_the_v_prefix_is_accepted():
    release = update.release_from_feed(_feed("0.2.0"), installed="0.1.0")
    assert release is not None and release.version == "0.2.0"


def test_a_prerelease_tag_is_accepted():
    release = update.release_from_feed(_feed("v0.1.0b1"), installed="0.1.0a1")
    assert release is not None and release.version == "0.1.0b1"


@pytest.mark.parametrize("tag", ["v0.1.0", "v0.0.9"])
def test_the_same_or_an_older_release_is_not_an_update(tag: str):
    assert update.release_from_feed(_feed(tag), installed="0.1.0") is None


def test_a_tag_that_is_not_a_version_is_refused():
    """`latest` or `v1.0.3-evil` must not be string-compared into an upgrade."""
    assert update.release_from_feed(_feed("v1.0.3-evil"),
                                    installed="0.1.0") is None


def test_an_asset_named_for_a_different_version_is_not_the_installer():
    """The asset has to be identifiable without trusting the rest of the
    release, or a file attached beside the real one can be served instead."""
    payload = _feed("v0.2.0", assets=[{
        "name": "Offloader-Setup-0.1.0.exe",
        "browser_download_url": "https://github.com/x/y/z.exe",
    }])
    assert update.release_from_feed(payload, installed="0.1.0") is None


def test_an_extra_asset_does_not_confuse_the_match():
    payload = _feed("v0.2.0", assets=[
        {"name": "checksums.txt",
         "browser_download_url": "https://github.com/a/b/checksums.txt"},
        {"name": "Offloader-Setup-0.2.0.exe",
         "browser_download_url": "https://github.com/a/b/setup.exe"},
    ])
    release = update.release_from_feed(payload, installed="0.1.0")
    assert release is not None and release.download_url.endswith("setup.exe")


@pytest.mark.parametrize("payload", [
    None, [], "release", {}, {"tag_name": 2}, {"tag_name": "v0.2.0"},
    {"tag_name": "v0.2.0", "assets": "none"},
])
def test_a_malformed_feed_is_not_an_update(payload):
    assert update.release_from_feed(payload, installed="0.1.0") is None


# ------------------------------------------------------- the whole collection


def _collection() -> list[dict]:
    """What the releases endpoint actually returns: newest created first, betas
    and release candidates among them, and a draft nobody else can see yet."""
    return [
        dict(_feed("v0.2.0b1"), draft=True, prerelease=True),
        dict(_feed("v0.1.0b2"), prerelease=True),
        dict(_feed("v0.1.0rc1"), prerelease=True),
        dict(_feed("v0.1.0b1"), prerelease=True),
        dict(_feed("v0.0.9"), prerelease=False),
    ]


def test_a_beta_finds_the_next_prerelease_in_the_collection():
    """REGRESSION. `/releases/latest` excludes prereleases, so a shipped
    `0.1.0b1` could not see `0.1.0b2` or `0.1.0rc1` through it, and a
    repository holding only betas answered with nothing at all."""
    release = update.release_from_feed(_collection(), installed="0.1.0b1")
    assert release is not None and release.version == "0.1.0rc1"


def test_the_greatest_version_wins_not_the_one_listed_first():
    """The feed is ordered by creation date, so a patched `0.1.0b2` published
    after `0.1.0rc1` sits above it in the list without being above it."""
    release = update.release_from_feed(_collection(), installed="0.1.0a1")
    assert release is not None and release.version == "0.1.0rc1"


def test_a_draft_release_is_not_offered():
    """Drafts are visible to anyone who can write to the repository, and their
    assets are not published."""
    feed = [dict(_feed("v0.3.0"), draft=True)]
    assert update.release_from_feed(feed, installed="0.1.0") is None


def test_a_stable_install_is_not_offered_a_prerelease():
    """A build that is itself a prerelease is testing them. One that is not did
    not volunteer for the next beta."""
    feed = [dict(_feed("v0.2.0b1"), prerelease=True),
            dict(_feed("v0.1.0"), prerelease=False)]
    assert update.release_from_feed(feed, installed="0.1.0") is None

    feed.append(dict(_feed("v0.2.0"), prerelease=False))
    release = update.release_from_feed(feed, installed="0.1.0")
    assert release is not None and release.version == "0.2.0"


def test_a_prerelease_install_still_takes_the_stable_release():
    feed = [dict(_feed("v0.1.0b2"), prerelease=True),
            dict(_feed("v0.1.0"), prerelease=False)]
    release = update.release_from_feed(feed, installed="0.1.0b1")
    assert release is not None and release.version == "0.1.0"


def test_a_collection_with_nothing_newer_is_not_an_update():
    assert update.release_from_feed(_collection(), installed="0.2.0") is None


def test_an_unusable_entry_does_not_hide_the_rest_of_the_collection():
    """One release with a renamed asset or an unreadable tag is skipped, not a
    reason to decline every other release in the feed."""
    feed = [
        {"tag_name": "latest", "assets": []},
        dict(_feed("v0.2.0"), assets=[{"name": "Offloader-Setup-0.1.0.exe",
                                       "browser_download_url": "https://x/y"}]),
        dict(_feed("v0.1.5")),
    ]
    release = update.release_from_feed(feed, installed="0.1.0")
    assert release is not None and release.version == "0.1.5"


def test_the_feed_url_is_the_collection_not_the_latest_endpoint():
    """`/releases/latest` is documented as excluding prereleases, which is the
    only kind of release this project has so far."""
    assert not update.FEED_URL.endswith("/releases/latest")
    assert "/releases" in update.FEED_URL


def test_check_never_raises():
    """It runs on a timer inside a desktop app. An exception here would take
    the app down over a failed DNS lookup."""
    def explode(_url):
        raise OSError("no network")

    assert update.check("0.1.0", fetch=explode) is None


# ------------------------------------------------------------------ download


@pytest.mark.parametrize("url", [
    "http://github.com/a/b.exe",
    "https://evil.example.com/a/b.exe",
    "https://github.com.evil.example/a/b.exe",
    "file:///C:/windows/system32/calc.exe",
])
def test_a_download_from_the_wrong_place_is_refused(url: str, tmp_path: Path):
    release = Release(version="0.2.0", asset_name="Offloader-Setup-0.2.0.exe",
                      download_url=url)
    with pytest.raises(UpdateError):
        update.download(release, tmp_path)


def test_a_redirect_to_another_host_is_refused(tmp_path: Path):
    """The pre-flight check only sees the feed's URL. GitHub redirects asset
    downloads to a different domain, so the host that actually serves the bytes
    is the one that has to be checked."""
    release = Release(version="0.2.0", asset_name="Offloader-Setup-0.2.0.exe",
                      download_url="https://github.com/a/b/setup.exe")
    opener = _opener(b"MZ", url="https://evil.example.com/setup.exe")

    with pytest.raises(UpdateError, match="evil.example.com"):
        update.download(release, tmp_path, opener=opener)


def test_a_declared_size_over_the_ceiling_is_refused(tmp_path: Path):
    release = Release(version="0.2.0", asset_name="Offloader-Setup-0.2.0.exe",
                      download_url="https://github.com/a/b/setup.exe")
    opener = _opener(b"MZ", url="https://github.com/a/b/setup.exe",
                     length=str(update.MAX_DOWNLOAD_BYTES + 1))

    with pytest.raises(UpdateError, match="ceiling"):
        update.download(release, tmp_path, opener=opener)


def test_a_body_over_the_ceiling_is_refused_mid_stream(tmp_path: Path,
                                                       monkeypatch):
    """A truthful Content-Length is not required of a hostile server, so the
    cap has to hold while the bytes are arriving too."""
    monkeypatch.setattr(update, "MAX_DOWNLOAD_BYTES", 1024)
    release = Release(version="0.2.0", asset_name="Offloader-Setup-0.2.0.exe",
                      download_url="https://github.com/a/b/setup.exe")
    opener = _opener(b"x" * 4096, url="https://github.com/a/b/setup.exe")

    with pytest.raises(UpdateError, match="ceiling"):
        update.download(release, tmp_path, opener=opener)


def test_the_digest_describes_the_bytes_that_were_written(tmp_path: Path):
    import hashlib

    body = b"installer bytes" * 1000
    release = Release(version="0.2.0", asset_name="Offloader-Setup-0.2.0.exe",
                      download_url="https://github.com/a/b/setup.exe")
    opener = _opener(body, url="https://objects.githubusercontent.com/x")

    path, digest = update.download(release, tmp_path, opener=opener)

    assert path.read_bytes() == body
    assert digest == hashlib.sha256(body).hexdigest()


def test_an_empty_download_is_refused(tmp_path: Path):
    release = Release(version="0.2.0", asset_name="Offloader-Setup-0.2.0.exe",
                      download_url="https://github.com/a/b/setup.exe")
    opener = _opener(b"", url="https://github.com/a/b/setup.exe")

    with pytest.raises(UpdateError, match="empty"):
        update.download(release, tmp_path, opener=opener)


def test_progress_is_reported_while_downloading(tmp_path: Path):
    body = b"y" * (3 << 20)
    release = Release(version="0.2.0", asset_name="Offloader-Setup-0.2.0.exe",
                      download_url="https://github.com/a/b/setup.exe")
    opener = _opener(body, url="https://github.com/a/b/setup.exe",
                     length=str(len(body)))
    seen: list[tuple[int, int]] = []

    update.download(release, tmp_path, opener=opener,
                    progress=lambda done, total: seen.append((done, total)))

    assert seen and seen[-1] == (len(body), len(body))


# -------------------------------------------------------------- verification


def _written(tmp_path: Path) -> Path:
    path = tmp_path / "Offloader-Setup-0.2.0.exe"
    path.write_bytes(b"MZ")
    return path


def test_a_correctly_signed_installer_verifies(tmp_path: Path):
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")

    record = update.verify(path, release, run=_runner(_signature()),
                           expected_digest=hashlib.sha256(b"MZ").hexdigest())
    assert record["Status"] == "Valid"


def test_a_digest_that_changed_after_download_is_refused(tmp_path: Path):
    """The file on disk is re-hashed, not the streamed digest compared with
    itself. Anything local that can write to the download directory could
    swap the bytes after they were hashed."""
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")

    with pytest.raises(UpdateError, match="changed after"):
        update.verify(path, release, run=_runner(_signature()),
                      expected_digest="b" * 64)


def test_a_file_swapped_while_its_signature_was_read_is_refused(tmp_path: Path):
    """REGRESSION. Bytes changed between the first hash and the signature
    probe are what Authenticode inspected; the second hash catches that."""
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")
    honest = _runner(_signature())

    def swap_then_answer(command, **kwargs):
        path.write_bytes(b"MZ swapped")
        return honest(command, **kwargs)

    with pytest.raises(UpdateError, match="while its signature was read"):
        update.verify(path, release, run=swap_then_answer,
                      expected_digest=hashlib.sha256(b"MZ").hexdigest())


@pytest.mark.parametrize("status", ["NotSigned", "HashMismatch",
                                    "UnknownError", ""])
def test_an_untrusted_signature_is_refused(tmp_path: Path, status: str):
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")

    with pytest.raises(UpdateError, match="signature"):
        update.verify(path, release, run=_runner(_signature(Status=status)))


def test_another_valid_certificate_is_still_refused(tmp_path: Path):
    """A valid signature is not the test. Anyone can buy one; the question is
    whether this is the certificate the release was built with."""
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")

    with pytest.raises(UpdateError, match="different certificate"):
        update.verify(path, release, run=_runner(_signature(Thumbprint="C" * 40)))


def test_a_thumbprint_is_compared_without_spacing_or_case(tmp_path: Path):
    """PowerShell has returned it both ways; a formatting difference must not
    read as a different certificate."""
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")
    spaced = " ".join(update.EXPECTED_CERT_THUMBPRINT[i:i + 4]
                      for i in range(0, 40, 4)).lower()

    update.verify(path, release, run=_runner(_signature(Thumbprint=spaced)))


def test_a_different_publisher_name_is_refused(tmp_path: Path):
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")
    subject = "CN=Someone Else Ltd., O=Someone Else Ltd., C=US"

    with pytest.raises(UpdateError, match="publisher"):
        update.verify(path, release, run=_runner(_signature(Subject=subject)))


def test_an_installer_reporting_another_version_is_refused(tmp_path: Path):
    """The rollback defence. Someone able to re-upload an asset could
    otherwise re-serve an older, still validly signed installer under a newer
    name and move every install back onto a build whose faults are fixed."""
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")

    with pytest.raises(UpdateError, match="0.1.0"):
        update.verify(path, release,
                      run=_runner(_signature(FileVersion="0.1.0")))


def test_the_embedded_version_string_is_compared_as_the_release_version(
        tmp_path: Path):
    """`installer.nsi` writes the release version itself (`0.2.0`, `0.1.0b1`)
    into the FileVersion string; the four-field Windows number lives in
    `VIProductVersion` and is not what is read. So the string matches the
    release exactly, a `+local` suffix is ignored, and a four-field value is
    refused rather than guessed at."""
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")

    update.verify(path, release, run=_runner(_signature(FileVersion="0.2.0")))
    update.verify(path, release,
                  run=_runner(_signature(FileVersion="0.2.0+build7")))
    with pytest.raises(UpdateError, match="0.2.0.0"):
        update.verify(path, release,
                      run=_runner(_signature(FileVersion="0.2.0.0")))


def test_an_unreadable_signature_probe_is_a_failure(tmp_path: Path):
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")

    with pytest.raises(UpdateError):
        update.verify(path, release, run=_runner({}, returncode=1))




def test_a_release_signed_by_an_announced_next_certificate_verifies(
        tmp_path: Path, monkeypatch):
    """Rotation. A build that lists the renewed certificate ahead of time
    keeps taking releases once they are signed with it; the publisher name is
    still required of every accepted certificate."""
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")
    following = "D" * 40
    monkeypatch.setattr(update, "EXPECTED_CERT_THUMBPRINTS",
                        (update.EXPECTED_CERT_THUMBPRINT, following))

    update.verify(path, release, run=_runner(_signature(Thumbprint=following)))
    with pytest.raises(UpdateError, match="publisher"):
        update.verify(path, release, run=_runner(_signature(
            Thumbprint=following, Subject="CN=Someone Else Ltd.")))
    with pytest.raises(UpdateError, match="different certificate"):
        update.verify(path, release, run=_runner(_signature(Thumbprint="")))


def test_the_pinned_certificate_is_the_one_releases_are_signed_with():
    """The values are repeated from the build script rather than imported, so
    nothing else would notice them drifting apart."""
    signing = _signing()
    assert update.EXPECTED_CERT_THUMBPRINTS[0] == signing.DEFAULT_CERT_THUMBPRINT
    assert update.EXPECTED_CERT_THUMBPRINT == signing.DEFAULT_CERT_THUMBPRINT
    assert update.EXPECTED_SIGNER_NAME == signing.EXPECTED_CERTIFICATE_NAME


# ------------------------------------------------------------ signature probe


def _capturing_runner(seen: list[tuple[list[str], dict]], record: dict | None = None):
    def run(command, **kwargs):
        seen.append((list(command), kwargs))
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(record or _signature()), stderr="")
    return run


def test_powershell_is_run_by_absolute_path_from_a_trusted_directory(
        tmp_path: Path, monkeypatch):
    """REGRESSION. A bare `powershell` is found by the Windows search order,
    which tries the current directory before System32; the CLI is routinely
    run from a card's root, where a planted `powershell.exe` would both run
    and decide the signature verdict."""
    windows = tmp_path / "Windows"
    card = tmp_path / "card"
    card.mkdir()
    monkeypatch.setenv("SystemRoot", str(windows))
    monkeypatch.chdir(card)
    seen: list[tuple[list[str], dict]] = []

    update.inspect_installer(_written(tmp_path), run=_capturing_runner(seen))

    command, kwargs = seen[0]
    expected = windows / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    assert command[0] == str(expected)
    assert Path(command[0]).is_absolute()
    assert command[1:5] == ["-NoProfile", "-NonInteractive",
                            "-ExecutionPolicy", "Bypass"]
    assert kwargs["cwd"] == str(windows)
    assert Path(kwargs["cwd"]).resolve() != card.resolve()


@pytest.mark.parametrize("value", [None, "", "Windows", r"\Windows"])
def test_an_unusable_system_root_is_refused_before_anything_runs(
        tmp_path: Path, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SystemRoot", raising=False)
    else:
        monkeypatch.setenv("SystemRoot", value)
    seen: list[tuple[list[str], dict]] = []

    with pytest.raises(UpdateError, match="SystemRoot"):
        update.inspect_installer(_written(tmp_path), run=_capturing_runner(seen))
    assert seen == []


def test_the_installer_path_never_becomes_script_text(tmp_path: Path):
    """REGRESSION. The path used to be pasted into a single-quoted literal, so
    an apostrophe ended it (a profile named O'Brien broke every update) and a
    crafted directory name became PowerShell in the probe whose output is the
    trust verdict."""
    hostile = tmp_path / "O'Brien $profile `n"
    hostile.mkdir()
    path = hostile / "Offloader-Setup-0.2.0.exe"
    path.write_bytes(b"MZ")
    seen: list[tuple[list[str], dict]] = []

    update.inspect_installer(path, run=_capturing_runner(seen))

    command, kwargs = seen[0]
    assert all("O'Brien" not in part for part in command)
    assert kwargs["env"]["OFFLOADER_UPDATE_INSTALLER"] == str(path.absolute())
    assert "$env:OFFLOADER_UPDATE_INSTALLER" in command[-1]


def test_a_probe_that_cannot_start_is_a_failure(tmp_path: Path):
    def missing(_command, **_kwargs):
        raise FileNotFoundError("powershell.exe")

    with pytest.raises(UpdateError, match="signature"):
        update.inspect_installer(_written(tmp_path), run=missing)


# --------------------------------------------------------- download hygiene


def _release() -> Release:
    return Release(version="0.2.0", asset_name="Offloader-Setup-0.2.0.exe",
                   download_url="https://github.com/a/b/setup.exe")


def _link_dir(link: Path, target: Path) -> None:
    """A directory symlink where the platform allows one, else a junction."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    pytest.skip("cannot create a directory link here")


def test_the_download_is_staged_until_it_is_promoted(tmp_path: Path):
    """Nothing carries the installer's own name until verification passed."""
    body = b"installer bytes"
    staged, _digest = update.download(
        _release(), tmp_path,
        opener=_opener(body, url="https://github.com/a/b/setup.exe"))

    assert staged.name == "Offloader-Setup-0.2.0.exe" + update.PARTIAL_SUFFIX
    assert not (tmp_path / "Offloader-Setup-0.2.0.exe").exists()

    final = update.promote(staged)
    assert final == tmp_path / "Offloader-Setup-0.2.0.exe"
    assert final.read_bytes() == body
    assert not staged.exists()


def test_a_partial_download_leaves_no_file_under_the_installer_name(
        tmp_path: Path, monkeypatch):
    """REGRESSION. The bytes went straight to `Offloader-Setup-X.exe`, so a
    refused or interrupted download left a plausible installer behind."""
    monkeypatch.setattr(update, "MAX_DOWNLOAD_BYTES", 1024)
    opener = _opener(b"x" * 4096, url="https://github.com/a/b/setup.exe")

    with pytest.raises(UpdateError, match="ceiling"):
        update.download(_release(), tmp_path, opener=opener)
    assert list(tmp_path.iterdir()) == []


def test_a_body_shorter_than_its_content_length_is_refused(tmp_path: Path):
    opener = _opener(b"MZ" * 5, url="https://github.com/a/b/setup.exe",
                     length="100")

    with pytest.raises(UpdateError, match="stopped at 10 of 100"):
        update.download(_release(), tmp_path, opener=opener)
    assert list(tmp_path.iterdir()) == []


def test_a_connection_lost_mid_stream_is_an_update_error(tmp_path: Path):
    """A socket error mid-read used to escape as a bare OSError or traceback,
    leaving the partial file under the final name."""
    class Dropping(_Response):
        def read(self, size: int = -1) -> bytes:
            if self._at:
                raise ConnectionResetError("reset by peer")
            return super().read(4)

    def opener(_request, timeout=None):
        return Dropping(b"MZ" * 100, url="https://github.com/a/b/setup.exe")

    with pytest.raises(UpdateError, match="reset by peer"):
        update.download(_release(), tmp_path, opener=opener)
    assert list(tmp_path.iterdir()) == []


def test_a_stale_partial_from_an_earlier_run_is_replaced(tmp_path: Path):
    stale = tmp_path / ("Offloader-Setup-0.2.0.exe" + update.PARTIAL_SUFFIX)
    stale.write_bytes(b"old interrupted bytes")

    staged, _digest = update.download(
        _release(), tmp_path,
        opener=_opener(b"new", url="https://github.com/a/b/setup.exe"))
    assert staged.read_bytes() == b"new"


def test_a_linked_download_directory_is_refused(tmp_path: Path):
    """A shared `--dir` that is really a link would let whoever owns its
    target choose where the installer is written."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _link_dir(link, real)

    with pytest.raises(UpdateError, match="link or junction"):
        update.download(_release(), link, opener=_opener(
            b"MZ", url="https://github.com/a/b/setup.exe"))
    assert list(real.iterdir()) == []


def test_a_link_at_the_installer_name_is_refused(tmp_path: Path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    _link_dir(downloads / "Offloader-Setup-0.2.0.exe", elsewhere)

    with pytest.raises(UpdateError, match="link"):
        update.download(_release(), downloads, opener=_opener(
            b"MZ", url="https://github.com/a/b/setup.exe"))


# ------------------------------------------------------------------ applying


def _install_dir(tmp_path: Path, *, manifest: bool = True) -> tuple[Path, Path]:
    """An installed copy as the installer leaves it, or a portable bundle."""
    installed = tmp_path / "Offloader"
    installed.mkdir()
    executable = installed / "offloader-cli.exe"
    executable.write_bytes(b"MZ")
    if manifest:
        (installed / ".offloader-install.json").write_text(json.dumps({
            "format": 1,
            "files": {"offloader-cli.exe": {"sha256": "0" * 64, "size": 2}},
        }), encoding="utf-8")
    return installed, executable


def test_the_target_is_the_running_installation(monkeypatch, tmp_path: Path):
    """Computed from the running executable rather than the registry, and
    only for a frozen build the installer put there.

    Uses a real directory rather than a literal Windows path: `install_target`
    resolves the running executable, and a backslash is not a separator off
    Windows, so a hardcoded `C:\\...` resolves against the working directory
    and the assertion would only hold on one platform.
    """
    installed, executable = _install_dir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    assert update.install_target() == installed.resolve()


def test_a_pip_or_source_install_is_refused(monkeypatch):
    """REGRESSION. It used to fall back to `%ProgramFiles%\\Offloader`, which
    installs a second copy beside the registered one (or under Program Files
    (x86) for a 32-bit Python) and compares the pip package's version with a
    build it is not."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    with pytest.raises(UpdateError, match="Python installation"):
        update.install_target()


def test_a_portable_bundle_is_refused(monkeypatch, tmp_path: Path):
    """REGRESSION. Without the installer's manifest the maintenance step
    refuses the directory every time, silently under `/S`."""
    installed, executable = _install_dir(tmp_path, manifest=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    with pytest.raises(UpdateError, match="portable bundle"):
        update.install_target()


def test_the_install_directory_argument_is_last_and_unquoted():
    """Two rules NSIS enforces silently. Quoting `/D=` installs into a
    directory whose name contains a quote; anything after it is swallowed into
    the path."""
    _path, arguments = update.install_command(
        Path(r"C:\tmp\Offloader-Setup-0.2.0.exe"),
        Path(r"C:\Program Files\Offloader"))

    assert arguments == r"/S /D=C:\Program Files\Offloader"
    assert arguments.endswith(r"Offloader")
    assert '"' not in arguments


class _Shell:
    """Stands in for `ShellExecuteExW`; nothing is ever launched."""

    def __init__(self, result: ShellResult | None = None):
        self.result = result or ShellResult(0, 0)
        self.calls: list[tuple[str, str, str, str, bool]] = []

    def __call__(self, verb, path, arguments, directory, wait):
        self.calls.append((verb, path, arguments, directory, wait))
        return self.result


def _installer(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "Offloader-Setup-0.2.0.exe"
    path.write_bytes(b"MZ installer")
    return path, hashlib.sha256(b"MZ installer").hexdigest()


def test_applying_elevates_with_the_runas_verb(tmp_path: Path):
    """A plain spawn does not honour the installer's manifest, so it fails
    instead of prompting."""
    installer, digest = _installer(tmp_path)
    shell = _Shell()

    assert update.apply(installer, Path(r"C:\Program Files\Offloader"),
                        expected_digest=digest, shell_execute=shell) is True

    verb, path, arguments, _directory, wait = shell.calls[0]
    assert verb == "runas"
    assert path == str(installer.absolute())
    assert arguments == r"/S /D=C:\Program Files\Offloader"
    assert wait is True


def test_the_installer_runs_in_its_own_directory(tmp_path: Path, monkeypatch):
    """REGRESSION. `lpDirectory` was None, so the elevated installer inherited
    the operator's current directory, which may be a card."""
    card = tmp_path / "card"
    card.mkdir()
    monkeypatch.chdir(card)
    installer, digest = _installer(tmp_path)
    shell = _Shell()

    update.apply(installer, Path(r"C:\x"), expected_digest=digest,
                 shell_execute=shell)

    directory = shell.calls[0][3]
    assert directory == str(installer.parent.absolute())
    assert Path(directory).resolve() != card.resolve()


def test_an_installer_that_refuses_is_reported_as_a_failure(tmp_path: Path):
    """REGRESSION. The launch returned as soon as the process existed, so a
    silent installer refusing because Offloader was still running (exit 1,
    its message box suppressed by `/S`) was reported as success."""
    installer, digest = _installer(tmp_path)

    with pytest.raises(UpdateError, match=r"refused or failed \(exit code 1\)"):
        update.apply(installer, Path(r"C:\x"), expected_digest=digest,
                     shell_execute=_Shell(ShellResult(0, 1)))


def test_an_installer_not_waited_for_is_not_reported_as_success(tmp_path: Path):
    installer, digest = _installer(tmp_path)
    shell = _Shell(ShellResult(0, None))

    assert update.apply(installer, Path(r"C:\x"), expected_digest=digest,
                        wait=False, shell_execute=shell) is False
    assert shell.calls[0][4] is False


def test_a_file_changed_after_verification_is_not_launched(tmp_path: Path):
    """REGRESSION. The digest was compared with itself; the file was never
    re-read before it was elevated."""
    installer, digest = _installer(tmp_path)
    installer.write_bytes(b"MZ something else")
    shell = _Shell()

    with pytest.raises(UpdateError, match="Offloader-Setup-0.2.0.exe"):
        update.apply(installer, Path(r"C:\x"), expected_digest=digest,
                     shell_execute=shell)
    assert shell.calls == []


def test_a_declined_elevation_prompt_is_reported_as_such(tmp_path: Path):
    """Cancelling the UAC prompt is the most likely outcome of all, and it is
    not a broken download."""
    installer, digest = _installer(tmp_path)
    with pytest.raises(UpdateError, match="administrator"):
        update.apply(installer, Path(r"C:\x"), expected_digest=digest,
                     shell_execute=_Shell(ShellResult(1223, None)))


def test_a_failure_to_start_the_installer_is_reported(tmp_path: Path):
    installer, digest = _installer(tmp_path)
    with pytest.raises(UpdateError, match="code 2"):
        update.apply(installer, Path(r"C:\x"), expected_digest=digest,
                     shell_execute=_Shell(ShellResult(2, None)))


# ------------------------------------------------------ the update command


NEXT = "99.0.0"
NEXT_ASSET = f"Offloader-Setup-{NEXT}.exe"
NEXT_BYTES = b"MZ the next installer"


class _Flow:
    """`offloader update --install` with every outside effect replaced.

    The feed goes through the real `_fetch_json` with `urlopen` patched, the
    download through the real `download` with an injected opener, and the
    signature probe through the real `_powershell` with `subprocess.run`
    patched. `ShellExecuteExW` is a recorder. Nothing reaches the network,
    PowerShell, the registry or UAC.
    """

    def __init__(self, monkeypatch, tmp_path: Path):
        self.tmp_path = tmp_path
        self.temp = tmp_path / "temp"
        self.temp.mkdir()
        self.target, self.executable = _install_dir(tmp_path)
        self.shell = _Shell()
        self.probes: list[list[str]] = []
        self.downloads = 0

        import tempfile

        monkeypatch.setattr(tempfile, "tempdir", str(self.temp))
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(update, "install_target", lambda: self.target)
        monkeypatch.setattr(update, "_shell_execute_ex", self.shell)

        feed = json.dumps([_feed(f"v{NEXT}")]).encode()
        monkeypatch.setattr(
            update.urllib.request, "urlopen",
            lambda _request, timeout=None: _Response(
                feed, url="https://api.github.com/repos/owenpkent/offloader/releases"))

        real_download = update.download

        def download(release, directory, **kwargs):
            self.downloads += 1
            return real_download(release, directory, opener=_opener(
                NEXT_BYTES, url="https://objects.githubusercontent.com/x",
                length=str(len(NEXT_BYTES))), **kwargs)

        monkeypatch.setattr(update, "download", download)

        def run(command, **kwargs):
            self.probes.append(list(command))
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(_signature(FileVersion=NEXT)),
                stderr="")

        monkeypatch.setattr(subprocess, "run", run)

    def main(self, *extra: str) -> int:
        return cli.main(["update", "--install", *extra])


@pytest.fixture
def flow(monkeypatch, tmp_path: Path) -> _Flow:
    return _Flow(monkeypatch, tmp_path)


def test_cmd_update_installs_a_verified_release_end_to_end(flow: _Flow, capsys):
    status = flow.main()
    out = capsys.readouterr().out

    assert status == 0
    assert f"verified {NEXT_ASSET}" in out
    assert hashlib.sha256(NEXT_BYTES).hexdigest() in out
    assert f"Offloader {NEXT} was installed." in out
    assert Path(flow.probes[0][0]).is_absolute()
    verb, path, arguments, directory, wait = flow.shell.calls[0]
    assert verb == "runas"
    assert Path(path).name == NEXT_ASSET
    assert directory == str(Path(path).parent)
    assert arguments == f"/S /D={flow.target}"
    assert wait is True
    # The private temporary directory is removed once the installer is done.
    assert list(flow.temp.iterdir()) == []


def test_cmd_update_reports_an_installer_refusal(flow: _Flow, capsys):
    """REGRESSION. A refused silent install used to exit 0."""
    flow.shell.result = ShellResult(0, 1)

    status = flow.main()
    captured = capsys.readouterr()

    assert status == 2
    assert "refused or failed" in captured.err
    assert "was installed" not in captured.out
    assert list(flow.temp.iterdir()) == []


def test_cmd_update_from_inside_the_installation_does_not_claim_success(
        flow: _Flow, monkeypatch, capsys):
    """The installed CLI holds its own installation open, so it cannot wait
    for the installer and must say so rather than report an install."""
    monkeypatch.setattr(sys, "executable", str(flow.executable))
    flow.shell.result = ShellResult(0, None)

    status = flow.main()
    out = capsys.readouterr().out

    assert status == 6
    assert flow.shell.calls[0][4] is False
    assert "cannot see the installer's result" in out
    assert "was installed" not in out


def test_cmd_update_refuses_a_file_swapped_after_verification(
        flow: _Flow, monkeypatch, capsys):
    """REGRESSION. `verify` was given the streamed digest twice, so a file
    replaced after hashing was elevated anyway."""
    real_promote = update.promote

    def promote_then_swap(staged):
        final = real_promote(staged)
        final.write_bytes(b"MZ not what was verified")
        return final

    monkeypatch.setattr(update, "promote", promote_then_swap)

    status = flow.main("--dir", str(flow.tmp_path / "downloads"))
    err = capsys.readouterr().err

    assert status == 2
    assert NEXT_ASSET in err and "before it was launched" in err
    assert flow.shell.calls == []


def test_cmd_update_refuses_a_linked_download_directory(flow: _Flow, capsys):
    real = flow.tmp_path / "real"
    real.mkdir()
    link = flow.tmp_path / "link"
    _link_dir(link, real)

    status = flow.main("--dir", str(link))

    assert status == 2
    assert "link or junction" in capsys.readouterr().err
    assert flow.downloads == 0 and flow.shell.calls == []


def test_cmd_update_leaves_no_partial_after_a_refused_signature(
        flow: _Flow, monkeypatch, capsys):
    def unsigned(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(_signature(Status="NotSigned")),
            stderr="")

    monkeypatch.setattr(subprocess, "run", unsigned)
    downloads = flow.tmp_path / "downloads"

    status = flow.main("--dir", str(downloads))

    assert status == 2
    assert "NotSigned" in capsys.readouterr().err
    assert list(downloads.iterdir()) == []
    assert flow.shell.calls == []


@pytest.mark.parametrize("frozen, manifest, message", [
    (False, True, "Python installation"),
    (True, False, "portable bundle"),
])
def test_cmd_update_refuses_a_build_that_cannot_update_itself(
        monkeypatch, tmp_path: Path, capsys, frozen, manifest, message):
    """REGRESSION. Detection runs before anything is downloaded, and says
    which build it found."""
    shell = _Shell()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(update, "_shell_execute_ex", shell)
    feed = json.dumps([_feed(f"v{NEXT}")]).encode()
    monkeypatch.setattr(
        update.urllib.request, "urlopen",
        lambda _request, timeout=None: _Response(feed, url=update.FEED_URL))
    called: list[str] = []
    monkeypatch.setattr(update, "download",
                        lambda *a, **k: called.append("download"))
    _installed, executable = _install_dir(tmp_path, manifest=manifest)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    status = cli.main(["update", "--install"])

    assert status == 2
    assert message in capsys.readouterr().err
    assert called == [] and shell.calls == []


def _urlopen_raising(exc: BaseException):
    def urlopen(_request, timeout=None):
        raise exc
    return urlopen


@pytest.mark.parametrize("urlopen, reason", [
    (_urlopen_raising(urllib.error.URLError("no route to host")),
     "could not reach the release feed"),
    (_urlopen_raising(urllib.error.HTTPError(
        update.FEED_URL, 403, "rate limit exceeded", None, io.BytesIO(b""))),
     "HTTP 403"),
    (lambda _request, timeout=None: _Response(
        b"<html>captive portal</html>", url=update.FEED_URL),
     "not readable JSON"),
    (lambda _request, timeout=None: _Response(
        b"[]", url="https://evil.example/releases"),
     "evil.example"),
], ids=["offline", "rate-limited", "bad-json", "redirected"])
def test_a_feed_failure_is_not_reported_as_up_to_date(monkeypatch, capsys,
                                                      urlopen, reason):
    """REGRESSION. Every one of these used to print "the newest release
    available" and exit 0, the documented "up to date" status, so a broken
    proxy or a network attacker could suppress updates indefinitely."""
    monkeypatch.setattr(update.urllib.request, "urlopen", urlopen)

    status = cli.main(["update"])
    captured = capsys.readouterr()

    assert status == 5
    assert status not in (0, 1)
    assert "could not check for updates" in captured.err
    assert reason in captured.err
    assert "newest" not in captured.out + captured.err
    assert "up to date" not in captured.out + captured.err


def test_an_empty_feed_is_still_up_to_date(monkeypatch, capsys):
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda _request, timeout=None: _Response(
                            b"[]", url=update.FEED_URL))

    assert cli.main(["update"]) == 0
    assert "newest release available" in capsys.readouterr().out


def test_find_release_raises_where_check_returns_none():
    def explode(_url):
        raise urllib.error.URLError("offline")

    with pytest.raises(UpdateError, match="offline"):
        update.find_release("0.1.0", fetch=explode)
    assert update.check("0.1.0", fetch=explode) is None


def test_the_feed_error_message_reaches_the_exception():
    """GitHub's error body names the cause (rate limit, bad credentials). The
    message must survive into the FeedError so the operator can read it."""
    message = "API rate limit exceeded for 1.2.3.4"

    def fetch(_url):
        return {"message": message}

    with pytest.raises(update.FeedError) as caught:
        update.find_release("0.5.0", fetch=fetch)
    assert message in str(caught.value)
