from __future__ import annotations

from PySide6.QtCore import QStringListModel, QTimer, Qt
from PySide6.QtWidgets import (
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .bookmarks import normalize_tag, normalize_tags


class BookmarkDialog(QDialog):
    """Edit tags and a note for a bookmark."""

    def __init__(
        self,
        parent,
        *,
        title: str,
        path: str,
        tags=(),
        note: str = "",
        completer_tags=(),
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        completer_tags = tuple(completer_tags)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(self.tr("Path:")))
        path_label = QLabel(path)
        path_label.setWordWrap(True)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(path_label)

        layout.addWidget(QLabel(self.tr("Tags:")))
        self.tags_edit = QLineEdit(self)
        self.tags_edit.setText(", ".join(tags))
        self.tags_edit.setPlaceholderText(self.tr("Comma separated tags"))
        if completer_tags:
            completer = QCompleter(QStringListModel(list(completer_tags), self), self)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setCompletionMode(QCompleter.PopupCompletion)
            self.tags_edit.setCompleter(completer)
        layout.addWidget(self.tags_edit)

        self._tag_buttons = {}
        if completer_tags:
            chips_widget = QWidget()
            chips_layout = QHBoxLayout(chips_widget)
            chips_layout.setContentsMargins(0, 0, 0, 0)
            chips_layout.setSpacing(4)
            for tag in completer_tags:
                button = QToolButton(text=tag, checkable=True)
                button.setToolTip(self.tr("Toggle tag"))
                button.clicked.connect(lambda _=False, value=tag: self._toggle_tag(value))
                chips_layout.addWidget(button)
                self._tag_buttons[tag] = button
            chips_layout.addStretch(1)
            layout.addWidget(QLabel(self.tr("Quick tags:")))
            layout.addWidget(chips_widget)

        layout.addWidget(QLabel(self.tr("Note:")))
        self.note_edit = QPlainTextEdit(self)
        self.note_edit.setPlainText(note)
        self.note_edit.setMaximumHeight(80)
        layout.addWidget(self.note_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        save_button = buttons.button(QDialogButtonBox.Save)
        if save_button:
            save_button.setDefault(True)
        layout.addWidget(buttons)

        self.tags_edit.textChanged.connect(self._refresh_chips)
        self.tags_edit.returnPressed.connect(self.accept)
        self._refresh_chips()
        QTimer.singleShot(0, self.tags_edit.setFocus)

    def tags(self) -> list[str]:
        return [
            tag
            for tag in (part.strip() for part in self.tags_edit.text().split(","))
            if tag
        ]

    def note(self) -> str:
        return self.note_edit.toPlainText().strip()

    def _toggle_tag(self, tag: str) -> None:
        tag = normalize_tag(tag)
        current = [part.strip() for part in self.tags_edit.text().split(",") if part.strip()]
        normalized = set(normalize_tags(current))
        if tag in normalized:
            current = [value for value in current if normalize_tag(value) != tag]
        else:
            current.append(tag)
        self.tags_edit.setText(", ".join(current))

    def _refresh_chips(self) -> None:
        current = set(normalize_tags(self.tags()))
        for tag, button in self._tag_buttons.items():
            button.setChecked(tag in current)
