from __future__ import annotations

lazy import numpy as np
lazy import fasttexenc

lazy from .lanes import pair_lanes, pair_vectors
from .dxgi import (
    DXGI_FORMAT_A8_UNORM,
    DXGI_FORMAT_B8G8R8A8_UNORM,
    DXGI_FORMAT_B8G8R8A8_UNORM_SRGB,
    DXGI_FORMAT_BC1_UNORM,
    DXGI_FORMAT_BC1_UNORM_SRGB,
    DXGI_FORMAT_BC3_UNORM,
    DXGI_FORMAT_BC3_UNORM_SRGB,
    DXGI_FORMAT_BC4_UNORM,
    DXGI_FORMAT_BC5_UNORM,
    DXGI_FORMAT_BC7_UNORM,
    DXGI_FORMAT_BC7_UNORM_SRGB,
    DXGI_FORMAT_R16G16B16A16_FLOAT,
    DXGI_FORMAT_R16G16B16A16_UNORM,
    DXGI_FORMAT_R8_UNORM,
    DXGI_FORMAT_R8G8_UNORM,
    DXGI_FORMAT_R8G8B8A8_UNORM,
    DXGI_FORMAT_R8G8B8A8_UNORM_SRGB,
)

BLOCK_FORMATS = frozenset({
    DXGI_FORMAT_BC1_UNORM, DXGI_FORMAT_BC1_UNORM_SRGB, DXGI_FORMAT_BC3_UNORM, DXGI_FORMAT_BC3_UNORM_SRGB,
    DXGI_FORMAT_BC4_UNORM, DXGI_FORMAT_BC5_UNORM, DXGI_FORMAT_BC7_UNORM, DXGI_FORMAT_BC7_UNORM_SRGB,
})
_PACKED = {
    DXGI_FORMAT_R8G8B8A8_UNORM: ((0, 1, 2, 3), "u1"),
    DXGI_FORMAT_R8G8B8A8_UNORM_SRGB: ((0, 1, 2, 3), "u1"),
    DXGI_FORMAT_B8G8R8A8_UNORM: ((2, 1, 0, 3), "u1"),
    DXGI_FORMAT_B8G8R8A8_UNORM_SRGB: ((2, 1, 0, 3), "u1"),
    DXGI_FORMAT_R8G8_UNORM: ((0, 1), "u1"),
    DXGI_FORMAT_R8_UNORM: ((0,), "u1"),
    DXGI_FORMAT_A8_UNORM: ((3,), "u1"),
    DXGI_FORMAT_R16G16B16A16_UNORM: ((0, 1, 2, 3), "<u2"),
    DXGI_FORMAT_R16G16B16A16_FLOAT: ((0, 1, 2, 3), "<f2"),
}
ENCODABLE = BLOCK_FORMATS | _PACKED.keys()


def mip_levels(image, count: int, *, srgb: bool, type_name: str | None, first: int = 0) -> list:
    height, width = image.shape[:2]
    codec = ("xyz" if type_name in ("Normal", "NormalRoughness") else "pair" if type_name == "Normal2chRoughness"
             else "srgb" if srgb else "")
    working = _to_working(image, codec)
    levels = [image] if first == 0 else []
    for level in range(max(first, 1), count):
        working = _area(_area(working, 0, max(1, height >> level)), 1, max(1, width >> level))
        if codec == "xyz":
            length = np.linalg.norm(working[..., :3], axis=-1, keepdims=True)
            np.divide(working[..., :3], length, out=working[..., :3], where=length > 0)
        levels.append(_from_working(working, codec))
    return levels


def encode(level, dxgi_format: int) -> bytes:
    values = np.clip(level, 0, 1)
    if dxgi_format in BLOCK_FORMATS:
        return fasttexenc.encode(dxgi_format, np.rint(values * 255).astype(np.uint8), level.shape[1], level.shape[0])
    channels, dtype = _PACKED[dxgi_format]
    values = values[..., channels]
    return (values if dtype == "<f2" else np.rint(values * np.iinfo(dtype).max)).astype(dtype).tobytes()


def _area(values, axis: int, size: int):
    length = values.shape[axis]
    start = np.arange(size) * (length / size)
    first = np.floor(start).astype(np.intp)
    shape = [1] * values.ndim
    shape[axis] = size
    out = 0
    for tap in range(-(-length // size) + 1):
        cell = first + tap
        covered = np.clip(np.minimum(start + length / size, cell + 1) - np.maximum(start, cell), 0, None)
        weight = (covered * (size / length)).astype(np.float32).reshape(shape)
        out = out + np.take(values, np.minimum(cell, length - 1), axis) * weight
    return out


def _to_working(image, codec: str):
    if codec == "srgb":
        rgb = image[..., :3]
        linear = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
        return np.concatenate([linear, image[..., 3:]], -1)
    if codec == "xyz":
        return np.concatenate([image[..., :3] * 2 - 1, image[..., 3:]], -1)
    if codec == "pair":
        return np.concatenate([image[..., :1], pair_vectors(image[..., 1], image[..., 3]), image[..., 2:3]], -1)
    return image


def _from_working(values, codec: str):
    if codec == "srgb":
        rgb = np.clip(values[..., :3], 0, 1)
        encoded = np.where(rgb <= 0.0031308, rgb * 12.92, 1.055 * rgb ** (1 / 2.4) - 0.055)
        return np.concatenate([encoded, values[..., 3:]], -1)
    if codec == "xyz":
        return np.concatenate([(values[..., :3] + 1) / 2, values[..., 3:]], -1)
    if codec == "pair":
        green, alpha = pair_lanes(values[..., 1:4])
        return np.stack([values[..., 0], green, values[..., 4], alpha], -1)
    return values
