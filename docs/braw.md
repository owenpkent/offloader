# Blackmagic RAW

BRAW is the one format where a general-purpose offload tool goes blind. **ffprobe
returns an empty document for a `.braw` file** — not an error, not partial data,
nothing. Without special handling a card of camera originals reports a filename,
a size, and a placeholder icon. ShotPut Pro's own report labels these clips
"FFmpeg Utility" and stops there.

Everything below was worked out against real footage: a Blackmagic PYXIS 6K
shoot, `A001_08041254_C001.braw` at 27.8 GB with a matching proxy.

## The container

BRAW is QuickTime:

```
wide   8 bytes
mdat   4,648,952 bytes          <- the picture
moov   435,568 bytes            <- everything else
  trak                          <- video: mdhd (timescale), stts (frame count)
  trak                          <- timecode
  meta
    keys  1,262 bytes           <- 53 metadata key names
    ilst  432,908 bytes         <- their values
```

The `moov` sits *after* the media, so metadata is read by seeking the top-level
atom chain — a handful of 8-byte header reads — and then reading only the `moov`.
A 28 GB clip costs the same as a 5 MB one. There is a test that asserts this:
reading metadata from a 40 MB file must touch under 1 MB.

Two useful facts fell out of the survey:

- **There is no embedded thumbnail.** The 431 KB inside `ilst` is the embedded
  3D LUT, not a preview. So a contact sheet has to come from somewhere else.
- Blackmagic uses three non-standard `data` type indicators alongside the
  QuickTime ones: `71` is a pair of float32 (resolutions, crop rectangles), `76`
  is int16 (flags and small enums), `77` is uint32 (the codec bitrate).

## What comes out

From that PYXIS clip, decoded:

| | |
| --- | --- |
| Camera | Blackmagic PYXIS 6K, firmware 10.2 |
| Lens | Sigma 24-70mm F2.8 DG DN II \| Art 024 |
| Resolution | 6048 × 4032 |
| Compression | 8:1, 924 Mb/s |
| Slate | Reel 1 · Scene 1 · Take 14, camera A |
| Good take | false |
| Colour | Gen 5, "Gen 5 Film to Extended Video" |

Plus 44 raw keys in total — shutter type, environment, day/night, sensor
photosite pitch, crop origin, lens correction flags, camera UUID, record date.
All of it is on `MediaInfo.camera` and in `BrawInfo.raw`, and the named fields
appear in the PDF, CSV and HTML reports.

## Thumbnails come from the proxy

Nothing but Blackmagic's SDK can decode BRAW, so the frames have to come from
the proxy the camera recorded alongside it — same take, same framing, already on
the card:

```
A001/A001_08041254_C001.braw          27.8 GB
A001/Proxy/A001_08041254_C001.mp4      258 MB     <- same stem
```

Matching is by stem across a set of candidate directories (`Proxy`, `proxy`,
`Proxies`, beside the original), so it survives the usual naming variations. When
no proxy exists the clip falls back to the placeholder icon rather than failing.

The report always says where the frames came from — `Frames from proxy:
A001_08041254_C001.mp4`. Without that line a reader would take the contact sheet
as evidence the original decoded, which it is not.

## The check checksums cannot do

A clip whose recording was interrupted — battery pulled, card yanked mid-write —
has `mdat` but no `moov`. The camera never got to write the index.

That file **copies perfectly and verifies perfectly.** The bytes on the card
really are the bytes on the disk, so every checksum matches and the job reports
`Verified`. It is also completely unplayable.

```
size            : 4,648,960 bytes (vs 5,084,528)
copies cleanly  : yes — it is just bytes
checksum        : a26dc86735a320b0
container check : no-moov
detail          : no moov atom — the recording was interrupted and the clip
                  will not play, even though its bytes copied intact
```

So every `.braw` gets a structural check during the offload, and a failure
becomes a job warning. The time to discover an unplayable clip is while the card
is still in your hand — not in the edit, after it was reformatted. This is the
one class of loss a checksum categorically cannot see: the copy is *correct*, and
the original was already broken.

The same walk catches an atom whose declared length runs past the end of the
file, which is what a truncated transfer from some other tool looks like.

## Reading a file that is lying to you

Every offset in this parser is derived from a length the *file* supplied, and a
corrupt clip supplies whatever it likes. The rule throughout is that a declared
size is a claim and the buffer is the fact:

- Fixed-offset reads inside an atom are bounded by that atom's real end, so a
  short `mdhd` or an `stts` promising entries it does not carry yields nothing
  rather than raising out of the parser.
- Descent into container atoms is depth-limited. Real BRAW nests about six
  deep; without a cap, a 16 KB file of nothing but nested headers exhausts the
  interpreter stack.
- The `moov` read is clamped to the bytes actually present. The extended-size
  header can claim up to 2^64, and asking the allocator for that is its own
  kind of failure.
- Sensor dimensions arrive as a raw IEEE 754 pair, so they can decode to NaN or
  infinity. Neither survives `int()`, and both are rejected.

None of this makes a broken clip readable. It makes a broken clip cost its own
metadata instead of the rest of the card: metadata is read *after* the bytes
are copied and verified, and a container that will not parse now leaves a
warning on the job rather than ending it. `tests/test_fuzz_edges.py` fuzzes
this by pinning the atom shape `_read_timing` looks for and generating the two
bodies it then reads at fixed offsets.

## Testing

Fixtures are synthesised — `tests/test_braw.py` builds a structurally faithful
BRAW container atom by atom — because a real clip is gigabytes and is somebody's
footage. A synthetic fixture that only matches itself is worthless, so one test
runs the same assertions against a real PYXIS file when one is present and skips
cleanly when it is not.

### Validated against real footage

The parser was developed against a single 5 MB still, then run over a working
drive: **510 clips, 2.84 TB**, from two camera bodies it had never seen.

| | |
| --- | --- |
| Structurally sound | 510 of 510 |
| Time to check all of them | 45 s, about 88 ms per clip |
| Largest clip parsed | 79 GB, reading 646 KB of it |
| Bodies | Blackmagic Pocket Cinema Camera 6K (fw 7.3), PYXIS 6K (fw 10.2) |
| Frame counts | 1,744 to 23,858 |

Both bodies parsed with no expected key missing, including a difference the
5 MB sample could not have shown: the Pocket 6K records at **constant quality**
(`Q3`) where the PYXIS records at a **ratio** (`8:1`). Both appear correctly as
the codec label.

That sweep also fixed a real bug — see below.

### The bug real clips found

`_read_timing` took the first track that had samples. Every file to hand happened
to list `vide` first, so it worked.

A real BRAW clip also carries a `soun` track, and its sample count is one per
*audio* sample: 34,242,000 for an 11-minute take. An audio-first file would have
reported 34 million "frames" and a duration to match — a wrong number in the
report, with nothing to flag it as wrong.

The video track is now selected by handler type. The test synthesiser can emit
tracks in either order, and a regression test asserts the result does not depend
on it.

## Limits

- **No decoding.** Frames come from the proxy or not at all. Real BRAW decoding
  needs the Blackmagic RAW SDK, which is a separate, platform-specific
  dependency.
- **Timecode** is reported as a start of `00:00:00:00` derived from the frame
  rate. The real start timecode lives in the `tmcd` track's sample data, which
  is not parsed yet — the track is present and located, only its payload is
  unread.
- **Audio tracks** inside BRAW are not enumerated; the clip line shows picture
  metadata only. The `soun` track is found and deliberately skipped, so the
  information is a parse away.
- **Spanned clips** (a single take split across files) are treated as separate
  files. Blackmagic does not normally span, but a very long take can.
- **`.sidecar` files** are copied like any other file but are not linked to
  their clip in the report, so a missing one is not flagged.
