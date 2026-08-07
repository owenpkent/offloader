"""Dark chrome for the application.

A set cart is usually dim and the operator is usually looking at a monitor, so
the interface is dark by default with one accent colour reserved for actions
that move data.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

BG = "#16181c"
BG_RAISED = "#1c1f25"
BG_INPUT = "#22262d"
BORDER = "#2f343c"
FG = "#e6e8ea"
FG_MUTED = "#9aa0a6"
ACCENT = "#5577b0"
ACCENT_HOVER = "#6688c4"
OK = "#4caf7d"
WARN = "#d8a13c"
BAD = "#e0645c"

STATUS_COLORS = {
    "queued": FG_MUTED,
    "running": ACCENT,
    "paused": WARN,
    "verified": OK,
    "copied": OK,
    "done": OK,
    "failed": BAD,
    "cancelled": FG_MUTED,
}


def status_color(state: str) -> str:
    return STATUS_COLORS.get(state.lower(), FG_MUTED)


def apply(app) -> None:
    """Install the palette and stylesheet on a QApplication."""
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BG))
    palette.setColor(QPalette.WindowText, QColor(FG))
    palette.setColor(QPalette.Base, QColor(BG_INPUT))
    palette.setColor(QPalette.AlternateBase, QColor(BG_RAISED))
    palette.setColor(QPalette.Text, QColor(FG))
    palette.setColor(QPalette.Button, QColor(BG_RAISED))
    palette.setColor(QPalette.ButtonText, QColor(FG))
    palette.setColor(QPalette.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ToolTipBase, QColor(BG_RAISED))
    palette.setColor(QPalette.ToolTipText, QColor(FG))
    palette.setColor(QPalette.PlaceholderText, QColor(FG_MUTED))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(FG_MUTED))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(FG_MUTED))
    app.setPalette(palette)

    app.setStyleSheet(STYLESHEET)


STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {FG};
    font-size: 13px;
}}
/* Labels must not paint their own ground, or they punch opaque rectangles
   through the raised cards and drop zones they sit on. */
QLabel, QCheckBox, QRadioButton {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 20px; font-weight: 600; }}
QLabel[role="heading"] {{ font-size: 14px; font-weight: 600; }}
QLabel[role="muted"] {{ color: {FG_MUTED}; }}
QLabel[role="mono"] {{ font-family: "Consolas", "DejaVu Sans Mono", monospace; }}

QFrame[role="card"] {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QFrame[role="dropzone"] {{
    background: {BG_RAISED};
    border: 2px dashed {BORDER};
    border-radius: 10px;
}}
QFrame[role="dropzone"][active="true"] {{
    border-color: {ACCENT};
    background: #1f2531;
}}

QPushButton {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{ background: #2a2f38; }}
QPushButton:pressed {{ background: #191d23; }}
QPushButton:disabled {{ color: {FG_MUTED}; border-color: #262a31; }}
QPushButton[accent="true"] {{
    background: {ACCENT};
    border-color: {ACCENT};
    color: #ffffff;
    font-weight: 600;
    padding: 9px 20px;
}}
QPushButton[accent="true"]:hover {{ background: {ACCENT_HOVER}; }}
QPushButton[accent="true"]:disabled {{ background: #3a4557; border-color: #3a4557; color: #8d95a3; }}
QPushButton[flat="true"] {{ background: transparent; border: none; padding: 4px 8px; }}
QPushButton[flat="true"]:hover {{ background: {BG_INPUT}; }}
QPushButton[mode="true"] {{
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    color: {FG_MUTED};
    padding: 6px 16px;
}}
QPushButton[mode="true"]:hover {{ color: {FG}; }}
QPushButton[mode="true"]:checked {{
    color: {FG};
    border-bottom-color: {ACCENT};
    font-weight: 600;
}}

QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
}}

QListWidget, QTableView, QTreeWidget {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    alternate-background-color: {BG_RAISED};
}}
QListWidget::item, QTreeWidget::item {{ padding: 5px 6px; border-radius: 4px; }}
QListWidget::item:selected, QTreeWidget::item:selected {{ background: {ACCENT}; }}
QHeaderView::section {{
    background: {BG_RAISED};
    color: {FG_MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    font-weight: 600;
}}
QTableView {{ gridline-color: {BORDER}; }}

QProgressBar {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 5px;
    height: 10px;
    text-align: center;
    color: {FG};
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}

QTabBar::tab {{
    background: transparent;
    color: {FG_MUTED};
    padding: 8px 18px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {FG}; border-bottom-color: {ACCENT}; }}
QTabWidget::pane {{ border: none; }}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {BG_INPUT};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QSplitter::handle {{ background: {BORDER}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #3a4048; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #4a515b; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #3a4048; border-radius: 5px; min-width: 30px; }}

QToolTip {{
    background: {BG_RAISED};
    color: {FG};
    border: 1px solid {BORDER};
    padding: 5px;
}}
QStatusBar {{ color: {FG_MUTED}; border-top: 1px solid {BORDER}; }}
QMenu {{ background: {BG_RAISED}; border: 1px solid {BORDER}; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
"""
