"""Command line interface."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import (PRODUCT_NAME, __version__, engine, hashers, longpath,
               probe, retry, thumbs)
from .models import FileStatus, Job, VerificationMode
from .reports import WRITERS
from .util import format_elapsed, format_size

#: Filenames written inside "<name>_Reports/", matching the reference layout.
REPORT_FILENAMES = {
    "pdf": "JobReport.pdf",
    "csv": "JobReport.csv",
    "mhl": "JobReport.mhl",
    "html": "JobReport.html",
}

DEFAULT_REPORTS = "pdf"


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


def _write_reports(job: Job, formats: list[str], out_dir: Path,
                   logo: Path | None, footer: str | None = None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for key in formats:
        target = out_dir / REPORT_FILENAMES[key]
        try:
            written.append(WRITERS[key](job, target, logo=logo, footer=footer))
        except Exception as exc:  # a bad report must not mask a good offload
            print(f"  ! {key} report failed: {exc}", file=sys.stderr)

    if "mhl" in formats:
        written.extend(_write_extra_manifests(job, out_dir))
    return written


def _write_extra_manifests(job: Job, primary_dir: Path) -> list[Path]:
    """One MHL per destination, written beside that copy.

    A manifest that lives only with the first copy cannot re-verify the second.
    Each copy needs its own chain of custody.
    """
    written: list[Path] = []
    for index, root in enumerate(job.destination_roots[1:], start=1):
        target = root / f"{job.name}_Reports" / REPORT_FILENAMES["mhl"]
        if target.parent == primary_dir:
            continue
        try:
            written.append(WRITERS["mhl"](job, target, destination_index=index))
        except Exception as exc:
            print(f"  ! MHL for {root} failed: {exc}", file=sys.stderr)
    return written


def _summarize(job: Job, reports: list[Path]) -> None:
    failed = [f for f in job.files if f.status is FileStatus.FAILED]
    print()
    print(f"  {job.name}: {job.final_status}")
    print(f"  {job.total_files} files, {format_size(job.total_bytes)}"
          f" in {format_elapsed(job.elapsed_sec)}"
          f"  ({job.video_files} video)")
    print(f"  Verification: {job.verification_label}")
    for destination in job.destination_roots:
        print(f"  -> {destination}")
    for report in reports:
        print(f"  report: {report}")
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
                        help="checksum algorithm (default: %(default)s)")
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
                        help="attempts per file when a read fails for a "
                             "transient reason (default: %(default)s, 1 disables)")
    parser.add_argument("--retry-wait", type=float, default=2.0, metavar="SECONDS",
                        help="pause before the first retry, backing off after "
                             "(default: %(default)s)")
    parser.add_argument("--no-probe", action="store_true",
                        help="skip ffprobe metadata and thumbnails")
    parser.add_argument("--quiet", action="store_true", help="suppress progress")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="offloader",
        description=f"{PRODUCT_NAME} — verified media offload with job reports.",
    )
    parser.add_argument("--version", action="version",
                        version=f"{PRODUCT_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    offload = sub.add_parser("offload", help="copy and verify a source to destinations")
    offload.add_argument("--source", type=Path, required=True,
                         help="card or folder to offload")
    offload.add_argument("--dest", type=Path, action="append", required=True,
                         dest="destinations", metavar="PATH",
                         help="destination root (repeat for multiple copies)")
    offload.add_argument("--verify", default=VerificationMode.SOURCE_ONLY.value,
                         choices=[m.value for m in VerificationMode],
                         help="verification depth (default: %(default)s)")
    offload.add_argument("--flat", action="store_true",
                         help="do not recreate the source folder structure")
    offload.add_argument("--skip-existing", action="store_true",
                         help="skip files already present with a matching size")
    _common_options(offload)

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
    return parser


def _options_from(args: argparse.Namespace, destinations: list[Path]) -> engine.OffloadOptions:
    return engine.OffloadOptions(
        destinations=destinations,
        algorithm=args.hash,
        verification=VerificationMode(getattr(args, "verify", "source-only")),
        thumbnail_count=0 if args.no_probe else max(0, args.thumbs),
        excludes=tuple(engine.DEFAULT_EXCLUDES) + tuple(args.exclude),
        preserve_structure=not args.flat,
        skip_existing=getattr(args, "skip_existing", False),
        job_name=args.name,
        extra_probe=not args.no_probe,
        retry=retry.RetryPolicy(attempts=max(1, args.retries),
                                delay=max(0.0, args.retry_wait)),
    )


def _report_dir(args: argparse.Namespace, job: Job, fallback_root: Path) -> Path:
    if args.report_dir:
        return args.report_dir
    return fallback_root / f"{job.name}_Reports"


def cmd_offload(args: argparse.Namespace) -> int:
    options = _options_from(args, args.destinations)
    progress = _Progress(not args.quiet)
    job = engine.run(args.source, options, progress)
    progress.done()

    reports = _write_reports(
        job,
        args.report if isinstance(args.report, list) else _parse_reports(args.report),
        _report_dir(args, job, job.destination_roots[0]),
        args.logo,
        args.footer,
    )
    _summarize(job, reports)
    return 1 if any(f.status is FileStatus.FAILED for f in job.files) else 0


def cmd_report(args: argparse.Namespace) -> int:
    options = _options_from(args, args.destinations or [args.source])
    progress = _Progress(not args.quiet)
    job = engine.rescan(args.source, args.destinations, options, progress)
    progress.done()

    reports = _write_reports(
        job,
        args.report if isinstance(args.report, list) else _parse_reports(args.report),
        _report_dir(args, job, (args.destinations or [args.source])[0]),
        args.logo,
        args.footer,
    )
    _summarize(job, reports)
    return 0


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
    for manifest in manifests:
        try:
            report = verify_mod.verify_manifest(
                manifest, progress=progress, bypass_cache=not args.allow_cache)
        except Exception as exc:
            print(f"error: could not read {manifest}: {exc}", file=sys.stderr)
            worst = max(worst, 2)
            continue
        if show:
            sys.stderr.write('\r' + " " * 90 + '\r')

        print('\n' + str(manifest))
        print(f"  {report.summary()}")
        for verdict in report.failures:
            print(f"  {verdict.describe()}")
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
    print(f"  checksums:   {', '.join(sorted(hashers.algorithm_keys()))}")
    print(f"  reports:     {', '.join(WRITERS)}")
    return 0


def cmd_gui(_args: argparse.Namespace) -> int:
    from .gui.app import main as gui_main

    return gui_main([sys.argv[0]])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"offload": cmd_offload, "report": cmd_report,
                "verify": cmd_verify, "info": cmd_info, "gui": cmd_gui}
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except engine.UnsafeDestination as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
