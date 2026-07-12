from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from utils.app_paths import resource_path


class HomePageWidget(QWidget):
    def __init__(self, on_open_file, on_new_project, on_open_project, on_reopen_last, parent=None):
        super().__init__(parent)
        self.setObjectName("homePage")

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 32, 32, 32)
        root.addStretch(3)

        hero = QWidget(self)
        hero.setObjectName("homeHero")
        hero.setMaximumWidth(720)
        layout = QVBoxLayout(hero)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        logo = QLabel(hero)
        logo.setObjectName("homeLogo")
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(64, 64)
        logo.setPixmap(
            QIcon(str(resource_path("resources/icons/reasy_editor_logo.ico"))).pixmap(64, 64)
        )
        layout.addWidget(logo, alignment=Qt.AlignHCenter)

        title = QLabel(self.tr("REasy Editor"), hero)
        title.setObjectName("homeTitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel(
            self.tr("Open files. Change things. Try not to anger the engine."),
            hero,
        )
        subtitle.setObjectName("homeSubtitle")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

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
        recent_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(recent_title)

        self.recent_label = QLabel(self.tr("No recently closed files yet."), hero)
        self.recent_label.setObjectName("recentLabel")
        self.recent_label.setAlignment(Qt.AlignCenter)
        self.recent_label.setWordWrap(True)
        self.recent_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.recent_label)

        self.reopen_button = self._button(
            self.tr("Reopen last closed  →"), "linkButton", on_reopen_last
        )
        layout.addWidget(self.reopen_button, alignment=Qt.AlignHCenter)

        tip = QLabel(
            self.tr("Tip: drag and drop a file anywhere in the window."), hero
        )
        tip.setObjectName("homeTip")
        tip.setAlignment(Qt.AlignCenter)
        layout.addWidget(tip)

        root.addWidget(hero, alignment=Qt.AlignHCenter)
        root.addStretch(4)

    def _button(self, text, object_name, callback):
        button = QPushButton(text, self)
        button.setObjectName(object_name)
        button.setCursor(Qt.PointingHandCursor)
        button.setMinimumHeight(38)
        button.clicked.connect(callback)
        return button

    def set_theme(self, colors: dict, accent_color: str):
        accent = QColor(accent_color)
        if not accent.isValid():
            accent = QColor("#ff851b")
        accent_text = "#111111" if accent.lightness() > 155 else "#ffffff"
        accent_hover = accent.lighter(112).name()
        accent_pressed = accent.darker(116).name()
        muted = "#a8b0b8"
        subtle = "#7f8992"
        surface = "#34383c"
        hover = "#3d4247"
        border = "#464c52"

        self.setStyleSheet(f"""
            QWidget#homePage {{ background: {colors['bg']}; }}
            QWidget#homeHero {{ background: transparent; }}
            QLabel#homeLogo {{ background: transparent; }}
            QLabel#homeTitle {{ color: {colors['fg']}; font-size: 36px; font-weight: 700; }}
            QLabel#homeSubtitle {{ color: {muted}; font-size: 15px; }}
            QPushButton#primaryButton, QPushButton#secondaryButton {{ border-radius: 19px;
                padding: 7px 18px; font-size: 13px; font-weight: 600; }}
            QPushButton#primaryButton {{ color: {accent_text}; background: {accent.name()};
                border: 1px solid {accent.name()}; }}
            QPushButton#primaryButton:hover {{ background: {accent_hover}; }}
            QPushButton#primaryButton:pressed {{ background: {accent_pressed}; }}
            QPushButton#secondaryButton {{ color: {colors['fg']}; background: {surface};
                border: 1px solid {border}; }}
            QPushButton#secondaryButton:hover {{ background: {hover}; border-color: {accent.name()}; }}
            QFrame#homeDivider {{ color: {border}; background: {border}; border: none;
                max-height: 1px; margin: 14px 90px 4px 90px; }}
            QLabel#recentTitle {{ color: {subtle}; font-size: 10px; font-weight: 700; }}
            QLabel#recentLabel {{ color: {muted}; font-size: 13px; }}
            QPushButton#linkButton {{ color: {accent.name()}; background: transparent; border: none;
                padding: 3px 8px; min-width: 0; font-weight: 600; }}
            QPushButton#linkButton:hover {{ color: {accent_hover}; }}
            QPushButton#linkButton:disabled {{ color: {subtle}; }}
            QLabel#homeTip {{ color: {subtle}; font-size: 11px; margin-top: 12px; }}
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
