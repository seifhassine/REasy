
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLabel,
)
from PySide6.QtCore import Qt
from i18n import tr


def create_about_dialog(parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle(tr("AboutDialog", "About REasy Editor"))
    dialog.setFixedSize(450, 250)
    layout = QVBoxLayout(dialog)

    from REasy import CURRENT_VERSION
    title_label = QLabel(tr("AboutDialog", "REasy Editor v{version}").format(version=CURRENT_VERSION))
    title_label.setStyleSheet("font-size: 16pt; font-weight: bold;text-align: center;")
    layout.addWidget(title_label, alignment=Qt.AlignHCenter)

    info_label = QLabel(
        tr("AboutDialog", "REasy Editor is a quality of life toolkit for modders.") + "\n\n" +
        tr("AboutDialog", "It supports viewing and full editing of RE Engine files with 3D capabilities.") + "\n\n" +
        tr("AboutDialog", "For more information, visit my GitHub page:")
    )
    info_label.setWordWrap(True)
    info_label.setAlignment(Qt.AlignCenter)
    layout.addWidget(info_label, alignment=Qt.AlignCenter)

    link = QLabel('<a href="http://github.com/seifhassine">http://github.com/seifhassine</a>')
    link.setOpenExternalLinks(True)
    layout.addWidget(link, alignment=Qt.AlignHCenter)

    dialog.exec()
