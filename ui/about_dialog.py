from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon

lazy from app_config import CURRENT_VERSION
lazy from ui.editor_widgets import styled_label
lazy from utils.app_paths import resource_path


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("About REasy Editor"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(12)
        layout.setSizeConstraint(QLayout.SetFixedSize)

        header = QHBoxLayout()
        header.setSpacing(14)
        logo = QLabel()
        logo.setPixmap(QIcon(str(resource_path("resources/icons/reasy_editor_logo.ico"))).pixmap(48, 48))
        header.addWidget(logo, 0, Qt.AlignTop)

        text = QWidget()
        text.setFixedWidth(400)
        text_layout = QVBoxLayout(text)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)
        title_label = styled_label(self.tr("REasy Editor v{version}").format(version=CURRENT_VERSION), heading="title")
        text_layout.addWidget(title_label)
        text_layout.addSpacing(4)
        for line in (
            self.tr("REasy Editor is a quality of life toolkit for modders."),
            self.tr("It supports viewing and full editing of RE Engine files with 3D capabilities."),
            self.tr("For more information, visit my GitHub page:"),
        ):
            info_label = styled_label(line, tone="muted")
            info_label.setWordWrap(True)
            text_layout.addWidget(info_label)

        link = QLabel('<a href="http://github.com/seifhassine">http://github.com/seifhassine</a>')
        link.setOpenExternalLinks(True)
        text_layout.addWidget(link)
        header.addWidget(text)
        layout.addLayout(header)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
