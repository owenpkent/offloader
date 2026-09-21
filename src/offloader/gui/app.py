"""Application entry point."""

from __future__ import annotations

import sys
from pathlib import Path

from .. import PRODUCT_NAME, __version__


def _app_icon():
    """The same filmstrip the shell entry and the PDF header use.

    Built in memory rather than read from a file: the mark is drawn by
    `shellicon`, so launching the app does not need to have written it to disk
    first. Only the small sizes — the 256 costs a per-pixel Python loop for
    something no title bar will show.
    """
    from PySide6.QtGui import QIcon, QPixmap

    from .. import shellicon

    icon = QIcon()
    for size in (16, 32, 48):
        pixmap = QPixmap()
        pixmap.loadFromData(shellicon.png_at(size), "PNG")
        icon.addPixmap(pixmap)
    return icon


def main(argv: list[str] | None = None) -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "The GUI needs PySide6. Install it with:\n"
            "    pip install \"offloader[gui]\"",
            file=sys.stderr,
        )
        return 1

    from . import theme
    from .main_window import MainWindow

    args = list(argv if argv is not None else sys.argv)
    # A single trailing path is a source to start from, which is what the
    # Explorer context-menu entry passes. Anything Qt wants is left for it.
    source: Path | None = None
    if len(args) > 1 and not args[-1].startswith("-"):
        candidate = Path(args[-1])
        if candidate.exists():
            source = candidate
            args = args[:-1]

    app = QApplication(args)
    app.setApplicationName(PRODUCT_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(PRODUCT_NAME)
    app.setWindowIcon(_app_icon())
    theme.apply(app)

    window = MainWindow(source)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
