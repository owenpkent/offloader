"""Chrome for the application.

The look borrows from modern audio-plugin interfaces: near-black graphite
panels with a hairline edge, uppercase micro-labels, monospace readouts, and a
single cyan accent reserved for the things that move data. A set cart is dim
and the operator is looking at a monitor, so everything is dark by default and
the accent is the brightest thing on screen.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette

# ---------------------------------------------------------------- surfaces
BG = "#0b0d11"           # window ground
BG_PANEL = "#12151b"     # cards, rails
BG_RAISED = "#181c24"    # hovered cards, list rows
BG_INPUT = "#0e1116"     # inputs sit *below* the panel, like an inset well
BORDER = "#232a35"
BORDER_SOFT = "#1b2029"
EDGE_LIGHT = "#2a3340"   # the one-pixel top highlight on a raised panel

# ---------------------------------------------------------------- text
FG = "#e8ecf1"
FG_DIM = "#b4bcc8"
FG_MUTED = "#6f7887"
FG_FAINT = "#434b58"

# ---------------------------------------------------------------- signal
ACCENT = "#3fd8ff"
ACCENT_HOVER = "#7ae6ff"
ACCENT_DEEP = "#1a9fc4"
ACCENT_DIM = "#0f3a4a"   # accent at low opacity, for fills and selections
ACCENT_GLOW = "#163f50"
OK = "#39d98a"
WARN = "#f2b544"
BAD = "#ff5c6c"

FONT_UI = '"Inter", "Segoe UI", "SF Pro Text", "Noto Sans", "Helvetica Neue", sans-serif'
FONT_MONO = ('"JetBrains Mono", "Cascadia Code", "SF Mono", "Consolas", '
             '"DejaVu Sans Mono", monospace')

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
    """Install the palette, font and stylesheet on a QApplication."""
    app.setStyle("Fusion")

    font = QFont()
    for family in ("Inter", "Segoe UI", "SF Pro Text", "Noto Sans", "DejaVu Sans"):
        font.setFamily(family)
        if QFont(family).exactMatch():
            break
    font.setPointSize(10)
    app.setFont(font)

    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BG))
    palette.setColor(QPalette.WindowText, QColor(FG))
    palette.setColor(QPalette.Base, QColor(BG_INPUT))
    palette.setColor(QPalette.AlternateBase, QColor(BG_PANEL))
    palette.setColor(QPalette.Text, QColor(FG))
    palette.setColor(QPalette.Button, QColor(BG_PANEL))
    palette.setColor(QPalette.ButtonText, QColor(FG))
    palette.setColor(QPalette.Highlight, QColor(ACCENT_DIM))
    palette.setColor(QPalette.HighlightedText, QColor(FG))
    palette.setColor(QPalette.ToolTipBase, QColor(BG_RAISED))
    palette.setColor(QPalette.ToolTipText, QColor(FG))
    palette.setColor(QPalette.PlaceholderText, QColor(FG_MUTED))
    palette.setColor(QPalette.Link, QColor(ACCENT))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(FG_FAINT))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(FG_FAINT))
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(FG_FAINT))
    app.setPalette(palette)

    app.setStyleSheet(STYLESHEET)


STYLESHEET = f"""
QWidget {{
    background: transparent;
    color: {FG};
    font-family: {FONT_UI};
    font-size: 13px;
}}
QMainWindow, QDialog, QMessageBox, QFileDialog {{ background: {BG}; }}
QMenuBar {{
    background: {BG};
    color: {FG_MUTED};
    border-bottom: 1px solid {BORDER_SOFT};
    padding: 2px 6px;
}}
QMenuBar::item {{ padding: 5px 10px; border-radius: 4px; }}
QMenuBar::item:selected {{ background: {BG_RAISED}; color: {FG}; }}

/* ----------------------------------------------------------- typography */
QLabel {{ background: transparent; }}
QLabel[role="wordmark"] {{
    font-size: 15px; font-weight: 700; letter-spacing: 4px; color: {FG};
}}
QLabel[role="title"] {{ font-size: 20px; font-weight: 600; letter-spacing: -0.3px; }}
QLabel[role="heading"] {{ font-size: 14px; font-weight: 600; }}
QLabel[role="section"] {{
    font-size: 10px; font-weight: 700; letter-spacing: 2px; color: {FG_MUTED};
}}
QLabel[role="muted"] {{ color: {FG_MUTED}; }}
QLabel[role="dim"] {{ color: {FG_DIM}; }}
QLabel[role="fine"] {{ color: {FG_MUTED}; font-size: 11px; }}
QLabel[role="mono"] {{ font-family: {FONT_MONO}; }}
QLabel[role="readout"] {{
    font-family: {FONT_MONO}; font-size: 11px; color: {FG_DIM}; letter-spacing: 0.5px;
}}
QLabel[role="readout-accent"] {{
    font-family: {FONT_MONO}; font-size: 11px; color: {ACCENT}; letter-spacing: 0.5px;
}}
QLabel[role="version"] {{
    font-family: {FONT_MONO}; font-size: 10px; color: {FG_MUTED};
    border: 1px solid {BORDER}; border-radius: 3px; padding: 1px 6px;
}}
QLabel[role="badge"] {{
    font-size: 9px; font-weight: 800; letter-spacing: 1.5px; color: {FG_MUTED};
    border: 1px solid {BORDER}; border-radius: 3px; padding: 2px 6px 1px 7px;
}}
QLabel[role="badge"][accent="true"] {{
    color: {ACCENT}; border-color: {ACCENT_DEEP}; background: {ACCENT_DIM};
}}

/* -------------------------------------------------------------- panels */
QFrame[role="card"] {{
    background: {BG_PANEL};
    border: 1px solid {BORDER_SOFT};
    border-top-color: {EDGE_LIGHT};
    border-radius: 8px;
}}
QFrame[role="card"]:hover {{ border-color: {BORDER}; border-top-color: {EDGE_LIGHT}; }}
QFrame[role="rail"] {{
    background: {BG_PANEL};
    border: 1px solid {BORDER_SOFT};
    border-top-color: {EDGE_LIGHT};
    border-radius: 10px;
}}
QFrame[role="hairline"] {{ background: {BORDER_SOFT}; max-height: 1px; min-height: 1px; border: none; }}
QFrame[role="vhairline"] {{ background: {BORDER}; max-width: 1px; min-width: 1px; border: none; }}

/* -------------------------------------------------------------- buttons */
QPushButton {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-top-color: {EDGE_LIGHT};
    border-radius: 6px;
    padding: 6px 14px;
    color: {FG_DIM};
}}
QPushButton:hover {{ background: #1f2530; color: {FG}; border-color: #2f3947; }}
QPushButton:pressed {{ background: {BG_INPUT}; border-top-color: {BORDER}; }}
QPushButton:disabled {{ color: {FG_FAINT}; border-color: {BORDER_SOFT}; background: {BG_PANEL}; }}

QPushButton[accent="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {ACCENT_HOVER}, stop:0.08 {ACCENT}, stop:1 {ACCENT_DEEP});
    border: 1px solid {ACCENT_DEEP};
    color: #05222c;
    font-weight: 700;
    letter-spacing: 0.6px;
    padding: 9px 22px;
}}
QPushButton[accent="true"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 #a4eeff, stop:0.08 {ACCENT_HOVER}, stop:1 {ACCENT});
}}
QPushButton[accent="true"]:pressed {{ background: {ACCENT_DEEP}; }}
QPushButton[accent="true"]:disabled {{
    background: {BG_PANEL}; border-color: {BORDER_SOFT}; color: {FG_FAINT}; font-weight: 600;
}}

QPushButton[flat="true"] {{
    background: transparent; border: 1px solid transparent; padding: 4px 10px; color: {FG_MUTED};
}}
QPushButton[flat="true"]:hover {{ background: {BG_RAISED}; color: {FG}; border-color: {BORDER_SOFT}; }}
QPushButton[flat="true"]:disabled {{ color: {FG_FAINT}; background: transparent; border-color: transparent; }}

QPushButton[ghost="true"] {{
    background: transparent; border: 1px solid {BORDER}; padding: 3px 10px;
    color: {FG_MUTED}; font-size: 11px; font-weight: 600; letter-spacing: 0.8px;
}}
QPushButton[ghost="true"]:hover {{ color: {ACCENT}; border-color: {ACCENT_DEEP}; background: {ACCENT_DIM}; }}

/* segmented switch */
QPushButton[mode="true"] {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 5px;
    color: {FG_MUTED};
    padding: 4px 16px;
    font-size: 11px; font-weight: 700; letter-spacing: 1.5px;
}}
QPushButton[mode="true"]:hover {{ color: {FG_DIM}; }}
QPushButton[mode="true"]:checked {{
    color: {ACCENT};
    background: {BG_RAISED};
    border-color: {BORDER};
    border-top-color: {EDGE_LIGHT};
}}
QFrame[role="segment"] {{
    background: {BG_INPUT};
    border: 1px solid {BORDER_SOFT};
    border-radius: 7px;
}}

/* --------------------------------------------------------------- inputs */
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {{
    background: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 9px;
    selection-background-color: {ACCENT_DEEP};
    selection-color: #ffffff;
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border-color: #2f3947; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus {{
    border-color: {ACCENT_DEEP};
    background: #0c1319;
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {FG_FAINT}; border-color: {BORDER_SOFT};
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{
    width: 0; height: 0;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid {FG_MUTED};
    margin-right: 6px;
}}
QComboBox:hover::down-arrow {{ border-top-color: {ACCENT}; }}
QComboBox QAbstractItemView {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px;
    outline: none;
    selection-background-color: {ACCENT_DIM};
    selection-color: {ACCENT};
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none; background: transparent; }}
QSpinBox::up-arrow {{
    width: 0; height: 0; border-left: 3px solid transparent; border-right: 3px solid transparent;
    border-bottom: 4px solid {FG_MUTED};
}}
QSpinBox::down-arrow {{
    width: 0; height: 0; border-left: 3px solid transparent; border-right: 3px solid transparent;
    border-top: 4px solid {FG_MUTED};
}}
QSpinBox::up-arrow:hover {{ border-bottom-color: {ACCENT}; }}
QSpinBox::down-arrow:hover {{ border-top-color: {ACCENT}; }}

/* ---------------------------------------------------------------- lists */
QListWidget, QTableView, QTreeWidget, QScrollArea {{
    background: {BG_INPUT};
    border: 1px solid {BORDER_SOFT};
    border-radius: 8px;
    alternate-background-color: #10131a;
    outline: none;
}}
QScrollArea[role="bare"] {{ background: transparent; border: none; }}
QListWidget::item, QTreeWidget::item {{ padding: 5px 6px; border-radius: 5px; margin: 1px 3px; }}
QListWidget::item:hover, QTreeWidget::item:hover {{ background: {BG_RAISED}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: {ACCENT_DIM};
    color: {FG};
    border: 1px solid {ACCENT_GLOW};
}}
QTableView {{ gridline-color: transparent; selection-background-color: {ACCENT_DIM}; selection-color: {FG}; }}
QTableView::item {{ padding: 0 8px; border: none; }}
QTableView::item:selected {{ background: {ACCENT_DIM}; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
    background: {BG_PANEL};
    color: {FG_MUTED};
    border: none;
    border-bottom: 1px solid {BORDER_SOFT};
    padding: 7px 8px;
    font-size: 10px; font-weight: 700; letter-spacing: 1.5px;
}}

/* ------------------------------------------------------------- progress */
QProgressBar {{
    background: {BG_INPUT};
    border: 1px solid {BORDER_SOFT};
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: {FG};
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}

QTabBar::tab {{
    background: transparent;
    color: {FG_MUTED};
    padding: 8px 18px;
    border-bottom: 2px solid transparent;
    font-size: 11px; font-weight: 700; letter-spacing: 1.5px;
}}
QTabBar::tab:selected {{ color: {ACCENT}; border-bottom-color: {ACCENT}; }}
QTabWidget::pane {{ border: none; }}

/* ---------------------------------------------------------- check boxes */
QCheckBox, QRadioButton {{ background: transparent; spacing: 7px; color: {FG_DIM}; }}
QCheckBox:hover, QRadioButton:hover {{ color: {FG}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {BG_INPUT};
}}
QRadioButton::indicator {{ border-radius: 7px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT_DEEP}; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT_HOVER};
}}
QCheckBox::indicator:disabled {{ border-color: {BORDER_SOFT}; background: {BG_PANEL}; }}

/* ---------------------------------------------------------------- misc */
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 10px; }}
QSplitter::handle:vertical {{ height: 10px; }}
QSplitter::handle:hover {{ background: {BORDER_SOFT}; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3a4556; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 4px; min-width: 30px; }}

QToolTip {{
    background: {BG_RAISED};
    color: {FG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 8px;
}}
QStatusBar {{
    background: {BG};
    color: {FG_MUTED};
    border-top: 1px solid {BORDER_SOFT};
    font-family: {FONT_MONO}; font-size: 11px;
}}
QStatusBar::item {{ border: none; }}
QMenu {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px;
}}
QMenu::item {{ padding: 6px 28px 6px 12px; border-radius: 4px; color: {FG_DIM}; }}
QMenu::item:selected {{ background: {ACCENT_DIM}; color: {ACCENT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 6px; }}
QMenu::indicator {{ width: 12px; height: 12px; margin-left: 6px; }}
QMenu::indicator:checked {{ background: {ACCENT}; border-radius: 3px; }}
QMenu::indicator:unchecked {{ border: 1px solid {BORDER}; border-radius: 3px; }}

QDialogButtonBox QPushButton {{ min-width: 84px; }}
QMessageBox QLabel {{ color: {FG_DIM}; }}
"""
