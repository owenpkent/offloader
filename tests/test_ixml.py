"""Broadcast WAV metadata.

The happy path is one test; the rest is what a card can actually contain. A
sound card is untrusted input, and a chunk walker that trusts its size fields
is a walker that hangs or allocates a gigabyte on a file that got truncated
when a battery died mid-write.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

import bwf
from offloader import ixml
from offloader.models import SoundInfo


def _wav(tmp_path: Path, **kwargs) -> Path:
    return bwf.write_wav(tmp_path / "MIX_001.wav", **kwargs)


# ------------------------------------------------------------- the happy path


def test_reads_the_slate_from_ixml(tmp_path: Path):
    path = _wav(tmp_path, ixml=bwf.ixml_document(), bext=bwf.bext_chunk())
    info = ixml.read_sound_info(path)

    assert info is not None
    assert (info.scene, info.take, info.tape) == ("12A", "3", "SR082226")
    assert info.project == "ChairsDoc"
    assert info.circled is True
    assert info.note == "wind on the boom"
    assert info.track_names == ["Boom", "Lav 1"]
    assert info.recorder == "Sound Devices 833"
    assert info.origination == "2026-08-22 10:00:00"


def test_ixml_is_found_whichever_side_of_the_audio_it_sits_on(tmp_path: Path):
    """Recorders disagree about this, and both are legal."""
    for first in (True, False):
        path = bwf.write_wav(tmp_path / f"take_{first}.wav",
                             ixml=bwf.ixml_document(), ixml_first=first)
        info = ixml.read_sound_info(path)
        assert info is not None and info.scene == "12A"


def test_the_chunk_identifier_is_matched_case_insensitively(tmp_path: Path):
    for identifier in (b"iXML", b"IXML", b"ixml"):
        path = bwf.write_wav(tmp_path / f"{identifier.decode()}.wav",
                             ixml=bwf.ixml_document(),
                             ixml_identifier=identifier)
        assert ixml.read_sound_info(path) is not None


# ------------------------------------------------------------------ timecode


def test_timecode_uses_the_rate_ixml_supplies(tmp_path: Path):
    """36000 s at 25 fps is 10:00:00:00 -- the frame rate only iXML knows."""
    path = _wav(tmp_path, ixml=bwf.ixml_document(rate="25/1"))
    info = ixml.read_sound_info(path)
    assert ixml.timecode_of(info) == "10:00:00:00 NDF"


def test_drop_frame_is_carried_through(tmp_path: Path):
    path = _wav(tmp_path, ixml=bwf.ixml_document(rate="30000/1001", flag="DF"))
    info = ixml.read_sound_info(path)
    assert info is not None and info.drop_frame
    assert (ixml.timecode_of(info) or "").endswith("DF")


def test_no_timecode_without_a_rate(tmp_path: Path):
    """A clock with no frame rate is exactly what probe already renders."""
    path = _wav(tmp_path, ixml=bwf.ixml_document(rate=""))
    info = ixml.read_sound_info(path)
    assert info is not None
    assert ixml.timecode_of(info) is None


@pytest.mark.parametrize("rate", ["0/0", "0", "-25", "garbage", "25/0", ""])
def test_an_unusable_rate_is_no_rate(tmp_path: Path, rate: str):
    path = bwf.write_wav(tmp_path / "r.wav", ixml=bwf.ixml_document(rate=rate))
    info = ixml.read_sound_info(path)
    assert info is not None and info.timecode_rate is None


def test_the_sample_count_spans_both_halves(tmp_path: Path):
    """A day at 48 kHz overflows 32 bits, which is why the field is split."""
    samples = (3 << 32) | 1234
    path = _wav(tmp_path, ixml=bwf.ixml_document(samples_since_midnight=samples))
    info = ixml.read_sound_info(path)
    assert info is not None and info.samples_since_midnight == samples


def test_bext_supplies_the_count_when_ixml_has_none(tmp_path: Path):
    path = _wav(tmp_path, bext=bwf.bext_chunk(time_reference=1_728_000_000))
    info = ixml.read_sound_info(path)
    assert info is not None and info.samples_since_midnight == 1_728_000_000
    # ...but without iXML there is still no rate, so no frame timecode.
    assert ixml.timecode_of(info, fallback_rate_hz=48000) is None


def test_ixml_wins_over_bext_for_the_count(tmp_path: Path):
    """They can disagree; iXML's is the one written beside its own rate."""
    path = _wav(tmp_path,
                ixml=bwf.ixml_document(samples_since_midnight=111),
                bext=bwf.bext_chunk(time_reference=999))
    info = ixml.read_sound_info(path)
    assert info is not None and info.samples_since_midnight == 111


def test_timecode_of_tolerates_nothing_at_all():
    assert ixml.timecode_of(None) is None
    assert ixml.timecode_of(SoundInfo()) is None


# ----------------------------------------------------------------- hostile input


def test_a_doctype_is_refused(tmp_path: Path):
    """Billion laughs and XXE both need one, and no recorder writes one."""
    document = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE BWFXML [<!ENTITY a "aaaaaaaaaa">]>'
        "<BWFXML><SCENE>&a;</SCENE></BWFXML>"
    )
    path = _wav(tmp_path, ixml=document)
    assert ixml.read_sound_info(path) is None


def test_an_entity_declaration_is_refused(tmp_path: Path):
    document = '<!ENTITY x SYSTEM "file:///etc/passwd"><BWFXML></BWFXML>'
    path = _wav(tmp_path, ixml=document)
    assert ixml.read_sound_info(path) is None


def test_malformed_xml_costs_the_slate_and_nothing_else(tmp_path: Path):
    path = _wav(tmp_path, ixml="<BWFXML><SCENE>12A", bext=bwf.bext_chunk())
    info = ixml.read_sound_info(path)
    # bext still parses, so the file is described by what could be read.
    assert info is not None and info.scene is None
    assert info.recorder == "Sound Devices 833"


def test_an_oversized_ixml_chunk_is_not_read(tmp_path: Path):
    """The cap is what stops a corrupt size field becoming an allocation."""
    path = _wav(tmp_path, ixml="<BWFXML><SCENE>12A</SCENE></BWFXML>")
    raw = bytearray(path.read_bytes())
    marker = raw.find(b"iXML")
    raw[marker + 4:marker + 8] = struct.pack("<I", ixml._MAX_IXML_BYTES + 1)
    path.write_bytes(bytes(raw))
    assert ixml.read_sound_info(path) is None


def test_a_zero_size_chunk_does_not_hang_the_walk(tmp_path: Path):
    """Every step must advance, or a truncated file spins here forever."""
    path = _wav(tmp_path, ixml=bwf.ixml_document())
    raw = bytearray(path.read_bytes())
    marker = raw.find(b"fmt ")
    raw[marker + 4:marker + 8] = struct.pack("<I", 0)
    path.write_bytes(bytes(raw))
    # Whatever it makes of the wreckage, it must terminate: _MAX_CHUNKS is the
    # backstop, and every iteration advances by at least the 8-byte header.
    result = ixml.read_sound_info(path)
    assert result is None or isinstance(result, SoundInfo)


def test_a_size_past_the_end_of_the_file_is_survivable(tmp_path: Path):
    path = _wav(tmp_path, ixml=bwf.ixml_document())
    raw = bytearray(path.read_bytes())
    marker = raw.find(b"data")
    raw[marker + 4:marker + 8] = struct.pack("<I", 0xFFFFFFF0)
    path.write_bytes(bytes(raw))
    # The walk steps past the end of the file and stops, so the trailing iXML
    # is unreachable -- which is the correct answer, not a crash.
    assert ixml.read_sound_info(path) is None


def test_a_truncated_file_returns_nothing(tmp_path: Path):
    path = _wav(tmp_path, ixml=bwf.ixml_document())
    raw = path.read_bytes()
    path.write_bytes(raw[:20])
    assert ixml.read_sound_info(path) is None


def test_an_empty_file_returns_nothing(tmp_path: Path):
    path = tmp_path / "empty.wav"
    path.write_bytes(b"")
    assert ixml.read_sound_info(path) is None


def test_a_file_that_is_not_riff_returns_nothing(tmp_path: Path):
    path = tmp_path / "not.wav"
    path.write_bytes(b"NOTRIFF" + bytes(64))
    assert ixml.read_sound_info(path) is None


def test_a_riff_that_is_not_wave_returns_nothing(tmp_path: Path):
    path = tmp_path / "avi.wav"
    path.write_bytes(b"RIFF" + struct.pack("<I", 4) + b"AVI ")
    assert ixml.read_sound_info(path) is None


def test_a_missing_file_returns_nothing(tmp_path: Path):
    assert ixml.read_sound_info(tmp_path / "nope.wav") is None


def test_a_wav_with_neither_chunk_returns_nothing(tmp_path: Path):
    assert ixml.read_sound_info(_wav(tmp_path)) is None


# ------------------------------------------------------------------- parsing


def test_rf64_data_size_comes_from_ds64(tmp_path: Path):
    """Over 4 GB the data size saturates, and a naive skip misses the iXML."""
    path = _wav(tmp_path, ixml=bwf.ixml_document(), magic=b"RF64")
    raw = bytearray(path.read_bytes())
    marker = raw.find(b"data")
    (real_size,) = struct.unpack("<I", bytes(raw[marker + 4:marker + 8]))
    raw[marker + 4:marker + 8] = struct.pack("<I", 0xFFFFFFFF)
    ds64 = (b"ds64" + struct.pack("<I", 28)
            + struct.pack("<Q", len(raw)) + struct.pack("<Q", real_size)
            + struct.pack("<Q", 0) + struct.pack("<I", 0))
    raw[12:12] = ds64
    path.write_bytes(bytes(raw))

    info = ixml.read_sound_info(path)
    assert info is not None and info.scene == "12A"


@pytest.mark.parametrize("value,expected", [
    ("TRUE", True), ("true", True), ("YES", True), ("1", True),
    ("FALSE", False), ("no", False), ("0", False),
    ("maybe", None), ("", None),
])
def test_circled_accepts_what_recorders_actually_write(value, expected):
    info = ixml.parse_ixml(f"<BWFXML><CIRCLED>{value}</CIRCLED></BWFXML>")
    assert (info.circled if info else None) == expected


def test_tags_are_matched_case_insensitively():
    info = ixml.parse_ixml("<BWFXML><scene>12A</scene><Take>3</Take></BWFXML>")
    assert info is not None
    assert (info.scene, info.take) == ("12A", "3")


def test_a_track_without_a_name_is_skipped():
    document = ("<BWFXML><TRACK_LIST>"
                "<TRACK><NAME>Boom</NAME></TRACK>"
                "<TRACK><CHANNEL_INDEX>2</CHANNEL_INDEX></TRACK>"
                "<TRACK><NAME></NAME></TRACK>"
                "</TRACK_LIST></BWFXML>")
    info = ixml.parse_ixml(document)
    assert info is not None and info.track_names == ["Boom"]


def test_an_empty_document_is_no_document():
    assert ixml.parse_ixml("<BWFXML></BWFXML>") is None
    assert ixml.parse_ixml("") is None
    assert ixml.parse_ixml("not xml at all") is None


def test_whitespace_only_fields_are_treated_as_absent():
    info = ixml.parse_ixml("<BWFXML><SCENE>   </SCENE><TAKE>3</TAKE></BWFXML>")
    assert info is not None
    assert info.scene is None and info.take == "3"


def test_is_wav_covers_the_suffixes_a_recorder_writes():
    assert ixml.is_wav(Path("a.wav")) and ixml.is_wav(Path("a.BWF"))
    assert not ixml.is_wav(Path("a.braw"))
    # Wave64 uses GUID chunk identifiers, so this walker must decline it.
    assert not ixml.is_wav(Path("a.w64"))
