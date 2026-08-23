# Broadcast WAV metadata

ffprobe reads a WAV's streams and its `bext` time reference and stops. Every
field a sound report is actually read for — scene, take, sound roll, the
mixer's note, the circled-take flag, what each track was — lives in the `iXML`
chunk, which ffprobe does not surface at all. `src/offloader/ixml.py` reads it
directly, the same way [`braw.md`](braw.md) reads the `moov` atom.

## What it costs

A few seeks and a few kilobytes. iXML is a plain XML document in a RIFF chunk,
so nothing here reads the audio. The walker is only entered for a file with a
WAV suffix and no video stream, so a card of camera originals never opens a
file for it.

## Where the timecode comes from

Three things are needed to print frame timecode, and no single chunk has all
three:

| What | Where it lives |
| --- | --- |
| Where the recording started | `bext` `TimeReference`, and again in iXML's `SPEED/TIMESTAMP_SAMPLES_SINCE_MIDNIGHT_HI`/`_LO` |
| How many samples make a second | the `fmt ` chunk, which ffprobe already reports |
| How many frames make a second | **only** iXML's `SPEED/TIMECODE_RATE` |

So a WAV with a `bext` chunk and no iXML can be given a clock and nothing more.
`probe` renders that as `10:00:00.000` — milliseconds, not frames. It would be
easy to print `10:00:00:00` instead and it would look more like what a sound
report is expected to show, but the frame count would be invented: the rate to
divide by is not in the file. A guess in the one field the report exists for is
worse than an unfamiliar-looking exact number.

When iXML *is* present the rate comes with it, and the same origin renders as
`10:00:00:00 NDF`, with `SPEED/TIMECODE_FLAG` deciding NDF or DF.

The sample count is split across two fields because it does not fit in one: at
48 kHz a day is a little over 4.1 billion samples, past a 32-bit field. When
both chunks carry a count, iXML's wins — it is the one the recorder wrote
alongside the rate it should be read at.

## What is read

From `iXML`:

`PROJECT`, `SCENE`, `TAKE`, `TAPE` (the sound roll), `NOTE`, `CIRCLED`,
`FILE_UID`, `SPEED/TIMECODE_RATE`, `SPEED/TIMECODE_FLAG`,
`SPEED/FILE_SAMPLE_RATE`, the two `TIMESTAMP_SAMPLES_SINCE_MIDNIGHT` halves,
and every `TRACK_LIST/TRACK/NAME`.

From `bext`, for what iXML has no field for:

`Originator` (the recorder names itself here, and nowhere else), `Description`,
`OriginationDate`/`OriginationTime`, and `TimeReference` as the fallback origin.

Tags are matched case- and namespace-insensitively. The specification says they
are uppercase; a dozen recorder vendors have interpreted that with a range of
enthusiasm.

## A card is untrusted input

Everything here is defensive, because the alternative is that a WAV truncated
by a battery dying mid-write takes the offload down with it. A parse failure
costs a file its slate and never more than that.

- **Every step of the chunk walk advances.** A zero-size chunk would otherwise
  pin the walk on one offset forever. `_MAX_CHUNKS` is the backstop; a real
  broadcast WAV carries well under a dozen chunks.
- **The iXML payload is capped at 1 MiB.** A corrupt size field is otherwise an
  allocation, not a metadata read.
- **A doctype or entity declaration is refused outright.** ElementTree expands
  internal entities, so a hand-made iXML chunk could ask for a gigabyte of
  nested entities (billion laughs) or for a local file (XXE). No recorder
  writes a doctype, so refusing one costs nothing real and closes both. This is
  why the module does not need `defusedxml` as a dependency.
- **RF64 is handled.** Past 4 GB the `data` chunk's size field saturates at
  `0xFFFFFFFF` and the true size moves to `ds64`. A walker that missed this
  would skip to the wrong offset and never reach an iXML chunk filed after the
  audio — and recorders file it on both sides, so both are tested.
- **Wave64 (`.w64`) is declined, not attempted.** It uses GUID chunk
  identifiers rather than four-character codes, so this walker would misread it
  rather than politely fail.
- **The text reaches the HTML report escaped.** It is attacker-controlled
  string data from a removable device, and it is rendered into a page.

`tests/test_ixml.py` carries a reproduction for each of these. The fixtures are
built by `tests/bwf.py` rather than shelled out to ffmpeg, which cannot write
an iXML chunk at all.

## What is not read

- **iXML `HISTORY`, `USER` and `LOCATION` blocks.** Nothing in the report shows
  them yet.
- **The `bext` `CodingHistory` field**, which on some recorders is a long
  free-text log.
- **Sound Devices' structured `Description` string** (`sSPEED=`, `sTAKE=` and
  friends) is not *split into fields*: the encoding is a vendor convention
  rather than part of any specification. It is captured whole, and reaches the
  CSV's `Note` column when iXML supplied no note of its own.
