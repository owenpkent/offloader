"""Resolve an edit timeline to the media it references.

A card offload knows its source: everything under one root, and the only
question is whether it arrived intact. This module answers the question that
arrives with a cut coming back from an editor -- *which files does this
timeline actually need, and are they all here?* -- and turns the answer into a
file set `engine.run` can offload with the same verified copy and the same
paperwork.

Reading the timeline is OpenTimelineIO's job and nothing else's. What it is
deliberately not trusted with is structure. Measured against the conform that
prompted this module, the `fcp_xml` adapter recovered all 334 media references
of a Premiere-exported FCP 7 XML exactly -- including the clipitems whose
`<file>` is an id reference carrying no path of its own, which is the trap a
hand-rolled sweep falls into -- while reporting that sequence as 29.97 fps with
12 audio tracks when the file plainly declares 24 fps and 23. So the adapter is
read for `media_reference` and for nothing else, and no rate, duration or track
count from it reaches a caller. See `docs/timeline.md`.

The resolver is the part worth having. That same timeline produced:

* 244 references already under the search root, addressed by their real path.
* 32 also already under it, but addressed by a path on another volume: the
  same bytes, filed somewhere else. Copying those would have been 1.4 GB of
  pointless transfer and, worse, would have manufactured 32 basename
  collisions on a drive that had none.
* 15 camera originals -- 55 GB of BRAW -- each with a frame-accurate proxy
  already on the drive.
* 43 genuinely absent, which is the offload.
* 20 basenames with more than one copy under the root, 12 of them resolving to
  *different* files. Ten were "MISSING MEDIA" stand-in slates left by an
  earlier conform, sharing a name with the real archival footage that arrived
  later. Relinking by filename picks one of those at random, and the one it
  picks reports as online.

That last number is why `AMBIGUOUS` is a status here rather than a warning a
caller may ignore. A tool that quietly picked the best-looking candidate would
have put a purple slate in a finished film and said "Verified" underneath it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePath
from urllib.parse import unquote, urlparse

from . import companions, engine
from .hashers import DEFAULT_ALGORITHM, hash_file


class TimelineSupportMissing(RuntimeError):
    """OpenTimelineIO, or the adapter this file needs, is not installed."""


#: File suffix to the OTIO adapter that reads it. `.xml` is FCP 7 XML, which
#: is what Premiere and Resolve both export for interchange; Final Cut Pro X's
#: format is a different schema and uses its own suffix.
ADAPTERS = {
    ".xml": "fcp_xml",
    ".fcpxml": "fcpx_xml",
    ".edl": "cmx_3600",
    ".aaf": "AAF",
    ".otio": "otio_json",
}

#: The pip extra that supplies each adapter, for the error message. Only
#: `otio_json` ships with OpenTimelineIO itself; every other adapter was moved
#: out of core into its own distribution, so a bare `pip install
#: opentimelineio` reads none of the formats an editor will hand back.
ADAPTER_PACKAGES = {
    "fcp_xml": "otio-fcp-adapter",
    "fcpx_xml": "otio-fcpx-xml-adapter",
    "cmx_3600": "otio-cmx3600-adapter",
    "AAF": "otio-aaf-adapter",
    "otio_json": "opentimelineio",
}


class Status(str, Enum):
    """What became of one media reference."""

    #: Found under a search root. Nothing to copy.
    PRESENT = "present"
    #: Found nowhere under the roots, but readable where the timeline says it
    #: is. This is the offload.
    GAP = "gap"
    #: The reference is a camera original with no decoder, and a frame-matched
    #: proxy is already under a root. Nothing to copy; relink to the proxy.
    SUBSTITUTED = "substituted"
    #: More than one file under the roots carries this name and they are not
    #: the same file. Refuses to choose.
    AMBIGUOUS = "ambiguous"
    #: Not under the roots and not where the timeline says either.
    MISSING = "missing"
    #: The timeline names a clip but carries no path for it: an OTIO
    #: MissingReference, or an FCP 7 `<file>` with a name and no `<pathurl>`.
    #: Real media the editor had offline. Nothing can be found or copied by
    #: path, so a human relinks it.
    OFFLINE = "offline"
    #: A media reference this module does not resolve, such as an image
    #: sequence. Real media, reported by name, never counted as satisfied.
    UNSUPPORTED = "unsupported"
    #: A non-file URL (http, https, s3 and so on). Never treated as a local
    #: path, never searched by basename, never copied.
    EXTERNAL = "external"
    #: The timeline carries no media for this at all -- a Premiere-native
    #: graphic, a title, a generator. No file exists to supply.
    GENERATED = "generated"


#: Statuses that leave real media unsupplied: the cut will not conform until
#: a human settles each one, so no offload may call itself complete while any
#: remain. GENERATED is not among them: there is no file to supply.
UNRESOLVED = (Status.AMBIGUOUS, Status.MISSING, Status.OFFLINE,
              Status.UNSUPPORTED, Status.EXTERNAL)

#: `Reference.kind` values. Only FILE references carry a local target.
KIND_FILE = "file"
KIND_GENERATED = "generated"
KIND_OFFLINE = "offline"
KIND_SEQUENCE = "sequence"
KIND_EXTERNAL = "external"
KIND_UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Reference:
    """One media reference in a timeline, and how often the cut uses it."""

    name: str
    #: Where the timeline says the media is. None for a generated reference.
    target: Path | None
    #: Placements in the cut, not clipitems in the file. An FCP 7 XML writes
    #: one clipitem per audio *channel*, so a stereo effect placed ten times
    #: appears as twenty clipitems across two tracks. Counting those would
    #: report a sound effect as used twice as often as it is.
    uses: int = 1
    generated: bool = False
    #: What sort of reference this is; see the KIND_* constants. Anything
    #: but KIND_FILE has no local target and can never be PRESENT or a GAP.
    kind: str = KIND_FILE
    #: The timeline's own text for a reference with no local target: a URL,
    #: an image sequence's base, or the media reference's type. Shown to the
    #: user so an unresolved item can be found by hand.
    url: str = ""


@dataclass
class Resolution:
    """A reference, and where -- if anywhere -- it was found."""

    reference: Reference
    status: Status
    #: The file that satisfies this reference: where it already is, or the
    #: source to copy from.
    location: Path | None = None
    #: Other files under the roots with the same name. Populated for
    #: AMBIGUOUS, and for PRESENT when the copies are byte-identical.
    alternatives: list[Path] = field(default_factory=list)
    note: str = ""
    #: A SUBSTITUTED proxy whose frame count could not be compared with the
    #: original's. It may well be right, but nothing has shown it, so it
    #: counts against completeness.
    unverified: bool = False

    @property
    def name(self) -> str:
        return self.reference.name


@dataclass
class Report:
    """The whole timeline resolved against a set of search roots."""

    timeline: Path
    search_roots: list[Path]
    resolutions: list[Resolution]

    def of(self, *statuses: Status) -> list[Resolution]:
        return [r for r in self.resolutions if r.status in statuses]

    @property
    def gaps(self) -> list[Resolution]:
        return self.of(Status.GAP)

    @property
    def unresolved(self) -> list[Resolution]:
        """References whose media is not supplied: see `UNRESOLVED`."""
        return self.of(*UNRESOLVED)

    @property
    def unconfirmed(self) -> list[Resolution]:
        """Proxy substitutions whose frame counts could not be compared."""
        return [r for r in self.resolutions
                if r.status is Status.SUBSTITUTED and r.unverified]

    @property
    def incomplete(self) -> list[Resolution]:
        """Everything a human has to deal with before the cut will conform.

        Unresolved references, and proxies standing in without proof. While
        this is non-empty neither `resolve` nor `offload` may report the cut
        as complete, and both exit non-zero.
        """
        return [r for r in self.resolutions
                if r.status in UNRESOLVED
                or (r.status is Status.SUBSTITUTED and r.unverified)]

    @property
    def differing(self) -> list[Resolution]:
        """GAPs copied from the timeline's own path although a *different*
        file of the same name is under a search root. Worth a warning: after
        the copy the drive holds two files of that name."""
        return [r for r in self.gaps if r.alternatives]

    @property
    def counts(self) -> dict[Status, int]:
        return {s: sum(1 for r in self.resolutions if r.status is s) for s in Status}

    @property
    def gap_bytes(self) -> int:
        total = 0
        for resolution in self.gaps:
            if resolution.location is not None:
                try:
                    total += resolution.location.stat().st_size
                except OSError:          # pragma: no cover - vanished mid-run
                    pass
        return total


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def _url_to_path(url: str) -> Path | None:
    """A `file:` URL from a timeline, as a local path.

    NLEs are not consistent here: Premiere writes `file://localhost/E:/...`,
    Resolve writes `file:///E:/...`, and a hand-edited XML may carry a bare
    path. Percent-escapes are always possible because a clip name may contain
    a space.

    The leading slash is the trap, and it is worth spelling out because
    getting it wrong is invisible on one platform. After the scheme and any
    `localhost` authority, what is left always begins with `/`:

        file://localhost/E:/Media/a.mov   ->  /E:/Media/a.mov
        file:///Volumes/Edit/a.mov        ->  /Volumes/Edit/a.mov

    That slash belongs to a POSIX path and must stay, but on a Windows path it
    sits in front of the drive letter and must go. Stripping it unconditionally
    turns `/Volumes/Edit/a.mov` into the *relative* path `Volumes/Edit/a.mov`,
    which still resolves by basename and so looks fine, while every "is it
    where the timeline says?" test quietly answers no. An editor cutting on a
    Mac addresses every clip that way, so this is the common case, not an edge.

    Forward slashes are otherwise left alone: `Path` accepts them on Windows,
    and rewriting them by hand is how a UNC path loses its leading pair.
    """
    if not url:
        return None
    text = unquote(url)

    if text.lower().startswith("file:"):
        text = text[len("file:"):]
        if text.startswith("//"):
            rest = text[2:]
            if rest.lower().startswith("localhost/"):
                rest = rest[len("localhost"):]      # keeps the leading slash
            elif not rest.startswith("/"):
                # file://server/share/... is a UNC path; the pair is the point.
                return Path("//" + rest) if rest else None
            text = rest

    # A drive letter behind the leading slash means this is a Windows path.
    if re.match(r"^/[A-Za-z]:", text):
        text = text[1:]
    if not text:
        return None
    return Path(text)


_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")


def _url_scheme(url: str) -> str:
    """The URL scheme, lowercased, or "" for a bare path.

    A single letter is a Windows drive (`E:/Media/a.mov`), not a scheme.
    """
    match = _SCHEME.match(url.strip())
    if not match or len(match.group(1)) == 1:
        return ""
    return match.group(1).lower()


def _anchored(target: Path, base: Path) -> Path:
    """`target`, resolved against `base` when the timeline wrote it relative.

    A relative target means "relative to the timeline", never "relative to
    wherever the command happened to be run": resolved against the working
    directory, `media/clip.mov` would find, and copy, an unrelated file of
    that name. `anchor` rather than `is_absolute()` decides, because on
    Windows a Mac editor's `/Volumes/Edit/a.mov` has a root but no drive and
    is not relative to anything here.
    """
    if target.anchor:
        return target
    return Path(os.path.normpath(base / target))


def _adapter_for(path: Path, adapter: str | None = None) -> str:
    if adapter:
        return adapter
    suffix = path.suffix.lower()
    if suffix not in ADAPTERS:
        raise TimelineSupportMissing(
            f"no timeline adapter for {suffix or path.name!r}; "
            f"known formats: {', '.join(sorted(ADAPTERS))}"
        )
    return ADAPTERS[suffix]


def read_references(path: Path, adapter: str | None = None) -> list[Reference]:
    """Every distinct piece of media a timeline points at.

    Deduplicated by path and counted, so `uses` is how many times the cut
    actually places that file. A reference the timeline carries no media for
    -- a Premiere-native graphic, a title, a generator -- comes back with
    `generated=True` and no target, because there is no file to supply and
    saying so is more useful than dropping it.

    Real media the module cannot resolve by path is never folded in with
    those. An offline clip (no path), an image sequence, a non-file URL and
    any other kind of media reference each come back by name, with a `kind`
    that makes the resolver report it as unresolved.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"timeline not found: {path}")
    name = _adapter_for(path, adapter)

    try:
        import opentimelineio as otio
    except ImportError as exc:           # pragma: no cover - import-guard path
        raise TimelineSupportMissing(
            "reading timelines needs OpenTimelineIO: "
            'pip install "offloader[timeline]"'
        ) from exc

    if name not in set(otio.adapters.available_adapter_names()):
        package = ADAPTER_PACKAGES.get(name, "")
        hint = f": pip install {package}" if package else ""
        raise TimelineSupportMissing(
            f"OpenTimelineIO has no {name!r} adapter installed{hint}"
        )

    timeline = otio.adapters.read_from_file(str(path), name)
    base = Path(os.path.abspath(path)).parent
    schema = otio.schema

    order: list[tuple[str, str]] = []
    found: dict[tuple[str, str], dict] = {}
    generated = 0

    def add(kind: str, key: str, label: str, target: Path | None = None,
            url: str = "") -> None:
        slot = (kind, key)
        if slot not in found:
            order.append(slot)
            found[slot] = {"name": label, "target": target, "url": url,
                           "kind": kind, "uses": 0}
        found[slot]["uses"] += 1

    for clip in timeline.find_clips():
        media = clip.media_reference
        clip_name = (getattr(clip, "name", "") or "").strip()
        media_name = (getattr(media, "name", "") or "").strip()

        if isinstance(media, schema.GeneratorReference):
            # A generator, a title, a matte: real clips in the cut that no
            # file can satisfy. Counted once, reported once.
            generated += 1
            continue

        if isinstance(media, schema.ImageSequenceReference):
            padding = "#" * max(1, media.frame_zero_padding)
            stem = f"{media.name_prefix}{padding}{media.name_suffix}"
            url = media.target_url_base or ""
            add(KIND_SEQUENCE, os.path.normcase(url + stem), stem, url=url)
            continue

        if media is None or isinstance(media, schema.MissingReference):
            label = media_name or clip_name or "unnamed offline clip"
            add(KIND_OFFLINE, os.path.normcase(label), label)
            continue

        if not isinstance(media, schema.ExternalReference):
            kind_name = type(media).__name__
            label = media_name or clip_name or kind_name
            add(KIND_UNSUPPORTED, f"{kind_name}:{os.path.normcase(label)}", label,
                url=kind_name)
            continue

        url = (media.target_url or "").strip()
        if not url:
            label = media_name or clip_name or "unnamed offline clip"
            add(KIND_OFFLINE, os.path.normcase(label), label)
            continue

        scheme = _url_scheme(url)
        if scheme and scheme != "file":
            # Not a local path, whatever its last component looks like.
            # Matching https://host/clip.mov by basename would "find" an
            # unrelated local clip.mov.
            label = Path(unquote(urlparse(url).path)).name or url
            add(KIND_EXTERNAL, url, label, url=url)
            continue

        resolved = _url_to_path(url)
        if resolved is None:
            label = media_name or clip_name or "unnamed offline clip"
            add(KIND_OFFLINE, os.path.normcase(label), label)
            continue
        resolved = _anchored(resolved, base)
        add(KIND_FILE, os.path.normcase(str(resolved)), resolved.name, resolved)

    references = [
        Reference(name=found[k]["name"], target=found[k]["target"],
                  uses=found[k]["uses"], kind=found[k]["kind"], url=found[k]["url"])
        for k in order
    ]
    if generated:
        references.append(
            Reference(name=f"{generated} timeline-native item(s)", target=None,
                      uses=generated, generated=True, kind=KIND_GENERATED)
        )
    return references


# --------------------------------------------------------------------------
# Resolving
# --------------------------------------------------------------------------

def index(roots: Iterable[Path],
          excludes: Sequence[str] = engine.DEFAULT_EXCLUDES) -> dict[str, list[Path]]:
    """Every file under every search root, keyed by lowercased basename.

    Built once and shared across the whole resolve: a timeline of a few hundred
    references against a drive of a few thousand files would otherwise be a
    few hundred tree walks.
    """
    table: dict[str, list[Path]] = {}
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            raise NotADirectoryError(f"search root not found: {root}")
        for found in engine.scan(root, excludes):
            table.setdefault(found.name.lower(), []).append(found)
    return table


def _same_file(paths: Sequence[Path], algorithm: str = DEFAULT_ALGORITHM) -> bool:
    """Whether every path holds identical bytes.

    Size first, because it settles almost every case for the price of a stat,
    and only then the checksum. Two files of the same size are common; two of
    the same size and checksum are the same file.
    """
    try:
        sizes = {p.stat().st_size for p in paths}
    except OSError:                      # pragma: no cover - vanished mid-run
        return False
    if len(sizes) > 1:
        return False
    digests = set()
    for path in paths:
        try:
            digests.add(hash_file(path, algorithm))
        except OSError:                  # pragma: no cover - unreadable
            return False
    return len(digests) == 1


def _closest(candidates: Sequence[Path], target: Path | None) -> Path:
    """The candidate whose tail matches the wanted path for longest.

    A guess, and labelled as one wherever it is used. `.../Sound Effects/
    Wooshes/Ceiling Fan.mp3` scores 3 against the same three trailing
    components and 1 against a stand-in of that name sitting loose in another
    folder, which is the right ranking and still not proof.
    """
    if target is None:
        return candidates[0]
    wanted = [p.lower() for p in PurePath(target).parts]

    def score(path: Path) -> tuple[int, str]:
        parts = [p.lower() for p in PurePath(path).parts]
        n = 0
        while n < min(len(wanted), len(parts)) and wanted[-1 - n] == parts[-1 - n]:
            n += 1
        return n, str(path)

    return max(candidates, key=score)


def _proxy_for(target: Path, table: dict[str, list[Path]]) -> Path | None:
    """A proxy under the search roots standing in for a camera original.

    Stem matching, the same rule `companions.find_proxy` uses on a card, but
    looking across the indexed roots rather than in the sibling directories:
    by the time a cut comes back, the proxy the editor cut with is rarely
    filed next to the original any more.
    """
    stem = target.stem.lower()
    for suffix in companions.PROXY_SUFFIXES:
        hits = table.get(f"{stem}{suffix}", [])
        if len(hits) == 1:
            return hits[0]
        if hits:
            # More than one proxy of that name is exactly the ambiguity this
            # module refuses to guess through. Leave it to the caller.
            return None
    return None


def _frames(path: Path) -> int | None:
    """Frame count, or None when it cannot be established.

    Best effort on purpose: ffprobe is an optional dependency here, and BRAW
    is read out of the container because ffprobe cannot open it at all. A
    number that cannot be got is reported as absent, never as agreement.
    """
    try:
        from . import probe as probe_mod
        return probe_mod.probe(path).frame_count
    except Exception:                    # pragma: no cover - probe unavailable
        return None


def resolve(references: Sequence[Reference], search_roots: Sequence[Path], *,
            timeline: Path | None = None,
            excludes: Sequence[str] = engine.DEFAULT_EXCLUDES,
            substitute_proxies: bool = True,
            algorithm: str = DEFAULT_ALGORITHM,
            table: dict[str, list[Path]] | None = None) -> Report:
    """Work out, for each reference, whether the media is already here.

    The order of the tests is the whole design:

    1. A reference the timeline carries no media for is GENERATED and stops
       there. No search would ever satisfy it. Real media with no usable
       local path (offline, an image sequence, a URL) is OFFLINE,
       UNSUPPORTED or EXTERNAL and stops there too, but counts as unresolved.
    2. Anything under a search root with that basename wins, whatever path the
       timeline asked for. This is what stops a file being copied again under
       a second name because an editor addressed it on their own volume.
       Several copies that are byte-identical are still PRESENT, with the
       duplicates recorded; several that differ are AMBIGUOUS and nothing is
       chosen. Two limits on "wins":

       * When the timeline's own path is readable it is the truth. The file
         under the root counts only if it holds the same bytes; otherwise the
         original is a GAP and the other file is reported as differing.
       * Distinct timeline references that share a basename must resolve to
         distinct files. Without content evidence one file cannot be shown to
         be any particular one of them, so each such reference is AMBIGUOUS
         rather than all of them mapping to the one file.
    3. A camera original with no decoder may be satisfied by a proxy already
       under the roots, if the frame counts agree.
    4. Only then, a file readable where the timeline says it is, is a GAP: the
       thing actually worth copying.
    5. Anything left is MISSING, and no offload will fix it.
    """
    roots = [Path(r) for r in search_roots]
    if table is None:
        table = index(roots, excludes)

    base = Path(os.path.abspath(timeline)).parent if timeline else None
    prepared: list[Reference] = []
    for reference in references:
        if (base is not None and reference.target is not None
                and not reference.target.anchor):
            reference = replace(reference, target=_anchored(reference.target, base))
        prepared.append(reference)

    # How many *distinct* files the timeline asks for under each basename.
    # Lowercased like the index, so A.MOV and a.mov from a case-sensitive
    # volume count as two references competing for one name.
    wanted: dict[str, set[str]] = {}
    for reference in prepared:
        if _is_file_reference(reference):
            wanted.setdefault(reference.target.name.lower(), set()).add(
                os.path.normcase(str(reference.target)))

    resolutions: list[Resolution] = []
    for reference in prepared:
        siblings = (len(wanted.get(reference.target.name.lower(), ()))
                    if _is_file_reference(reference) else 1)
        resolutions.append(
            _resolve_one(reference, table, substitute_proxies, algorithm, siblings)
        )
    return Report(timeline=Path(timeline) if timeline else Path(),
                  search_roots=roots, resolutions=resolutions)


def _is_file_reference(reference: Reference) -> bool:
    return (not reference.generated and reference.kind == KIND_FILE
            and reference.target is not None)


def _matches(target: Path, candidate: Path, algorithm: str) -> bool:
    """Whether a readable timeline target and a candidate hold the same bytes.

    The same file is trivially a match and is not hashed: a reference
    addressed by its real path under the root is the common case.
    """
    try:
        if os.path.samefile(target, candidate):
            return True
    except OSError:
        return False
    return _same_file([target, candidate], algorithm)


def _resolve_one(reference: Reference, table: dict[str, list[Path]],
                 substitute_proxies: bool, algorithm: str,
                 siblings: int = 1) -> Resolution:
    if reference.generated or reference.kind == KIND_GENERATED:
        return Resolution(reference, Status.GENERATED,
                          note="the timeline carries no media for this")

    if reference.kind == KIND_SEQUENCE:
        return Resolution(
            reference, Status.UNSUPPORTED,
            note=(f"image sequence at {reference.url or 'an unknown location'}; "
                  "sequences are not resolved, check it by hand"))

    if reference.kind == KIND_EXTERNAL:
        return Resolution(
            reference, Status.EXTERNAL,
            note=f"not a local file: {reference.url}; not searched or copied")

    if reference.kind == KIND_UNSUPPORTED:
        return Resolution(
            reference, Status.UNSUPPORTED,
            note=f"{reference.url or 'unknown'} media reference is not supported")

    if reference.kind == KIND_OFFLINE or reference.target is None:
        # Searching by the clip's name would be a guess at which file the
        # editor meant; offering the hits is useful, choosing one is not.
        hits = table.get(reference.name.lower(), [])
        note = "the timeline has no media path for this clip (offline)"
        if hits:
            note += (f"; {len(hits)} file(s) of this name under the search "
                     "roots, not chosen")
        return Resolution(reference, Status.OFFLINE, alternatives=list(hits),
                          note=note)

    target = reference.target
    candidates = table.get(target.name.lower(), [])
    readable = target.is_file()

    if candidates:
        best = _closest(candidates, target)
        others = [c for c in candidates if c != best]
        if others and not _same_file(candidates, algorithm):
            return Resolution(
                reference, Status.AMBIGUOUS, location=None,
                alternatives=[best, *others],
                note=(f"{len(candidates)} different files share this name; "
                      f"closest to the timeline path is {best}"),
            )
        copies = (f"{len(candidates)} byte-identical copies; either resolves"
                  if others else "")

        if readable:
            if _matches(target, best, algorithm):
                return Resolution(reference, Status.PRESENT, location=best,
                                  alternatives=others, note=copies)
            # The "MISSING MEDIA" slate case: the real file is reachable and
            # the one under the root of that name is something else.
            return Resolution(
                reference, Status.GAP, location=target,
                alternatives=list(candidates),
                note=(f"a different file of this name is under the search roots "
                      f"({best}); the timeline's original is copied"),
            )

        if siblings > 1:
            return Resolution(
                reference, Status.AMBIGUOUS, location=None,
                alternatives=[best, *others],
                note=(f"{siblings} different timeline references are named "
                      f"{target.name} but the search roots hold only one file "
                      "of that name; not chosen"),
            )
        return Resolution(reference, Status.PRESENT, location=best,
                          alternatives=others, note=copies)

    proxy_note = ""
    if substitute_proxies and companions.needs_proxy(target):
        proxy = _proxy_for(target, table)
        if proxy is not None:
            original = target if readable else None
            verdict, note = _substitution(original, proxy)
            if verdict == "mismatch":
                return Resolution(
                    reference, Status.AMBIGUOUS, location=None,
                    alternatives=[proxy],
                    note=("a proxy of this name is present but its frame count "
                          "does not match the original; relinking to it would "
                          "move every edit point"),
                )
            if verdict in ("verified", "unverified"):
                return Resolution(reference, Status.SUBSTITUTED, location=proxy,
                                  note=note, unverified=verdict == "unverified")
            # "unusable": the stand-in could not be read as media at all.
            proxy_note = note

    if readable:
        return Resolution(reference, Status.GAP, location=target, note=proxy_note)

    note = f"not under any search root, and not at {target}"
    if proxy_note:
        note += f"; {proxy_note}"
    return Resolution(reference, Status.MISSING, location=None, note=note)


def _substitution(original: Path | None, proxy: Path) -> tuple[str, str]:
    """Whether `proxy` may stand in for `original`, and why.

    Returns a verdict and a note. The verdict is "verified" (frame counts
    agree), "unverified" (the proxy is readable media but the original's count
    could not be got), "mismatch" (the counts disagree: refuse), or
    "unusable" (the proxy itself yields no frame count, so nothing shows it is
    media at all, let alone this media: it must not stand in).

    A proxy is only a safe stand-in if it is the same length as what it
    replaces, frame for frame: relink a clip to a shorter file and every edit
    point after it moves.
    """
    got = _frames(proxy)
    if got is None:
        return "unusable", (f"same-stem file {proxy} could not be read as media "
                            "(no frame count), so it does not stand in")
    if original is None:
        return "unverified", ("proxy stands in for an original that is not "
                              "reachable; frames unverified")
    want = _frames(original)
    if want is None:
        return "unverified", "proxy stands in for the original; frame counts unverified"
    if want != got:
        return "mismatch", ""
    return "verified", f"proxy matches the original frame for frame ({want} frames)"


def describe(resolution: Resolution) -> str:
    """One line for a report or a summary: status, name, uses and why."""
    label = ("unverified proxy" if resolution.unverified
             else resolution.status.value)
    text = f"{label}: {resolution.name} (used {resolution.reference.uses}x)"
    if resolution.status is Status.SUBSTITUTED and resolution.location is not None:
        text += f", stood in for by {resolution.location}"
    elif resolution.reference.target is not None:
        text += f", timeline path {resolution.reference.target}"
    if resolution.note:
        text += f"; {resolution.note}"
    return text


# --------------------------------------------------------------------------
# Handing the gaps to the engine
# --------------------------------------------------------------------------

def selection(report: Report, *, flat: bool = False) -> list[engine.SelectedFile]:
    """The GAP files, as a file set `engine.run` can offload.

    Only the gaps. PRESENT and SUBSTITUTED are already on the drive and
    copying them again would put a second file of the same name on it, which
    is the collision this module exists to detect rather than create.

    The default layout keeps each file's path below its volume root, so
    `D:/ChairsDoc_backup/audio/boom/260828_001.wav` lands at
    `ChairsDoc_backup/audio/boom/260828_001.wav`. That is longer than a flat
    folder and worth it: it records where each file came from, and two files
    of the same name from different trees cannot land on each other.
    """
    files: list[engine.SelectedFile] = []
    for resolution in report.gaps:
        source = resolution.location
        if source is None:               # pragma: no cover - gaps always have one
            continue
        if flat:
            relative = PurePath(source.name)
        else:
            relative = PurePath(*PurePath(source).parts[1:])
        files.append(engine.SelectedFile(source=source, relative=Path(relative),
                                         root=Path(source.anchor)))
    # Proxies first, for the same reason a card offload hoists them: an editor
    # can start cutting while the originals are still moving.
    return sorted(files, key=lambda f: not engine.is_proxy(f.source, f.root))
