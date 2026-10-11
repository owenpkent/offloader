"""Command line interface."""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from . import PRODUCT_NAME, __version__, engine, hashers, longpath, probe, retry, thumbs
from . import timeline as timeline_mod
from .models import FileStatus, Job, Profile, VerificationMode
from .reports import WRITERS, paperwork
from .util import format_elapsed, format_size

#: Filenames written inside "<name>_Reports/", matching the reference layout.
REPORT_FILENAMES = paperwork.REPORT_FILENAMES

DEFAULT_REPORTS = "pdf"

#: Exit codes for `offload`, `resolve`, `report` and `verify`. Documented in
#: the README; a format script keys off them, so a new meaning gets a new
#: number rather than reusing one.
EXIT_OK = 0                  # Verified, or Copied under --verify none
EXIT_FAILED = 1              # a copy, verify or scan failure
EXIT_ERROR = 2               # usage, environment, unreadable manifest
EXIT_REFUSED = 3             # unsafe destination or control file, nothing copied
EXIT_INCOMPLETE = 4          # unresolved timeline references, empty source,
                             # or an all-skipped rerun: nothing failed, but
                             # not everything asked for was verified
EXIT_REPORTS_FAILED = 5      # copies good, but a report or manifest failed
EXIT_CANCELLED = 130         # Ctrl+C or a control-file cancel
#: What `resolve` and a timeline `offload` return while any reference is
#: unresolved, so a caller gating on the exit code never reads a partial cut
#: as a complete one. The same code as any other incomplete job.
EXIT_UNRESOLVED = EXIT_INCOMPLETE


class _Progress:
    """Single-line progress on stderr; silent when not a TTY."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled and sys.stderr.isatty()
        self._last = 0.0
        self._width = 0

    def __call__(self, event: engine.ProgressEvent) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        final = event.job_bytes_done >= event.job_bytes_total
        if now - self._last < 0.1 and not final:
            return
        self._last = now

        pct = (event.job_bytes_done / event.job_bytes_total * 100
               if event.job_bytes_total else 100.0)
        line = (f"  [{pct:5.1f}%] {event.stage:<6} "
                f"{event.file_index + 1}/{event.file_total}  {event.file_name}")
        line = line[:110]
        pad = max(0, self._width - len(line))
        self._width = len(line)
        sys.stderr.write("\r" + line + " " * pad)
        sys.stderr.flush()

    def done(self) -> None:
        if self.enabled and self._width:
            sys.stderr.write("\r" + " " * self._width + "\r")
            sys.stderr.flush()
            self._width = 0


def _parse_reports(value: str) -> list[str]:
    keys = [part.strip().lower() for part in value.split(",") if part.strip()]
    unknown = [key for key in keys if key not in WRITERS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown report format(s): {', '.join(unknown)}; "
            f"choose from {', '.join(WRITERS)}"
        )
    return keys


def _write_reports(job: Job, formats: list[str], out_dir: Path | None,
                   logo: Path | None, footer: str | None = None) -> list[Path]:
    """Every requested report, plus one manifest inside each destination.

    `out_dir` is `--report-dir`, or None for the job's own folder. Failures
    land on `job.report_failures`, which the summary and exit code report.
    """
    return paperwork.write_reports(job, formats, report_dir=out_dir,
                                   logo=logo, footer=footer)


def _summarize(job: Job, reports: list[Path]) -> None:
    failed = [f for f in job.files if f.status is FileStatus.FAILED]
    print()
    print(f"  {job.name}: {job.final_status}")
    # The counts are meaningful only when media was probed; a generic data
    # transfer never looks inside a file, so reporting "0 video" would be noise
    # rather than information. A sound card reports its audio count instead of
    # a zero, and a card carrying both reports both.
    counts = ""
    if job.profile.probes_media:
        parts = []
        if job.video_files or not job.audio_files:
            parts.append(f"{job.video_files} video")
        if job.audio_files:
            parts.append(f"{job.audio_files} audio")
        counts = f"  ({', '.join(parts)})"
    print(f"  {job.total_files} files, {format_size(job.total_bytes)}"
          f" in {format_elapsed(job.elapsed_sec)}{counts}")
    print(f"  Verification: {job.verification_label}"
          f"{' + second source read' if job.paranoid else ''}")
    for destination in job.destination_roots:
        print(f"  -> {destination}")
    for report in reports:
        print(f"  report: {report}")
    if job.cancelled:
        print(f"  cancelled: {len(job.not_attempted)} file(s) not attempted")
    if job.unresolved:
        # Every one, uncapped: this is the list of media the job did not
        # supply, and a truncated version of it is a false "that was all".
        print(file=sys.stderr)
        print(f"  {len(job.unresolved)} timeline reference(s) NOT supplied; "
              "the cut is not complete:", file=sys.stderr)
        for line in job.unresolved:
            print(f"    - {line}", file=sys.stderr)
    if job.report_failures:
        print(file=sys.stderr)
        print(f"  {len(job.report_failures)} REPORT(S) FAILED:", file=sys.stderr)
        for failure in job.report_failures:
            print(f"    ! {failure}", file=sys.stderr)
    if job.scan_errors:
        print(file=sys.stderr)
        print(f"  {len(job.scan_errors)} folder(s) could not be read, so "
              "their files were not copied:", file=sys.stderr)
        for problem in job.scan_errors:
            print(f"    {problem.describe()}", file=sys.stderr)
    if job.warnings:
        print(file=sys.stderr)
        print(f"  {len(job.warnings)} warning(s):", file=sys.stderr)
        for warning in job.warnings[:20]:
            print(f"    - {warning}", file=sys.stderr)
        if len(job.warnings) > 20:
            print(f"    ... and {len(job.warnings) - 20} more", file=sys.stderr)
    if failed:
        print(f"\n  {len(failed)} FAILED:", file=sys.stderr)
        for entry in failed:
            reason = next((d.error for d in entry.destinations if d.error), "unknown")
            print(f"    {entry.name}: {reason}", file=sys.stderr)


def _common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hash", default=hashers.DEFAULT_ALGORITHM,
                        choices=sorted(hashers.algorithm_keys()),
                        help="checksum algorithm (default: %(default)s; the "
                             "engine hashes every byte on the copy path, so a "
                             "slow choice caps copy speed — md5 is ~40x slower "
                             "than the default; see 'offloader info')")
    parser.add_argument("--report", type=_parse_reports, default=DEFAULT_REPORTS,
                        metavar="FMT[,FMT...]",
                        help=f"report formats: {', '.join(WRITERS)} (default: pdf)")
    parser.add_argument("--report-dir", type=Path, default=None,
                        help="where reports go (default: <dest>/<name>_Reports)")
    parser.add_argument("--thumbs", type=int, default=4, metavar="N",
                        help="thumbnails per clip, 0 to disable (default: %(default)s)")
    parser.add_argument("--name", default=None,
                        help="job name (default: source folder name)")
    parser.add_argument("--logo", type=Path, default=None,
                        help="image for the PDF header")
    parser.add_argument("--footer", default=None, metavar="TEXT",
                        help="footer line for the PDF (default: product and version)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="extra filename pattern to skip (repeatable)")
    parser.add_argument("--retries", type=int, default=3, metavar="N",
                        help="attempts per failing read when the error looks "
                             "transient (default: %(default)s, 1 disables)")
    parser.add_argument("--retry-wait", type=float, default=2.0, metavar="SECONDS",
                        help="pause before the first retry, backing off after "
                             "(default: %(default)s)")
    parser.add_argument("--profile", choices=[p.value for p in Profile],
                        default=Profile.MEDIA.value,
                        help="'media' (default) offloads camera cards with "
                             "ffprobe metadata, thumbnails and the BRAW check; "
                             "'data' is a generic large-data transfer that "
                             "copies and verifies but skips all media probing")
    parser.add_argument("--generic", action="store_true",
                        help="shorthand for --profile data")
    parser.add_argument("--no-probe", action="store_true",
                        help="skip ffprobe metadata and thumbnails")
    parser.add_argument("--quiet", action="store_true", help="suppress progress")


def _timeline_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--search-root", type=Path, action="append", default=[],
                        dest="search_roots", metavar="PATH",
                        help="where the media already lives (repeatable). "
                             "Anything found here is not copied again")
    parser.add_argument("--adapter", default=None, metavar="NAME",
                        help="OpenTimelineIO adapter to read with "
                             "(default: chosen from the file suffix)")
    parser.add_argument("--no-proxy-substitution", dest="substitute_proxies",
                        action="store_false", default=True,
                        help="do not let a proxy already on disk stand in for "
                             "a camera original the timeline references")
    parser.add_argument("--layout", choices=("mirror", "flat"), default="mirror",
                        help="'mirror' (default) keeps each file's path below "
                             "its volume root, so where it came from stays "
                             "visible and two same-named files cannot land on "
                             "each other; 'flat' puts every file in one folder")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="offloader",
        description=f"{PRODUCT_NAME} — verified copy for large data transfers, "
                    f"with camera-card offload and job reports built in.",
    )
    parser.add_argument("--version", action="version",
                        version=f"{PRODUCT_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    offload = sub.add_parser("offload", help="copy and verify a source to destinations")
    what = offload.add_mutually_exclusive_group(required=True)
    what.add_argument("--source", type=Path,
                      help="card or folder to offload")
    what.add_argument("--timeline", type=Path,
                      help="edit timeline whose media to offload; only the "
                           "files not already under a --search-root are copied")
    offload.add_argument("--dest", type=Path, action="append", required=True,
                         dest="destinations", metavar="PATH",
                         help="destination root (repeat for multiple copies)")
    offload.add_argument("--verify", default=VerificationMode.FULL.value,
                         choices=[m.value for m in VerificationMode],
                         help="verification depth (default: %(default)s)")
    offload.add_argument("--flat", action="store_true",
                         help="do not recreate the source folder structure")
    offload.add_argument("--skip-existing", action="store_true",
                         help="skip files already present with a matching size")
    order = offload.add_mutually_exclusive_group()
    order.add_argument("--proxies-first", dest="proxies_first",
                       action="store_true", default=True,
                       help="copy the camera's proxy folders before the "
                            "originals so an edit can start early (default)")
    order.add_argument("--originals-first", dest="proxies_first",
                       action="store_false",
                       help="copy in plain tree order instead")
    offload.add_argument("--control-file", type=Path, default=None, metavar="PATH",
                         help="make the job pausable from another terminal: "
                              "it reads this file between chunks. Drive it with "
                              "'offloader control PATH --pause|--resume|--cancel'")
    offload.add_argument("--replace-existing", action="store_true",
                         help="let a copy replace a file already at the "
                              "destination whose content differs (by default "
                              "that file is kept and the copy is failed)")
    offload.add_argument("--paranoid", action="store_true",
                         help="read each source file twice and compare, to "
                              "catch a read that returned wrong bytes without "
                              "reporting an error (costs a second pass)")
    _timeline_options(offload)
    _common_options(offload)

    control = sub.add_parser(
        "control",
        help="pause, resume or cancel a running offload through its "
             "--control-file")
    control.add_argument("path", type=Path,
                         help="the --control-file the job was started with")
    state = control.add_mutually_exclusive_group()
    state.add_argument("--pause", action="store_const", dest="state",
                       const=engine.FileControl.PAUSE,
                       help="hold the job at the next 8 MiB chunk")
    state.add_argument("--resume", action="store_const", dest="state",
                       const=engine.FileControl.RUN, help="let it continue")
    state.add_argument("--cancel", action="store_const", dest="state",
                       const=engine.FileControl.CANCEL,
                       help="stop it; finished files are kept, the file in "
                            "flight is discarded")
    control.set_defaults(state=None)

    resolve = sub.add_parser(
        "resolve",
        help="report which of a timeline's media is already here and which "
             "is not, without copying anything")
    resolve.add_argument("--timeline", type=Path, required=True,
                         help="the edit timeline to resolve")
    resolve.add_argument("--csv", type=Path, default=None, metavar="PATH",
                         help="write the full per-reference table here")
    resolve.add_argument("--quiet", action="store_true",
                         help="print the counts only, not the detail")
    _timeline_options(resolve)

    report = sub.add_parser(
        "report", help="regenerate reports from an existing tree without copying")
    report.add_argument("--source", type=Path, required=True,
                        help="offloaded folder to describe")
    report.add_argument("--dest", type=Path, action="append", default=[],
                        dest="destinations", metavar="PATH",
                        help="destination root to cross-check (repeatable)")
    report.add_argument("--flat", action="store_true",
                        help="destinations are flat, not structure-preserving")
    _common_options(report)

    verify = sub.add_parser(
        "verify",
        help="re-check an offloaded tree against its MHL — run this before "
             "erasing a card, and again later to catch bit rot")
    verify.add_argument("path", type=Path,
                        help="an .mhl file, or a folder to search for them")
    verify.add_argument("--allow-cache", action="store_true",
                        help="do not evict files before reading (faster, and "
                             "may verify memory rather than the device)")
    verify.add_argument("--quiet", action="store_true", help="suppress progress")

    sub.add_parser("info", help="show tool and environment status")
    sub.add_parser("gui", help="launch the desktop interface")

    update = sub.add_parser(
        "update", help="check for a newer release, and install it (Windows)")
    update.add_argument("--install", action="store_true",
                        help="download, verify and run the installer rather "
                             "than only reporting what is available")
    update.add_argument("--dir", type=Path, default=None, metavar="PATH",
                        help="where to download the installer (default: a "
                             "private temporary directory)")
    return parser


def _options_from(args: argparse.Namespace, destinations: list[Path]) -> engine.OffloadOptions:
    profile = Profile.DATA if getattr(args, "generic", False) else Profile(args.profile)
    return engine.OffloadOptions(
        destinations=destinations,
        algorithm=args.hash,
        verification=VerificationMode(getattr(args, "verify", "full")),
        thumbnail_count=0 if args.no_probe else max(0, args.thumbs),
        excludes=tuple(engine.DEFAULT_EXCLUDES) + tuple(args.exclude),
        preserve_structure=not args.flat,
        skip_existing=getattr(args, "skip_existing", False),
        # `report` copies nothing, so it never defines this flag and the
        # default is inert there -- rescan() reads the tree in scan order.
        proxies_first=getattr(args, "proxies_first", True),
        job_name=args.name,
        extra_probe=not args.no_probe,
        profile=profile,
        retry=retry.RetryPolicy(attempts=max(1, args.retries),
                                delay=max(0.0, args.retry_wait)),
        paranoid=getattr(args, "paranoid", False),
        replace_existing=getattr(args, "replace_existing", False),
    )


def exit_code(job: Job) -> int:
    """The `offload` exit code for a finished job. See the EXIT_* constants."""
    status = job.copy_status
    if status == "Failed":
        return EXIT_FAILED
    if status == "Cancelled":
        # The same code as Ctrl+C, so a detached run's caller sees an
        # interruption rather than the 0 of a finished offload.
        return EXIT_CANCELLED
    if status not in ("Verified", "Copied"):
        # Incomplete: references a timeline offload could not supply, nothing
        # to copy, or files skipped on size alone rather than verified. Not a
        # failure, and not a pass either.
        return EXIT_INCOMPLETE
    if job.report_failures:
        return EXIT_REPORTS_FAILED
    return EXIT_OK


def _resolve_timeline(args: argparse.Namespace) -> timeline_mod.Report:
    """Read the timeline and work out what is already here."""
    if not args.search_roots:
        # A usage error, routed through main() like every other one so it
        # reports as exit 2 rather than leaving by its own door.
        raise ValueError(
            "--timeline needs at least one --search-root: without one, every "
            "reference is a gap and the whole cut would be copied"
        )
    references = timeline_mod.read_references(args.timeline, args.adapter)
    return timeline_mod.resolve(
        references, args.search_roots,
        timeline=args.timeline,
        excludes=tuple(engine.DEFAULT_EXCLUDES) + tuple(getattr(args, "exclude", [])),
        substitute_proxies=args.substitute_proxies,
    )


#: Status to the line it prints. Ordered worst-first, so what needs a human
#: is at the bottom of the terminal where it will be read.
_STATUS_LINES = (
    (timeline_mod.Status.PRESENT, "already here"),
    (timeline_mod.Status.SUBSTITUTED, "satisfied by a proxy already here"),
    (timeline_mod.Status.GAP, "not here, and copyable"),
    (timeline_mod.Status.GENERATED, "no media in the timeline to supply"),
    (timeline_mod.Status.EXTERNAL, "NOT A LOCAL FILE (URL), not searched"),
    (timeline_mod.Status.UNSUPPORTED, "UNSUPPORTED media reference, check by hand"),
    (timeline_mod.Status.OFFLINE, "OFFLINE in the timeline, no media path"),
    (timeline_mod.Status.MISSING, "not here and not where the timeline says"),
    (timeline_mod.Status.AMBIGUOUS, "MORE THAN ONE CANDIDATE, not chosen"),
)


def _print_resolution(report: timeline_mod.Report, detail: bool = True) -> None:
    counts = report.counts
    total = len(report.resolutions)
    print(f"{report.timeline.name}: {total} media references, "
          f"{len(report.search_roots)} search root(s)")
    for status, label in _STATUS_LINES:
        if counts[status]:
            print(f"  {counts[status]:5d}  {label}")
    if report.unconfirmed:
        print(f"  {len(report.unconfirmed):5d}  of those proxies, frame counts "
              "UNVERIFIED")
    if report.gaps:
        print(f"  {format_size(report.gap_bytes)} to copy")

    if not detail:
        return
    # Every reference that keeps the cut from being complete, plus the gaps
    # copied despite a different same-named file under a root. The notes are
    # the point: "frames unverified" must be read, not left in a CSV.
    shown = {id(r) for r in report.incomplete + report.differing}
    for resolution in report.resolutions:
        if id(resolution) not in shown:
            continue
        label = ("unverified proxy" if resolution.unverified
                 else resolution.status.value)
        print(f"\n  [{label}] {resolution.name} "
              f"(used {resolution.reference.uses}x)")
        if resolution.reference.target is not None:
            print(f"      timeline path: {resolution.reference.target}")
        elif resolution.reference.url:
            print(f"      timeline target: {resolution.reference.url}")
        if resolution.status is timeline_mod.Status.SUBSTITUTED and resolution.location:
            print(f"      proxy: {resolution.location}")
        if resolution.note:
            print(f"      {resolution.note}")
        for candidate in resolution.alternatives:
            print(f"      candidate: {candidate}")


def _write_resolution_csv(report: timeline_mod.Report, path: Path) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["file", "status", "uses", "timeline_path",
                         "location", "other_candidates", "note"])
        for r in report.resolutions:
            writer.writerow([
                r.name, r.status.value, r.reference.uses,
                str(r.reference.target) if r.reference.target else r.reference.url,
                str(r.location) if r.location else "",
                " | ".join(str(a) for a in r.alternatives),
                r.note,
            ])


def cmd_control(args: argparse.Namespace) -> int:
    """Read or set a running job's control file."""
    control = engine.FileControl(args.path)
    if args.state is None:
        if not args.path.exists():
            print(f"no control file at {args.path}", file=sys.stderr)
            return 2
        state = control.read()
        print(state if state is not None
              else "unreadable, so the job continues in its current state")
        return 0

    # Written whole and moved into place: a job polling between chunks can
    # otherwise read a file that has been truncated but not yet rewritten,
    # which is precisely the "no opinion" case this avoids needing.
    args.path.parent.mkdir(parents=True, exist_ok=True)
    staging = args.path.with_name(args.path.name + ".writing")
    staging.write_text(args.state + "\n", encoding="utf-8")
    os.replace(staging, args.path)
    print(f"{args.state} -> {args.path}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    report = _resolve_timeline(args)
    _print_resolution(report, detail=not args.quiet)
    if args.csv:
        _write_resolution_csv(report, args.csv)
        print(f"\nwrote {args.csv}")

    # Exit non-zero on anything a human has to settle, including a proxy whose
    # frames could not be checked. A generated reference is reported but does
    # not fail the command: no offload can supply it, so holding the exit code
    # hostage to it would only teach a caller to ignore the code.
    return EXIT_UNRESOLVED if report.incomplete else 0


def cmd_offload(args: argparse.Namespace) -> int:
    options = _options_from(args, args.destinations)
    source = args.source
    report: timeline_mod.Report | None = None

    if getattr(args, "timeline", None):
        report = _resolve_timeline(args)
        _print_resolution(report, detail=True)
        selection = timeline_mod.selection(report, flat=args.layout == "flat")
        if not selection:
            if report.incomplete:
                print(f"\nnothing copyable, and {len(report.incomplete)} "
                      "reference(s) unresolved: the cut is NOT complete.")
                return EXIT_UNRESOLVED
            print("\nnothing to copy: every reference is already here.")
            return 0
        print(f"\ncopying {len(selection)} file(s), "
              f"{format_size(report.gap_bytes)}\n")
        options = replace(options, selection=selection)
        source = args.timeline

    control = None
    if args.control_file:
        def announce(state: str) -> None:
            # From the reader thread, and the progress line is written from
            # this one, so start on a fresh line rather than overwriting it.
            print(f"\n[{state}] {args.control_file}", file=sys.stderr, flush=True)

        control = engine.FileControl(args.control_file, on_change=announce)
        # Validate before claim() writes anything: the path is user input and
        # may alias a source clip or a good copy.
        control.guard(
            source,
            [Path(e.source) for e in options.selection]
            if options.selection is not None else None,
            options.destinations,
        )
        control.claim()
        if not args.quiet:
            print(f"pausable: offloader control \"{args.control_file}\" --pause",
                  file=sys.stderr)

    progress = _Progress(not args.quiet)
    job = engine.run(source, options, progress, control)
    progress.done()

    if report is not None:
        # Recorded before the reports are written, so every one of them shows
        # what this job did not supply and none reads as the whole cut.
        job.unresolved = [timeline_mod.describe(r) for r in report.incomplete]
        for resolution in report.unconfirmed:
            job.warnings.append(
                f"{resolution.name}: treated as satisfied by proxy "
                f"{resolution.location}, frame counts unverified")
        for resolution in report.differing:
            others = ", ".join(str(a) for a in resolution.alternatives)
            job.warnings.append(
                f"{resolution.name}: copied from {resolution.location}; a "
                f"different file of this name is under the search roots ({others})")

    reports = _write_reports(
        job,
        args.report if isinstance(args.report, list) else _parse_reports(args.report),
        args.report_dir,
        args.logo,
        args.footer,
    )
    _summarize(job, reports)
    return exit_code(job)


def cmd_report(args: argparse.Namespace) -> int:
    options = _options_from(args, args.destinations or [args.source])
    progress = _Progress(not args.quiet)
    job = engine.rescan(args.source, args.destinations, options, progress)
    progress.done()

    reports = _write_reports(
        job,
        args.report if isinstance(args.report, list) else _parse_reports(args.report),
        args.report_dir,
        args.logo,
        args.footer,
    )
    _summarize(job, reports)
    return EXIT_REPORTS_FAILED if job.report_failures else 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-hash a tree against its manifests.

    Exit 0 only when every listed file matched. This is meant to be the gate a
    format script checks.
    """
    from . import verify as verify_mod

    target = Path(args.path)
    if target.is_file():
        manifests = [target]
    else:
        manifests = verify_mod.find_manifests(target)
    if not manifests:
        print(f"error: no .mhl manifest found under {target}", file=sys.stderr)
        return 2

    show = not args.quiet and sys.stderr.isatty()

    def progress(index: int, total: int, path: Path) -> None:
        if show:
            sys.stderr.write('\r' + f"  [{index + 1}/{total}] {path.name[:70]:<70}")
            sys.stderr.flush()

    worst = 0
    reports = []
    for manifest in manifests:
        try:
            reports.append(verify_mod.verify_manifest(
                manifest, progress=progress, bypass_cache=not args.allow_cache))
        except Exception as exc:
            print(f"error: could not read {manifest}: {exc}", file=sys.stderr)
            worst = max(worst, 2)
            continue
        if show:
            sys.stderr.write('\r' + " " * 90 + '\r')

    # Every manifest is checked before any is reported, so a rerun's manifest
    # can answer the entries an earlier failed run could not hash. Only that
    # direction: see `verify.supersede`.
    verify_mod.supersede(reports)

    for report in reports:
        manifest = report.manifest
        print('\n' + str(manifest))
        print(f"  {report.summary()}")
        for verdict in report.failures:
            print(f"  {verdict.describe()}")
        for verdict in report.superseded:
            print(f"  {verdict.describe()}")
        for verdict in report.directory_failures:
            print(f"  {verdict.describe()}")
        for warning in report.warnings:
            print(f"  warning: {warning}")
        for extra in report.unlisted[:20]:
            print(f"  not in manifest: {extra}")
        if len(report.unlisted) > 20:
            print(f"  ... and {len(report.unlisted) - 20} more not in manifest")
        if not report.passed:
            worst = max(worst, 1)

    print()
    print("VERIFIED — safe to erase the source" if worst == 0
          else "NOT VERIFIED — do not erase the source")
    return worst


def cmd_info(_args: argparse.Namespace) -> int:
    from . import sysinfo
    from .reports import fonts

    host = sysinfo.collect()
    print(f"{PRODUCT_NAME} {__version__}")
    print(f"  OS:          {host.os_version}")
    print(f"  Processors:  {host.processors}")
    print(f"  System RAM:  {host.system_ram or 'unknown'}")
    print(f"  ffprobe:     {probe.ffprobe_path() or 'NOT FOUND (metadata disabled)'}")
    print(f"  ffmpeg:      {thumbs.ffmpeg_path() or 'NOT FOUND (thumbnails disabled)'}")
    print(f"  report font: {fonts.describe()}"
          f"{'' if fonts.using_reference_fonts() else '  (Verdana missing — metrics differ)'}")
    enabled = longpath.os_long_paths_enabled()
    if enabled is not None:
        prefix = "\\\\?\\"
        note = ("also applied" if enabled
                else "required for destinations past 260 characters")
        print(f"  long paths:  Windows support {'on' if enabled else 'off'};"
              f" {prefix} prefix {note}")
    print("  checksums:   " + "; ".join(
        f"{key} ({alg.speed})" if alg.speed else key
        for key, alg in sorted(hashers.ALGORITHMS.items())))
    print(f"  reports:     {', '.join(WRITERS)}")
    print(f"  profiles:    {', '.join(p.value for p in Profile)} "
          f"(--profile; 'data' skips media probing for generic transfers)")
    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    from .gui.app import main as gui_main

    return gui_main([sys.argv[0]])


def cmd_update(args: argparse.Namespace) -> int:
    """Report or apply a newer release.

    Exit status (documented in docs/updates.md):

    * 0: up to date, or the installer ran and reported success;
    * 1: an update exists but was not applied (no `--install`);
    * 2: usage, a build that cannot update itself, or a download,
      verification, launch or installer failure;
    * 5: the release feed could not be fetched or read, so nothing is known;
    * 6: the installer was started but its result could not be observed.

    A caller scripting this can therefore tell "nothing to do" from "something
    to do" from "could not find out".
    """
    import tempfile

    from . import update as update_mod

    try:
        release = update_mod.find_release()
    except update_mod.UpdateError as exc:
        print(f"error: could not check for updates: {exc}", file=sys.stderr)
        return 5
    if release is None:
        print(f"Offloader {__version__} is the newest release available.")
        return 0

    print(f"Offloader {release.version} is available "
          f"(installed: {__version__}).")
    if not args.install:
        print("  offloader update --install    download, verify and install")
        return 1

    if sys.platform != "win32":
        print("error: only the Windows release can be installed this way",
              file=sys.stderr)
        return 2

    try:
        target = update_mod.install_target()
    except update_mod.UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    def report(done: int, total: int) -> None:
        if total:
            sys.stderr.write(f"\r  downloading {done * 100 // total:3d}%")
        else:
            sys.stderr.write(f"\r  downloading {format_size(done)}")
        sys.stderr.flush()

    created: Path | None = None
    staged: Path | None = None
    installer: Path | None = None
    observed: bool | None = None
    wait = False
    try:
        directory = args.dir
        if directory is None:
            directory = created = Path(tempfile.mkdtemp(prefix="offloader-update-"))
        elif update_mod.is_link(directory):
            raise update_mod.UpdateError(
                f"refusing a download directory that is a link or junction: "
                f"{directory}")
        else:
            directory.mkdir(parents=True, exist_ok=True)

        staged, digest = update_mod.download(release, directory,
                                             progress=report)
        sys.stderr.write("\r" + " " * 32 + "\r")
        update_mod.verify(staged, release, expected_digest=digest)
        installer = update_mod.promote(staged)
        staged = None
        print(f"  verified {installer.name}")
        print(f"  sha256   {digest}")
        # Said before the prompt appears, because the installer refuses rather
        # than closing anything: an operator who does not know that reads the
        # refusal as a broken update.
        print("\nThe installer will not replace a running Offloader. Close "
              "the app and any\nCLI transfer first; an active job makes it "
              "exit without changing anything.")
        # A process running from the installation it replaces cannot wait:
        # it holds that installation's lock and files open, which is exactly
        # what makes the installer refuse.
        wait = not update_mod.runs_from(target)
        observed = update_mod.apply(installer, target, expected_digest=digest,
                                    wait=wait)
    except update_mod.UpdateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if staged is not None:
            update_mod.discard(staged)
        # A temporary directory this command created is removed unless the
        # installer may still be running from it (started, not waited for).
        if created is not None and observed is not False:
            if installer is not None:
                update_mod.discard(installer)
            try:
                created.rmdir()
            except OSError:
                pass

    if observed:
        print(f"Offloader {release.version} was installed.")
        return 0
    if wait:
        print("The installer has started, but Windows returned no process to "
              "wait on, so its result is unknown. Silent mode shows no "
              "message if it refuses; run `offloader --version` once it has "
              f"finished to confirm {release.version} is installed.")
    else:
        print("The installer has started. This command runs from the "
              "installation it is replacing, so it exits now rather than hold "
              "that installation open, and it cannot see the installer's "
              "result. Silent mode shows no message if it refuses; run "
              f"`offloader --version` once it has finished to confirm "
              f"{release.version} is installed.")
    return 6


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"offload": cmd_offload, "report": cmd_report,
                "verify": cmd_verify, "info": cmd_info, "gui": cmd_gui,
                "resolve": cmd_resolve, "control": cmd_control,
                "update": cmd_update}
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_CANCELLED
    except (engine.UnsafeDestination, engine.UnsafeControlFile) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except timeline_mod.TimelineSupportMissing as exc:
        # A missing optional dependency is an environment error.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
