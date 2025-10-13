from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLabel,
)
from PySide6.QtCore import Qt


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("About REasy Editor"))
        self.setFixedSize(450, 250)
        layout = QVBoxLayout(self)

        from REasy import CURRENT_VERSION
        title_label = QLabel(self.tr("REasy Editor v{version}").format(version=CURRENT_VERSION))
        title_label.setStyleSheet("font-size: 16pt; font-weight: bold;text-align: center;")
        layout.addWidget(title_label, alignment=Qt.AlignHCenter)

        info_label = QLabel(
            self.tr("REasy Editor is a quality of life toolkit for modders.") + "\n\n" +
            self.tr("It supports viewing and full editing of RE Engine files with 3D capabilities.") + "\n\n" +
            self.tr("For more information, visit my GitHub page:")
        )
        info_label.setWordWrap(True)
        info_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(info_label, alignment=Qt.AlignCenter)

        link = QLabel('<a href="http://github.com/seifhassine">http://github.com/seifhassine</a>')
        link.setOpenExternalLinks(True)
        layout.addWidget(link, alignment=Qt.AlignHCenter)
