# Offloading from an edit timeline

A card offload knows its source: everything under one root, and the only
question is whether it arrived intact. This is the other job, the one that
turns up when a cut comes back from an editor.

**Which files does this timeline actually need, and are they all here?**

```sh
offloader resolve --timeline "01 Chairs Row V6.xml" --search-root E:\ChairsDoc
offloader offload --timeline "01 Chairs Row V6.xml" --search-root E:\ChairsDoc \
                  --dest E:\ChairsDoc\RowV6_Media
```

`resolve` copies nothing and answers the question. `offload` answers it and
then copies the files that are missing, with the same verified copy, the same
checksums and the same reports as a card.

Reading timelines is optional and not installed by default:

```sh
pip install "offloader[timeline]"
```

## What it does, and in what order

Every reference gets exactly one status. The order the tests run in is the
whole design.

| Status | Meaning | Copied? |
| --- | --- | --- |
| `present` | A file of that name is under a search root | no |
| `substituted` | A camera original, satisfied by a frame-matched proxy already here | no |
| `gap` | Not here, but readable where the timeline says it is | **yes** |
| `ambiguous` | Several files under the roots share the name and differ | no, refuses |
| `missing` | Not here, and not where the timeline says either | no, cannot |
| `generated` | The timeline carries no media for it at all | no, cannot |

1. **A generated reference stops immediately.** A Premiere-native graphic, a
   title, a generator: there is no file, and no search would ever find one.
2. **Anything under a search root with that basename wins**, whatever path the
   timeline asked for. An editor's export addresses media on the editor's
   volume, so the path is almost always wrong and the name almost always
   right. This is the test that stops a file being copied a second time under
   a name the drive already holds.
3. **A camera original may be satisfied by a proxy**, if the frame counts
   agree. See below.
4. **Only then is a readable file a gap**, which is the thing worth copying.
5. Anything left is `missing`, and no offload will fix it.

`resolve` exits non-zero when anything is `ambiguous` or `missing`. A
`generated` reference does not fail it: no offload can supply one, and holding
the exit code hostage to it would only teach a caller to ignore the code.

## Why `ambiguous` refuses instead of choosing

This is the part that earns the feature.

On the conform this was written for, 20 basenames had more than one copy under
the search root. Seven were byte-identical, which is harmless. The other
thirteen were *different files sharing a name*:

- Eleven were "MISSING MEDIA" stand-in slates left by an earlier conform,
  sitting alongside the real archival footage that arrived weeks later. Both
  under the same root, both matching the name the timeline asks for.
- Two were the same sound recorder filename from two different cards
  (`f2/260223_009.WAV` and `f3/260223_009.WAV`), which are different
  recordings that happen to be named alike.

Relinking by filename picks one of those at random. When it picks a slate, the
clip **reports as online** and the cut looks fine until someone watches it.

So the resolver refuses. It reports every candidate, and it offers a guess -
the candidate whose trailing path components match the timeline's path for
longest - clearly labelled as a guess. On this material that heuristic picked
the correct file all thirteen times, which is a good reason to show it and not
a good enough reason to act on it unsupervised.

Byte-identical duplicates are not ambiguous. They resolve to one copy, with the
others recorded in `other_candidates`.

## Why a proxy may stand in for a camera original

A timeline that references 55 GB of BRAW usually references it because that is
what the *camera* wrote, not because the cut needs it: the editor cut with
proxies, and the proxies are already on the drive.

`substituted` means a stem-matched proxy was found under a search root **and
the frame counts agree**. That check is not decoration. Relink a clip to a file
one frame shorter and every edit point after it moves, quietly, and the first
person to notice is whoever watches the export.

The count comes from `probe.py`, which reads BRAW out of the container because
ffprobe cannot open the format at all. Where a count cannot be established -
no ffprobe, an unreachable original - the substitution is still offered but the
note says `frame counts unverified` rather than implying agreement. Where the
counts are established and **disagree**, the reference is reported `ambiguous`
and nothing is substituted.

`--no-proxy-substitution` turns the whole step off, and every camera original
becomes an ordinary gap.

## What OpenTimelineIO is trusted with

Reading. Nothing else.

OTIO recovers media references from this material exactly. Measured against a
Premiere-exported FCP 7 XML with 913 clipitems, it found all 334 unique media
references, with none missed and none invented, including the clipitems whose
`<file>` element is an id reference carrying no path of its own. A sweep that
reads only `pathurl` finds a source's first use and silently misses every
later one, which is how a file gets left out of a package.

Its reading of **structure**, on the same file, is wrong:

| | the XML says | OTIO reports |
| --- | --- | --- |
| sequence rate | 24, ntsc FALSE | 29.97 |
| duration | 67148 frames | 96231 frames |
| audio tracks | 23 | 12 |

So nothing in this module reads a rate, a duration, a track or a timecode from
the adapter, and none reaches a caller. If that ever changes, it needs its own
measurement first.

One more subtlety in the counts: `uses` is **placements in the cut, not
clipitems in the file**. An FCP 7 XML writes one clipitem per audio channel, so
a stereo effect placed ten times appears as twenty clipitems across two tracks.
OTIO reports ten, which is the number a human means.

## Adapters

The adapters are not part of OpenTimelineIO's core distribution. A bare
`pip install opentimelineio` reads `.otio` and nothing else - none of the
formats an NLE actually exports.

| Format | Suffix | Package |
| --- | --- | --- |
| FCP 7 XML (Premiere, Resolve) | `.xml` | `otio-fcp-adapter` |
| Final Cut Pro X | `.fcpxml` | `otio-fcpx-xml-adapter` |
| CMX 3600 EDL | `.edl` | `otio-cmx3600-adapter` |
| AAF (Avid) | `.aaf` | `otio-aaf-adapter` |
| OpenTimelineIO | `.otio` | (core) |

`offloader[timeline]` installs the first of these, because FCP 7 XML is what
Premiere and Resolve both export for interchange. The others work if installed;
`--adapter` overrides the choice made from the suffix.

## Layout at the destination

`--layout mirror` (the default) keeps each file's path below its volume root,
so `D:\ChairsDoc_backup\audio\boom\260828_001.wav` lands at
`ChairsDoc_backup\audio\boom\260828_001.wav`. Longer than a flat folder, and
worth it for two reasons: it records where each file came from, and two files
of the same name from different trees cannot land on each other.

`--layout flat` puts everything in one folder. If that would put two files on
one path, the offload is refused before a byte moves, the same as for a card.

## The safety rule is different here, and narrower

For a card, `assert_safe_destinations` refuses a destination inside the source.
That is right for a card: the source root *is* the card, and writing into it is
how you lose it.

It is the wrong question for a selection. The search roots are a library being
read, and collecting a cut's gaps into a new folder on that same drive is the
ordinary case:

```sh
offloader offload --timeline cut.xml --search-root E:\ChairsDoc \
                  --dest E:\ChairsDoc\RowV6_Media
```

So a selection is checked against a narrower property that is exactly what
matters: **no file being read may sit at or beneath somewhere being written**.
That admits the case above and still refuses the one that destroys data. A
destination-relative path that is absolute, or that climbs out with `..`, is
refused too, before the job starts rather than partway through it.

## What this deliberately does not do

- **It does not rewrite the timeline.** Nothing here relinks, conforms, or
  emits a repointed XML. Choosing between two files that share a name is a
  decision with a person's judgement in it, and a tool that made it silently
  would be worse than no tool. `resolve` gives you the table; you relink.
- **It does not trust OTIO with structure.** See above.
- **It does not copy what is already there.** `present` and `substituted` are
  not offloaded, because copying them would put a second file of that name on
  the drive and manufacture the ambiguity this whole module exists to detect.
