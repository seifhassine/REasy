from __future__ import annotations

lazy from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

lazy from ui.styles import current_colors


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
        self.header_layout.setContentsMargins(8, 4, 4, 4)
        self.header_layout.setSpacing(4)
        self.title_label = QLabel(title.upper(), header)
        self.title_label.setObjectName("motionPaneTitle")
        self.header_layout.addWidget(self.title_label)
        self.header_layout.addStretch(1)
        root.addWidget(header)

        self.body = QWidget(self)
        self.body.setObjectName("motionPaneBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(6, 6, 6, 6)
        self.body_layout.setSpacing(4)
        root.addWidget(self.body, 1)

    def add_widget(self, widget: QWidget, stretch: int = 0) -> None:
        self.body_layout.addWidget(widget, stretch)


class MotionEditorWorkspace(QWidget):
    """Reusable three-pane shell with a compact editor toolbar."""

    def __init__(self, title: str, *, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("motionEditorWorkspace")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.top_bar = QFrame(self)
        self.top_bar.setObjectName("motionEditorTopBar")
        self.toolbar = QHBoxLayout(self.top_bar)
        self.toolbar.setContentsMargins(8, 4, 8, 4)
        self.toolbar.setSpacing(6)
        self.title_label = QLabel(title, self.top_bar)
        self.title_label.setObjectName("motionWorkspaceTitle")
        self.toolbar.addWidget(self.title_label)
        self.toolbar.addStretch(1)
        root.addWidget(self.top_bar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("motionWorkspaceSplitter")
        self.splitter.setChildrenCollapsible(False)
        root.addWidget(self.splitter, 1)
        self.setStyleSheet(_editor_stylesheet())

    def add_pane(self, pane: MotionEditorPane, stretch: int) -> None:
        self.splitter.addWidget(pane)
        self.splitter.setStretchFactor(self.splitter.count() - 1, stretch)


def _editor_stylesheet() -> str:
    c = current_colors()
    return f"""
        QWidget#motionEditorWorkspace {{ background: {c['gutter']}; }}
        QFrame#motionEditorTopBar {{
            background: {c['header_bg']}; border-bottom: 1px solid {c['gutter']};
        }}
        QWidget#motionEditorWorkspace QLabel {{
            background: transparent; border: none;
        }}
        QWidget#motionEditorWorkspace QCheckBox {{
            background: transparent; border: none;
        }}
        QWidget#motionEditorWorkspace QLabel#motionWorkspaceTitle {{
            color: {c['text_bright']}; font-weight: 600; padding-right: 8px;
        }}
        QFrame#motionEditorPane {{
            background: {c['panel']}; border: none;
        }}
        QFrame#motionPaneHeader {{
            background: {c['header_bg']}; border-bottom: 1px solid {c['border_subtle']};
        }}
        QWidget#motionEditorWorkspace QLabel#motionPaneTitle {{
            color: {c['text_muted']}; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;
        }}
        QWidget#motionEditorWorkspace QLabel#motionInspectorLabel {{
            color: {c['text_subtle']}; font-size: 11px; font-weight: 700; letter-spacing: 0.6px;
            margin-top: 6px;
        }}
        QWidget#motionEditorWorkspace QLabel#motionCountLabel {{
            color: {c['text_muted']}; background: {c['surface_alt']};
            border: 1px solid {c['border_subtle']}; border-radius: 3px; padding: 0px 5px;
        }}
        QWidget#motionEditorWorkspace QLabel#motionNotice {{
            background: {c['warning_soft']}; color: {c['text']};
            border: none; border-left: 3px solid {c['warning']}; border-radius: 2px;
            padding: 4px 8px;
        }}
        QWidget#motionEditorWorkspace QLabel#motionStatusBar {{
            color: {c['text_muted']}; border-top: 1px solid {c['border_subtle']};
            padding: 4px 4px 2px 4px;
        }}
        QWidget#motionPaneBody {{ background: {c['panel']}; }}
        QFrame#motionEditorPane[role="viewport"] QWidget#motionPaneBody {{
            background: {c['view_bg']};
        }}
        QWidget#motionEditorWorkspace QListWidget::item {{
            border-bottom: 1px solid {c['border_subtle']};
        }}
        QWidget#motionEditorWorkspace QPushButton {{ padding: 3px 8px; }}
        QWidget#motionPlaybackControls {{
            border-top: 1px solid {c['border_subtle']}; padding-top: 4px;
        }}
    """
