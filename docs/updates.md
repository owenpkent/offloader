# Updating an installed copy

`offloader update` asks GitHub whether a newer release exists, and
`offloader update --install` downloads it, proves it came from this project,
and hands it to the installer.

An updater runs code that arrived over the network, with elevation, on someone
else's machine. So the design question is not how it installs but what it
refuses, and every check below fails closed: an unreadable version, a
mismatched digest, a signature from another certificate all mean "no update",
never "install it anyway".

## The feed

GitHub Releases, read through the REST API. There is no manifest server to run
or keep honest, and no second place a version number is written down.

```
https://api.github.com/repos/owenpkent/offloader/releases/latest
```

That URL is compiled into every shipped build, so moving it orphans every
install that already exists. Renaming or transferring the repository is a
breaking change for installed copies, not an administrative detail.

Three fields are read. `tag_name` must be a version this project publishes
(`0.2.0`, `0.1.0b1`, with or without a leading `v`); anything else, including
`latest` or `1.0.3-evil`, is refused rather than compared as a string.
`assets[]` must contain an entry named exactly
`Offloader-Setup-{version}.exe` for the version the tag claims, so a file
attached beside the real installer cannot be served in its place. `body`
becomes the release notes.

## Version ordering

The grammar is the one `build/windows/versioning.py` already enforces, which
means prereleases are ordered rather than rejected: `0.1.0a2` < `0.1.0b1` <
`0.1.0rc1` < `0.1.0`. A late alpha never outranks a first beta, matching the
disjoint numeric ranges that module reserves when it writes the installer's
Windows version fields. `tests/test_update.py` asserts both implementations
agree on a list of versions, because if they diverge an update can install a
build that Windows then considers older than the one it replaced.

If either version is unreadable the answer is "not newer". String comparison
is what makes `1.0.10` look older than `1.0.9`.

## What is checked before anything runs

| Check | What it stops |
| --- | --- |
| HTTPS, and a host allowlist applied **after** redirects | GitHub redirects asset downloads to another domain, so the host that actually serves the bytes is the one that matters |
| A size ceiling, enforced against `Content-Length` and again while streaming | A hostile or confused feed filling the disk; a truthful `Content-Length` is not something a server owes us |
| SHA-256 taken while streaming, compared again before launch | The gap between writing the file and executing it, in which anything able to write to the download directory could swap it |
| Authenticode status is `Valid` | An unsigned or tampered installer |
| The signing certificate's thumbprint matches | A valid signature from somebody else. Anyone can obtain one; the question is whether this is the certificate the release was built with |
| The publisher name matches | The same, read the way the UAC prompt will read it |
| The installer's **embedded** `FileVersion` matches the release version | A rollback. Someone able to re-upload an asset could otherwise re-serve an older, still validly signed installer under a newer name, moving every install back onto a build whose faults are already fixed |

The certificate values are the ones in `build/windows/sign.py`, repeated in
`src/offloader/update.py` rather than imported, because the build scripts are
not part of the shipped package.

## Applying it

The verified installer is run through `ShellExecuteW` with the `runas` verb,
silently: `/S /D=<target>`. A plain process spawn does not honour the
installer's manifest request for elevation and fails instead of prompting.

Two details are easy to break silently. `/D=` must be the **last** argument
and must **not** be quoted even when the path contains spaces: quoting it
installs into a directory whose name contains a quote, and anything placed
after it is swallowed into the path. And the target is computed from the
running executable, never read from the uninstall registry key, which is
writable by anything running as the user and would otherwise let a planted
value redirect an elevated silent install.

## What it deliberately does not do

**It does not close anything.** Offloader's installer refuses maintenance
while a transfer is in flight and exits non-zero without changing the
installation; it never force-kills a running copy. That refusal is a promise
in [data-safety.md](data-safety.md), so the updater's job is to verify and
hand over, not to clear the way. `offloader update --install` says so before
the elevation prompt appears, because an operator who does not know it reads
the installer's refusal as a broken update.

The consequence is that updating requires closing the app and any CLI
transfer first. That is the intended trade: a packaged updater that could
interrupt a card offload would be a worse tool than one that asks.

**There is no rollback.** A failed install leaves the previous version in
place where the installer's own transactional recovery manages it (see
`installation.py`); the updater does not attempt a second recovery mechanism
on top. Nothing is deleted by the updater itself.

**There is no automatic check yet.** `offloader update` is explicit. An in-app
check, a notification and a timer belong with the desktop interface and are
not implemented here; see [release-plan.md](release-plan.md).

## Reference

Modelled on Alpha-OSK's `src/updater.py`, which solves the same problem for a
different app. Two of its decisions are deliberately not carried over: it
terminates the running application before installing, and it rejects any tag
that is not `X.Y.Z`. The first conflicts with the guarantee above; the second
would make this updater blind to Offloader's own first packaged release, which
is planned as a beta. Nothing else of its configuration, endpoints or product
identifiers is reused.
