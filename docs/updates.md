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
https://api.github.com/repos/owenpkent/offloader/releases?per_page=30
```

That URL is compiled into every shipped build, so moving it orphans every
install that already exists. Renaming or transferring the repository is a
breaking change for installed copies, not an administrative detail.

The collection, not `/releases/latest`. GitHub documents that endpoint as
returning the newest published *full* release and excluding prereleases, so an
installed `0.1.0b1` could never discover `0.1.0b2` through it, and a repository
holding only the betas the candidate workflow publishes with `--prerelease`
would answer with nothing at all. Ordering prereleases is the point of the
grammar below, so the feed has to be one that carries them.

The collection is ordered by creation date, which is not the same as version
order: a patched `0.1.0b2` published after `0.1.0rc1` is listed above it. So
every entry is read and the greatest eligible version wins, rather than the
first one that parses.

Four fields are read per release. `draft` excludes it: a draft is visible to
anyone who can write to the repository and its assets are not published, so
offering one would hand an installer to the maintainer's own machine before the
release exists for anybody else. `tag_name` must be a version this project
publishes (`0.2.0`, `0.1.0b1`, with or without a leading `v`); anything else,
including `latest` or `1.0.3-evil`, is refused rather than compared as a
string. `assets[]` must contain an entry named exactly
`Offloader-Setup-{version}.exe` for the version the tag claims, so a file
attached beside the real installer cannot be served in its place. `body`
becomes the release notes.

A feed that cannot be read is a failure, not an answer. Being offline, an HTTP
error (including GitHub's 403 or 429 when the unauthenticated rate limit is
reached), a redirect to a host outside the allowlist, or a body that is not
the expected JSON, or an error body, makes `offloader update` print the reason
to stderr and exit with status 5. It never prints "the newest release available" in that case:
reporting a failure as "up to date" would let a broken proxy or a network
attacker suppress updates indefinitely without anyone noticing. (The library's
`check()` still returns None on any failure for a future background caller;
the command and the app use `find_release()`, which raises `FeedError`.)

## Exit status

| Status | Meaning |
| --- | --- |
| 0 | Up to date, or the installer ran and reported success |
| 1 | A newer release exists and was not installed (no `--install`) |
| 2 | A usage error, a build that cannot update itself, or a download, verification, launch or installer failure (including a declined UAC prompt and an installer that refused) |
| 5 | The release feed could not be fetched or read, so nothing is known |
| 6 | The installer was started but its result could not be observed (see [Applying it](#applying-it)) |

## Which releases an install is offered

Newer, and on the channel it is already on. An installation that is itself a
prerelease is testing the prereleases, so it takes the next one, beta or
release candidate or final. An installation on a stable release is offered only
stable releases: it did not volunteer for the next beta, and `0.1.0b1` finding
`0.1.0b2` must not also mean `1.0.0` finding `1.0.1b1`.

A prerelease install still takes the stable release when it arrives, because
`0.1.0b2` < `0.1.0` in the ordering below. That is how a beta tester ends up on
the shipping build without doing anything.

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

Because the feed is the releases collection, this ordering is applied to
releases the updater can actually see: an installed `0.1.0b1` finds `0.1.0b2`
or `0.1.0rc1` once either is published, and a repository holding only betas
still answers. The channel comes from the version in the tag, not from GitHub's
`prerelease` flag, which the updater does not read.

## What is checked before anything runs

| Check | What it stops |
| --- | --- |
| HTTPS, and a host allowlist applied **after** redirects | GitHub redirects asset downloads to another domain, so the host that actually serves the bytes is the one that matters |
| A size ceiling, enforced against `Content-Length` and again while streaming | A hostile or confused feed filling the disk; a truthful `Content-Length` is not something a server owes us |
| Staged download: written to `Offloader-Setup-{version}.exe.offloader-partial`, created exclusively, flushed to disk, and given its real name only after the size and signature checks pass | Incomplete or unverified bytes left under a plausible installer name after a failed or interrupted download. Any failure deletes the partial file, and a temporary directory the command created is removed |
| A download directory, or an existing file at either download name, that is a symlink or junction is refused | A shared `--dir` redirecting where the installer is written |
| A body shorter than its declared `Content-Length` is refused | A truncated download reaching the signature check |
| SHA-256 taken while streaming, then the file on disk re-hashed before the signature is read, after it is read, and immediately before launch | A file replaced after it was hashed: a mismatch at any point refuses, names the file, and nothing is launched |
| Authenticode status is `Valid` | An unsigned or tampered installer |
| The signing certificate's thumbprint is one of the pinned thumbprints | A valid signature from somebody else. Anyone can obtain one; the question is whether this is the certificate the release was built with |
| The publisher name matches | The same, read the way the UAC prompt will read it |
| The installer's **embedded** `FileVersion` matches the release version | A rollback. Someone able to re-upload an asset could otherwise re-serve an older, still validly signed installer under a newer name, moving every install back onto a build whose faults are already fixed |

The signature and version are read by Windows PowerShell, launched by absolute
path (`%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`) with
`-NoProfile -NonInteractive -ExecutionPolicy Bypass` and the Windows directory
as its working directory. A bare `powershell` would be found by the Windows
search order, which tries the current directory before System32, and the CLI
is routinely run from a card's root. The installer's path reaches the script
through an environment variable, never as script text, so an apostrophe or `$`
in a profile or `--dir` name is just part of the path. The Security and
Utility modules are imported from `$PSHOME` explicitly and their cmdlets
called by qualified name.

**Known limits.** The re-hashes and the launch are separate opens of the path:
a process able to write the download directory in the moment between the last
hash and `ShellExecuteExW` opening the file could still swap it. The default
directory is a fresh per-user temporary directory, which narrows that to the
operator's own account; `--dir` refuses links but does not check who else can
write the directory, so point it only at a private location. A swapped,
unsigned binary would also show an "Unknown publisher" UAC prompt. Holding one
handle with write sharing denied, or verifying with `WinVerifyTrust` on that
handle, would close the gap and is not implemented.

### Certificate rotation

The certificate values are the ones in `build/windows/sign.py`, repeated in
`src/offloader/update.py` rather than imported, because the build scripts are
not part of the shipped package. A test asserts the current thumbprint and the
publisher name still match that script.

`EXPECTED_CERT_THUMBPRINTS` is a tuple, current certificate first, so a
renewal does not strand installed copies. A build that knows only one leaf
certificate refuses every release signed by its successor, and recovering
from that takes a manual reinstall. To rotate:

1. Before the current certificate is replaced, ship a release (signed with the
   current certificate) whose tuple lists the next certificate's thumbprint
   after the current one.
2. Give installs time to take that release.
3. Sign releases with the new certificate and move its thumbprint to the front
   (and update `DEFAULT_CERT_THUMBPRINT` in `sign.py`). Drop the old one once
   it is no longer used.

Every accepted certificate must still present `EXPECTED_SIGNER_NAME`; a
renewal that changes the publisher name needs that constant changed in the
same step-1 release. The revocation behaviour of `Get-AuthenticodeSignature`
is whatever Windows applies by default; it is not configured explicitly.

## Which builds can update themselves

Only an installed packaged build: a frozen executable whose directory carries
the installer's `.offloader-install.json` manifest. `offloader update
--install` checks this before downloading anything, and refuses with a message
naming what it found:

- **A pip install or a source checkout.** The installer does not replace
  those, and guessing `Program Files` would install a second copy beside the
  registered one. Update it the way it was installed (`pip install --upgrade`,
  `git pull`) or run the installer from the releases page.
- **A portable bundle** (frozen, no manifest). The installer's maintenance
  step refuses a directory it does not own, every time, so the command
  refuses up front instead. Run the installer, or replace the folder by hand.

`offloader update` without `--install` still reports what is available on any
build.

## Applying it

The verified installer is run through `ShellExecuteExW` with the `runas` verb,
silently: `/S /D=<target>`, with the installer's own directory as its working
directory rather than the operator's. A plain process spawn does not honour
the installer's manifest request for elevation and fails instead of
prompting. A declined UAC prompt is reported as such (status 2).

Two details are easy to break silently. `/D=` must be the **last** argument
and must **not** be quoted even when the path contains spaces: quoting it
installs into a directory whose name contains a quote, and anything placed
after it is swallowed into the path. And the target is the running
executable's directory, not the uninstall key's `InstallLocation`. That key is
under HKLM and only an administrator can write it; the reason is that it
records where the last install went, which is not necessarily the copy that is
running. **Known limit:** NSIS consumes `/D=` before `.onInit`, and the only
sign of an explicit target is that it differs from the compiled default
(`$PROGRAMFILES64\Offloader`). When the running copy is in that default
location, the installer cannot tell `/D=` from no `/D=` and uses the
registered `InstallLocation` instead, so in the common case the registry value
still decides the target.

**Waiting for the result.** `ShellExecuteExW` is called with
`SEE_MASK_NOCLOSEPROCESS`, so the process can be waited for and its exit code
read. Silent mode suppresses the installer's own message box, so that exit
code is the only report of a refusal: a non-zero code is printed as "the
installer refused or failed" and the command exits 2.

In practice the installed CLI cannot wait. It runs from the installation it is
replacing: for its whole lifetime it holds that installation's shared lock,
which maintenance needs exclusively, and its own executable, which maintenance
must move and delete. Waiting would make every update refuse. So when the
running executable is inside the target, the command starts the installer,
exits at once to release the installation, prints that it cannot see the
result, and returns status 6. Confirm the update afterwards with `offloader
--version`. The download directory is left in place in that case, because the
installer is still running from it. Exiting promptly is what lets maintenance
take the lock; this ordering is practical (the UAC prompt alone takes longer)
rather than enforced.

## What it deliberately does not do

**It never closes a running transfer.** The command line closes nothing. The
desktop app declines the update while a job is running or paused, and
otherwise closes itself, after asking, once the installer has started.
Offloader's installer refuses maintenance
while any Offloader program (the desktop app or a CLI process, busy or idle)
holds the installation's shared lock, and exits non-zero without changing the
installation; it never force-kills a running copy. That refusal is a promise
in [data-safety.md](data-safety.md), so the updater's job is to verify and
hand over, not to clear the way. `offloader update --install` says so before
the elevation prompt appears, because an operator who does not know it reads
the installer's refusal as a broken update. Under `/S` the refusal is visible
only through the exit code, which the command reports when it can wait and
says it cannot see when it cannot.

The consequence is that updating requires closing the app and any CLI
transfer first. That is the intended trade: a packaged updater that could
interrupt a card offload would be a worse tool than one that asks.

**There is no rollback.** A failed install leaves the previous version in
place where the installer's own transactional recovery manages it (see
`installation.py`); the updater does not attempt a second recovery mechanism
on top. The updater deletes nothing but its own download files.

## In the desktop app

The app checks once, a couple of seconds after the window opens, and says
nothing unless there is something to say: an unrequested check that reports
"you are up to date" is noise. When a release is found, the header carries a
line naming it. **Help, Check for updates now** runs the same check and does
report either outcome, and **Options, Check for updates on launch** turns the
automatic one off. The check runs on a pool thread, so a slow or unreachable
network delays nothing and a failure appears in the status bar rather than in
a dialog.

Three outcomes, not two. `update.check()` never raises and returns `None` for
a failed fetch, a TLS error and an unparseable feed alike, so a caller that
reports its result to somebody cannot use it: "you are the newest release" is
a claim, and it would be made on the strength of a failed DNS lookup. The app
and `offloader update` both use `find_release()`, which returns `None` only when
the feed answered and had nothing newer, and raises `FeedError` otherwise. A
check that could not be made is reported as a failure. From the command line
that is an error on stderr and exit status 5, distinct from 0 (up to date) and
1 (a newer release exists).

"Answered" means it returned the releases collection, or a single release
object (one with a `tag_name`). An empty collection, or one with nothing newer
for this install's channel, is up to date. Anything else is malformed and
raises `FeedError`: any other object (which this endpoint returns only as an
error body, such as a rate limit notice, whose message is reported), a list
holding something other than release objects, a proxy's HTML page, or no body
at all.

Whose result it is belongs to the check that is running, not to whoever asked
last. A manual check started inside the first couple of seconds is still
running when the launch timer fires; the timer's check is refused as a
duplicate, and the announcement the manual request asked for survives it. The
intent is only ever raised while work is in flight, so a manual request behind
an automatic check is answered too.

**Cancel cancels.** The download runs on a pool thread and cannot be
interrupted, so cancellation is cooperative: the progress callback is the one
place the loop hands control back often enough, and it raises there. The
request is then terminal — the incomplete download is removed, nothing is
verified, and `ready` is not emitted, so the app cannot go on to offer what
the user just declined. Only the file named by the release is removed, because
the download directory can be one the caller owns.

Installing from the app follows the refusal above rather than working around
it. If any job is running or paused, the update is declined with the reason;
a paused job counts, because it is a partially written destination waiting to
continue. Otherwise the installer is downloaded, verified, and the app asks
once more before closing itself so the installer can proceed. The app closing
is what makes the update possible, so it is announced rather than surprising,
and the queue is checked a second time immediately before it happens: a job
can be started while the bytes are arriving.

`src/offloader/gui/updates.py` holds that sequence, with every step
injectable, so `tests/test_gui_updates.py` drives all of it, including each
refusal, without a network, a certificate or an installer.

## Reference

Modelled on Alpha-OSK's `src/updater.py`, which solves the same problem for a
different app. Two of its decisions are deliberately not carried over: it
terminates the running application before installing, and it rejects any tag
that is not `X.Y.Z`. The first conflicts with the guarantee above; the second
would make this updater blind to Offloader's own first packaged release, which
is planned as a beta. Nothing else of its configuration, endpoints or product
identifiers is reused.
