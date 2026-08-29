"""Durable colour and control vocabulary for the desktop UI."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

INK = "#08111D"
INK_HOVER = "#10263A"
CANVAS = "#0E1622"
PAPER = "#151F2D"
BORDER = "#2B3949"
TEXT = "#E8EEF5"
MUTED = "#9BAABD"
TEAL = "#49CFC0"
TEAL_FILL = "#0E7E77"
TEAL_HOVER = "#14827A"
TEAL_SOFT = "#123A39"
WARNING = "#F1BB68"
WARNING_SOFT = "#352A18"
DANGER = "#FF818B"
DANGER_SOFT = "#331D25"
SUCCESS = "#75D5A6"


STYLESHEET = f"""
QWidget {{
    color: {TEXT};
    font-family: "Segoe UI Variable", "Segoe UI";
    font-size: 14px;
}}
QMainWindow, QDialog, QWidget#Canvas, QStackedWidget {{
    background: {CANVAS};
}}
QWidget#Sidebar {{
    background: {INK};
}}
QLabel#Brand {{
    color: #FFFFFF;
    font-size: 21px;
    font-weight: 700;
}}
QLabel#BrandDetail {{
    color: #A8B7C8;
    font-size: 12px;
}}
QLabel#PageTitle {{
    color: {TEXT};
    font-size: 28px;
    font-weight: 700;
}}
QLabel#PageSubtitle {{
    color: {MUTED};
    font-size: 14px;
}}
QLabel#SectionTitle {{
    color: {TEXT};
    font-size: 17px;
    font-weight: 650;
}}
QLabel#RowTitle {{
    color: {TEXT};
    font-size: 14px;
    font-weight: 600;
}}
QLabel#Muted {{
    color: {MUTED};
    font-size: 12px;
}}
QLabel#ErrorText {{
    color: {DANGER};
    font-size: 12px;
}}
QLabel#SuccessText {{
    color: {SUCCESS};
    font-size: 12px;
}}
QFrame#Panel, QFrame#ListRow, QFrame#PromptCard {{
    background: {PAPER};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#PromptCard {{
    border-radius: 13px;
}}
QFrame#ListRow:hover {{
    background: #182535;
    border-color: #405267;
}}
QFrame#IntakeStrip {{
    background: {TEAL_SOFT};
    border: 1px solid #285C58;
    border-radius: 8px;
}}
QFrame#WarningStrip {{
    background: {WARNING_SOFT};
    border: 1px solid #6D5430;
    border-radius: 8px;
}}
QPushButton {{
    min-height: 36px;
    padding: 0 15px;
    border: 1px solid {BORDER};
    border-radius: 7px;
    background: {PAPER};
    color: {TEXT};
    font-weight: 600;
}}
QPushButton:hover {{
    background: #1B2939;
    border-color: #46586C;
}}
QPushButton:pressed {{
    background: #111A27;
}}
QPushButton:focus {{
    border: 2px solid {TEAL};
}}
QPushButton:disabled {{
    color: #667586;
    background: #111A25;
    border-color: #1F2B39;
}}
QPushButton[variant="primary"] {{
    color: #FFFFFF;
    background: {TEAL_FILL};
    border-color: {TEAL_FILL};
}}
QPushButton[variant="primary"]:hover {{
    background: {TEAL_HOVER};
    border-color: {TEAL_HOVER};
}}
QPushButton[variant="quiet"] {{
    background: transparent;
    border-color: transparent;
    color: {TEAL};
}}
QPushButton[variant="quiet"]:hover {{
    background: {TEAL_SOFT};
}}
QPushButton[variant="danger"] {{
    background: transparent;
    border-color: #6B3941;
    color: {DANGER};
}}
QPushButton[variant="danger"]:hover {{
    background: {DANGER_SOFT};
}}
QPushButton[nav="true"] {{
    min-height: 43px;
    padding: 0 15px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: #C2CFDC;
    text-align: left;
    font-size: 14px;
    font-weight: 550;
}}
QPushButton[nav="true"]:hover {{
    background: {INK_HOVER};
    color: #FFFFFF;
}}
QPushButton[nav="true"]:checked {{
    background: #17344B;
    color: #FFFFFF;
}}
QPushButton[chip="true"] {{
    min-height: 38px;
    padding: 0 13px;
    border-radius: 7px;
    background: #182433;
    border: 1px solid {BORDER};
}}
QPushButton[chip="true"]:checked {{
    color: #FFFFFF;
    background: {TEAL_FILL};
    border-color: {TEAL_FILL};
}}
QLineEdit, QTextEdit, QComboBox, QDateEdit, QSpinBox {{
    min-height: 38px;
    padding: 0 10px;
    background: #101A27;
    border: 1px solid #334456;
    border-radius: 7px;
    selection-background-color: {TEAL_FILL};
    selection-color: #FFFFFF;
}}
QTextEdit {{
    padding: 9px 10px;
}}
QLineEdit:hover, QTextEdit:hover, QComboBox:hover, QDateEdit:hover, QSpinBox:hover {{
    border-color: #53677C;
}}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus {{
    border: 2px solid {TEAL};
}}
QLineEdit:disabled, QTextEdit:disabled, QComboBox:disabled,
QDateEdit:disabled, QSpinBox:disabled {{
    color: #667586;
    background: #111A25;
    border-color: #1F2B39;
}}
QComboBox::drop-down, QDateEdit::drop-down {{
    border: 0;
    width: 28px;
}}
QComboBox QAbstractItemView {{
    background: {PAPER};
    border: 1px solid {BORDER};
    selection-background-color: {TEAL_SOFT};
    selection-color: {TEXT};
    outline: 0;
}}
QCheckBox {{
    spacing: 8px;
}}
QCheckBox::indicator:unchecked {{
    background: #101A27;
    border: 1px solid #53677C;
    border-radius: 2px;
}}
QCheckBox::indicator:unchecked:hover {{
    border-color: {TEAL};
}}
QCheckBox:disabled {{
    color: #667586;
}}
QCalendarWidget QWidget {{
    alternate-background-color: #101A27;
}}
QCalendarWidget QAbstractItemView:enabled {{
    color: {TEXT};
    background: {PAPER};
    selection-color: #FFFFFF;
    selection-background-color: {TEAL_FILL};
}}
QCalendarWidget QToolButton {{
    color: {TEXT};
    background: transparent;
    border-color: transparent;
}}
QScrollArea {{
    border: 0;
    background: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    width: 10px;
    margin: 2px;
    background: transparent;
}}
QScrollBar::handle:vertical {{
    min-height: 30px;
    border-radius: 4px;
    background: #3A4B5D;
}}
QScrollBar::handle:vertical:hover {{
    background: #52667A;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    height: 0;
    background: transparent;
}}
QMenu {{
    background: {PAPER};
    border: 1px solid {BORDER};
    padding: 6px;
}}
QMenu::item {{
    min-height: 30px;
    padding: 2px 28px 2px 10px;
    border-radius: 5px;
}}
QMenu::item:selected {{
    background: {TEAL_SOFT};
    color: {TEXT};
}}
QMenu::separator {{
    height: 1px;
    margin: 5px 8px;
    background: {BORDER};
}}
QToolTip {{
    color: {TEXT};
    background: #1B2939;
    border: 1px solid #405267;
    padding: 5px 7px;
}}
"""


def apply_theme(application: QApplication) -> None:
    """Install the global product theme."""

    application.setStyle("Fusion")
    application.styleHints().setColorScheme(Qt.ColorScheme.Dark)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(CANVAS))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(PAPER))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#101A27"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#1B2939"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(PAPER))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Light, QColor("#52667A"))
    palette.setColor(QPalette.ColorRole.Midlight, QColor("#405267"))
    palette.setColor(QPalette.ColorRole.Mid, QColor("#344659"))
    palette.setColor(QPalette.ColorRole.Dark, QColor("#263548"))
    palette.setColor(QPalette.ColorRole.Shadow, QColor(INK))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(DANGER))
    palette.setColor(QPalette.ColorRole.Link, QColor(TEAL))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(TEAL_FILL))
    palette.setColor(QPalette.ColorRole.Accent, QColor(TEAL_FILL))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#718195"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#667586"))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#667586"))
    application.setPalette(palette)
    application.setStyleSheet(STYLESHEET)
