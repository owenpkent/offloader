"""Map supported release versions to ordered Windows version fields."""

import re


def windows_version(version: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?", version)
    if match is None:
        raise ValueError(f"Unsupported Windows bundle version: {version!r}")
    major, minor, patch = (int(part) for part in match.group(1, 2, 3))
    stage = match.group(4)
    sequence = int(match.group(5) or 0)
    # Reserve disjoint ranges so a late alpha never sorts above the first beta.
    if sequence > 999:
        raise ValueError("Windows prerelease sequence must be between 0 and 999")
    fourth = {"a": 1000, "b": 2000, "rc": 3000, None: 65535}[stage] + sequence
    fields = (major, minor, patch, fourth)
    if any(field > 65535 for field in fields):
        raise ValueError(f"Windows version field exceeds 65535: {version!r}")
    return fields
