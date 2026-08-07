from __future__ import annotations

from pathlib import Path

import pytest

from offloader import hashers


def test_known_vectors(tmp_path: Path):
    target = tmp_path / "data.bin"
    target.write_bytes(b"nobody inspects the spammish repetition")

    assert hashers.hash_file(target, "md5") == "f81cbdf994b88acebafc3a83da2d5ee1"
    assert (hashers.hash_file(target, "sha1")
            == "6f8ce5497cec0a6182c1f4341a238e7ccffbb756")
    assert hashers.hash_file(target, "xxh64") == "9f65ecf9c862f68d"
    assert hashers.hash_file(target, "xxh3-64") == "f330ab07216c25d1"


def test_streaming_matches_whole_file(tmp_path: Path):
    payload = bytes(range(256)) * 4096
    target = tmp_path / "big.bin"
    target.write_bytes(payload)

    incremental = hashers.new_hasher("xxh3-64")
    for offset in range(0, len(payload), 7919):   # deliberately ragged chunks
        incremental.update(payload[offset:offset + 7919])

    assert incremental.hexdigest() == hashers.hash_file(target, "xxh3-64")


def test_none_algorithm_yields_empty_digest(tmp_path: Path):
    target = tmp_path / "x.bin"
    target.write_bytes(b"data")
    assert hashers.hash_file(target, "none") == ""
    assert hashers.get_algorithm("none").label == "None"


def test_labels_used_by_the_report():
    assert hashers.get_algorithm("xxh3-64").label == "XXHash3-64"
    assert hashers.get_algorithm("xxh64").label == "XXHash-64"


def test_unknown_algorithm_is_rejected():
    with pytest.raises(ValueError, match="unknown checksum algorithm"):
        hashers.get_algorithm("crc32")


def test_algorithm_lookup_is_case_insensitive():
    assert hashers.get_algorithm("XXH3-64").key == "xxh3-64"
