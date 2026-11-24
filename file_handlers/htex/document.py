"""See docs/internals/htex-format.md, Editing."""
from __future__ import annotations

from PySide6.QtCore import QObject

from file_handlers.document_commands import EditDocument, ReplaceContents
lazy import numpy as np

from .htex_file import HeightImage, HtexFile, read_htex, write_htex


def height_image(rgba) -> HeightImage:
    texels = np.rint(np.clip(rgba[..., 0], 0, 1) * 255).astype(np.uint8)
    return HeightImage(texels.shape[1], texels.shape[0], texels.tobytes())


def image_values(image: HeightImage):
    return np.frombuffer(image.texels, np.uint8).reshape(image.height, image.width, 1).astype(np.float32) / 255


class HtexDocument(EditDocument):
    def __init__(self, file: HtexFile, parent: QObject | None = None):
        super().__init__(parent)
        self.file = file

    @classmethod
    def read(cls, data: bytes, parent: QObject | None = None) -> HtexDocument:
        return cls(read_htex(data), parent)

    def write(self) -> bytes:
        return write_htex(self.file)

    def _replace(self, images: list[HeightImage], text: str) -> None:
        self.push(ReplaceContents(self, self.file, "images", images, text))

    def insert(self, at: int, image: HeightImage) -> None:
        held = self.file.images
        self._replace([*held[:at], image, *held[at:]], self.tr("Add image"))

    def replace(self, at: int, image: HeightImage) -> None:
        held = self.file.images
        self._replace([*held[:at], image, *held[at + 1:]], self.tr("Replace image"))

    def remove(self, at: int) -> None:
        held = self.file.images
        self._replace([*held[:at], *held[at + 1:]], self.tr("Delete image"))

    def move(self, at: int, to: int) -> None:
        held = [*self.file.images]
        held.insert(to, held.pop(at))
        self._replace(held, self.tr("Move image"))
