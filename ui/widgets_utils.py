from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QLabel, QPushButton


def create_list_file_help_label():
    help_label = QLabel(
        '<small><a href="https://github.com/Ekey/REE.PAK.Tool/tree/main/Projects">'
        'Missing list files? Download here</a></small>'
    )
    help_label.setOpenExternalLinks(True)
    help_label.setAlignment(Qt.AlignCenter)
    return help_label


def create_list_file_help_widget(button_text="Load .list…", button_callback=None):
    container = QVBoxLayout()
    container.setSpacing(2)
    
    help_label = create_list_file_help_label()
    
    button = QPushButton(button_text)
    if button_callback:
        button.clicked.connect(button_callback)
    
    container.addWidget(help_label)
    container.addWidget(button)
    
    return container, button