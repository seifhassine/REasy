from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget


class HomePageWidget(QWidget):
    def __init__(self, on_open_file, on_new_project, on_open_project, on_reopen_last, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(28, 28, 28, 28)
        root.addStretch()

        card = QFrame(self)
        card.setMaximumWidth(760)
        card.setStyleSheet(
            "QFrame { border: 1px solid #3a3a3a; border-radius: 12px; background: #242424; }"
            "QPushButton { padding: 8px 14px; font-weight: 600; }"
        )

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 28, 28, 28)
        card_layout.setSpacing(12)

        title = QLabel("Welcome to REasy")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 30px; font-weight: 700; border: 0; background: transparent;")
        card_layout.addWidget(title)

        subtitle = QLabel("Start by opening a file or project.")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("font-size: 14px; color: #b4bdcc; border: 0; background: transparent;")
        card_layout.addWidget(subtitle)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        for text, callback in (
            ("Open File…", on_open_file),
            ("New Project…", on_new_project),
            ("Open Project…", on_open_project),
            ("Reopen Last Closed", on_reopen_last),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(callback)
            actions.addWidget(btn)
        card_layout.addLayout(actions)

        self.recent_label = QLabel("No recently closed files yet.")
        self.recent_label.setAlignment(Qt.AlignCenter)
        self.recent_label.setStyleSheet("font-size: 12px; color: #8c96a8; border: 0; background: transparent;")
        card_layout.addWidget(self.recent_label)

        tips = QLabel("Tip: you can drag and drop files directly into the window.")
        tips.setAlignment(Qt.AlignCenter)
        tips.setStyleSheet("font-size: 12px; color: #8c96a8; border: 0; background: transparent;")
        card_layout.addWidget(tips)

        root.addWidget(card, alignment=Qt.AlignHCenter)
        root.addStretch()

    def set_recently_closed(self, label: str):
        self.recent_label.setText(label)


class HomePageStack:
    def __init__(self, notebook: QWidget, homepage: HomePageWidget):
        self.notebook = notebook
        self.homepage = homepage
        self.widget = QStackedWidget()
        self.widget.addWidget(self.homepage)
        self.widget.addWidget(self.notebook)

    def refresh(self, show_notebook: bool, recent_label: str):
        self.widget.setCurrentWidget(self.notebook if show_notebook else self.homepage)
        self.homepage.set_recently_closed(recent_label)
