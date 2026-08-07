"""Job queue and the worker thread that drains it.

Jobs run one at a time. That is not a simplification — offloads are I/O bound,
and running two at once against the same bus makes both slower while making the
progress readout meaningless.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .. import engine, history, naming
from ..models import Job
from ..presets import Preset
from ..reports import WRITERS

REPORT_FILENAMES = {
    "pdf": "JobReport.pdf",
    "csv": "JobReport.csv",
    "mhl": "JobReport.mhl",
    "ascmhl": "ascmhl",
    "html": "JobReport.html",
}


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


@dataclass
class QueueItem:
    """One entry in the queue: a source, the preset to run it under, and the
    live state of that run."""

    identifier: int
    source: Path
    name: str
    preset: Preset
    state: JobState = JobState.QUEUED
    fraction: float = 0.0
    stage: str = ""
    current_file: str = ""
    bytes_done: int = 0
    bytes_total: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    job: Job | None = None
    reports: list[Path] = field(default_factory=list)
    error: str | None = None
    control: engine.JobControl = field(default_factory=engine.JobControl)

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.monotonic()) - self.started_at

    @property
    def rate_bytes_per_sec(self) -> float:
        elapsed = self.elapsed
        return self.bytes_done / elapsed if elapsed > 0.5 else 0.0

    @property
    def eta_seconds(self) -> float | None:
        rate = self.rate_bytes_per_sec
        remaining = self.bytes_total - self.bytes_done
        if rate <= 0 or remaining <= 0 or self.state is not JobState.RUNNING:
            return None
        return remaining / rate

    @property
    def status_text(self) -> str:
        if self.state is JobState.RUNNING:
            return self.stage.capitalize() or "Running"
        if self.state is JobState.FAILED and self.error:
            return "Failed"
        if self.state is JobState.DONE and self.job:
            return self.job.final_status
        return self.state.value.capitalize()


class _Runner(QThread):
    """Runs a single queue item off the UI thread."""

    progressed = Signal(int, float, str, str, int, int)
    completed = Signal(int, object, object, object)   # id, Job|None, reports, error

    def __init__(self, item: QueueItem, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._item = item
        self._last_emit = 0.0

    def _on_progress(self, event: engine.ProgressEvent) -> None:
        # The engine fires once per 8 MiB chunk; throttle so a fast NVMe copy
        # cannot flood the event loop.
        now = time.monotonic()
        finished = event.job_bytes_done >= event.job_bytes_total
        if now - self._last_emit < 0.05 and not finished:
            return
        self._last_emit = now
        fraction = (event.job_bytes_done / event.job_bytes_total
                    if event.job_bytes_total else 0.0)
        self.progressed.emit(
            self._item.identifier, fraction, event.stage, event.file_name,
            event.job_bytes_done, event.job_bytes_total,
        )

    def run(self) -> None:  # noqa: D102 - QThread entry point
        item = self._item
        try:
            options = item.preset.to_options(job_name=item.name)
            job = engine.run(item.source, options, self._on_progress, item.control)
            reports = self._write_reports(job, item.preset)
            self.completed.emit(item.identifier, job, reports, None)
        except Exception as exc:  # surfaced in the queue row, never a crash
            self.completed.emit(item.identifier, None, [], str(exc))

    def _write_reports(self, job: Job, preset: Preset) -> list[Path]:
        if job.cancelled or not preset.reports or not job.destination_roots:
            return []
        out_dir = job.destination_roots[0] / f"{job.name}_Reports"
        written: list[Path] = []
        for key in preset.reports:
            writer = WRITERS.get(key)
            if writer is None:
                continue
            try:
                written.append(writer(job, out_dir / REPORT_FILENAMES[key],
                                      logo=preset.logo, footer=preset.footer))
            except Exception:
                # A report that fails to render must not invalidate a good copy.
                continue

        if "ascmhl" in preset.reports:
            from ..ascmhl import write_manifest
            for index, root in enumerate(job.destination_roots[1:], start=1):
                try:
                    written.append(write_manifest(job, root,
                                                  destination_index=index))
                except Exception:
                    continue

        if "mhl" in preset.reports:
            # Each copy needs its own manifest, or the second destination has
            # nothing to re-verify itself against.
            for index, root in enumerate(job.destination_roots[1:], start=1):
                target = root / f"{job.name}_Reports" / REPORT_FILENAMES["mhl"]
                if target.parent == out_dir:
                    continue
                try:
                    written.append(WRITERS["mhl"](job, target,
                                                  destination_index=index))
                except Exception:
                    continue
        return written


class QueueController(QObject):
    """Owns the queue and drives it. All signals arrive on the UI thread."""

    itemsChanged = Signal()
    itemChanged = Signal(int)
    jobStarted = Signal(int)
    jobFinished = Signal(int)
    queueIdle = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.items: list[QueueItem] = []
        self.history = history.History()
        self._runner: _Runner | None = None
        self._next_id = 1
        self._auto_start = True

    # ---------------------------------------------------------------- queue
    def enqueue(self, source: Path, preset: Preset, name: str | None = None) -> QueueItem:
        source = Path(source)
        resolved = name or naming.build(
            preset.naming_template, source, taken=self._taken_names()
        )
        item = QueueItem(
            identifier=self._next_id,
            source=source,
            name=resolved,
            preset=preset,
        )
        self._next_id += 1
        self.items.append(item)
        self.itemsChanged.emit()
        if self._auto_start:
            self.start_next()
        return item

    def _taken_names(self) -> list[str]:
        """Names already used by queued jobs or past offloads, so auto-naming
        never points two jobs at one report folder."""
        return [item.name for item in self.items] + list(self.history.used_names())

    def find(self, identifier: int) -> QueueItem | None:
        return next((i for i in self.items if i.identifier == identifier), None)

    def remove(self, identifier: int) -> None:
        item = self.find(identifier)
        if item is None or item.state is JobState.RUNNING:
            return
        self.items.remove(item)
        self.itemsChanged.emit()

    def clear_finished(self) -> None:
        before = len(self.items)
        self.items = [i for i in self.items if not i.state.is_terminal]
        if len(self.items) != before:
            self.itemsChanged.emit()

    def move(self, identifier: int, offset: int) -> None:
        """Reorder a pending job — the queue's priority control."""
        item = self.find(identifier)
        if item is None or item.state is not JobState.QUEUED:
            return
        index = self.items.index(item)
        target = max(0, min(len(self.items) - 1, index + offset))
        if self.items[target].state is JobState.RUNNING:
            return
        if target != index:
            self.items.insert(target, self.items.pop(index))
            self.itemsChanged.emit()

    # ---------------------------------------------------------------- run
    @property
    def is_busy(self) -> bool:
        return self._runner is not None and self._runner.isRunning()

    def start_next(self) -> None:
        if self.is_busy:
            return
        item = next((i for i in self.items if i.state is JobState.QUEUED), None)
        if item is None:
            self.queueIdle.emit()
            return

        item.state = JobState.RUNNING
        item.started_at = time.monotonic()
        runner = _Runner(item, self)
        runner.progressed.connect(self._on_progress)
        runner.completed.connect(self._on_completed)
        runner.finished.connect(self._on_runner_finished)
        self._runner = runner
        runner.start()
        self.jobStarted.emit(item.identifier)
        self.itemChanged.emit(item.identifier)

    def pause(self, identifier: int) -> None:
        item = self.find(identifier)
        if item is None or item.state is not JobState.RUNNING:
            return
        item.control.pause()
        item.state = JobState.PAUSED
        self.itemChanged.emit(identifier)

    def resume(self, identifier: int) -> None:
        item = self.find(identifier)
        if item is None or item.state is not JobState.PAUSED:
            return
        item.control.resume()
        item.state = JobState.RUNNING
        self.itemChanged.emit(identifier)

    def cancel(self, identifier: int) -> None:
        item = self.find(identifier)
        if item is None or item.state.is_terminal:
            return
        if item.state is JobState.QUEUED:
            item.state = JobState.CANCELLED
            item.finished_at = time.monotonic()
            self.itemChanged.emit(identifier)
            return
        item.control.cancel()

    def cancel_all(self) -> None:
        for item in list(self.items):
            self.cancel(item.identifier)

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Cancel everything and wait for the worker, so closing the window
        cannot leave a half-written file on a card."""
        self.cancel_all()
        if self._runner is not None:
            self._runner.wait(timeout_ms)

    # ---------------------------------------------------------------- slots
    def _on_progress(self, identifier: int, fraction: float, stage: str,
                     filename: str, done: int, total: int) -> None:
        item = self.find(identifier)
        if item is None:
            return
        item.fraction = fraction
        item.stage = stage
        item.current_file = filename
        item.bytes_done = done
        item.bytes_total = total
        self.itemChanged.emit(identifier)

    def _on_completed(self, identifier: int, job: Job | None,
                      reports: list, error: str | None) -> None:
        item = self.find(identifier)
        if item is None:
            return
        item.finished_at = time.monotonic()
        item.job = job
        item.reports = list(reports or [])
        item.error = error

        if error is not None:
            item.state = JobState.FAILED
        elif job is not None and job.cancelled:
            item.state = JobState.CANCELLED
        elif job is not None and job.final_status == "Failed":
            item.state = JobState.FAILED
            item.error = job.notes or "one or more files failed verification"
        else:
            item.state = JobState.DONE
            item.fraction = 1.0
            item.preset.mark_used()

        if job is not None and not job.cancelled:
            try:
                paths = engine.scan(item.source, item.preset.to_options().excludes)
                self.history.record(job, history.fingerprint(paths, item.source))
            except OSError:
                pass

        self.itemChanged.emit(identifier)
        self.jobFinished.emit(identifier)

    def _on_runner_finished(self) -> None:
        self._runner = None
        self.start_next()
