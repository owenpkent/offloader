"""Broadcast WAV metadata that ffprobe does not read.

ffprobe reports a WAV's streams and its `bext` time reference and stops there.
Everything a sound report is actually read for -- scene, take, the circled-take
flag, the mixer's note, what each track was -- lives in the `iXML` chunk, which
ffprobe does not surface at all. Without this module a card of production sound
reports a duration and a sample rate and nothing that identifies the take.

iXML is a plain XML document sitting in a RIFF chunk, so reading it costs a few
seeks and a few KB rather than a pass over the media, exactly as `braw.py` does
for the `moov` atom.

It also settles the timecode. `probe` can only divide the `bext` sample count by
the sample rate and render milliseconds, because the frame rate to convert the
remainder lives here, in `SPEED/TIMECODE_RATE`. When the iXML is present the
report can show real frame timecode -- "10:00:00:00 NDF" -- instead of an
honest-but-unfamiliar "10:00:00.000".

A card is untrusted input, so every read here is bounded and every parse
failure is a return of nothing rather than an exception: a malformed chunk
costs a file its slate, never the offload.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree as ET

from .models import SoundInfo
from .util import format_timecode

#: Suffixes that are RIFF WAVE containers. Wave64 (`.w64`) is deliberately not
#: here: it uses GUID chunk identifiers rather than four-character codes, so
#: this walker would misread it rather than politely decline.
WAV_SUFFIXES = {".wav", ".wave", ".bwf", ".rf64"}

#: A malformed file must not be walked forever. Real broadcast WAVs carry well
#: under a dozen chunks.
_MAX_CHUNKS = 512

#: iXML is a few KB in practice. The cap is what stops a corrupt size field
#: from turning a metadata read into a gigabyte allocation.
_MAX_IXML_BYTES = 1 << 20

#: Offsets into the `bext` chunk, from EBU Tech 3285.
_BEXT_DESCRIPTION = (0, 256)
_BEXT_ORIGINATOR = (256, 288)
_BEXT_ORIGINATION_DATE = (320, 330)
_BEXT_ORIGINATION_TIME = (330, 338)
_BEXT_TIME_REFERENCE = 338

_RIFF_MAGIC = (b"RIFF", b"RF64")
_SIZE_SATURATED = 0xFFFFFFFF


def is_wav(path: Path) -> bool:
    return Path(path).suffix.lower() in WAV_SUFFIXES


def _chunks(handle: BinaryIO, file_size: int):
    """Yield `(chunk_id, payload_offset, payload_size)` for a RIFF WAVE file.

    Each iteration seeks for itself, so the caller is free to seek between
    them -- which is what makes it safe to read a chunk's payload mid-walk.
    """
    header = handle.read(12)
    if len(header) < 12:
        return
    if header[0:4] not in _RIFF_MAGIC or header[8:12] != b"WAVE":
        return

    offset = 12
    data_size_64: int | None = None
    seen = 0

    while offset + 8 <= file_size and seen < _MAX_CHUNKS:
        handle.seek(offset)
        head = handle.read(8)
        if len(head) < 8:
            return
        chunk_id = head[0:4]
        (size,) = struct.unpack("<I", head[4:8])
        payload = offset + 8

        if chunk_id == b"ds64":
            # RF64 keeps the real 64-bit sizes here. A file over 4 GB saturates
            # the `data` chunk's own size field, and without this the walk
            # would skip to the wrong place and never reach a trailing iXML.
            blob = handle.read(min(size, 28))
            if len(blob) >= 16:
                (data_size_64,) = struct.unpack("<Q", blob[8:16])
        elif chunk_id == b"data" and size == _SIZE_SATURATED and data_size_64 is not None:
            size = data_size_64

        yield chunk_id, payload, size

        # RIFF chunks are word-aligned. The `+ 8` guarantees forward progress:
        # a zero-size chunk would otherwise pin the walk on one offset forever.
        offset = payload + size + (size & 1)
        seen += 1


def _decode(raw: bytes) -> str:
    """A RIFF text payload as a string, minus its padding."""
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()


def _child(node, name: str):
    """A direct child by name, case- and namespace-insensitively.

    iXML is written by a dozen recorder vendors and the case of the tags is not
    as agreed-upon as the specification suggests.
    """
    if node is None:
        return None
    wanted = name.lower()
    for child in node:
        tag = child.tag
        if isinstance(tag, str) and tag.rsplit("}", 1)[-1].lower() == wanted:
            return child
    return None


def _text(node, name: str) -> str | None:
    found = _child(node, name)
    if found is None or not found.text:
        return None
    return found.text.strip() or None


def _flag(node, name: str) -> bool | None:
    value = _text(node, name)
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in ("true", "yes", "1"):
        return True
    if lowered in ("false", "no", "0"):
        return False
    return None


def _ratio(value: str | None) -> float | None:
    """"25/1" or "30000/1001" or "25" as a float, or None."""
    if not value:
        return None
    text = value.strip()
    try:
        if "/" in text:
            numerator, _, denominator = text.partition("/")
            bottom = float(denominator)
            if bottom == 0:
                return None
            rate = float(numerator) / bottom
        else:
            rate = float(text)
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_ixml(text: str) -> SoundInfo | None:
    """A SoundInfo from an iXML document, or None if it is not usable."""
    if not text or "<" not in text:
        return None

    # A card is untrusted input and ElementTree expands internal entities, so a
    # hand-made iXML chunk could ask for a gigabyte of nested entities or a
    # local file. Neither belongs in a metadata read, and no real recorder
    # writes a doctype, so refusing one costs nothing and closes both.
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        return None

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    info = SoundInfo(
        project=_text(root, "PROJECT"),
        scene=_text(root, "SCENE"),
        take=_text(root, "TAKE"),
        tape=_text(root, "TAPE"),
        note=_text(root, "NOTE"),
        circled=_flag(root, "CIRCLED"),
        file_uid=_text(root, "FILE_UID"),
    )

    speed = _child(root, "SPEED")
    if speed is not None:
        info.timecode_rate = _ratio(_text(speed, "TIMECODE_RATE"))
        flag = (_text(speed, "TIMECODE_FLAG") or "").upper()
        info.drop_frame = flag == "DF"
        info.sample_rate_hz = _int(_text(speed, "FILE_SAMPLE_RATE"))

        high = _int(_text(speed, "TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI")) or 0
        low = _int(_text(speed, "TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_LO"))
        if low is not None:
            # The count is split across two fields because it does not fit in
            # one: at 48 kHz a day is a little over 4.1 billion samples.
            info.samples_since_midnight = (high << 32) | low

    tracks = _child(root, "TRACK_LIST")
    if tracks is not None:
        for track in tracks:
            tag = track.tag
            if not isinstance(tag, str) or tag.rsplit("}", 1)[-1].upper() != "TRACK":
                continue
            name = _text(track, "NAME")
            if name:
                info.track_names.append(name)

    return info if info else None


def _read_bext(info: SoundInfo, payload: bytes) -> None:
    """Fill in what the `bext` chunk knows, without overwriting iXML."""
    if len(payload) < _BEXT_ORIGINATOR[1]:
        return
    description = _decode(payload[slice(*_BEXT_DESCRIPTION)])
    originator = _decode(payload[slice(*_BEXT_ORIGINATOR)])
    info.description = description or None
    # The recorder names itself here. iXML has no field for it, so this is the
    # only place a report can learn what the sound was recorded on.
    info.recorder = originator or None

    if len(payload) >= _BEXT_ORIGINATION_TIME[1]:
        date = _decode(payload[slice(*_BEXT_ORIGINATION_DATE)])
        clock = _decode(payload[slice(*_BEXT_ORIGINATION_TIME)])
        info.origination = " ".join(part for part in (date, clock) if part) or None

    if info.samples_since_midnight is None and len(payload) >= _BEXT_TIME_REFERENCE + 8:
        # iXML's count is preferred when present: it is the one the recorder
        # wrote alongside the rate it should be divided by.
        (reference,) = struct.unpack(
            "<Q", payload[_BEXT_TIME_REFERENCE:_BEXT_TIME_REFERENCE + 8])
        info.samples_since_midnight = reference


def read_sound_info(path: Path) -> SoundInfo | None:
    """The iXML and `bext` metadata in a broadcast WAV, or None.

    Returns None for a file that is not a RIFF WAVE, carries neither chunk, or
    cannot be read -- all of which are ordinary rather than exceptional.
    """
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            file_size = handle.seek(0, 2)
            handle.seek(0)

            info: SoundInfo | None = None
            bext: bytes | None = None

            for chunk_id, payload, size in _chunks(handle, file_size):
                name = chunk_id.strip().lower()
                if name == b"ixml" and info is None:
                    if size <= 0 or size > _MAX_IXML_BYTES:
                        continue
                    handle.seek(payload)
                    info = parse_ixml(_decode(handle.read(size)))
                elif name == b"bext" and bext is None:
                    if 0 < size <= 4096:
                        handle.seek(payload)
                        bext = handle.read(size)

                if info is not None and bext is not None:
                    break
    except OSError:
        return None

    if info is None and bext is None:
        return None
    if info is None:
        info = SoundInfo()
    if bext is not None:
        _read_bext(info, bext)
    return info if info else None


def timecode_of(info: SoundInfo | None, fallback_rate_hz: int | None = None) -> str | None:
    """Frame timecode for a take, when the file says enough to build one.

    Needs three things: where the recording started (a sample count), how many
    of those samples make a second, and how many frames make a second. The
    first two can come from `bext` and the stream; the third only ever comes
    from iXML, which is why `probe` alone has to settle for milliseconds.
    """
    if info is None or info.samples_since_midnight is None:
        return None
    rate = info.timecode_rate
    sample_rate = info.sample_rate_hz or fallback_rate_hz
    if not rate or not sample_rate:
        return None

    seconds = info.samples_since_midnight / sample_rate
    frames = int(round(seconds * rate))
    return format_timecode(frames, rate, info.drop_frame)
