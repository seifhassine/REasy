
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLabel,
)
from PySide6.QtCore import Qt

def create_about_dialog(parent):
    """Creates and shows the About dialog"""
    dialog = QDialog(parent)
    dialog.setWindowTitle("About REasy Editor")
    dialog.setFixedSize(450, 250)
    layout = QVBoxLayout(dialog)

    title_label = QLabel("REasy Editor v0.0.8")
    title_label.setStyleSheet("font-size: 16pt; font-weight: bold;")
    layout.addWidget(title_label, alignment=Qt.AlignHCenter)

    info_label = QLabel(
        "REasy Editor is a quality of life toolkit for modders.\n\n"
        "It supports viewing and full editing of UVAR files.\n"
        "Limited editing of scn files is also supported.\n\n"
        "For more information, visit my GitHub page:"
    )
    info_label.setWordWrap(True)
    layout.addWidget(info_label, alignment=Qt.AlignCenter)

    link = QLabel(
        '<a href="http://github.com/seifhassine">http://github.com/seifhassine</a>'
    )
    link.setOpenExternalLinks(True)
    layout.addWidget(link, alignment=Qt.AlignHCenter)

    dialog.exec()
