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

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from offloader import update
from offloader.update import Release, UpdateError

REPO = Path(__file__).resolve().parent.parent
GOOD_DIGEST = "a" * 64


def _versioning():
    """`build/windows/versioning.py`, which is a build script, not a module."""
    path = REPO / "build" / "windows" / "versioning.py"
    spec = importlib.util.spec_from_file_location("_versioning", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
                           expected_digest=GOOD_DIGEST,
                           actual_digest=GOOD_DIGEST)
    assert record["Status"] == "Valid"


def test_a_digest_that_changed_after_download_is_refused(tmp_path: Path):
    """The window between writing the file and executing it. Anything local
    that can write to the download directory could swap it in that gap."""
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")

    with pytest.raises(UpdateError, match="changed after"):
        update.verify(path, release, run=_runner(_signature()),
                      expected_digest=GOOD_DIGEST, actual_digest="b" * 64)


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


def test_a_trailing_windows_build_field_still_matches(tmp_path: Path):
    """The installer's embedded FileVersion carries a fourth field the release
    version does not have."""
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")

    update.verify(path, release, run=_runner(_signature(FileVersion="0.2.0")))


def test_an_unreadable_signature_probe_is_a_failure(tmp_path: Path):
    path = _written(tmp_path)
    release = Release(version="0.2.0", asset_name=path.name,
                      download_url="https://github.com/a/b")

    with pytest.raises(UpdateError):
        update.verify(path, release, run=_runner({}, returncode=1))


# ------------------------------------------------------------------ applying


def test_the_target_is_computed_not_read_from_the_registry(monkeypatch,
                                                           tmp_path: Path):
    """The uninstall key is writable by anything running as the user, so a
    planted value would redirect an elevated silent install.

    Uses a real directory rather than a literal Windows path: `install_target`
    resolves the running executable, and a backslash is not a separator off
    Windows, so a hardcoded `C:\\...` resolves against the working directory
    and the assertion would only hold on one platform.
    """
    installed = tmp_path / "Offloader"
    installed.mkdir()
    executable = installed / "Offloader.exe"
    executable.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))

    assert update.install_target() == installed.resolve()


def test_an_unfrozen_checkout_falls_back_to_program_files(monkeypatch):
    """Not a path a release takes, but it must not resolve to the source tree
    and offer to install over it."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")

    assert update.install_target() == Path(r"C:\Program Files") / "Offloader"


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


def test_applying_elevates_with_the_runas_verb(tmp_path: Path):
    """A plain spawn does not honour the installer's manifest, so it fails
    instead of prompting."""
    calls: list[tuple] = []

    def shell_execute(handle, verb, path, arguments, directory, show):
        calls.append((handle, verb, path, arguments, directory, show))
        return 42

    update.apply(tmp_path / "setup.exe", Path(r"C:\Program Files\Offloader"),
                 shell_execute=shell_execute)

    assert calls[0][1] == "runas"
    assert calls[0][3] == r"/S /D=C:\Program Files\Offloader"


def test_a_declined_elevation_prompt_is_reported_as_such(tmp_path: Path):
    """Cancelling the UAC prompt is the most likely outcome of all, and it is
    not a broken download."""
    with pytest.raises(UpdateError, match="administrator"):
        update.apply(tmp_path / "setup.exe", Path(r"C:\x"),
                     shell_execute=lambda *_: 5)


def test_a_failure_to_start_the_installer_is_reported(tmp_path: Path):
    with pytest.raises(UpdateError, match="code 2"):
        update.apply(tmp_path / "setup.exe", Path(r"C:\x"),
                     shell_execute=lambda *_: 2)
