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
from offloader.models import VerificationMode  # noqa: E402
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


def seed_config() -> None:
    (config_dir() / "presets.json").write_text(
        json.dumps([p.to_dict() for p in PRESETS], indent=2), encoding="utf-8")


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
                  started_at=now - 940, finished_at=now - 512),
    ]
    controller.itemsChanged.emit()


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

    # The panel scans real volumes on a worker thread; give it ours instead.
    drives.list_volumes = lambda: list(VOLUMES)

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
    shoot(window, out, "app-preset-mode.png")

    window._set_mode(1)
    # A named folder rather than the bare drive root: the drop zone shows the
    # name above the full path, and for a root both lines read "E:\".
    window.simple.set_source(Path(r"E:\A001_PYXIS"))
    window.simple.destinations.set_paths([Path(r"D:\Archive\2026"),
                                          Path(r"N:\cold\2026")])
    settle(app)
    shoot(window, out, "app-simple-mode.png")

    editor = PresetEditor(PRESETS[2])
    editor.show()
    settle(app)
    shoot(editor, out, "app-preset-editor.png")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
