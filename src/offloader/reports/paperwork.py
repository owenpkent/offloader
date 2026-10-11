"""Writing a finished job's reports and manifests, for the CLI and the GUI.

One implementation, so the two front ends cannot drift apart on the rules that
decide whether a later `verify` can be trusted:

* Every destination gets its own MHL, inside that destination, so each copy
  can be verified on its own. `--report-dir` moves the rest of the paperwork,
  and holds a manifest only for the destination it sits inside, if any.
* An existing manifest is never overwritten. A rerun's manifest is numbered
  (`JobReport-2.mhl`), because the earlier one may be the only record of
  files the rerun skipped.
* Every writer failure is recorded on the job (`Job.report_failures`), which
  keeps its final status from reading as a plain success.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..models import Job
from .mhl import next_free_path

#: Filenames written inside "<name>_Reports/", matching the reference layout.
REPORT_FILENAMES = {
    "pdf": "JobReport.pdf",
    "csv": "JobReport.csv",
    "mhl": "JobReport.mhl",
    "ascmhl": "ascmhl",
    "html": "JobReport.html",
}

#: Formats that describe one destination each rather than the job as a whole.
MANIFEST_FORMATS = ("mhl", "ascmhl")


def _containing_root(directory: Path, roots: list[Path]) -> int | None:
    """Index of the destination root `directory` sits inside, the deepest if
    roots nest, or None when it is outside them all."""
    try:
        target = Path(directory).resolve()
    except OSError:
        return None
    best: tuple[int, int] | None = None
    for index, root in enumerate(roots):
        try:
            resolved = Path(root).resolve()
            target.relative_to(resolved)
        except (OSError, ValueError):
            continue
        depth = len(resolved.parts)
        if best is None or depth > best[0]:
            best = (depth, index)
    return None if best is None else best[1]


def paperwork_dir(job: Job, report_dir: Path | None = None) -> Path:
    """Where the PDF, CSV and HTML go."""
    if report_dir is not None:
        return Path(report_dir)
    roots = job.destination_roots or [job.source_root]
    return job.report_dir_for(roots[0])


def write_reports(job: Job, formats: list[str], *, report_dir: Path | None = None,
                  logo: Path | None = None, footer: str | None = None) -> list[Path]:
    """Write every requested format and return what was written.

    Failures are appended to `job.report_failures` rather than raised: a bad
    PDF must not mask a good offload, but it must not vanish either.
    Manifests are written first, so a manifest failure or a renumbered
    manifest is already on the job when the PDF and CSV describe it.
    """
    # Looked up per call, so a test can stand a failing writer in.
    from . import WRITERS

    roots = list(job.destination_roots or [job.source_root])
    paperwork = paperwork_dir(job, report_dir)
    relocated_into = (_containing_root(paperwork, roots)
                      if report_dir is not None else None)
    written: list[Path] = []

    if "mhl" in formats:
        for index, root in enumerate(roots):
            directory = (paperwork if index == relocated_into
                         else job.report_dir_for(root))
            planned = directory / REPORT_FILENAMES["mhl"]
            try:
                target = next_free_path(planned)
                written.append(WRITERS["mhl"](job, target, destination_index=index))
            except Exception as exc:  # noqa: BLE001 - recorded, see docstring
                job.report_failures.append(f"MHL for {root}: {exc}")
                continue
            if target != planned:
                job.warnings.append(
                    f"{planned} already existed and was kept; this run's "
                    f"manifest is {target.name}")

    if "ascmhl" in formats:
        for index, root in enumerate(roots):
            try:
                written.append(WRITERS["ascmhl"](
                    job, paperwork / REPORT_FILENAMES["ascmhl"],
                    destination_index=index))
            except Exception as exc:  # noqa: BLE001
                job.report_failures.append(f"ASC MHL for {root}: {exc}")

    for key in formats:
        if key in MANIFEST_FORMATS:
            continue
        writer = WRITERS.get(key)
        if writer is None:
            continue
        try:
            os.makedirs(paperwork, exist_ok=True)
            written.append(writer(job, paperwork / REPORT_FILENAMES[key],
                                  logo=logo, footer=footer))
        except Exception as exc:  # noqa: BLE001
            job.report_failures.append(f"{key} report: {exc}")
    return written
