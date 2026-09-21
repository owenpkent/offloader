"""Fail unless a release tag names the version the source declares.

    python scripts/check_tag.py v0.1.0b1
    python scripts/check_tag.py refs/tags/v0.1.0b1

A tag and a version literal that disagree produce a release whose assets,
installer metadata, Add/Remove Programs entry and update feed all claim
different things. The updater compares the feed's tag against the version
compiled into the installer and refuses a mismatch, so the failure would not
surface as a bad release: it would surface later as an update that every
installed copy declines, for reasons nobody can see from the outside.

Checking it in a script rather than inline in the workflow keeps it testable,
and lets the same gate run locally before a tag is pushed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from offloader._version import __version__  # noqa: E402
from offloader.update import parse_version  # noqa: E402

#: Tags are pushed as `v0.1.0b1`; a workflow hands over the full ref.
_PREFIXES = ("refs/tags/", "v")


def version_from_tag(tag: str) -> str:
    """The version a tag names, with the ref path and `v` prefix removed."""
    value = tag.strip()
    for prefix in _PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="the release tag, or its full ref")
    args = parser.parse_args(argv)

    tagged = version_from_tag(args.tag)
    if parse_version(tagged) is None:
        print(f"error: {args.tag!r} does not name a release version. Tags look "
              f"like v0.1.0 or v0.1.0b1.", file=sys.stderr)
        return 2
    # Compared as text, not as parsed tuples: `0.1.0` and `0.1.0+1` would
    # order the same while naming different things on disk.
    if tagged != __version__:
        print(f"error: tag {args.tag!r} names version {tagged!r}, but "
              f"src/offloader/_version.py declares {__version__!r}. Bump the "
              f"version literal and commit before tagging.", file=sys.stderr)
        return 1
    print(f"tag {args.tag} matches the declared version {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
