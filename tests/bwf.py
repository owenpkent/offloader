"""Build broadcast WAVs for the tests.

ffmpeg writes a `bext` chunk but has no way to write `iXML`, and iXML is where
everything a sound report is read for lives. So the fixtures are assembled here
rather than shelled out for, which also means the tests do not need ffmpeg on
PATH to exercise the parser.
"""

from __future__ import annotations

import struct
from pathlib import Path

NUL = bytes(1)

IXML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<BWFXML>
  <IXML_VERSION>1.5</IXML_VERSION>
  <PROJECT>{project}</PROJECT>
  <SCENE>{scene}</SCENE>
  <TAKE>{take}</TAKE>
  <TAPE>{tape}</TAPE>
  <CIRCLED>{circled}</CIRCLED>
  <NOTE>{note}</NOTE>
  <FILE_UID>{uid}</FILE_UID>
  <SPEED>
    <MASTER_SPEED>{rate}</MASTER_SPEED>
    <TIMECODE_RATE>{rate}</TIMECODE_RATE>
    <TIMECODE_FLAG>{flag}</TIMECODE_FLAG>
    <FILE_SAMPLE_RATE>{sample_rate}</FILE_SAMPLE_RATE>
    <TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI>{hi}</TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI>
    <TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO>{lo}</TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO>
  </SPEED>
  <TRACK_LIST>
    <TRACK_COUNT>{track_count}</TRACK_COUNT>
{tracks}
  </TRACK_LIST>
</BWFXML>
"""

TRACK_TEMPLATE = """    <TRACK>
      <CHANNEL_INDEX>{index}</CHANNEL_INDEX>
      <INTERLEAVE_INDEX>{index}</INTERLEAVE_INDEX>
      <NAME>{name}</NAME>
    </TRACK>"""


def ixml_document(*, project="ChairsDoc", scene="12A", take="3", tape="SR082226",
                  circled="TRUE", note="wind on the boom", uid="ABC123",
                  rate="25/1", flag="NDF", sample_rate=48000,
                  samples_since_midnight=1_728_000_000,
                  track_names=("Boom", "Lav 1")) -> str:
    tracks = "\n".join(
        TRACK_TEMPLATE.format(index=index, name=name)
        for index, name in enumerate(track_names, start=1)
    )
    return IXML_TEMPLATE.format(
        project=project, scene=scene, take=take, tape=tape, circled=circled,
        note=note, uid=uid, rate=rate, flag=flag, sample_rate=sample_rate,
        hi=samples_since_midnight >> 32,
        lo=samples_since_midnight & 0xFFFFFFFF,
        track_count=len(track_names), tracks=tracks,
    )


def _chunk(identifier: bytes, payload: bytes) -> bytes:
    """A RIFF chunk, word-aligned as the format requires."""
    body = payload + (NUL if len(payload) % 2 else b"")
    return identifier + struct.pack("<I", len(payload)) + body


def bext_chunk(*, description="", originator="Sound Devices 833",
               originator_reference="", origination_date="2026-08-22",
               origination_time="10:00:00", time_reference=1_728_000_000) -> bytes:
    payload = (
        description.encode("ascii", "replace").ljust(256, NUL)[:256]
        + originator.encode("ascii", "replace").ljust(32, NUL)[:32]
        + originator_reference.encode("ascii", "replace").ljust(32, NUL)[:32]
        + origination_date.encode("ascii", "replace").ljust(10, NUL)[:10]
        + origination_time.encode("ascii", "replace").ljust(8, NUL)[:8]
        + struct.pack("<Q", time_reference)
        + struct.pack("<H", 1)
        + bytes(254)                     # UMID, then reserved
    )
    return _chunk(b"bext", payload)


def fmt_chunk(*, channels=2, sample_rate=48000, bit_depth=24) -> bytes:
    block_align = channels * bit_depth // 8
    payload = struct.pack(
        "<HHIIHH", 1, channels, sample_rate,
        sample_rate * block_align, block_align, bit_depth,
    )
    return _chunk(b"fmt ", payload)


def write_wav(path: Path, *, frames=480, channels=2, sample_rate=48000,
              bit_depth=24, ixml: str | None = None, bext: bytes | None = None,
              ixml_first=False, magic=b"RIFF",
              ixml_identifier=b"iXML") -> Path:
    """A playable broadcast WAV carrying the chunks the tests need.

    `ixml_first` puts the metadata before the audio, which is where some
    recorders file it; the default puts it after, which is where the rest do.
    A walker that only handles one of the two would pass half the cards.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    audio = bytes(frames * channels * bit_depth // 8)
    body = fmt_chunk(channels=channels, sample_rate=sample_rate,
                     bit_depth=bit_depth)
    if bext is not None:
        body += bext
    metadata = _chunk(ixml_identifier, ixml.encode("utf-8")) if ixml else b""
    data = _chunk(b"data", audio)
    body += (metadata + data) if ixml_first else (data + metadata)

    payload = b"WAVE" + body
    path.write_bytes(magic + struct.pack("<I", len(payload)) + payload)
    return path


def write_sound_card(root: Path, takes=3, **overrides) -> Path:
    """A folder of takes, as a recorder would leave it."""
    root = Path(root)
    for index in range(1, takes + 1):
        options: dict = dict(
            scene="12A", take=str(index),
            samples_since_midnight=1_728_000_000 + (index - 1) * 48_000 * 90,
        )
        options.update(overrides)
        write_wav(
            root / f"MIX_{index:03d}.wav",
            ixml=ixml_document(**options),
            bext=bext_chunk(time_reference=options["samples_since_midnight"]),
        )
    return root
