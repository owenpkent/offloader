"""Files that belong with a clip.

Camera originals that no general-purpose decoder can open — BRAW, R3D, ARRIRAW —
are normally recorded alongside a proxy the camera also wrote. When the original
cannot be decoded for a contact sheet, the proxy is the picture: it is the same
take, framed the same way, and it is already on the card.

Blackmagic's layout is `A001/A001_08041254_C001.braw` beside
`A001/Proxy/A001_08041254_C001.mp4` — same stem, sibling directory.
"""

from __future__ import annotations

from pathlib import Path

#: Camera originals ffmpeg cannot decode without a vendor SDK.
UNDECODABLE_SUFFIXES = {
    ".braw", ".r3d", ".ari", ".arx", ".crm", ".cine", ".dng",
}

#: Directory names cameras and DITs put proxies in, in search order.
PROXY_DIRECTORIES = ("Proxy", "proxy", "Proxies", "proxies", "PROXY")

#: Container suffixes a proxy might use, in preference order.
PROXY_SUFFIXES = (".mp4", ".mov", ".m4v", ".mxf")


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
