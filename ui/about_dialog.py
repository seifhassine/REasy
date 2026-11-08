from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon

lazy from app_config import CURRENT_VERSION
lazy from file_handlers.sound.audio_player import AudioPlayer
lazy from ui.editor_widgets import styled_label
lazy from ui.icons import line_icon
lazy from utils.app_paths import resource_path


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("About REasy Editor"))
        self.setAttribute(Qt.WA_DeleteOnClose)
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
        for line, url in (
            (self.tr("REasy Editor is a quality of life toolkit for modders."), ""),
            (self.tr("It supports viewing and full editing of RE Engine files with 3D capabilities."), ""),
            (self.tr("For more information, visit my GitHub page:"), "http://github.com/seifhassine"),
            (self.tr("The REasy Wiki is maintained by BrewedVFX:"), "https://github.com/seifhassine/REasy-Wiki"),
        ):
            info_label = styled_label(line, tone="muted")
            info_label.setWordWrap(True)
            text_layout.addWidget(info_label)
            if url:
                link = QLabel(f'<a href="{url}">{url}</a>')
                link.setOpenExternalLinks(True)
                text_layout.addWidget(link)
        header.addWidget(text)
        layout.addLayout(header)

        theme = AudioPlayer(self)
        theme.setSource(resource_path("resources/audio/credits.wav"))
        theme.setLoops(True)

        footer = QHBoxLayout()
        layout.addLayout(footer)
        volume_icon = QLabel()
        volume_icon.setPixmap(line_icon("sound").pixmap(16, 16))
        footer.addWidget(volume_icon)
        volume = QSlider(Qt.Horizontal)
        volume.setRange(0, 100)
        volume.setFixedWidth(90)
        volume.setToolTip(self.tr("Music volume"))
        volume.valueChanged.connect(lambda value: theme.setVolume(value / 100))
        volume.setValue(10)
        footer.addWidget(volume)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        theme.play()
