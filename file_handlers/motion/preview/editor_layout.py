from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ui.styles import get_color_scheme


class MotionEditorPane(QFrame):
    """Small dock-like pane shared by motion preview surfaces."""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("motionEditorPane")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame(self)
        header.setObjectName("motionPaneHeader")
        self.header_layout = QHBoxLayout(header)
        self.header_layout.setContentsMargins(10, 7, 8, 7)
        self.header_layout.setSpacing(6)
        self.title_label = QLabel(title.upper(), header)
        self.title_label.setObjectName("motionPaneTitle")
        self.header_layout.addWidget(self.title_label)
        self.header_layout.addStretch(1)
        root.addWidget(header)

        self.body = QWidget(self)
        self.body.setObjectName("motionPaneBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(8, 8, 8, 8)
        self.body_layout.setSpacing(7)
        root.addWidget(self.body, 1)

    def add_widget(self, widget: QWidget, stretch: int = 0) -> None:
        self.body_layout.addWidget(widget, stretch)


class MotionEditorWorkspace(QWidget):
    """Reusable three-pane shell with a compact editor toolbar."""

    def __init__(
        self,
        title: str,
        *,
        settings: dict | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setObjectName("motionEditorWorkspace")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.top_bar = QFrame(self)
        self.top_bar.setObjectName("motionEditorTopBar")
        self.toolbar = QHBoxLayout(self.top_bar)
        self.toolbar.setContentsMargins(12, 7, 10, 7)
        self.toolbar.setSpacing(8)
        self.title_label = QLabel(title, self.top_bar)
        self.title_label.setObjectName("motionWorkspaceTitle")
        self.toolbar.addWidget(self.title_label)
        self.toolbar.addStretch(1)
        root.addWidget(self.top_bar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("motionWorkspaceSplitter")
        self.splitter.setChildrenCollapsible(False)
        root.addWidget(self.splitter, 1)
        self.setStyleSheet(_editor_stylesheet(settings or {}))

    def add_pane(self, pane: MotionEditorPane, stretch: int) -> None:
        self.splitter.addWidget(pane)
        self.splitter.setStretchFactor(self.splitter.count() - 1, stretch)


def _editor_stylesheet(settings: dict) -> str:
    colors = get_color_scheme(settings.get("tree_highlight_color"))
    background = QColor(colors["bg"])
    panel = background.lighter(112).name()
    header = background.lighter(120).name()
    viewport = background.darker(118).name()
    border = colors["border"]
    input_bg = colors["input_bg"]
    highlight = colors["highlight"]
    return f"""
        QWidget#motionEditorWorkspace {{ background: {background.name()}; }}
        QFrame#motionEditorTopBar {{
            background: {header}; border-bottom: 1px solid {border};
        }}
        QWidget#motionEditorWorkspace QLabel {{
            background: transparent; border: none;
        }}
        QWidget#motionEditorWorkspace QCheckBox {{
            background: transparent; border: none;
        }}
        QLabel#motionWorkspaceTitle {{
            font-size: 14px; font-weight: 600; padding-right: 12px;
        }}
        QFrame#motionEditorPane {{
            background: {panel}; border: none;
        }}
        QFrame#motionPaneHeader {{
            background: {header}; border-bottom: 1px solid {border};
        }}
        QLabel#motionPaneTitle {{
            color: rgba(255,255,255,180); font-size: 10px;
            font-weight: 700; letter-spacing: 1px;
        }}
        QLabel#motionInspectorLabel {{
            color: rgba(255,255,255,145); font-size: 9px;
            font-weight: 700; margin-top: 6px;
        }}
        QLabel#motionCountLabel {{
            color: rgba(255,255,255,170); background: {input_bg};
            border-radius: 8px; padding: 2px 7px;
        }}
        QLabel#motionNotice {{
            background: rgba(219,154,57,35); color: rgba(255,235,205,230);
            border: 1px solid rgba(219,154,57,90); border-radius: 3px;
            padding: 6px 8px;
        }}
        QLabel#motionStatusBar {{
            color: rgba(255,255,255,145); border-top: 1px solid {border};
            padding: 5px 2px 1px 2px;
        }}
        QWidget#motionPaneBody {{ background: {panel}; }}
        QFrame#motionEditorPane[role="viewport"] QWidget#motionPaneBody {{
            background: {viewport};
        }}
        QSplitter#motionWorkspaceSplitter::handle {{
            background: {border}; width: 1px;
        }}
        QWidget#motionEditorWorkspace QLineEdit,
        QWidget#motionEditorWorkspace QComboBox,
        QWidget#motionEditorWorkspace QDoubleSpinBox {{
            background: {input_bg}; border: 1px solid {border};
            border-radius: 3px; padding: 4px 6px; min-height: 20px;
        }}
        QWidget#motionEditorWorkspace QListWidget {{
            background: {background.name()}; border: 1px solid {border};
            border-radius: 3px; outline: none;
        }}
        QWidget#motionEditorWorkspace QListWidget::item {{
            border-bottom: 1px solid rgba(255,255,255,18);
        }}
        QWidget#motionEditorWorkspace QListWidget::item:hover {{
            background: rgba(255,255,255,14);
        }}
        QWidget#motionEditorWorkspace QListWidget::item:selected {{
            background: {highlight};
        }}
        QWidget#motionEditorWorkspace QPushButton,
        QWidget#motionEditorWorkspace QToolButton {{
            background: {input_bg}; border: 1px solid {border};
            border-radius: 3px; padding: 4px 8px; min-height: 22px;
        }}
        QWidget#motionEditorWorkspace QPushButton:hover,
        QWidget#motionEditorWorkspace QToolButton:hover {{
            background: {header};
        }}
        QWidget#motionEditorWorkspace QPushButton:checked,
        QWidget#motionEditorWorkspace QToolButton:checked {{
            background: {highlight};
        }}
        QWidget#motionPlaybackControls {{
            border-top: 1px solid {border}; padding-top: 7px;
        }}
        QWidget#motionEditorWorkspace QSlider::groove:horizontal {{
            height: 4px; background: {input_bg}; border-radius: 2px;
        }}
        QWidget#motionEditorWorkspace QSlider::sub-page:horizontal {{
            background: {highlight}; border-radius: 2px;
        }}
        QWidget#motionEditorWorkspace QSlider::handle:horizontal {{
            width: 12px; margin: -5px 0; background: white;
            border: 1px solid {border}; border-radius: 6px;
        }}
    """
