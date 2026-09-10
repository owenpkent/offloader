"""Frozen graphical entry point for Offloader."""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path


def main() -> int:
    # The smoke runner uses the real frozen event loop without opening a window
    # indefinitely on CI. This variable has no effect during normal launches.
    if os.environ.get("OFFLOADER_GUI_SMOKE") == "1":
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from offloader import PRODUCT_NAME, __version__
        from offloader.gui import theme
        from offloader.gui.main_window import MainWindow

        app = QApplication(sys.argv)
        app.setApplicationName(PRODUCT_NAME)
        app.setApplicationVersion(__version__)
        app.setOrganizationName(PRODUCT_NAME)
        theme.apply(app)
        window = MainWindow()
        window.show()
        QTimer.singleShot(250, app.quit)
        return app.exec()

    from offloader.gui.app import main as gui_main

    return gui_main(sys.argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        if os.environ.get("OFFLOADER_GUI_SMOKE") != "1":
            raise
        # Windowed executables have no stderr. Save the failure for the smoke
        # runner instead of opening the bootloader's error dialog on CI.
        Path(os.environ["OFFLOADER_GUI_SMOKE_LOG"]).write_text(
            traceback.format_exc(), encoding="utf-8",
        )
        raise SystemExit(1) from None
