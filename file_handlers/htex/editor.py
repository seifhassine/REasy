"""See docs/internals/htex-format.md, Editing."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import QFileDialog, QListWidget, QSplitter, QVBoxLayout, QWidget

lazy from file_handlers.tex.images import read_image_file, write_png, write_tga
lazy from ui.editor_widgets import FitImage, editor_strip, pane_header, styled_label, tool_button, toolbar_separator

from .document import HtexDocument, height_image, image_values


class HtexEditor(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, document: HtexDocument, handler, parent: QWidget | None = None):
        super().__init__(parent)
        self.document, self.handler, self._row = document, handler, 0
        self.images = QListWidget()
        self.view = FitImage()
        self.readout = styled_label(tone="muted")
        left, right = QWidget(), QWidget()
        listing = QVBoxLayout(left)
        listing.setContentsMargins(0, 0, 0, 0)
        listing.setSpacing(0)
        listing.addWidget(pane_header(self.tr("Images")))
        listing.addWidget(self.images, 1)
        shown = QVBoxLayout(right)
        shown.setContentsMargins(6, 6, 6, 6)
        shown.addWidget(self.view, 1)
        shown.addWidget(self.readout)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar())
        layout.addWidget(split, 1)
        self._clear = QTimer(self)
        self._clear.setSingleShot(True)
        self._clear.setInterval(8000)
        self._clear.timeout.connect(self.status.clear)
        for keys, slot, widget in ((QKeySequence.StandardKey.Undo, document.undo_stack.undo, self),
                                   (QKeySequence.StandardKey.Redo, document.undo_stack.redo, self),
                                   (QKeySequence.StandardKey.Delete, self._delete, self.images)):
            QShortcut(keys, widget, slot).setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut if widget is self
                                                     else Qt.ShortcutContext.WidgetShortcut)
        document.changed.connect(self._refresh)
        document.undo_stack.cleanChanged.connect(self._clean_changed)
        self.images.currentRowChanged.connect(self._show)
        self.view.sample_selected.connect(self._sample)
        self._refresh()

    def _toolbar(self) -> QWidget:
        strip, row = editor_strip()
        stack = self.document.undo_stack
        undo = tool_button("undo", self.tr("Undo (Ctrl+Z)"), stack.undo)
        redo = tool_button("redo", self.tr("Redo (Ctrl+Y)"), stack.redo)
        undo.setEnabled(stack.canUndo())
        redo.setEnabled(stack.canRedo())
        stack.canUndoChanged.connect(undo.setEnabled)
        stack.canRedoChanged.connect(redo.setEnabled)
        add = tool_button("add", self.tr("Add an image file after the selection"), self._add, text=self.tr("Add"))
        self.held = [tool_button("import", self.tr("Replace the selected image with an image file"), self._replace),
                     tool_button("export", self.tr("Export the selected image"), self._export),
                     tool_button("delete", self.tr("Delete the selected image (Del)"), self._delete),
                     tool_button("arrow_up", self.tr("Move the selected image up"), lambda: self._move(-1)),
                     tool_button("arrow_down", self.tr("Move the selected image down"), lambda: self._move(1))]
        self.status = styled_label(tone="danger")
        for widget in (undo, redo, toolbar_separator(), add, *self.held[:3], toolbar_separator(), *self.held[3:]):
            row.addWidget(widget)
        row.addWidget(self.status, 1)
        return strip

    def _refresh(self, _change=None) -> None:
        images = self.document.file.images
        self.images.blockSignals(True)
        self.images.clear()
        self.images.addItems([self.tr("{0}: {1} × {2}").format(at, image.width, image.height)
                              for at, image in enumerate(images)])
        self._row = min(max(self._row, 0), len(images) - 1)
        self.images.setCurrentRow(self._row)
        self.images.blockSignals(False)
        for button in self.held:
            button.setEnabled(bool(images))
        self._show(self._row)

    def _show(self, row: int) -> None:
        self._row = row
        image = self.document.file.images[row] if row >= 0 else None
        self.view.set_image(QImage() if image is None else QImage(
            image.texels, image.width, image.height, image.width, QImage.Format.Format_Grayscale8).copy())
        self.readout.clear()

    def _sample(self, x: int, y: int) -> None:
        image = self.document.file.images[self._row]
        self.readout.setText(f"{x}, {y}: {image.texels[y * image.width + x]}")

    def _picked(self, title: str):
        path, _ = QFileDialog.getOpenFileName(self, title, "", "Images (*.png *.tga *.dds)")
        if not path:
            return None
        try:
            return height_image(read_image_file(path))
        except (OSError, ValueError) as error:
            self._refused(str(error))
            return None

    def _add(self) -> None:
        if (image := self._picked(self.tr("Add image"))) is not None:
            self._row += 1
            self.document.insert(self._row, image)

    def _replace(self) -> None:
        if (image := self._picked(self.tr("Replace image"))) is not None:
            self.document.replace(self._row, image)

    def _export(self) -> None:
        stem = Path(self.handler.filepath or "height").name.split(".", 1)[0]
        path, _ = QFileDialog.getSaveFileName(self, self.tr("Export image"), f"{stem}_{self._row}.png",
                                              "PNG (*.png);;TGA (*.tga)")
        if not path:
            return
        values = image_values(self.document.file.images[self._row])
        try:
            Path(path).write_bytes(write_tga(values) if path.lower().endswith(".tga") else write_png(values))
        except (OSError, ValueError) as error:
            self._refused(str(error))

    def _delete(self) -> None:
        if self.document.file.images:
            self.document.remove(self._row)

    def _move(self, step: int) -> None:
        if 0 <= self._row + step < len(self.document.file.images):
            self._row += step
            self.document.move(self._row - step, self._row)

    def _clean_changed(self, clean: bool) -> None:
        self.modified_changed.emit(not clean)

    def _refused(self, text: str) -> None:
        self.status.setText(text)
        self._clear.start()
