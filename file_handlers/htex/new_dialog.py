"""See docs/internals/htex-format.md, Editing."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout)

lazy from file_handlers.mmtr.profiles.base import same_mmtr_profile
lazy from file_handlers.tex.images import read_image_file
lazy from ui.icons import line_icon
lazy from ui.styles import set_style_role

from .document import height_image
from .htex_file import HtexFile, htex_versions, write_htex


class NewHeightTextureDialog(QDialog):
    def __init__(self, game: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("New Height Texture"))
        self.written = ""
        self.game = QComboBox()
        for name, version in sorted(htex_versions().items()):
            self.game.addItem(name, version)
        self.game.setCurrentIndex(next((at for at in range(self.game.count())
                                        if same_mmtr_profile(self.game.itemText(at), game)), 0))
        self.image, self.output = QLineEdit(), QLineEdit()
        self.image.setPlaceholderText(self.tr("Image file"))
        self.output.setPlaceholderText(self.tr("Where to write the .htex"))
        form = QGridLayout()
        form.setHorizontalSpacing(6)
        for row, (text, field, browse) in enumerate(((self.tr("Game"), self.game, None),
                                                     (self.tr("Image"), self.image, self._browse_image),
                                                     (self.tr("Output"), self.output, self._browse_output))):
            form.addWidget(QLabel(text), row, 0)
            form.addWidget(field, row, 1)
            if browse is not None:
                button = QPushButton()
                button.setIcon(line_icon("folder"))
                set_style_role(button, compact=True)
                button.clicked.connect(browse)
                form.addWidget(button, row, 2)
        self.status = QLabel()
        set_style_role(self.status, tone="danger")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.addLayout(form)
        root.addWidget(self.status)
        root.addWidget(self.buttons)
        for field in (self.image, self.output):
            field.textChanged.connect(self._validate)
        self._validate()

    def _browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Choose an image"), self.image.text(),
                                              "Images (*.png *.tga *.dds)")
        if path:
            self.image.setText(path)

    def _browse_output(self) -> None:
        stem = Path(self.image.text().strip()).name.split(".", 1)[0] or "height"
        path, _ = QFileDialog.getSaveFileName(self, self.tr("Save .htex"),
                                              self.output.text() or f"{stem}.htex.{self.game.currentData()}")
        if path:
            self.output.setText(path)

    def _validate(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            bool(self.image.text().strip() and self.output.text().strip()))

    def accept(self) -> None:
        output = self.output.text().strip()
        try:
            Path(output).write_bytes(write_htex(HtexFile([height_image(read_image_file(self.image.text().strip()))])))
        except (OSError, ValueError) as error:
            self.status.setText(str(error))
            return
        self.written = output
        super().accept()


def new_height_texture(app, parent=None) -> None:
    dialog = NewHeightTextureDialog(app.current_game, parent or app)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        app.add_tab(dialog.written, Path(dialog.written).read_bytes())
    dialog.deleteLater()
