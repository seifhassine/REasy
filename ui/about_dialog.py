
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLabel,
)
from PySide6.QtCore import Qt
from ui.styles import get_title_label_stylesheet

def create_about_dialog(parent):
    """Creates and shows the About dialog"""
    dialog = QDialog(parent)
    dialog.setWindowTitle("About REasy Editor")
    dialog.setFixedSize(450, 250)
    layout = QVBoxLayout(dialog)

    title_label = QLabel("REasy Editor v0.3.2")
    title_label.setStyleSheet(get_title_label_stylesheet())
    layout.addWidget(title_label, alignment=Qt.AlignHCenter)

    info_label = QLabel(
        "REasy Editor is a quality of life toolkit for modders.\n\n"
        "It supports viewing and full editing of UVAR, MSG and RSZ files.\n"
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
