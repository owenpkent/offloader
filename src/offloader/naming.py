"""Offload identification naming.

Job names drive the report folder (`<name>_Reports`) and, in structure-flattening
setups, the destination subfolder — so they need to be predictable and unique.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import re
from collections.abc import Iterable
from pathlib import Path

#: The default matches what a card offload usually wants: the card's own name.
DEFAULT_TEMPLATE = "{card}"

TOKENS = {
    "{card}": "source folder or volume name",
    "{volume}": "volume label of the source drive",
    "{date}": "offload date, YYYYMMDD",
    "{time}": "offload time, HHMMSS",
    "{year}": "four-digit year",
    "{month}": "two-digit month",
    "{day}": "two-digit day",
    "{user}": "current user name",
    "{index}": "sequence number, padded to three digits",
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: One pass over the template, so a substituted value is never re-scanned.
_TOKEN = re.compile(r"\{(\w+)\}")


def sanitize(name: str) -> str:
    """Strip characters Windows and exFAT reject, and trailing dots/spaces."""
    cleaned = _ILLEGAL.sub("_", name).strip().rstrip(". ")
    return cleaned or "Offload"


def context(source: Path, volume_label: str | None = None,
            when: _dt.datetime | None = None, index: int = 1) -> dict[str, str]:
    moment = when or _dt.datetime.now()
    source = Path(source)
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - no login name in some containers
        user = "unknown"
    return {
        "card": source.name or source.anchor.strip("\\/:") or "Offload",
        "volume": volume_label or source.name or "",
        "date": f"{moment:%Y%m%d}",
        "time": f"{moment:%H%M%S}",
        "year": f"{moment:%Y}",
        "month": f"{moment:%m}",
        "day": f"{moment:%d}",
        "user": user,
        "index": f"{index:03d}",
    }


def render(template: str, values: dict[str, str]) -> str:
    """Substitute tokens. Unknown tokens are left alone rather than raising:
    a typo in a preset should not stop an offload.

    Substitution is a single pass, so a value that itself contains token text
    is never substituted into again. A card folder genuinely named `{index}`
    keeps its name instead of being overwritten by the sequence number.
    """
    def replace(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(0))

    return sanitize(_TOKEN.sub(replace, template or DEFAULT_TEMPLATE))


def build(template: str, source: Path, *, volume_label: str | None = None,
          when: _dt.datetime | None = None, taken: Iterable[str] = ()) -> str:
    """Render `template`, incrementing `{index}` until the name is unused.

    Templates without `{index}` get a `-2`, `-3` suffix instead, so a name
    collision can never silently overwrite an earlier job's reports.

    Both searches are bounded by the size of `taken` rather than by a fixed
    ceiling. The suffix search is the backstop, and it always succeeds: it
    generates one more distinct candidate than there are taken names, so at
    least one of them has to be free.
    """
    used = {name.casefold() for name in taken}

    if "{index}" in (template or ""):
        for index in range(1, len(used) + 2):
            candidate = render(template, context(source, volume_label, when, index))
            if candidate.casefold() not in used:
                return candidate
        # A template whose `{index}` does not survive sanitising renders the
        # same name every time. Fall through to the suffix search rather than
        # handing back a name we know is taken.

    base = render(template, context(source, volume_label, when))
    if base.casefold() not in used:
        return base
    for suffix in range(2, len(used) + 3):
        candidate = f"{base}-{suffix}"
        if candidate.casefold() not in used:
            return candidate
    raise AssertionError("unreachable: more candidates than taken names")
