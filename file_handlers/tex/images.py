from __future__ import annotations

import struct
from pathlib import Path

lazy import numpy as np
lazy from PySide6.QtCore import QBuffer, QIODevice
lazy from PySide6.QtGui import QImage

lazy from .dds import DDS_MAGIC
lazy from .texture_decoder import decode_dds_mip, decode_tex_mip

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TGA_DEPTHS = {1: (8, 16), 2: (15, 16, 24, 32), 3: (8, 16), 9: (8, 16), 10: (15, 16, 24, 32), 11: (8, 16)}
_TGA_FOOTER = struct.pack("<2I18s", 0, 0, b"TRUEVISION-XFILE.\0")


def _float(decoded):
    return np.frombuffer(decoded.rgba, np.uint8).reshape(decoded.height, decoded.width, 4).astype(np.float32) / 255


def decoded_image(tex, level: int = 0, image: int = 0, slice_index: int = 0):
    return _float(decode_tex_mip(tex, image, level, slice_index))


def read_image(data: bytes):
    if data.startswith(PNG_SIGNATURE):
        return read_png(data)
    if data[:4] == DDS_MAGIC.to_bytes(4, "little"):
        return _float(decode_dds_mip(data))
    return read_tga(data)


def read_image_file(path: str):
    return read_image(Path(path).read_bytes())


def write_png(image, bits: int = 8) -> bytes:
    height, width, channels = image.shape
    dtype = np.uint16 if bits == 16 else np.uint8
    values = np.rint(np.clip(image, 0, 1) * np.iinfo(dtype).max).astype(dtype)
    if channels == 3:
        values = np.concatenate([values, np.full((height, width, 1), np.iinfo(dtype).max, dtype)], -1)
    formats = {(1, 8): QImage.Format.Format_Grayscale8, (1, 16): QImage.Format.Format_Grayscale16,
               (3, 8): QImage.Format.Format_RGBX8888, (3, 16): QImage.Format.Format_RGBX64,
               (4, 8): QImage.Format.Format_RGBA8888, (4, 16): QImage.Format.Format_RGBA64}
    values = np.ascontiguousarray(values)
    qimage = QImage(values.data, width, height, values.strides[0], formats[channels, bits]).copy()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not qimage.save(buffer, "PNG"):
        raise ValueError("the PNG could not be written")
    return bytes(buffer.data())


def write_tga(image) -> bytes:
    height, width, channels = image.shape
    values = np.rint(np.clip(image, 0, 1) * 255).astype(np.uint8)
    kind, pixels = (3, values) if channels == 1 else (2, values[..., [2, 1, 0, 3][:channels]])
    descriptor = 0x20 | (8 if channels == 4 else 0)
    header = struct.pack("<3B2HB4H2B", 0, 0, kind, 0, 0, 0, 0, 0, width, height, 8 * channels, descriptor)
    return header + np.ascontiguousarray(pixels).tobytes() + _TGA_FOOTER


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


def read_tga(data: bytes):
    if len(data) < 18:
        raise ValueError("not a readable TGA")
    id_length, map_type, kind, map_first, map_length, map_bits, width, height, bits, descriptor = struct.unpack_from(
        "<3B2HB4x2H2B", data)
    mapped = kind & 7 == 1
    if (bits not in _TGA_DEPTHS.get(kind, ()) or map_type > 1 or not width or not height or descriptor & 0xC0
            or mapped and (not map_type or map_bits not in (15, 16, 24, 32))):
        raise ValueError(f"not a readable TGA (image type {kind}, {bits}-bit pixels, {width}x{height})")
    palette_at, entry = 18 + id_length, -(-map_bits // 8)
    start = palette_at + map_type * map_length * entry
    size, count = -(-bits // 8), width * height
    raw = _tga_unpacked(data, start, count, size) if kind & 8 else data[start : start + count * size]
    if len(raw) < count * size:
        raise ValueError("the TGA's pixel data is cut short")
    pixels = np.frombuffer(raw, np.uint8).reshape(height, width, size)[
        :: 1 if descriptor & 0x20 else -1, :: -1 if descriptor & 0x10 else 1].reshape(-1, size)
    alpha = descriptor & 15
    if mapped:
        palette = _tga_rgba(np.frombuffer(data[palette_at:start], np.uint8).reshape(-1, entry), map_bits, False, alpha)
        indices = pixels.view(f"<u{size}")[:, 0].astype(np.intp) - map_first
        if indices.min() < 0 or indices.max() >= len(palette):
            raise ValueError("a TGA pixel indexes past its colour map")
        rgba = palette[indices]
    else:
        rgba = _tga_rgba(pixels, bits, kind & 7 == 3, alpha)
    return rgba.reshape(height, width, 4)


def _tga_unpacked(data: bytes, position: int, count: int, size: int) -> bytes:
    parts, done = [], 0
    while done < count and position < len(data):
        head = data[position]
        repeat = (head & 0x7F) + 1
        span = size if head & 0x80 else repeat * size
        chunk = data[position + 1 : position + 1 + span]
        if len(chunk) < span:
            break
        parts.append(chunk * repeat if head & 0x80 else chunk)
        position += 1 + span
        done += repeat
    return b"".join(parts)[: count * size]


def _tga_rgba(pixels, bits: int, gray: bool, alpha: int):
    if bits in (15, 16) and not gray:
        words = pixels.view("<u2")[:, 0]
        rgba = np.stack([words >> 10 & 31, words >> 5 & 31, words & 31, words >> 15], -1).astype(np.float32)
        rgba[:, :3] /= 31
    else:
        rgba = pixels[:, [0, 0, 0, -1] if gray else [2, 1, 0, -1]].astype(np.float32, order="C") / 255
    if not alpha or bits not in (16, 32):
        rgba[:, 3] = 1
    return rgba
