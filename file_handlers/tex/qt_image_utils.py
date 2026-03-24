from __future__ import annotations

from collections.abc import Callable

from PySide6.QtGui import QImage, QPixmap

from .tex_handler import TexHandler
from .texture_decoder import decode_tex_mip


def parse_tex_bytes(tex_bytes: bytes):
    handler = TexHandler()
    try:
        handler.read(tex_bytes)
    except Exception:
        return None
    return handler.tex


def _decode_qimage_from_tex(
    tex,
    *,
    image_index: int = 0,
    mip_index: int = 0,
    mip_selector: Callable[[object], int] | None = None,
    copy_image: bool = True,
) -> tuple[QImage, bytes] | None:
    if tex is None:
        return None
    selected_mip = mip_selector(tex) if callable(mip_selector) else mip_index
    decoded = decode_tex_mip(tex, image_index, selected_mip)
    rgba_bytes = bytes(decoded.rgba)
    image = QImage(rgba_bytes, decoded.width, decoded.height, QImage.Format.Format_RGBA8888)
    if copy_image:
        return image.copy(), b""
    return image, rgba_bytes


def decode_tex_bytes_to_qimage(
    tex_bytes: bytes,
    *,
    image_index: int = 0,
    mip_index: int = 0,
    mip_selector: Callable[[object], int] | None = None,
) -> QImage | None:
    tex = parse_tex_bytes(tex_bytes)
    decoded = _decode_qimage_from_tex(
        tex,
        image_index=image_index,
        mip_index=mip_index,
        mip_selector=mip_selector,
        copy_image=True,
    )
    return decoded[0] if decoded is not None else None


def decode_parsed_tex_to_qimage_with_buffer(
    tex,
    *,
    image_index: int = 0,
    mip_index: int = 0,
    mip_selector: Callable[[object], int] | None = None,
) -> tuple[QImage, bytes] | None:
    return _decode_qimage_from_tex(
        tex,
        image_index=image_index,
        mip_index=mip_index,
        mip_selector=mip_selector,
        copy_image=False,
    )


def decode_tex_bytes_to_qpixmap(
    tex_bytes: bytes,
    *,
    image_index: int = 0,
    mip_index: int = 0,
    mip_selector: Callable[[object], int] | None = None,
) -> QPixmap | None:
    image = decode_tex_bytes_to_qimage(
        tex_bytes,
        image_index=image_index,
        mip_index=mip_index,
        mip_selector=mip_selector,
    )
    return QPixmap.fromImage(image) if image is not None else None
