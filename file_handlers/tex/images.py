from __future__ import annotations

lazy import numpy as np
lazy from PySide6.QtGui import QImage

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def read_png(data: bytes):
    image = QImage.fromData(data, "PNG")
    if image.isNull():
        raise ValueError("not a readable PNG")
    deep = image.format() in (
        QImage.Format.Format_Grayscale16, QImage.Format.Format_RGBX64, QImage.Format.Format_RGBA64
    )
    image = image.convertToFormat(QImage.Format.Format_RGBA64 if deep else QImage.Format.Format_RGBA8888)
    dtype = np.uint16 if deep else np.uint8
    rows = np.frombuffer(image.constBits(), dtype).reshape(image.height(), -1)[:, : image.width() * 4]
    return rows.reshape(image.height(), image.width(), 4).astype(np.float32) / np.iinfo(dtype).max
