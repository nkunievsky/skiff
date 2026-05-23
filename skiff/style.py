"""Minimal, flat stylesheet."""

STYLESHEET = """
* {
    font-family: -apple-system, "SF Pro Text", "Helvetica Neue", Arial, sans-serif;
    font-size: 12px;
}

QMainWindow, QWidget {
    background: #f7f7f8;
    color: #1f1f23;
}

QToolBar {
    background: #ffffff;
    border: 0;
    border-bottom: 1px solid #e5e5e8;
    padding: 4px;
    spacing: 4px;
}

QToolBar::separator {
    background: #e5e5e8;
    width: 1px;
    margin: 6px 4px;
}

QLineEdit#pathEdit {
    background: #ffffff;
    border: 0;
    border-bottom: 1px solid #e5e5e8;
    padding: 6px 10px;
    color: #2f2f33;
    selection-background-color: #cfe2ff;
}

QLineEdit#pathEdit:focus {
    border-bottom: 1px solid #4f8cff;
}

QLabel#hostLabel {
    color: #6b6b72;
    padding: 0 8px;
    font-weight: 500;
}

QTreeWidget {
    background: #ffffff;
    alternate-background-color: #fafafb;
    border: 0;
    color: #1f1f23;
    selection-background-color: #cfe2ff;
    selection-color: #1f1f23;
    outline: 0;
}

QTreeWidget::item {
    padding: 4px 6px;
    border: 0;
}

QTreeWidget::item:hover {
    background: #f0f3f8;
}

QTreeWidget::item:selected {
    background: #cfe2ff;
    color: #1f1f23;
}

QHeaderView::section {
    background: #f3f3f5;
    border: 0;
    border-right: 1px solid #e5e5e8;
    border-bottom: 1px solid #e5e5e8;
    padding: 4px 8px;
    color: #6b6b72;
    font-weight: 500;
}

QSplitter::handle {
    background: #e5e5e8;
    width: 1px;
}

QTreeWidget#sidebar {
    background: #fafafb;
    border-right: 1px solid #e5e5e8;
    color: #2f2f33;
    show-decoration-selected: 1;
}

QTreeWidget#sidebar::item {
    padding: 3px 6px;
    border: 0;
}

QTreeWidget#sidebar::item:hover {
    background: #eef0f3;
}

QTreeWidget#sidebar::item:selected {
    background: #cfe2ff;
    color: #1f1f23;
}

QStatusBar {
    background: #ffffff;
    border-top: 1px solid #e5e5e8;
    color: #6b6b72;
}

QStatusBar QLabel {
    padding: 0 8px;
}

QProgressBar {
    background: #eef0f3;
    border: 0;
    border-radius: 3px;
    text-align: center;
    color: #1f1f23;
    height: 12px;
}

QProgressBar::chunk {
    background: #4f8cff;
    border-radius: 3px;
}

QFrame#previewFrame {
    background: #ffffff;
    border-top: 1px solid #e5e5e8;
}

QLabel#previewImage {
    background: #f3f3f5;
    color: #9c9ca6;
    border: 1px solid #e5e5e8;
    border-radius: 4px;
}

QLabel#previewTitle {
    color: #1f1f23;
    font-weight: 600;
}

QLabel#previewSubtitle {
    color: #8a8a92;
}

QPushButton {
    background: #ffffff;
    border: 1px solid #d5d5db;
    border-radius: 4px;
    padding: 5px 12px;
    color: #1f1f23;
}

QPushButton:hover {
    border-color: #b0b0bb;
    background: #fafafb;
}

QPushButton:default {
    background: #4f8cff;
    border-color: #4f8cff;
    color: #ffffff;
}

QPushButton:default:hover {
    background: #3e7bee;
    border-color: #3e7bee;
}

QDialog {
    background: #ffffff;
}

QSpinBox, QLineEdit {
    background: #ffffff;
    border: 1px solid #d5d5db;
    border-radius: 4px;
    padding: 5px 8px;
    selection-background-color: #cfe2ff;
}

QLineEdit:focus, QSpinBox:focus {
    border-color: #4f8cff;
}

QToolButton {
    background: transparent;
    border: 0;
    border-radius: 4px;
    padding: 4px 6px;
    color: #1f1f23;
}

QToolButton:hover {
    background: #eef0f3;
}

QToolButton:pressed {
    background: #e3e6ec;
}

QToolButton:disabled {
    color: #b8b8c0;
}
"""
