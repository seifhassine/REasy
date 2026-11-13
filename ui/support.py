from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

lazy from ui.changelog_dialog import IllustrationPanel
lazy from ui.editor_widgets import styled_label
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role
lazy from utils.app_paths import application_root

SUPPORT_URL = "https://linktr.ee/seifhassine"


class SupportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Support REasy"))
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(560, 190)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(IllustrationPanel(str(application_root() / "resources" / "images" / "reasy_guy.png")))
        body = QVBoxLayout()
        body.setContentsMargins(20, 18, 20, 14)
        body.setSpacing(10)
        root.addLayout(body, 1)
        body.addWidget(styled_label(self.tr("Support REasy"), heading="title"))
        text = QLabel(self.tr("REasy is free to use. If it helps your modding, you can support its development."))
        text.setWordWrap(True)
        body.addWidget(text)
        body.addStretch(1)
        buttons = QHBoxLayout()
        support = QPushButton(line_icon("heart", current_colors()["accent_text"]), self.tr("Support REasy"))
        set_style_role(support, variant="primary")
        support.setDefault(True)
        support.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(SUPPORT_URL)))
        support.clicked.connect(self.accept)
        buttons.addWidget(support)
        star = QPushButton(line_icon("star"), self.tr("Star on GitHub"))
        star.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/seifhassine/REasy")))
        buttons.addWidget(star)
        buttons.addStretch(1)
        close = QPushButton(self.tr("Close"))
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        body.addLayout(buttons)
