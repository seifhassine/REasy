from __future__ import annotations

from PySide6.QtCore import QStringListModel, Qt
from PySide6.QtWidgets import (
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

lazy from ui.editor_widgets import styled_label
lazy from ui.qt_timers import call_later

lazy from .bookmarks import normalize_tag, normalize_tags
lazy from .delegate import _chip_fg, _tag_color


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
        layout.setSpacing(10)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(6)
        layout.addLayout(form)
        path_label = styled_label(path, tone="muted")
        path_label.setWordWrap(True)
        path_label.setTextFormat(Qt.PlainText)
        path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(self._form_label(self.tr("Path:")), path_label)

        self.tags_edit = QLineEdit(self)
        self.tags_edit.setText(", ".join(tags))
        self.tags_edit.setPlaceholderText(self.tr("Comma separated tags"))
        if completer_tags:
            completer = QCompleter(QStringListModel(list(completer_tags), self), self)
            completer.setCaseSensitivity(Qt.CaseInsensitive)
            completer.setCompletionMode(QCompleter.PopupCompletion)
            self.tags_edit.setCompleter(completer)
        form.addRow(self._form_label(self.tr("Tags:")), self.tags_edit)

        self._tag_buttons = {}
        if completer_tags:
            chips_widget = QWidget()
            chips_layout = QHBoxLayout(chips_widget)
            chips_layout.setContentsMargins(0, 0, 0, 0)
            chips_layout.setSpacing(4)
            for tag in completer_tags:
                button = QToolButton(text=tag, checkable=True)
                button.setToolTip(self.tr("Toggle tag"))
                color = _tag_color(tag)
                button.setStyleSheet(
                    "QToolButton { border: 1px solid %s; border-radius: 3px; padding: 1px 8px; }"
                    "QToolButton:checked { background: %s; color: %s; }"
                    % (color.name(), color.name(), _chip_fg(color).name())
                )
                button.clicked.connect(lambda _=False, value=tag: self._toggle_tag(value))
                chips_layout.addWidget(button)
                self._tag_buttons[tag] = button
            chips_layout.addStretch(1)
            chips_scroll = QScrollArea()
            chips_scroll.setWidget(chips_widget)
            chips_scroll.setWidgetResizable(True)
            chips_scroll.setFrameShape(QScrollArea.NoFrame)
            chips_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            chips_scroll.setFixedHeight(chips_widget.sizeHint().height() + 14)
            form.addRow(self._form_label(self.tr("Quick tags:")), chips_scroll)

        self.note_edit = QPlainTextEdit(self)
        self.note_edit.setPlainText(note)
        self.note_edit.setMaximumHeight(80)
        form.addRow(self._form_label(self.tr("Note:")), self.note_edit)

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
        call_later(self, 0, self.tags_edit.setFocus)

    def _form_label(self, text: str) -> QLabel:
        label = styled_label(text, self, tone="muted")
        # Lines the label up with the text inside the field next to it.
        label.setContentsMargins(0, 4, 0, 0)
        return label

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
