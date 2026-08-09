"""Files that belong with a clip.

Camera originals that no general-purpose decoder can open — BRAW, R3D, ARRIRAW —
are normally recorded alongside a proxy the camera also wrote. When the original
cannot be decoded for a contact sheet, the proxy is the picture: it is the same
take, framed the same way, and it is already on the card.

Blackmagic's layout is `A001/A001_08041254_C001.braw` beside
`A001/Proxy/A001_08041254_C001.mp4` — same stem, sibling directory.

The same stem-matching answers a second question: which files have no meaning
on their own. A `.sidecar` is a clip's grade; delivered without its clip it is
nothing, and a clip delivered without it has silently lost the grade. Copying
both and reporting them as two unrelated files is how that goes unnoticed, so
`group` links them and the engine refuses to let them end up with different
verdicts quietly.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

#: Camera originals ffmpeg cannot decode without a vendor SDK.
UNDECODABLE_SUFFIXES = {
    ".braw", ".r3d", ".ari", ".arx", ".crm", ".cine", ".dng",
}

#: Directory names cameras and DITs put proxies in, in search order.
PROXY_DIRECTORIES = ("Proxy", "proxy", "Proxies", "proxies", "PROXY")

#: Container suffixes a proxy might use, in preference order.
PROXY_SUFFIXES = (".mp4", ".mov", ".m4v", ".mxf")

#: Suffixes worn by a file that describes a clip rather than being one. Each is
#: a format a camera or a grading tool writes beside the original, matched to it
#: by stem. Deliberately short: a file wrongly called a companion is reported as
#: belonging to something it does not.
COMPANION_SUFFIXES = {
    ".sidecar",   # Blackmagic RAW — colour metadata, written when a grade is set
    ".rmd",       # RED metadata
    ".xmp",       # Adobe sidecar metadata
}


def needs_proxy(path: Path) -> bool:
    """Whether this file needs a stand-in to produce a thumbnail."""
    return Path(path).suffix.lower() in UNDECODABLE_SUFFIXES


def find_proxy(source: Path, source_root: Path | None = None) -> Path | None:
    """The proxy that matches `source`, or None.

    Matching is by stem, so `A001_..._C001.braw` finds `A001_..._C001.mp4`
    wherever the camera filed it.
    """
    source = Path(source)
    stem = source.stem

    directories: list[Path] = []
    for name in PROXY_DIRECTORIES:
        directories.append(source.parent / name)
        if source_root is not None:
            directories.append(Path(source_root) / name)
    directories.append(source.parent)          # proxy sitting beside the original

    seen: set[Path] = set()
    for directory in directories:
        if directory in seen:
            continue
        seen.add(directory)
        if not directory.is_dir():
            continue
        for suffix in PROXY_SUFFIXES:
            candidate = directory / f"{stem}{suffix}"
            if candidate.is_file() and candidate != source:
                return candidate
    return None


def thumbnail_source(source: Path,
                     source_root: Path | None = None) -> tuple[Path, bool]:
    """Where thumbnails for `source` should be read from.

    Returns (path, used_proxy). Falls back to the original when no proxy is
    found, so an undecodable file simply produces no thumbnails rather than an
    error.
    """
    if needs_proxy(source):
        proxy = find_proxy(source, source_root)
        if proxy is not None:
            return proxy, True
    return Path(source), False


def is_companion(path: Path) -> bool:
    """Whether this file describes a clip rather than being one."""
    return Path(path).suffix.lower() in COMPANION_SUFFIXES


def in_proxy_directory(path: Path) -> bool:
    return Path(path).parent.name in PROXY_DIRECTORIES


def group(paths: Iterable[Path]) -> dict[Path, Path]:
    """Map each companion file to the clip it belongs to.

    Two kinds qualify: a sidecar carrying a clip's metadata, and a proxy the
    camera filed in its own directory. Both are matched by stem, which is the
    only relationship cameras actually record.

    An ambiguous stem — two clips of the same name in different folders, one
    sidecar — is left unlinked rather than guessed at. Claiming a `.sidecar`
    belongs to the wrong take would be worse than saying nothing, because the
    whole point of the link is that someone trusts it.
    """
    files = [Path(p) for p in paths]
    clips = [p for p in files if not is_companion(p) and not in_proxy_directory(p)]

    by_stem: dict[str, list[Path]] = {}
    for clip in clips:
        by_stem.setdefault(clip.stem, []).append(clip)

    linked: dict[Path, Path] = {}
    for candidate in files:
        if not (is_companion(candidate) or in_proxy_directory(candidate)):
            continue
        matches = by_stem.get(candidate.stem, [])
        if not matches:
            continue
        # A clip in the same folder wins; a proxy's clip is the folder above.
        near = [c for c in matches
                if c.parent == candidate.parent
                or c.parent == candidate.parent.parent]
        if len(near) == 1:
            linked[candidate] = near[0]
        elif not near and len(matches) == 1:
            linked[candidate] = matches[0]
    return linked
