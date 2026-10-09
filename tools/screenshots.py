"""Render the desktop app to the PNGs the README embeds.

    python tools/screenshots.py            # writes docs/images/
    python tools/screenshots.py OUT_DIR

Screenshots go stale the moment the interface moves, so they are generated
rather than captured by hand. What the app would otherwise read off the machine
running this is replaced:

- **The config directory is a throwaway.** Real presets, settings and offload
  history are neither read nor written, and the presets in the pictures are
  seeded into the sandbox from `PRESETS` below.
- **The drive panel is fed invented volumes.** Whatever is actually mounted
  would otherwise put its label and free space into a public README.

Everything else is the real thing: the status bar reports on the ffmpeg that is
genuinely on `PATH`, and the throughput and ETA are computed by the app from
the queue state set up here. Nothing is copied — the queue items are built
directly rather than enqueued, so no job ever runs.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO / "docs" / "images"

# This has to happen before the app reads any of it. `config_dir()` checks the
# environment on each call, but the preset store is built inside
# `MainWindow.__init__`, so the sandbox must be in place before the import
# below — hence the deliberate E402s.
_sandbox = Path(tempfile.mkdtemp(prefix="offloader-shots-"))
os.environ["APPDATA"] = str(_sandbox)
os.environ["XDG_CONFIG_HOME"] = str(_sandbox)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QSplitter  # noqa: E402

from offloader.config import config_dir  # noqa: E402
from offloader.gui import drives, theme  # noqa: E402
from offloader.gui.main_window import MainWindow  # noqa: E402
from offloader.gui.preset_editor import PresetEditor  # noqa: E402
from offloader.gui.worker import JobState, QueueItem  # noqa: E402
from offloader.models import (  # noqa: E402
    Destination,
    FileEntry,
    FileStatus,
    Job,
    VerificationMode,
)
from offloader.presets import PRESET_COLORS, Preset  # noqa: E402
from offloader.volumes import Volume  # noqa: E402

GB = 1024 ** 3

PRESETS = [
    Preset(name="Dailies — single copy",
           destinations=[Path(r"D:\Dailies")],
           algorithm="xxh3-64", verification=VerificationMode.SOURCE_ONLY,
           reports=["pdf"], color=PRESET_COLORS[0],
           naming_template="{card}", use_count=34),
    Preset(name="Archive — two copies, full verify",
           destinations=[Path(r"D:\Archive\2026"), Path(r"N:\cold\2026")],
           algorithm="xxh3-64", verification=VerificationMode.FULL,
           reports=["pdf", "csv", "ascmhl"], color=PRESET_COLORS[1],
           naming_template="{card}_{date}", use_count=12),
    Preset(name="Irreplaceable — read twice",
           destinations=[Path(r"D:\Masters"), Path(r"N:\cold\masters")],
           algorithm="sha256", verification=VerificationMode.FULL,
           reports=["pdf", "ascmhl", "html"], color=PRESET_COLORS[2],
           naming_template="{card}_{date}", paranoid=True, use_count=3),
]

#: Two cards and three fixed disks — enough for the panel to show the `CARD`
#: badge, the sort that puts cards first, and an amber bar past 80 %.
VOLUMES = [
    Volume(root=Path("E:\\"), label="A001 (PYXIS)", filesystem="exFAT",
           total_bytes=512 * GB, free_bytes=61 * GB,
           drive_type="removable", is_camera_card=True),
    Volume(root=Path("F:\\"), label="B002 (KOMODO)", filesystem="exFAT",
           total_bytes=256 * GB, free_bytes=203 * GB,
           drive_type="removable", is_camera_card=True),
    Volume(root=Path("C:\\"), label="System", filesystem="NTFS",
           total_bytes=1024 * GB, free_bytes=402 * GB),
    Volume(root=Path("D:\\"), label="Shuttle", filesystem="NTFS",
           total_bytes=8 * 1024 * GB, free_bytes=5734 * GB),
    Volume(root=Path("N:\\"), label="Cold storage", filesystem="NTFS",
           total_bytes=48 * 1024 * GB, free_bytes=7100 * GB),
]


def stub_volume_scan() -> None:
    """Replace the drive panel's scan, and refuse to run if there is nothing
    to replace.

    This used to assign `drives.list_volumes`. The panel's scan was later
    rewritten around `scan_batches`, and since nothing verified the target
    still existed, the assignment quietly began creating a new unused attribute
    instead of overriding anything. The real scan then ran on every render, and
    real drive labels and free space — including network shares — could reach
    the PNGs that go in a public README. Whether they did came down to which
    write landed last, because the network batch arrives seconds after the
    local one.

    So: patch the funnel every scan goes through, and make a missing name stop
    the run rather than hand the panel back to the machine.
    """
    if not callable(getattr(drives, "scan_batches", None)):
        raise SystemExit(
            "tools/screenshots.py: drives.scan_batches is gone, so the drive "
            "panel would scan this machine and put its real volumes in the "
            "screenshots. Point the stub at whatever the panel calls now.")
    # One final batch: no second delivery to race the first, and the panel
    # never sees a non-final batch it would merge network shares into.
    drives.scan_batches = lambda: iter([(list(VOLUMES), True)])


def assert_no_real_volumes(window: MainWindow) -> None:
    """Fail if anything but the invented volumes reached the panel.

    The stub above is the guard; this is the check that the guard worked. It
    reads what is actually on screen, so it survives the next rewrite of the
    scan in a way that patching a function name did not.
    """
    invented = {volume.label for volume in VOLUMES}
    showing = {row.volume.label for row in window.drives._rows}
    leaked = showing - invented
    if leaked:
        raise SystemExit(
            "tools/screenshots.py: the drive panel is showing real volumes "
            f"({', '.join(sorted(leaked))}); refusing to write screenshots.")


def seed_config() -> None:
    (config_dir() / "presets.json").write_text(
        json.dumps([p.to_dict() for p in PRESETS], indent=2), encoding="utf-8")


def finished_job() -> Job:
    """A completed job, so the file pane has checksums to show.

    The detail pane's whole point is the source and destination hashes beside
    each other, and a screenshot of it empty would document nothing. Invented
    clips, but real structure: every checksum here is what the pane will render
    for a genuine offload, and the pairs match because the job verified.
    """
    root = Path("E:\\")
    destination = Path("D:\\video\\080426\\A002")
    clips = [
        ("A002_08041151_C001.braw", 24_411_238_400, "3f2a9c17b48e05d1"),
        ("A002_08041203_C002.braw", 31_884_902_400, "b71e04c9a3fd2b68"),
        ("A002_08041219_C003.braw", 28_106_342_400, "0c4d8ba25e91f7a3"),
        ("A002_08041244_C004.braw", 19_907_481_600, "e58f13d072ac4b96"),
        ("A002_08041302_C005.braw", 34_022_297_600, "9a2b6e8f14c703de"),
    ]
    # A real duration, so the queue's finished row reads "138.33 GB in 0:07:08"
    # rather than an elapsed time of zero.
    started = datetime.now() - timedelta(seconds=428)
    job = Job(name="A002", source_root=root, destination_roots=[destination],
              verification=VerificationMode.FULL, hash_label="XXHash3-64",
              started=started, finished=started + timedelta(seconds=428))
    for name, size, checksum in clips:
        job.files.append(FileEntry(
            source=root / name, source_root=root, size=size,
            created=0.0, modified=0.0, checksum=checksum,
            destinations=[Destination(
                root=destination, path=destination / name,
                status=FileStatus.VERIFIED, checksum=checksum)],
        ))
    return job


def fill_queue(window: MainWindow) -> None:
    """One job running, one waiting, one done.

    Built by hand rather than enqueued: `enqueue` would start a real offload,
    and there is nothing here to copy.
    """
    controller = window.controller
    controller._auto_start = False
    now = time.monotonic()

    controller.items = [
        QueueItem(identifier=1, source=Path("E:\\"), name="A001",
                  preset=PRESETS[1], state=JobState.RUNNING, fraction=0.62,
                  stage="copy", current_file="A001_08041254_C007.braw",
                  bytes_done=int(283.4 * GB), bytes_total=int(457.0 * GB),
                  started_at=now - 512),
        QueueItem(identifier=2, source=Path("F:\\"), name="B002_080426",
                  preset=PRESETS[2], state=JobState.QUEUED,
                  bytes_total=int(198.0 * GB)),
        QueueItem(identifier=3, source=Path("E:\\"), name="A002",
                  preset=PRESETS[0], state=JobState.DONE, fraction=1.0,
                  stage="verify", bytes_done=int(129.7 * GB),
                  bytes_total=int(129.7 * GB),
                  started_at=now - 940, finished_at=now - 512,
                  job=finished_job()),
    ]
    controller.itemsChanged.emit()
    # The finished job, so the picture shows the file pane doing its job rather
    # than inviting the reader to select something. Scrolled back afterwards:
    # selecting the last row scrolls it into view, which pushed the running job
    # — the thing the queue panel is there to show — out of the frame.
    window.queue.table.selectRow(2)
    window.queue.table.scrollToTop()


def stamp_rate(item, mb_per_sec: float = 594.2, span: float = 2.0) -> None:
    """Give a hand-built queue item a trailing progress sample.

    The rate and the ETA come from a window of samples measured against the
    clock, so an item assembled here has neither and the throughput column
    renders empty. Called immediately before each picture rather than once when
    the queue is built: a sample older than the window reads as no rate at all,
    which made the figure come and go with however long Qt took to start.
    """
    rate = int(mb_per_sec * 1024 * 1024)
    item._samples.clear()
    item._samples.append((time.monotonic() - span,
                          item.bytes_done - int(rate * span)))


def settle(app: QApplication, rounds: int = 12) -> None:
    for _ in range(rounds):
        app.processEvents()


def shoot(widget, out: Path, name: str) -> None:
    pixmap = widget.grab()
    pixmap.save(str(out / name), "PNG")
    print(f"{name}  {pixmap.width()}x{pixmap.height()}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out = Path(argv[0]).resolve() if argv else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)
    seed_config()

    stub_volume_scan()

    app = QApplication([])
    theme.apply(app)

    window = MainWindow()
    window.resize(1280, 900)
    window.show()
    settle(app)

    # The queue is the part a reader most needs to see, and the default split
    # leaves it a row and a half tall.
    for splitter in window.findChildren(QSplitter):
        if splitter.orientation() == Qt.Vertical:
            splitter.setSizes([500, 400])

    window.drives._rebuild(list(VOLUMES))
    fill_queue(window)
    settle(app)

    window._set_mode(0)
    settle(app)
    assert_no_real_volumes(window)
    stamp_rate(window.controller.items[0])
    # The column repaints itself, but the running-job line above it is only
    # rebuilt when the panel ticks — without this the two disagree about
    # whether there is a rate at all.
    window.queue._tick()
    shoot(window, out, "app-preset-mode.png")

    window._set_mode(1)
    # A named folder rather than the bare drive root: the drop zone shows the
    # name above the full path, and for a root both lines read "E:\".
    window.simple.set_source(Path(r"E:\A001_PYXIS"))
    window.simple.destinations.set_paths([Path(r"D:\Archive\2026"),
                                          Path(r"N:\cold\2026")])
    settle(app)
    # Checked again: the panel polls every few seconds, so a scan that slipped
    # past the stub would land between the two pictures.
    assert_no_real_volumes(window)
    stamp_rate(window.controller.items[0])
    window.queue._tick()
    shoot(window, out, "app-simple-mode.png")

    editor = PresetEditor(PRESETS[2])
    editor.show()
    settle(app)
    shoot(editor, out, "app-preset-editor.png")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
