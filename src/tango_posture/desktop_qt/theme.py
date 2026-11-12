"""Design tokens and stylesheet for the TangoPosture Qt desktop application."""

from __future__ import annotations

COLOR_BASE = "#15191F"
COLOR_PANEL = "#272E38"
COLOR_ELEVATED = "#323B47"
COLOR_BORDER = "#3D4755"
COLOR_BURGUNDY = "#772D43"
COLOR_BURGUNDY_HOVER = "#8F3651"
COLOR_COPPER = "#C99572"
COLOR_COPPER_HOVER = "#D6A685"
COLOR_TEXT_PRIMARY = "#F2EFEA"
COLOR_TEXT_MUTED = "#9BA3AF"
COLOR_DANGER = "#D9534F"
COLOR_SUCCESS = "#4CAE4C"
COLOR_WARNING = "#F0AD4E"

STYLESHEET = f"""
QMainWindow, QDialog {{
    background-color: {COLOR_BASE};
    color: {COLOR_TEXT_PRIMARY};
}}

QWidget {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 13px;
    color: {COLOR_TEXT_PRIMARY};
}}

/* Surface panels & cards */
QFrame.card, QFrame#navRail, QFrame#statusStrip {{
    background-color: {COLOR_PANEL};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
}}

QFrame#navRail {{
    border-radius: 0px;
    border-top: none;
    border-bottom: none;
    border-left: none;
    background-color: {COLOR_PANEL};
}}

QFrame#statusStrip {{
    border-radius: 0px;
    border-bottom: none;
    border-left: none;
    border-right: none;
    background-color: {COLOR_PANEL};
}}

QLabel {{
    background: transparent;
}}

QLabel.title {{
    font-size: 18px;
    font-weight: bold;
    color: {COLOR_TEXT_PRIMARY};
}}

QLabel.subtitle {{
    font-size: 12px;
    color: {COLOR_TEXT_MUTED};
}}

QLabel.badge {{
    background-color: {COLOR_ELEVATED};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 11px;
}}

/* Push Buttons */
QPushButton {{
    background-color: {COLOR_ELEVATED};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    padding: 6px 14px;
    color: {COLOR_TEXT_PRIMARY};
    font-weight: 500;
}}

QPushButton:hover {{
    background-color: {COLOR_BORDER};
    border-color: {COLOR_COPPER};
}}

QPushButton:pressed {{
    background-color: {COLOR_BASE};
}}

QPushButton:focus {{
    border: 2px solid {COLOR_TEXT_PRIMARY};
}}

QPushButton:disabled {{
    background-color: {COLOR_PANEL};
    border-color: {COLOR_BORDER};
    color: #636B78;
}}

QPushButton.primary {{
    background-color: {COLOR_BURGUNDY};
    border-color: {COLOR_BURGUNDY};
    color: #FFFFFF;
    font-weight: 600;
}}

QPushButton.primary:hover {{
    background-color: {COLOR_BURGUNDY_HOVER};
    border-color: {COLOR_BURGUNDY_HOVER};
}}

QPushButton.accent {{
    background-color: {COLOR_COPPER};
    border-color: {COLOR_COPPER};
    color: #1A1F26;
    font-weight: 600;
}}

QPushButton.accent:hover {{
    background-color: {COLOR_COPPER_HOVER};
    border-color: {COLOR_COPPER_HOVER};
}}

QPushButton.danger {{
    background-color: {COLOR_DANGER};
    border-color: {COLOR_DANGER};
    color: #FFFFFF;
}}

/* Navigation Rail Buttons */
QPushButton.nav-btn {{
    text-align: left;
    padding: 10px 12px;
    border: none;
    border-radius: 4px;
    background-color: transparent;
    font-size: 13px;
    font-weight: 500;
    color: {COLOR_TEXT_MUTED};
}}

QPushButton.nav-btn:hover {{
    background-color: {COLOR_ELEVATED};
    color: {COLOR_TEXT_PRIMARY};
}}

QPushButton.nav-btn:checked {{
    background-color: {COLOR_BURGUNDY};
    border-left: 3px solid {COLOR_COPPER};
    padding-left: 11px;
    color: #FFFFFF;
    font-weight: 600;
}}

/* Input Fields */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {COLOR_BASE};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    padding: 6px 10px;
    color: {COLOR_TEXT_PRIMARY};
    selection-background-color: {COLOR_BURGUNDY};
}}

QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 1px solid {COLOR_COPPER};
}}

QComboBox {{
    background-color: {COLOR_BASE};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    padding: 5px 10px;
    color: {COLOR_TEXT_PRIMARY};
}}

QComboBox:focus {{
    border: 1px solid {COLOR_COPPER};
}}

QComboBox QAbstractItemView {{
    background-color: {COLOR_PANEL};
    border: 1px solid {COLOR_BORDER};
    selection-background-color: {COLOR_BURGUNDY};
    selection-color: #FFFFFF;
    color: {COLOR_TEXT_PRIMARY};
}}

/* Checkboxes */
QCheckBox {{
    spacing: 8px;
    color: {COLOR_TEXT_PRIMARY};
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border-radius: 3px;
    border: 1px solid {COLOR_BORDER};
    background-color: {COLOR_BASE};
}}

QCheckBox::indicator:checked {{
    background-color: {COLOR_BURGUNDY};
    border-color: {COLOR_BURGUNDY};
}}

QCheckBox::indicator:focus {{
    border: 2px solid {COLOR_TEXT_PRIMARY};
}}

/* ScrollBars */
QScrollBar:vertical {{
    background-color: {COLOR_BASE};
    width: 10px;
    margin: 0px;
}}

QScrollBar::handle:vertical {{
    background-color: {COLOR_ELEVATED};
    border-radius: 4px;
    min-height: 24px;
    margin: 2px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {COLOR_COPPER};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar:horizontal {{
    background-color: {COLOR_BASE};
    height: 10px;
    margin: 0px;
}}

QScrollBar::handle:horizontal {{
    background-color: {COLOR_ELEVATED};
    border-radius: 4px;
    min-width: 24px;
    margin: 2px;
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

/* Tables and Trees */
QTableWidget, QTreeWidget, QListView {{
    background-color: {COLOR_BASE};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    gridline-color: {COLOR_BORDER};
    color: {COLOR_TEXT_PRIMARY};
    selection-background-color: {COLOR_BURGUNDY};
    selection-color: #FFFFFF;
}}

QHeaderView::section {{
    background-color: {COLOR_PANEL};
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
    border-right: 1px solid {COLOR_BORDER};
    padding: 6px;
    font-weight: 600;
    color: {COLOR_TEXT_MUTED};
}}

/* Progress Bar */
QProgressBar {{
    background-color: {COLOR_BASE};
    border: 1px solid {COLOR_BORDER};
    border-radius: 4px;
    text-align: center;
    color: {COLOR_TEXT_PRIMARY};
}}

QProgressBar::chunk {{
    background-color: {COLOR_BURGUNDY};
    border-radius: 3px;
}}
"""
