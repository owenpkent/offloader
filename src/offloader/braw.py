"""Blackmagic RAW support.

ffprobe cannot read BRAW at all — it returns an empty document, not an error —
so without this module a card of camera originals reports nothing but a name and
a size. Everything ffmpeg would have given us has to come from the container.

BRAW is a QuickTime file: `wide` + `mdat` + `moov`, with camera metadata in the
`moov/meta/keys`+`ilst` pair that Blackmagic populates generously. Only the
`moov` is read — a few hundred KB — so a 28 GB clip costs a handful of seeks
rather than a pass over the media.

Three things this buys:

* Real metadata — camera model, lens, reel/scene/take, compression, colour
  science — none of which ffprobe can supply.
* A structural check. A clip whose recording was interrupted (battery pulled,
  card yanked mid-write) has no `moov` at all. It copies perfectly and verifies
  perfectly, because the bytes on the card really are the bytes on the disk, and
  it is unplayable. That is worth knowing while the card is still in your hand.
* Knowing there is no embedded thumbnail, so the report falls back to the
  matching proxy rather than to a placeholder icon.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, BinaryIO

BRAW_SUFFIX = ".braw"

#: Atoms whose children we descend into when hunting for metadata.
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"meta"}

#: QuickTime `data` atom type indicators, plus the three Blackmagic uses.
_TYPE_UTF8 = 1
_TYPE_INT_BE = 21
_TYPE_UINT_BE = 22
_TYPE_FLOAT32 = 23
_TYPE_FLOAT_PAIR = 71     # two float32: sensor area, crop size, crop origin
_TYPE_INT16 = 76          # flags and small enums
_TYPE_UINT32 = 77         # codec bitrate, in bits per second

#: Anything longer than this in a metadata value is a blob (the embedded 3D LUT
#: is ~431 KB) and is summarised rather than carried around.
_MAX_VALUE_BYTES = 4096


class ContainerState(str, Enum):
    OK = "ok"
    NO_MOOV = "no-moov"          # recording interrupted; unplayable
    TRUNCATED = "truncated"      # an atom runs past the end of the file
    UNREADABLE = "unreadable"
    NOT_BRAW = "not-braw"


@dataclass
class ContainerCheck:
    state: ContainerState
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state is ContainerState.OK

    @property
    def is_fatal(self) -> bool:
        """Whether the clip is structurally unplayable, however cleanly it copied."""
        return self.state in (ContainerState.NO_MOOV, ContainerState.TRUNCATED)


@dataclass
class BrawInfo:
    """What the container knows about itself."""

    width: int | None = None
    height: int | None = None
    fps: float | None = None
    duration_sec: float | None = None
    frame_count: int | None = None

    camera_type: str | None = None          # "Blackmagic PYXIS 6K"
    camera_id: str | None = None
    manufacturer: str | None = None
    firmware_version: str | None = None
    lens_type: str | None = None            # "Sigma 24-70mm F2.8 DG DN II | Art 024"

    clip_number: str | None = None
    reel: str | None = None
    scene: str | None = None
    take: str | None = None
    good_take: bool | None = None
    camera_number: str | None = None

    compression_ratio: str | None = None    # "8:1"
    codec_bitrate_bps: int | None = None
    iso_gain: float | None = None
    colour_science_gen: int | None = None    # 5 for Gen 5
    lut_name: str | None = None
    date_recorded: str | None = None

    #: Every decoded key, for callers that want more than the named fields.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def resolution(self) -> str | None:
        if self.width and self.height:
            return f"{self.width} x {self.height}"
        return None

    def slate(self) -> str | None:
        """"Reel 1 · Scene 1 · Take 14" — the identity a script supervisor uses."""
        parts = [f"Reel {self.reel}" if self.reel else None,
                 f"Scene {self.scene}" if self.scene else None,
                 f"Take {self.take}" if self.take else None]
        present = [p for p in parts if p]
        return " · ".join(present) if present else None

    def camera_summary(self) -> str | None:
        bits = [b for b in (self.camera_type, self.lens_type) if b]
        return "  ·  ".join(bits) if bits else None


# ------------------------------------------------------------------ atoms


def _read_atom_header(handle: BinaryIO) -> tuple[int, bytes, int] | None:
    """(size, type, header length) at the current position, or None at EOF."""
    header = handle.read(8)
    if len(header) < 8:
        return None
    size = struct.unpack(">I", header[:4])[0]
    atom_type = header[4:8]
    header_length = 8
    if size == 1:                       # 64-bit extended size
        extended = handle.read(8)
        if len(extended) < 8:
            return None
        size = struct.unpack(">Q", extended)[0]
        header_length = 16
    return size, atom_type, header_length


def _top_level_atoms(handle: BinaryIO, file_size: int):
    """Walk the top level by seeking. Never reads the media payload, so this
    costs the same on a 28 GB clip as on a 5 MB still."""
    offset = 0
    while offset + 8 <= file_size:
        handle.seek(offset)
        header = _read_atom_header(handle)
        if header is None:
            return
        size, atom_type, header_length = header
        if size == 0:                   # runs to end of file
            size = file_size - offset
        if size < header_length:
            return
        yield offset, size, atom_type, header_length
        offset += size


def check_container(path: Path) -> ContainerCheck:
    """Is this file structurally complete?

    A camera that lost power mid-record leaves `mdat` with no `moov`. The bytes
    copy and checksum perfectly; the clip is still unplayable.
    """
    path = Path(path)
    try:
        file_size = path.stat().st_size
        with open(path, "rb") as handle:
            seen: list[bytes] = []
            for offset, size, atom_type, _header in _top_level_atoms(handle, file_size):
                seen.append(atom_type)
                if offset + size > file_size:
                    return ContainerCheck(
                        ContainerState.TRUNCATED,
                        f"{atom_type.decode('latin-1', 'replace')} atom claims "
                        f"{size:,} bytes but only {file_size - offset:,} remain")
            if not seen:
                return ContainerCheck(ContainerState.NOT_BRAW, "no atoms found")
            if b"moov" not in seen:
                return ContainerCheck(
                    ContainerState.NO_MOOV,
                    "no moov atom — the recording was interrupted and the clip "
                    "will not play, even though its bytes copied intact")
            return ContainerCheck(ContainerState.OK)
    except OSError as exc:
        return ContainerCheck(ContainerState.UNREADABLE, str(exc))


def _find_moov(handle: BinaryIO, file_size: int) -> bytes | None:
    for offset, size, atom_type, header_length in _top_level_atoms(handle, file_size):
        if atom_type == b"moov":
            handle.seek(offset + header_length)
            # `size` came out of the file and can claim up to 2**64-1 through
            # the extended-size header. Ask only for what is actually there,
            # the way check_container already does, or a corrupt clip turns
            # into a request the allocator cannot serve.
            available = max(0, file_size - (offset + header_length))
            return handle.read(min(size - header_length, available))
    return None


def _uint(buf: bytes, offset: int, width: int, limit: int) -> int | None:
    """A big-endian unsigned int, or None if it does not fit.

    Every fixed offset in this module is measured from an atom header whose
    size the *file* declared. `limit` is that atom's real end, so a truncated
    atom yields None here instead of a short slice and a struct.error three
    frames up.
    """
    if offset < 0 or offset + width > min(limit, len(buf)):
        return None
    return int.from_bytes(buf[offset:offset + width], "big")


def _walk(buf: bytes, start: int, end: int):
    offset = start
    while offset + 8 <= end:
        size = struct.unpack(">I", buf[offset:offset + 4])[0]
        atom_type = buf[offset + 4:offset + 8]
        header_length = 8
        if size == 1:
            if offset + 16 > end:
                return
            size = struct.unpack(">Q", buf[offset + 8:offset + 16])[0]
            header_length = 16
        if size < header_length or offset + size > end:
            return
        yield offset, size, atom_type, header_length
        offset += size


#: Real BRAW nests moov/trak/mdia/minf/stbl about six deep. Anything past this
#: is a malformed or hostile file, and each level costs an interpreter frame.
_MAX_ATOM_DEPTH = 64


def _descend(buf: bytes, start: int, end: int, wanted: bytes, depth: int = 0):
    """Depth-first search for an atom type inside a parsed buffer."""
    if depth >= _MAX_ATOM_DEPTH:
        return None
    for offset, size, atom_type, header_length in _walk(buf, start, end):
        if atom_type == wanted:
            return offset, size, header_length
        if atom_type in _CONTAINERS:
            found = _descend(buf, offset + header_length, offset + size,
                             wanted, depth + 1)
            if found:
                return found
    return None


# ------------------------------------------------------------------ metadata


def _decode_value(type_indicator: int, payload: bytes) -> Any:
    if len(payload) > _MAX_VALUE_BYTES:
        return f"<{len(payload):,} bytes>"
    try:
        if type_indicator == _TYPE_UTF8:
            return payload.decode("utf-8", "replace")
        if type_indicator == _TYPE_FLOAT32 and len(payload) >= 4:
            return round(struct.unpack(">f", payload[:4])[0], 5)
        if type_indicator == _TYPE_FLOAT_PAIR and len(payload) >= 8:
            first, second = struct.unpack(">ff", payload[:8])
            return (first, second)
        if type_indicator == _TYPE_INT16 and len(payload) >= 2:
            return struct.unpack(">h", payload[:2])[0]
        if type_indicator == _TYPE_UINT32 and len(payload) >= 4:
            return struct.unpack(">I", payload[:4])[0]
        if type_indicator in (_TYPE_INT_BE, _TYPE_UINT_BE) and payload:
            return int.from_bytes(payload, "big",
                                  signed=type_indicator == _TYPE_INT_BE)
    except (struct.error, ValueError):
        return None
    return None


def _read_metadata_pairs(moov: bytes) -> dict[str, Any]:
    meta = _descend(moov, 0, len(moov), b"meta")
    if not meta:
        return {}
    meta_offset, meta_size, meta_header = meta
    keys = _descend(moov, meta_offset + meta_header, meta_offset + meta_size, b"keys")
    ilst = _descend(moov, meta_offset + meta_header, meta_offset + meta_size, b"ilst")
    if not keys or not ilst:
        return {}

    keys_offset, keys_size, _ = keys
    try:
        count = struct.unpack(">I", moov[keys_offset + 12:keys_offset + 16])[0]
    except struct.error:
        return {}

    names: list[str] = []
    cursor = keys_offset + 16
    for _ in range(min(count, 4096)):
        if cursor + 8 > keys_offset + keys_size:
            break
        entry_size = struct.unpack(">I", moov[cursor:cursor + 4])[0]
        if entry_size < 8:
            break
        names.append(moov[cursor + 8:cursor + entry_size].decode("utf-8", "replace"))
        cursor += entry_size

    values: dict[str, Any] = {}
    ilst_offset, ilst_size, ilst_header = ilst
    cursor = ilst_offset + ilst_header
    limit = ilst_offset + ilst_size
    while cursor + 16 <= limit:
        item_size = _uint(moov, cursor, 4, limit)
        index = _uint(moov, cursor + 4, 4, limit)
        if item_size is None or index is None:
            break
        if item_size < 16 or cursor + item_size > limit:
            break
        data_size = _uint(moov, cursor + 8, 4, limit)
        # The loop condition only guarantees 16 bytes, but the type indicator
        # sits at 16..20: an item declaring exactly 16 bytes at the end of the
        # atom leaves nothing to read.
        type_indicator = _uint(moov, cursor + 16, 4, limit)
        if data_size is None or type_indicator is None:
            break
        type_indicator &= 0x00FFFFFF
        payload = moov[cursor + 24:cursor + 8 + data_size]
        if 1 <= index <= len(names):
            decoded = _decode_value(type_indicator, payload)
            if decoded not in (None, ""):
                values[names[index - 1]] = decoded
        cursor += item_size
    return values


def _handler_type(moov: bytes, start: int, end: int) -> bytes:
    """The trak's handler type: `vide`, `soun`, `tmcd`, `meta`."""
    hdlr = _descend(moov, start, end, b"hdlr")
    if not hdlr:
        return b""
    # hdlr body: version+flags(4), pre_defined(4), handler_type(4)
    offset = hdlr[0] + 8 + 8
    return moov[offset:offset + 4]


def _read_timing(moov: bytes) -> tuple[float | None, float | None, int | None]:
    """(fps, duration seconds, frame count) from the video track.

    The video track is selected by handler type, not by position. A real BRAW
    clip also carries a `soun` track whose sample count runs into the tens of
    millions (one per audio sample, not per frame) — taking whichever track
    came first would report that as the frame count and the duration with it.
    """
    for offset, size, atom_type, header_length in _walk(moov, 0, len(moov)):
        if atom_type != b"trak":
            continue
        if _handler_type(moov, offset + header_length, offset + size) != b"vide":
            continue
        stts = _descend(moov, offset + header_length, offset + size, b"stts")
        mdhd = _descend(moov, offset + header_length, offset + size, b"mdhd")
        if not stts or not mdhd:
            continue

        mdhd_offset, mdhd_size, _ = mdhd
        mdhd_end = mdhd_offset + mdhd_size
        version = _uint(moov, mdhd_offset + 8, 1, mdhd_end)
        if version is None:
            continue
        if version == 1:
            timescale = _uint(moov, mdhd_offset + 28, 4, mdhd_end)
            duration = _uint(moov, mdhd_offset + 32, 8, mdhd_end)
        else:
            timescale = _uint(moov, mdhd_offset + 20, 4, mdhd_end)
            duration = _uint(moov, mdhd_offset + 24, 4, mdhd_end)
        if not timescale or duration is None:
            continue

        stts_offset, stts_size, _ = stts
        stts_end = stts_offset + stts_size
        entry_count = _uint(moov, stts_offset + 12, 4, stts_end)
        if entry_count is None:
            continue
        frames, delta = 0, None
        cursor = stts_offset + 16
        for _ in range(min(entry_count, 4096)):
            # The count is the file's claim; the atom's own end is the fact.
            sample_count = _uint(moov, cursor, 4, stts_end)
            sample_delta = _uint(moov, cursor + 4, 4, stts_end)
            if sample_count is None or sample_delta is None:
                break
            frames += sample_count
            delta = delta or sample_delta
            cursor += 8

        seconds = duration / timescale
        fps = (timescale / delta) if delta else (frames / seconds if seconds else None)
        # A track with no samples is the timecode track, not the picture.
        if frames:
            return (round(fps, 6) if fps else None, seconds, frames)
    return (None, None, None)


def _as_pixels(value: Any) -> int | None:
    """A sensor dimension, or None if the file's bytes did not decode to one."""
    try:
        if not math.isfinite(value):
            return None
        pixels = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return pixels if 0 < pixels <= 100_000 else None


def read_info(path: Path) -> BrawInfo | None:
    """Parse a BRAW file's metadata. Returns None if it is not readable BRAW."""
    path = Path(path)
    try:
        file_size = path.stat().st_size
        with open(path, "rb") as handle:
            moov = _find_moov(handle, file_size)
    except OSError:
        return None
    if not moov:
        return None

    values = _read_metadata_pairs(moov)
    fps, duration, frames = _read_timing(moov)

    info = BrawInfo(fps=fps, duration_sec=duration, frame_count=frames, raw=values)

    crop = values.get("crop_size") or values.get("sensor_area_captured")
    if isinstance(crop, tuple) and len(crop) >= 2:
        # These arrive as a raw IEEE754 pair out of the file, so they can be
        # NaN or infinity, neither of which survives int().
        width, height = _as_pixels(crop[0]), _as_pixels(crop[1])
        if width and height:
            info.width, info.height = width, height

    info.camera_type = values.get("camera_type")
    info.camera_id = values.get("camera_id")
    info.manufacturer = values.get("manufacturer")
    info.firmware_version = values.get("firmware_version")
    info.lens_type = values.get("lens_type")
    info.clip_number = values.get("clip_number")
    info.reel = values.get("reel_name")
    info.scene = values.get("scene")
    info.take = values.get("take")
    info.camera_number = values.get("camera_number")
    info.compression_ratio = values.get("braw_compression_ratio")
    info.date_recorded = values.get("date_recorded")
    info.lut_name = values.get("post_3dlut_embedded_title")

    good = values.get("good_take")
    if isinstance(good, str):
        info.good_take = good.strip().lower() == "true"

    bitrate = values.get("braw_codec_bitrate")
    if isinstance(bitrate, int):
        info.codec_bitrate_bps = bitrate
    gain = values.get("analog_gain")
    if isinstance(gain, (int, float)):
        info.iso_gain = float(gain)
    generation = values.get("viewing_bmdgen")
    if isinstance(generation, int):
        info.colour_science_gen = generation

    return info


def is_braw(path: Path) -> bool:
    return Path(path).suffix.lower() == BRAW_SUFFIX
