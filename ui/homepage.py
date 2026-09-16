from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

class HomePageWidget(QWidget):
    def __init__(self, on_open_file, on_new_project, on_open_project, on_reopen_last, parent=None):
        super().__init__(parent)
        self.setObjectName("homePage")

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.addStretch(1)

        hero = QWidget(self)
        hero.setObjectName("homeHero")
        hero.setMaximumWidth(620)
        layout = QVBoxLayout(hero)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel(self.tr("REasy Editor"), hero)
        title.setObjectName("homeTitle")
        title.setAlignment(Qt.AlignLeft)
        layout.addWidget(title)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 12, 0, 4)
        actions.setSpacing(10)
        actions.addStretch()
        self.open_button = self._button(self.tr("Open File"), "primaryButton", on_open_file)
        self.new_project_button = self._button(
            self.tr("New Project"), "secondaryButton", on_new_project
        )
        self.library_button = self._button(
            self.tr("Project Library"), "secondaryButton", on_open_project
        )
        actions.addWidget(self.open_button)
        actions.addWidget(self.new_project_button)
        actions.addWidget(self.library_button)
        actions.addStretch()
        layout.addLayout(actions)

        divider = QFrame(hero)
        divider.setObjectName("homeDivider")
        divider.setFrameShape(QFrame.HLine)
        layout.addWidget(divider)

        recent_title = QLabel(self.tr("RECENTLY CLOSED"), hero)
        recent_title.setObjectName("recentTitle")
        recent_title.setAlignment(Qt.AlignLeft)
        layout.addWidget(recent_title)

        self.recent_label = QLabel(self.tr("No recently closed files yet."), hero)
        self.recent_label.setObjectName("recentLabel")
        self.recent_label.setAlignment(Qt.AlignLeft)
        self.recent_label.setWordWrap(True)
        self.recent_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.recent_label)

        self.reopen_button = self._button(
            self.tr("Reopen last closed  →"), "linkButton", on_reopen_last
        )
        layout.addWidget(self.reopen_button, alignment=Qt.AlignLeft)

        root.addWidget(hero, alignment=Qt.AlignHCenter)
        root.addStretch(2)

    def _button(self, text, object_name, callback):
        button = QPushButton(text, self)
        button.setObjectName(object_name)
        button.setCursor(Qt.PointingHandCursor)
        button.setMinimumHeight(32)
        button.clicked.connect(callback)
        return button

    def set_theme(self, colors: dict, accent_color: str):
        accent = QColor(accent_color)
        if not accent.isValid():
            accent = QColor("#00aaff")
        accent_text = "#111111" if accent.lightness() > 155 else "#ffffff"
        accent_hover = accent.lighter(112).name()
        accent_pressed = accent.darker(116).name()
        muted = colors["text_muted"]
        subtle = colors["text_subtle"]
        surface = colors["surface_alt"]
        hover = colors["surface_hover"]
        border = colors["border"]

        self.setStyleSheet(f"""
            QWidget#homePage {{ background: {colors['bg']}; }}
            QWidget#homeHero {{ background: transparent; }}
            QLabel#homeTitle {{ color: {colors['fg']}; font-size: 20px; font-weight: 600; }}
            QPushButton#primaryButton, QPushButton#secondaryButton {{ border-radius: 4px;
                padding: 5px 14px; font-size: 13px; }}
            QPushButton#primaryButton {{ color: {accent_text}; background: {accent.name()};
                border: 1px solid {accent.name()}; }}
            QPushButton#primaryButton:hover {{ background: {accent_hover}; }}
            QPushButton#primaryButton:pressed {{ background: {accent_pressed}; }}
            QPushButton#secondaryButton {{ color: {colors['fg']}; background: {surface};
                border: 1px solid {border}; }}
            QPushButton#secondaryButton:hover {{ background: {hover}; border-color: {accent.name()}; }}
            QFrame#homeDivider {{ color: {border}; background: {border}; border: none;
                max-height: 1px; margin: 8px 0; }}
            QLabel#recentTitle {{ color: {subtle}; font-size: 10px; font-weight: 700; }}
            QLabel#recentLabel {{ color: {muted}; font-size: 13px; }}
            QPushButton#linkButton {{ color: {accent.name()}; background: transparent; border: none;
                padding: 3px 8px; min-width: 0; font-weight: 600; }}
            QPushButton#linkButton:hover {{ color: {accent_hover}; }}
            QPushButton#linkButton:disabled {{ color: {subtle}; }}
        """)

    def set_recently_closed(self, label: str, available: bool = True):
        self.recent_label.setText(label)
        self.reopen_button.setEnabled(available)


class HomePageStack:
    def __init__(self, notebook: QWidget, homepage: HomePageWidget):
        self.notebook = notebook
        self.homepage = homepage
        self.widget = QStackedWidget()
        self.widget.addWidget(self.homepage)
        self.widget.addWidget(self.notebook)

    def refresh(self, show_notebook: bool, recent_label: str, recent_available: bool = True):
        self.widget.setCurrentWidget(self.notebook if show_notebook else self.homepage)
        self.homepage.set_recently_closed(recent_label, recent_available)
