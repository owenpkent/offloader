"""Application entry point."""

from __future__ import annotations

import sys

from .. import PRODUCT_NAME, __version__


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

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(PRODUCT_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(PRODUCT_NAME)
    theme.apply(app)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
