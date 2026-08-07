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
    """Substitute tokens. Unknown tokens are left alone rather than raising —
    a typo in a preset should not stop an offload."""
    result = template or DEFAULT_TEMPLATE
    for token, value in values.items():
        result = result.replace("{" + token + "}", value)
    return sanitize(result)


def build(template: str, source: Path, *, volume_label: str | None = None,
          when: _dt.datetime | None = None, taken: Iterable[str] = ()) -> str:
    """Render `template`, incrementing `{index}` until the name is unused.

    Templates without `{index}` get a `-2`, `-3` suffix instead, so a name
    collision can never silently overwrite an earlier job's reports.
    """
    used = {name.casefold() for name in taken}
    if "{index}" in (template or ""):
        for index in range(1, 1000):
            candidate = render(template, context(source, volume_label, when, index))
            if candidate.casefold() not in used:
                return candidate
        return render(template, context(source, volume_label, when, 999))

    base = render(template, context(source, volume_label, when))
    if base.casefold() not in used:
        return base
    for suffix in range(2, 1000):
        candidate = f"{base}-{suffix}"
        if candidate.casefold() not in used:
            return candidate
    return base
