from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

import pytest

from offloader.models import (
    AudioTrack,
    Destination,
    FileEntry,
    FileStatus,
    Job,
    MediaInfo,
    VerificationMode,
)


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    """A small synthetic card: two clips, a sidecar, and OS junk."""
    root = tmp_path / "card"
    (root / "Clips").mkdir(parents=True)
    (root / "Clips" / "A001_C001.mov").write_bytes(os.urandom(64_000))
    (root / "Clips" / "A001_C002.mov").write_bytes(os.urandom(32_000))
    (root / "notes.txt").write_text("slate notes", encoding="utf-8")
    (root / ".DS_Store").write_bytes(b"junk")
    (root / "._A001_C001.mov").write_bytes(b"resource fork")
    return root


def write_png(path: Path, width: int = 276, height: int = 156,
              rgb: tuple[int, int, int] = (40, 60, 90)) -> Path:
    """A solid-colour PNG, built by hand so the tests need no image library."""
    import struct
    import zlib

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


@pytest.fixture
def sample_job(tmp_path: Path) -> Job:
    """A fully populated Job, so report writers can be exercised without
    touching ffmpeg or moving any bytes."""
    source_root = tmp_path / "A001"
    dest_root = tmp_path / "backup"
    source = source_root / "Clips" / "A001_C001.mov"
    destination = dest_root / "Clips" / "A001_C001.mov"
    source.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"x" * 2048)
    destination.write_bytes(b"x" * 2048)

    now = _dt.datetime(2026, 8, 4, 12, 54, 0)
    thumbs = [
        write_png(tmp_path / "thumbs" / f"A001_C001_thumb{i:02d}.png",
                  rgb=(30 + i * 20, 60, 90))
        for i in range(1, 5)
    ]
    entry = FileEntry(
        source=source,
        source_root=source_root,
        size=258_758_961,
        created=now.timestamp(),
        modified=(now + _dt.timedelta(minutes=4)).timestamp(),
        checksum="cd4990759f33f032",
        media=MediaInfo(
            container="QuickTime",
            width=1920,
            height=1080,
            video_codec="H264/AVC",
            fps=24.0,
            duration_sec=251.0,
            frame_count=6022,
            timecode="12:54:38:12 NDF",
            audio_tracks=[AudioTrack(2, "stereo", "LINEAR PCM", 2304.0, 48000)],
        ),
        thumbnails=thumbs,
        destinations=[
            Destination(
                root=dest_root,
                path=destination,
                status=FileStatus.VERIFIED,
                checksum="cd4990759f33f032",
                created=now.timestamp(),
                modified=(now + _dt.timedelta(minutes=4)).timestamp(),
            )
        ],
    )

    plain = FileEntry(
        source=source_root / "notes.txt",
        source_root=source_root,
        size=1024,
        created=now.timestamp(),
        modified=now.timestamp(),
        checksum="0011223344556677",
        destinations=[
            Destination(root=dest_root, path=dest_root / "notes.txt",
                        status=FileStatus.VERIFIED, checksum="0011223344556677")
        ],
    )

    return Job(
        name="A001",
        source_root=source_root,
        destination_roots=[dest_root],
        verification=VerificationMode.SOURCE_ONLY,
        hash_label="XXHash-64",
        started=now,
        finished=now + _dt.timedelta(hours=1, minutes=4),
        files=[entry, plain],
        os_version="Windows 11 (Build 26200)",
        processors=16,
        system_ram="48 GB",
    )


def pytest_addoption(parser):
    parser.addoption(
        "--fuzz", action="store_true", default=False,
        help="run the property-based tests with a much larger example budget",
    )


def pytest_configure(config):
    """Register hypothesis profiles.

    The default budget keeps the suite fast enough to run on every change;
    `--fuzz` is the soak profile for hunting the long tail.
    """
    try:
        from hypothesis import HealthCheck, settings
    except ImportError:
        return

    settings.register_profile(
        "ci", max_examples=100, deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    settings.register_profile(
        "fuzz", max_examples=3000, deadline=None,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
    )
    settings.load_profile("fuzz" if config.getoption("--fuzz") else "ci")
