from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import cache

lazy import numpy as np

CHANNELS = "rgba"


def pair_vectors(green, alpha):
    sum_lane, difference_lane = (np.sign(lane) * lane * lane for lane in (green * 2 - 1, alpha * 2 - 1))
    x, y = (sum_lane + difference_lane) / 2, (sum_lane - difference_lane) / 2
    return np.stack([x, y, 1 - np.abs(x) - np.abs(y)], -1)


def pair_lanes(vectors):
    scale = np.maximum(np.abs(vectors).sum(-1), np.finfo(np.float32).tiny)
    x, y = vectors[..., 0] / scale, vectors[..., 1] / scale
    return tuple((np.sign(lane) * np.sqrt(np.abs(lane)) + 1) / 2 for lane in (x + y, x - y))


def lane_tokens(lanes) -> tuple[str, ...]:
    counts = Counter(lane.meaning for lane in lanes)
    return tuple(lane.meaning if counts[lane.meaning] == 1 else f"{lane.meaning}_{lane.channels}" for lane in lanes)


def _indices(lane) -> list[int]:
    return [CHANNELS.index(channel) for channel in lane.channels]


def _unit(vectors):
    length = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return np.divide(vectors, length, out=np.zeros_like(vectors), where=length > 0)


def unpack(rgba, lanes) -> dict[str, object]:
    images = {}
    for lane, token in zip(lanes, lane_tokens(lanes)):
        if lane.codec == "pair":
            images[token] = (_unit(pair_vectors(rgba[..., 1], rgba[..., 3])) + 1) / 2
        elif lane.codec == "xy":
            x, y = rgba[..., 0] * 2 - 1, rgba[..., 1] * 2 - 1
            images[token] = (_unit(np.stack([x, y, np.sqrt(np.clip(1 - x * x - y * y, 0, 1))], -1)) + 1) / 2
        else:
            images[token] = rgba[..., _indices(lane)]
    return images


def pack(rgba, lanes, sources: dict[str, object]):
    out = np.array(rgba, np.float32)
    for lane, token in zip(lanes, lane_tokens(lanes)):
        if token not in sources:
            continue
        value = np.asarray(sources[token], np.float32)
        if lane.codec == "pair":
            out[..., 1], out[..., 3] = pair_lanes(value * 2 - 1)
        elif lane.codec == "xy":
            out[..., :2] = (_unit(value * 2 - 1)[..., :2] + 1) / 2
        else:
            out[..., _indices(lane)] = value
    return out


@dataclass(frozen=True, slots=True)
class LaneSource:
    image: str = ""
    channel: str = ""
    constant: float | None = None
    invert: bool = False
    directx: bool = False


def compose(lanes, sources: dict[str, LaneSource | None], base, neutral, read):
    read = cache(read)
    if base is None:
        first = next((source.image for source in sources.values() if source is not None and source.image), None)
        if first is None:
            raise ValueError("no lane takes an image, so nothing gives the texture its size")
        base = np.zeros((*read(first).shape[:2], 4), np.float32)
        base[..., 3] = 1
    rgba = np.array(base, np.float32)
    size = rgba.shape[1], rgba.shape[0]
    images = {}
    for lane, token in zip(lanes, lane_tokens(lanes)):
        if token not in sources:
            continue
        if sources[token] is None:
            rgba[..., _indices(lane)] = np.asarray(neutral, np.float32)[_indices(lane)] / 255
        else:
            images[token] = _source_image(sources[token], read, size,
                                          3 if lane.codec in ("pair", "xy") else len(lane.channels))
    return pack(rgba, lanes, images)


def _source_image(source: LaneSource, read, size: tuple[int, int], width: int):
    if source.constant is not None:
        value = np.full((size[1], size[0], width), source.constant, np.float32)
    else:
        image = read(source.image)
        if (image.shape[1], image.shape[0]) != size:
            raise ValueError(f"{source.image} is {image.shape[1]}x{image.shape[0]}, not {size[0]}x{size[1]}")
        if source.channel == "luminance":
            value = image[..., :3] @ np.array([0.2126, 0.7152, 0.0722], np.float32)
        elif source.channel:
            value = image[..., CHANNELS.index(source.channel)]
        else:
            value = image[..., :3]
        value = np.repeat(value[..., None], width, -1) if value.ndim == 2 else value[..., :width]
    if source.directx:
        value = value.copy()
        value[..., 1] = 1 - value[..., 1]
    return 1 - value if source.invert else value
