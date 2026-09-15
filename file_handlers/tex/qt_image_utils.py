from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtGui import QImage, QPixmap

from .dxgi import DXGI_FORMAT_B8G8R8A8_UNORM_SRGB, DXGI_FORMAT_R8G8B8A8_UNORM_SRGB
from .tex_handler import TexHandler
from .texture_decoder import decode_tex_mip


@dataclass(slots=True)
class TexPreviewMip:
    width: int
    height: int
    data: bytes


@dataclass(slots=True)
class TexPreviewUpload:
    gl_format: int
    levels: tuple[TexPreviewMip, ...]
    compressed: bool = True


_GL_COMPRESSED_FORMATS = {
    71: (0x83F1, 8), 72: (0x8C4D, 8), 74: (0x83F2, 16), 75: (0x8C4E, 16),
    77: (0x83F3, 16), 78: (0x8C4F, 16), 80: (0x8DBB, 8), 81: (0x8DBC, 8),
    83: (0x8DBD, 16), 84: (0x8DBE, 16), 95: (0x8E8F, 16), 96: (0x8E8E, 16),
    98: (0x8E8C, 16), 99: (0x8E8D, 16),
}


def _upload_format(header) -> tuple[int, int | None]:
    format_info = _GL_COMPRESSED_FORMATS.get(header.format)
    if format_info is not None:
        return format_info
    gl_format = (
        0x8C43  # GL_SRGB8_ALPHA8
        if header.format in (DXGI_FORMAT_R8G8B8A8_UNORM_SRGB, DXGI_FORMAT_B8G8R8A8_UNORM_SRGB)
        else 0x8058  # GL_RGBA8
    )
    return gl_format, None


def _read_preview_level(tex, mip: int, compressed: bool) -> tuple[bytes, int, int]:
    if not compressed:
        decoded = decode_tex_mip(tex, 0, mip)
        return bytes(decoded.rgba), decoded.width, decoded.height
    if tex.header.format_is_block_compressed() and not tex.header_is_power_of_two():
        return tex.read_non_pot_level(mip, 0)
    level = tex.get_mip_map_data(mip, 0)
    return level.data, level.width, level.height


def _expected_level_size(width: int, height: int, block_bytes: int | None) -> int:
    if block_bytes is None:
        return width * height * 4
    return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block_bytes


def parse_tex_bytes(tex_bytes: bytes, *, raise_errors: bool = False):
    handler = TexHandler()
    try:
        handler.read(tex_bytes)
    except Exception as exc:
        if raise_errors:
            raise ValueError(f"TEX parse failed: {exc}") from exc
        return None
    return handler.tex


def build_tex_preview_upload(tex, *, mip_selector: Callable[[object], int] | None = None) -> TexPreviewUpload:
    header = getattr(tex, "header", None)
    if tex is None or header is None:
        raise ValueError("missing TEX header")
    # Uncompressed formats are decoded to RGBA8 before upload. Keep sRGB
    # formats sRGB on the GPU so shader sampling performs the expected
    # color-space conversion.
    gl_format, block_bytes = _upload_format(header)
    compressed = block_bytes is not None
    first_mip = int(mip_selector(tex) if callable(mip_selector) else 0)
    if not 0 <= first_mip < header.mip_count:
        raise ValueError(f"invalid first mip {first_mip}/{header.mip_count}")

    levels = []
    expected_dimensions = None
    for mip in range(first_mip, header.mip_count):
        data, width, height = _read_preview_level(tex, mip, compressed)
        if expected_dimensions and (width, height) != expected_dimensions:
            raise ValueError(f"mip {mip} dimensions {(width, height)} != {expected_dimensions}")
        expected_size = _expected_level_size(width, height, block_bytes)
        if len(data) != expected_size:
            raise ValueError(f"mip {mip} size {len(data)} != {expected_size}")
        levels.append(TexPreviewMip(width, height, data))
        expected_dimensions = max(1, width // 2), max(1, height // 2)
    return TexPreviewUpload(gl_format, tuple(levels), compressed=compressed)


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
