"""Checksum algorithms.

The engine streams bytes through these once, so every algorithm is exposed as
an incremental hasher rather than a whole-file function.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Protocol

import xxhash


class Hasher(Protocol):
    def update(self, data: bytes) -> None: ...
    def hexdigest(self) -> str: ...


@dataclass(frozen=True)
class Algorithm:
    key: str
    #: Name as it appears in the report header, e.g. "XXHash3-64".
    label: str
    factory: Callable[[], Hasher] | None
    #: MHL/ASC-MHL element name, or None if the format has no slot for it.
    mhl_tag: str | None = None

    def new(self) -> Hasher | None:
        return self.factory() if self.factory else None


class _NullHasher:
    def update(self, data: bytes) -> None:  # noqa: D102
        pass

    def hexdigest(self) -> str:  # noqa: D102
        return ""


ALGORITHMS: dict[str, Algorithm] = {
    "xxh3-64": Algorithm("xxh3-64", "XXHash3-64", xxhash.xxh3_64, "xxh3"),
    "xxh3-128": Algorithm("xxh3-128", "XXHash3-128", xxhash.xxh3_128, "xxh3-128"),
    "xxh64": Algorithm("xxh64", "XXHash-64", xxhash.xxh64, "xxh64"),
    "xxh64be": Algorithm("xxh64be", "XXHash-64BE", xxhash.xxh64, "xxh64be"),
    "md5": Algorithm("md5", "MD5", hashlib.md5, "md5"),
    "sha1": Algorithm("sha1", "SHA-1", hashlib.sha1, "sha1"),
    "sha256": Algorithm("sha256", "SHA-256", hashlib.sha256, "sha256"),
    "none": Algorithm("none", "None", None, None),
}

DEFAULT_ALGORITHM = "xxh3-64"


def get_algorithm(key: str) -> Algorithm:
    try:
        return ALGORITHMS[key.lower()]
    except KeyError:
        raise ValueError(
            f"unknown checksum algorithm {key!r}; choose from {', '.join(ALGORITHMS)}"
        ) from None


def new_hasher(key: str) -> Hasher:
    """An incremental hasher, or a no-op one when checksums are disabled."""
    return get_algorithm(key).new() or _NullHasher()


def hash_file(path: Path, key: str, chunk_size: int = 8 << 20) -> str:
    """Hash a file off disk. Used by full verification to re-read a
    destination, and by `--rescan` to reconstruct checksums."""
    from .longpath import open_binary

    hasher = new_hasher(key)
    with open_binary(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def algorithm_keys() -> Iterable[str]:
    return ALGORITHMS.keys()
