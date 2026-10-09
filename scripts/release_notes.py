"""Write the draft release notes for a tagged candidate.

    python scripts/release_notes.py --tag v0.1.0b1 --commit "$GITHUB_SHA"

Kept out of the workflow because prose with backticks and blank lines inside a
YAML block scalar inside a shell heredoc has three levels of quoting to get
wrong, and the failure mode is a release note that silently truncates at the
first surprise.

The notes deliberately say the draft is not publishable. The workflow cannot
sign, the signing key only exists on the release workstation, and a draft that
looked finished is how an unsigned installer ends up as a download.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from offloader._version import __version__  # noqa: E402

TEMPLATE = """\
Offloader {version}

Built from {commit}.

**Not publishable yet.** The Windows download has to be the signed installer
produced on the release workstation. The artifact this workflow built is
unsigned and is attached to the workflow run for inspection only; see
`docs/release-plan.md`, which requires signing for any release including a
beta.

To finish this release, from a non-elevated shell with the signing token
available:

```powershell
git checkout {tag}
python build/windows/build.py --clean
python build/windows/build.py --verify-only
gh release upload {tag} dist/windows/Offloader-Setup-{version}.exe dist/windows/SHA256SUMS.txt dist/windows/Offloader-{version}-inventory.json
```

Then work through the acceptance matrix in `docs/release-plan.md` and publish
this draft only once every gate has recorded evidence.
"""


def notes(tag: str, commit: str, version: str = __version__) -> str:
    """The draft body.

    Refuses a tag that does not name `version`. The workflow gates that before
    it gets here, but the notes name artefact filenames built from the version
    while the heading names the tag: if the two ever diverged, the result would
    be a plausible-looking set of instructions pointing at files that do not
    exist. Cheaper to make the invariant local than to rely on call order.
    """
    tagged = tag.removeprefix("refs/tags/").removeprefix("v")
    if tagged != version:
        raise SystemExit(
            f"error: tag {tag!r} names {tagged!r} but the source declares "
            f"{version!r}; the release notes would contradict themselves")
    return TEMPLATE.format(tag=tag, commit=commit, version=version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", type=Path, default=None,
                        help="write here instead of standard output")
    args = parser.parse_args(argv)

    body = notes(args.tag, args.commit)
    if args.out is None:
        sys.stdout.write(body)
    else:
        args.out.write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
