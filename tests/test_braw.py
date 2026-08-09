"""Blackmagic RAW parsing, container checks, and proxy pairing.

The fixtures are synthesised rather than committed: a real BRAW clip is
gigabytes and is somebody's footage. The synthesiser writes the same structure
the parser was originally developed against — verified by hand on a real
Blackmagic PYXIS 6K file, whose values appear below as the expected results.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from offloader import braw, companions, probe

# ------------------------------------------------------------------ builders


def _atom(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + kind + payload


def _keys(names: list[str]) -> bytes:
    body = struct.pack(">II", 0, len(names))
    for name in names:
        encoded = name.encode("utf-8")
        body += struct.pack(">I", len(encoded) + 8) + b"mdta" + encoded
    return _atom(b"keys", body)


def _data(type_indicator: int, payload: bytes) -> bytes:
    return _atom(b"data", struct.pack(">II", type_indicator, 0) + payload)


def _ilst(values: list[tuple[int, int, bytes]]) -> bytes:
    """values: (key index, type indicator, payload)."""
    body = b""
    for index, type_indicator, payload in values:
        item = _data(type_indicator, payload)
        body += struct.pack(">I", len(item) + 8) + struct.pack(">I", index) + item
    return _atom(b"ilst", body)


def _mdhd(timescale: int, duration: int) -> bytes:
    return _atom(b"mdhd", struct.pack(">IIIIIhh", 0, 0, 0, timescale, duration, 0, 0))


def _stts(sample_count: int, delta: int) -> bytes:
    return _atom(b"stts", struct.pack(">IIII", 0, 1, sample_count, delta))


def _hdlr(handler: bytes) -> bytes:
    """A handler-reference atom: version+flags, pre_defined, handler type."""
    return _atom(b"hdlr", struct.pack(">II", 0, 0) + handler + bytes(12))


def _trak(handler: bytes, timescale: int, duration: int,
          samples: int, delta: int) -> bytes:
    stbl = _atom(b"stbl", _stts(samples, delta))
    minf = _atom(b"minf", stbl)
    mdia = _atom(b"mdia", _mdhd(timescale, duration) + _hdlr(handler) + minf)
    return _atom(b"trak", mdia)


def write_braw(path: Path, *, width: float = 6048.0, height: float = 4032.0,
               fps: int = 24, frames: int = 240,
               camera: str = "Blackmagic PYXIS 6K",
               lens: str = "Sigma 24-70mm F2.8 DG DN II | Art 024",
               reel: str = "1", scene: str = "1", take: str = "14",
               good_take: str = "false", compression: str = "8:1",
               bitrate: int = 923661952, gain: float = 1.0,
               colour_gen: int = 5, include_moov: bool = True,
               mdat_bytes: int = 4096, audio_first: bool = False) -> Path:
    """A minimal but structurally faithful BRAW file."""
    names = [
        "camera_type", "lens_type", "reel_name", "scene", "take", "good_take",
        "braw_compression_ratio", "braw_codec_bitrate", "analog_gain",
        "viewing_bmdgen", "crop_size", "camera_number", "manufacturer",
        "firmware_version", "camera_id", "post_3dlut_embedded_title",
        "date_recorded",
    ]
    utf8 = 1
    values = [
        (1, utf8, camera.encode()),
        (2, utf8, lens.encode()),
        (3, utf8, reel.encode()),
        (4, utf8, scene.encode()),
        (5, utf8, take.encode()),
        (6, utf8, good_take.encode()),
        (7, utf8, compression.encode()),
        (8, braw._TYPE_UINT32, struct.pack(">I", bitrate)),
        (9, braw._TYPE_FLOAT32, struct.pack(">f", gain)),
        (10, braw._TYPE_INT16, struct.pack(">h", colour_gen)),
        (11, braw._TYPE_FLOAT_PAIR, struct.pack(">ff", width, height)),
        (12, utf8, b"A"),
        (13, utf8, b"Blackmagic Design"),
        (14, utf8, b"10.2"),
        (15, utf8, b"382b6130-44e8-4b3d-9aec-a8774c618bc6"),
        (16, utf8, b"Gen 5 Film to Extended Video"),
        (17, utf8, b"2026:08:04"),
    ]

    video = _trak(b"vide", fps * 1000, frames * 1000, frames, 1000)
    # Real BRAW carries an audio track too, with one sample per *audio* sample:
    # tens of millions of them. 48 kHz for the clip's duration.
    audio = _trak(b"soun", 48000, int(frames / fps * 48000),
                  int(frames / fps * 48000), 1)
    traks = (audio + video) if audio_first else (video + audio)
    meta = _atom(b"meta", _keys(names) + _ilst(values))
    moov = _atom(b"moov", traks + meta)

    body = _atom(b"wide", b"") + _atom(b"mdat", b"\0" * mdat_bytes)
    if include_moov:
        body += moov
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


# ------------------------------------------------------------------ metadata


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    return write_braw(tmp_path / "A001_08041254_C001.braw")


def test_reads_the_values_a_real_pyxis_wrote(clip: Path):
    info = braw.read_info(clip)
    assert info is not None
    assert info.camera_type == "Blackmagic PYXIS 6K"
    assert info.lens_type == "Sigma 24-70mm F2.8 DG DN II | Art 024"
    assert info.resolution == "6048 x 4032"
    assert info.compression_ratio == "8:1"
    assert info.colour_science_gen == 5
    assert info.firmware_version == "10.2"
    assert info.camera_number == "A"
    assert info.lut_name == "Gen 5 Film to Extended Video"
    assert info.codec_bitrate_bps == 923661952
    assert info.iso_gain == pytest.approx(1.0)


def test_slate_reads_as_a_script_supervisor_would_write_it(clip: Path):
    info = braw.read_info(clip)
    assert info.slate() == "Reel 1 · Scene 1 · Take 14"
    assert info.good_take is False


def test_good_take_flag_is_parsed(tmp_path: Path):
    marked = write_braw(tmp_path / "good.braw", good_take="true")
    assert braw.read_info(marked).good_take is True


def test_timing_comes_from_the_container(clip: Path):
    info = braw.read_info(clip)
    assert info.fps == pytest.approx(24.0)
    assert info.frame_count == 240
    assert info.duration_sec == pytest.approx(10.0)


def test_camera_summary_joins_body_and_lens(clip: Path):
    assert "PYXIS" in braw.read_info(clip).camera_summary()
    assert "Sigma" in braw.read_info(clip).camera_summary()


def test_every_key_is_available_raw(clip: Path):
    raw = braw.read_info(clip).raw
    assert raw["manufacturer"] == "Blackmagic Design"
    assert raw["camera_id"].count("-") == 4


def test_metadata_is_read_without_touching_the_media(tmp_path: Path):
    """Only the moov is read, so a 28 GB clip costs the same as a small one."""
    big = write_braw(tmp_path / "big.braw", mdat_bytes=40_000_000)
    reads: list[int] = []
    real_open = open

    class Counting:
        def __init__(self, handle):
            self._handle = handle

        def read(self, size=-1):
            data = self._handle.read(size)
            reads.append(len(data))
            return data

        def seek(self, *args):
            return self._handle.seek(*args)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._handle.close()

    import builtins
    builtins.open = lambda p, m="r", *a, **k: (
        Counting(real_open(p, m, *a, **k)) if "b" in str(m) else real_open(p, m, *a, **k))
    try:
        assert braw.read_info(big) is not None
    finally:
        builtins.open = real_open

    assert sum(reads) < 1_000_000, f"read {sum(reads):,} bytes of a 40 MB file"


# ------------------------------------------------------------------ integrity


def test_a_healthy_clip_passes(clip: Path):
    assert braw.check_container(clip).ok


def test_an_interrupted_recording_is_caught(tmp_path: Path):
    """A clip the camera never finished writing has no moov. Its bytes copy and
    checksum perfectly; it will not play. Checksums cannot see this."""
    broken = write_braw(tmp_path / "interrupted.braw", include_moov=False)
    check = braw.check_container(broken)

    assert check.state is braw.ContainerState.NO_MOOV
    assert check.is_fatal
    assert "interrupted" in check.detail
    assert braw.read_info(broken) is None


def test_an_atom_overrunning_the_file_is_caught(clip: Path):
    payload = bytearray(clip.read_bytes())
    struct.pack_into(">I", payload, 8, 0x7FFFFFFF)   # mdat claims 2 GB
    clip.write_bytes(bytes(payload))

    check = braw.check_container(clip)
    assert check.state is braw.ContainerState.TRUNCATED
    assert check.is_fatal


def test_a_non_braw_file_is_reported_not_crashed(tmp_path: Path):
    junk = tmp_path / "notes.txt"
    junk.write_bytes(b"this is not a quicktime file at all")
    assert not braw.check_container(junk).ok


def test_a_missing_file_is_unreadable_not_an_exception(tmp_path: Path):
    check = braw.check_container(tmp_path / "gone.braw")
    assert check.state is braw.ContainerState.UNREADABLE


def test_is_braw_matches_on_suffix():
    assert braw.is_braw(Path("A001_C001.braw"))
    assert braw.is_braw(Path("A001_C001.BRAW"))
    assert not braw.is_braw(Path("A001_C001.mov"))


# ------------------------------------------------------------------ probe


def test_probe_routes_braw_around_ffprobe(clip: Path):
    """ffprobe returns an empty document for BRAW — not an error, nothing —
    so probing must not go near it."""
    media = probe.probe(clip)
    assert media.container == "Blackmagic RAW"
    assert media.video_codec == "8:1"          # container name is not repeated
    assert (media.width, media.height) == (6048, 4032)
    assert media.is_video
    assert media.camera.model == "Blackmagic PYXIS 6K"
    assert media.camera.colour_science == "Gen 5"


def test_probe_does_not_spawn_ffprobe_for_braw(clip: Path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("ffprobe must not be invoked for BRAW")

    monkeypatch.setattr(probe.subprocess, "run", explode)
    assert probe.probe(clip).container == "Blackmagic RAW"


def test_probe_of_an_unreadable_braw_still_names_the_format(tmp_path: Path):
    broken = write_braw(tmp_path / "broken.braw", include_moov=False)
    media = probe.probe(broken)
    assert media.container == "Blackmagic RAW"
    assert not media.is_video


# ------------------------------------------------------------------ proxies


def test_finds_the_proxy_the_camera_wrote(tmp_path: Path):
    card = tmp_path / "A001"
    (card / "Proxy").mkdir(parents=True)
    original = write_braw(card / "A001_08041254_C001.braw")
    proxy = card / "Proxy" / "A001_08041254_C001.mp4"
    proxy.write_bytes(b"proxy")

    assert companions.find_proxy(original) == proxy
    assert companions.thumbnail_source(original) == (proxy, True)


@pytest.mark.parametrize("directory", ["Proxy", "proxy", "Proxies", "PROXY"])
def test_proxy_directory_naming_variants(tmp_path: Path, directory: str):
    card = tmp_path / "A001"
    (card / directory).mkdir(parents=True)
    original = write_braw(card / "clip.braw")
    proxy = card / directory / "clip.mp4"
    proxy.write_bytes(b"proxy")

    found = companions.find_proxy(original)
    assert found is not None
    # samefile, not path equality: on a case-insensitive filesystem "Proxy" and
    # "proxy" are one directory, so the search returns whichever spelling it
    # tried first. That is the same file, which is what matters.
    assert found.samefile(proxy)


def test_a_proxy_beside_the_original_is_found(tmp_path: Path):
    original = write_braw(tmp_path / "clip.braw")
    proxy = tmp_path / "clip.mov"
    proxy.write_bytes(b"proxy")
    assert companions.find_proxy(original) == proxy


def test_no_proxy_falls_back_to_the_original(tmp_path: Path):
    original = write_braw(tmp_path / "clip.braw")
    assert companions.find_proxy(original) is None
    assert companions.thumbnail_source(original) == (original, False)


def test_a_decodable_file_never_looks_for_a_proxy(tmp_path: Path):
    (tmp_path / "Proxy").mkdir()
    (tmp_path / "Proxy" / "clip.mp4").write_bytes(b"decoy")
    ordinary = tmp_path / "clip.mov"
    ordinary.write_bytes(b"real")
    assert companions.thumbnail_source(ordinary) == (ordinary, False)


def test_proxy_matching_is_by_stem_not_position(tmp_path: Path):
    card = tmp_path / "A001"
    (card / "Proxy").mkdir(parents=True)
    for name in ("C001", "C002", "C003"):
        (card / "Proxy" / f"A001_{name}.mp4").write_bytes(name.encode())
    original = write_braw(card / "A001_C002.braw")
    assert companions.find_proxy(original).name == "A001_C002.mp4"


def test_needs_proxy_covers_the_undecodable_formats():
    for suffix in (".braw", ".r3d", ".ari", ".crm"):
        assert companions.needs_proxy(Path(f"clip{suffix}"))
    assert not companions.needs_proxy(Path("clip.mp4"))


# ------------------------------------------------------------- grouping


def test_a_sidecar_is_grouped_with_its_clip(tmp_path: Path):
    clip = write_braw(tmp_path / "A001_C001.braw")
    sidecar = tmp_path / "A001_C001.sidecar"
    sidecar.write_bytes(b"colour metadata")

    assert companions.group([clip, sidecar]) == {sidecar: clip}


def test_a_proxy_is_grouped_with_the_clip_a_folder_up(tmp_path: Path):
    card = tmp_path / "A001"
    (card / "Proxy").mkdir(parents=True)
    clip = write_braw(card / "A001_C001.braw")
    proxy = card / "Proxy" / "A001_C001.mp4"
    proxy.write_bytes(b"proxy")

    assert companions.group([clip, proxy]) == {proxy: clip}


def test_an_ambiguous_stem_is_left_ungrouped(tmp_path: Path):
    """Two takes of the same name in different folders, one sidecar between
    them. Naming the wrong clip would be worse than saying nothing, because the
    only value of the link is that it can be trusted."""
    first = write_braw(tmp_path / "Day1" / "A001_C001.braw")
    second = write_braw(tmp_path / "Day2" / "A001_C001.braw")
    sidecar = tmp_path / "A001_C001.sidecar"
    sidecar.write_bytes(b"whose?")

    assert companions.group([first, second, sidecar]) == {}


def test_a_sidecar_beside_one_of_two_takes_picks_the_near_one(tmp_path: Path):
    first = write_braw(tmp_path / "Day1" / "A001_C001.braw")
    second = write_braw(tmp_path / "Day2" / "A001_C001.braw")
    sidecar = tmp_path / "Day2" / "A001_C001.sidecar"
    sidecar.write_bytes(b"day two")

    assert companions.group([first, second, sidecar]) == {sidecar: second}


def test_an_orphan_sidecar_is_not_invented_a_clip(tmp_path: Path):
    sidecar = tmp_path / "A001_C009.sidecar"
    sidecar.write_bytes(b"no clip here")
    clip = write_braw(tmp_path / "A001_C001.braw")

    assert companions.group([clip, sidecar]) == {}


def test_clips_are_never_companions_of_each_other(tmp_path: Path):
    """Two ordinary takes sharing a stem are two takes, not a pair."""
    one = tmp_path / "A001_C001.mov"
    two = tmp_path / "A001_C001.mp4"
    for path in (one, two):
        path.write_bytes(b"a take")

    assert companions.group([one, two]) == {}


# ------------------------------------------------------------------ real file

#: A real Blackmagic PYXIS 6K still, if one happens to be around. The synthetic
#: fixtures above were modelled on this file; this test is the bridge that
#: proves the model still matches reality rather than only matching itself.
_REAL = Path(
    r"C:\Users\Owen\AppData\Local\Temp\claude\C--Users-Owen-dev"
    r"\3c278deb-e13c-44d2-ae85-83b4cc4461b8\scratchpad\src\Stills"
    r"\A001_08041415_C012_S001.braw"
)


@pytest.mark.skipif(not _REAL.is_file(), reason="no real BRAW sample available")
def test_synthetic_fixture_matches_a_real_camera_file():
    info = braw.read_info(_REAL)
    assert info is not None
    assert braw.check_container(_REAL).ok

    # The same fields, decoded the same way, as the synthesiser produces.
    assert info.camera_type == "Blackmagic PYXIS 6K"
    assert info.lens_type.startswith("Sigma 24-70mm")
    assert info.manufacturer == "Blackmagic Design"
    assert info.compression_ratio == "8:1"
    assert info.colour_science_gen == 5
    assert info.resolution == "6048 x 4032"
    assert info.fps == pytest.approx(24.0)
    assert info.codec_bitrate_bps == 923661952
    assert info.slate() == "Reel 1 · Scene 1 · Take 14"
    assert info.good_take is False
    assert len(info.raw) > 30


# ------------------------------------------------------------------ offload


def test_an_interrupted_clip_is_flagged_during_the_offload(tmp_path: Path):
    """The moment to notice an unplayable clip is while the card is still in
    your hand — not in the edit, after the card was reformatted."""
    from offloader import engine
    from offloader.models import VerificationMode

    card = tmp_path / "card"
    write_braw(card / "A001_C001.braw")                      # healthy
    write_braw(card / "A001_C002.braw", include_moov=False)  # interrupted

    job = engine.run(card, engine.OffloadOptions(
        destinations=[tmp_path / "dest"], verification=VerificationMode.FULL,
        thumbnail_count=0))

    # The bytes are perfect: it copied and verified.
    assert job.final_status == "Verified"
    # And it is still unplayable, which the report has to say.
    assert any("A001_C002.braw" in w and "will not play" in w
               for w in job.warnings)
    assert not any("A001_C001.braw" in w and "will not play" in w
                   for w in job.warnings)


def test_camera_metadata_reaches_the_job(tmp_path: Path):
    from offloader import engine
    from offloader.models import VerificationMode

    card = tmp_path / "card"
    write_braw(card / "A001_C001.braw")
    job = engine.run(card, engine.OffloadOptions(
        destinations=[tmp_path / "dest"], verification=VerificationMode.FULL,
        thumbnail_count=0))

    camera = job.files[0].media.camera
    assert camera.model == "Blackmagic PYXIS 6K"
    assert camera.slate() == "Reel 1 · Scene 1 · Take 14"
    assert job.files[0].media.container == "Blackmagic RAW"


def test_the_report_records_that_frames_came_from_a_proxy(tmp_path: Path):
    """Frames from a proxy must not read as evidence the original decoded."""
    from offloader.models import (
        CameraInfo,
        Destination,
        FileEntry,
        FileStatus,
        Job,
        MediaInfo,
    )
    from offloader.reports import write_csv, write_html

    entry = FileEntry(
        source=tmp_path / "A001_C001.braw",
        source_root=tmp_path, size=1024, created=0.0, modified=0.0,
        checksum="abc123",
        media=MediaInfo(
            container="Blackmagic RAW", width=6048, height=4032,
            video_codec="8:1", fps=24.0, duration_sec=10.0, frame_count=240,
            camera=CameraInfo(model="Blackmagic PYXIS 6K", lens="Sigma 24-70mm",
                              reel="1", scene="1", take="14", good_take=True,
                              colour_science="Gen 5"),
        ),
        thumbnail_source=tmp_path / "Proxy" / "A001_C001.mp4",
        destinations=[Destination(root=tmp_path, path=tmp_path / "A001_C001.braw",
                                  status=FileStatus.VERIFIED, checksum="abc123")],
    )
    job = Job(name="A001", source_root=tmp_path, destination_roots=[tmp_path],
              files=[entry])

    document = write_html(job, tmp_path / "r.html", thumbnails=False).read_text(
        encoding="utf-8")
    assert "Frames from proxy" in document
    assert "A001_C001.mp4" in document
    assert "Blackmagic PYXIS 6K" in document
    assert "GOOD TAKE" in document

    csv_text = write_csv(job, tmp_path / "r.csv").read_text(encoding="utf-8")
    assert "Blackmagic PYXIS 6K" in csv_text
    assert "Sigma 24-70mm" in csv_text
    assert ",yes," in csv_text          # good take


def test_the_video_track_is_chosen_by_handler_not_position(tmp_path: Path):
    """REGRESSION, found against real 79 GB clips. A BRAW file carries a `soun`
    track alongside the picture, with one sample per *audio* sample — tens of
    millions of them. Taking whichever track came first worked only because
    `vide` happened to be first in every file to hand; an audio-first file would
    have reported 34 million "frames" and a duration to match."""
    audio_first = write_braw(tmp_path / "audio_first.braw", frames=240, fps=24,
                             audio_first=True)
    info = braw.read_info(audio_first)

    assert info.frame_count == 240
    assert info.fps == pytest.approx(24.0)
    assert info.duration_sec == pytest.approx(10.0)


def test_track_order_does_not_change_the_result(tmp_path: Path):
    first = braw.read_info(write_braw(tmp_path / "a.braw", audio_first=False))
    second = braw.read_info(write_braw(tmp_path / "b.braw", audio_first=True))
    assert (first.frame_count, first.fps, first.duration_sec) ==            (second.frame_count, second.fps, second.duration_sec)
