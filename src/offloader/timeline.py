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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePath
from urllib.parse import unquote

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
    #: The timeline carries no media for this at all -- a Premiere-native
    #: graphic, a title, a generator. No file exists to supply.
    GENERATED = "generated"


#: Statuses that leave the caller with something to do before a conform.
UNRESOLVED = (Status.AMBIGUOUS, Status.MISSING, Status.GENERATED)


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
        """Everything a human has to deal with before the cut will conform."""
        return self.of(*UNRESOLVED)

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
    a space. Forward slashes are left alone -- `Path` accepts them on Windows,
    and normalising them by hand is how a UNC path loses its leading pair.
    """
    if not url:
        return None
    text = unquote(url)
    for prefix in ("file://localhost/", "file:///", "file://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if not text:
        return None
    return Path(text)


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

    order: list[str] = []
    found: dict[str, dict] = {}
    generated = 0
    for clip in timeline.find_clips():
        media = clip.media_reference
        url = getattr(media, "target_url", None)
        resolved = _url_to_path(url) if url else None
        if resolved is None:
            # A generator, a title, a missing reference: real clips in the cut
            # that no file can satisfy. Counted once, reported once.
            generated += 1
            continue
        key = os.path.normcase(str(resolved))
        if key not in found:
            order.append(key)
            found[key] = {"path": resolved, "name": resolved.name, "uses": 0}
        found[key]["uses"] += 1

    references = [
        Reference(name=found[k]["name"], target=found[k]["path"], uses=found[k]["uses"])
        for k in order
    ]
    if generated:
        references.append(
            Reference(name=f"{generated} timeline-native item(s)", target=None,
                      uses=generated, generated=True)
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
       there. No search would ever satisfy it.
    2. Anything under a search root with that basename wins, whatever path the
       timeline asked for. This is what stops a file being copied again under
       a second name because an editor addressed it on their own volume.
       Several copies that are byte-identical are still PRESENT, with the
       duplicates recorded; several that differ are AMBIGUOUS and nothing is
       chosen.
    3. A camera original with no decoder may be satisfied by a proxy already
       under the roots, if the frame counts agree.
    4. Only then, a file readable where the timeline says it is, is a GAP: the
       thing actually worth copying.
    5. Anything left is MISSING, and no offload will fix it.
    """
    roots = [Path(r) for r in search_roots]
    if table is None:
        table = index(roots, excludes)

    resolutions: list[Resolution] = []
    for reference in references:
        resolutions.append(
            _resolve_one(reference, table, substitute_proxies, algorithm)
        )
    return Report(timeline=Path(timeline) if timeline else Path(),
                  search_roots=roots, resolutions=resolutions)


def _resolve_one(reference: Reference, table: dict[str, list[Path]],
                 substitute_proxies: bool, algorithm: str) -> Resolution:
    if reference.generated or reference.target is None:
        return Resolution(reference, Status.GENERATED,
                          note="the timeline carries no media for this")

    target = reference.target
    candidates = table.get(target.name.lower(), [])

    if len(candidates) == 1:
        return Resolution(reference, Status.PRESENT, location=candidates[0])

    if len(candidates) > 1:
        if _same_file(candidates, algorithm):
            best = _closest(candidates, target)
            others = [c for c in candidates if c != best]
            return Resolution(
                reference, Status.PRESENT, location=best, alternatives=others,
                note=f"{len(candidates)} byte-identical copies; either resolves",
            )
        best = _closest(candidates, target)
        others = [c for c in candidates if c != best]
        return Resolution(
            reference, Status.AMBIGUOUS, location=None,
            alternatives=[best, *others],
            note=(f"{len(candidates)} different files share this name; "
                  f"closest to the timeline path is {best}"),
        )

    if substitute_proxies and companions.needs_proxy(target):
        proxy = _proxy_for(target, table)
        if proxy is not None:
            original = target if target.is_file() else None
            note = _substitution_note(original, proxy)
            if note is None:
                return Resolution(
                    reference, Status.AMBIGUOUS, location=None,
                    alternatives=[proxy],
                    note=("a proxy of this name is present but its frame count "
                          "does not match the original; relinking to it would "
                          "move every edit point"),
                )
            return Resolution(reference, Status.SUBSTITUTED, location=proxy,
                              note=note)

    if target.is_file():
        return Resolution(reference, Status.GAP, location=target)

    return Resolution(reference, Status.MISSING, location=None,
                      note=f"not under any search root, and not at {target}")


def _substitution_note(original: Path | None, proxy: Path) -> str | None:
    """Whether `proxy` may stand in for `original`, and why.

    None means refuse. A proxy is only a safe stand-in if it is the same length
    as what it replaces, frame for frame: relink a clip to a shorter file and
    every edit point after it moves. Where the original cannot be reached the
    check cannot be made, and the note says so rather than implying agreement.
    """
    if original is None:
        return "proxy stands in for an original that is not reachable; frames unverified"
    want = _frames(original)
    got = _frames(proxy)
    if want is None or got is None:
        return "proxy stands in for the original; frame counts unverified"
    if want != got:
        return None
    return f"proxy matches the original frame for frame ({want} frames)"


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
