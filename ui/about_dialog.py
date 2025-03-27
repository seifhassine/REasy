
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

    title_label = QLabel("REasy Editor v0.1.6")
    title_label.setStyleSheet("font-size: 16pt; font-weight: bold;text-align: center;")
    layout.addWidget(title_label, alignment=Qt.AlignHCenter)

    info_label = QLabel(
        "REasy Editor is a quality of life toolkit for modders.\n\n"
        "It supports viewing and full editing of UVAR and RSZ files.\n"
        "\n\n"
        "For more information, visit my GitHub page:"
    )
    info_label.setWordWrap(True)
    info_label.setAlignment(Qt.AlignCenter)
    layout.addWidget(info_label, alignment=Qt.AlignCenter)

    link = QLabel(
        '<a href="http://github.com/seifhassine">http://github.com/seifhassine</a>'
    )
    link.setOpenExternalLinks(True)
    layout.addWidget(link, alignment=Qt.AlignHCenter)

    dialog.exec()
