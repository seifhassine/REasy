"""See docs/internals/mesh-preview.md, Shown image."""
from __future__ import annotations

import math
import struct
from collections.abc import Callable, Sequence
from functools import lru_cache

lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.sdf.hlsl import compile_hlsl_source
from file_handlers.tex.dxgi import DXGI_FORMAT_R10G10B10A2_UNORM, DXGI_FORMAT_R32_UINT, DXGI_FORMAT_R8G8B8A8_UNORM

lazy from .material_preview import RANGE_SRV, RANGE_UAV, MaterialPreviewError
from .mesh_render import DEPTH_VIEWS, MeshScene
lazy from .resident import TEXTURE_2D, UNORDERED_ACCESS

_TOPS = {DXGI_FORMAT_R8G8B8A8_UNORM: 255, DXGI_FORMAT_R10G10B10A2_UNORM: 1023}
# Per channel: a normalized channel's largest integer, or None for floats (sRGB reads decode to float).
_CHANNELS = {
    13: (32767,) * 4,  # R16G16B16A16_SNORM
    DXGI_FORMAT_R10G10B10A2_UNORM: (1023, 1023, 1023, 3),
    26: (None,) * 3,  # R11G11B10_FLOAT
    DXGI_FORMAT_R8G8B8A8_UNORM: (255,) * 4,
    29: (None, None, None, 255),  # R8G8B8A8_UNORM_SRGB
}
# Float 1.0's bit pattern.
_ONE = 0x3F800000
# HLSL testing the float in ``bits`` against 0 as ``MeshScene.coverage`` compares.
_ABOVE, _ZERO = "bits - 1u < 0x7f800000u", "(bits & 0x7fffffffu) == 0u"
_COMPARISONS = {">": _ABOVE, "==": _ZERO, ">=": f"{_ZERO} || {_ABOVE}"}
_GROUP = 8


def shown_value(value: float) -> int:
    value = min(max(value, 0.0), 1.0)
    encoded = value * 12.92 if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055
    return math.floor(encoded * 255 + 0.5)


def _float(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits))[0]


@lru_cache
def _thresholds(top: int | None) -> tuple[int, ...]:
    if top is None:
        value, last = _float, _ONE
    else:
        def value(code: int) -> float:
            return code / top
        last = top
    found = []
    for shade in range(1, 256):
        low, high = 0, last + 1
        while low < high:
            middle = (low + high) // 2
            if shown_value(value(middle)) >= shade:
                high = middle
            else:
                low = middle + 1
        found.append(low)
    return tuple(found)


def target_lanes(form: int) -> str:
    return "rgba"[:len(_CHANNELS.get(form, ()))]


def _source(declarations: str, colour: str, comparisons: Sequence[str]) -> bytes:
    reads = "".join(f"Texture2D<float> coverage{index} : register(t{index + 2});\n"
                    for index in range(len(comparisons)))
    tests = "".join(f"\n    bits = asuint(coverage{index}[at]);\n    drawn = drawn || {_COMPARISONS[comparison]};"
                    for index, comparison in enumerate(comparisons))
    source = f"""Texture2D<float4> image : register(t0);
Texture2D<float> depth : register(t1);
{reads}RWTexture2D<uint> shown : register(u0);
{declarations}
[numthreads({_GROUP}, {_GROUP}, 1)]
void main(uint3 id : SV_DispatchThreadID)
{{
    uint width, height;
    shown.GetDimensions(width, height);
    if (id.x >= width || id.y >= height)
        return;
    uint2 at = id.xy;
    uint3 colour = {colour};
    uint bits = asuint(depth[at]);
    bool drawn = {_ABOVE};{tests}
    shown[at] = colour.r | colour.g << 8 | colour.b << 16 | (drawn ? 0xff000000u : 0u);
}}
"""
    return bytes(compile_hlsl_source(source, entry_point="main", target="cs_5_0"))


@lru_cache
def _shader(top: int, comparisons: tuple[str, ...]) -> bytes:
    return _source("", f"(uint3(round(image[at].rgb * {top})) * 510u + {top}u) / {2 * top}u", comparisons)


@lru_cache
def _target_shader(form: int, channels: str, comparisons: tuple[str, ...]) -> bytes:
    declarations = ""
    for lane in channels:
        top = _CHANNELS[form]["rgba".index(lane)]
        key, threshold = ("float key = value;", "asfloat") if top is None else (
            f"int key = int(round(value * {top}.0));", "int")
        declarations += f"""
static const uint thresholds_{lane}[255] = {{{", ".join(f"{item}u" for item in _thresholds(top))}}};

uint shade_{lane}(float value)
{{
    {key}
    uint count = 0u;
    [unroll] for (uint step = 128u; step > 0u; step >>= 1u)
        if (key >= {threshold}(thresholds_{lane}[count + step - 1u]))
            count += step;
    return count;
}}
"""
    colour = (f"uint3({', '.join(f'shade_{lane}(image[at].{lane})' for lane in channels)})" if len(channels) > 1
              else f"shade_{channels}(image[at].{channels}).xxx")
    return _source(declarations, colour, comparisons)


def _step(scene: MeshScene, image: int, form: int, shader: Callable[[tuple[str, ...]], bytes]) -> tuple[int, int]:
    device, coverage = scene.device, scene.coverage
    shown = device.texture(DXGI_FORMAT_R32_UINT, scene.size, UNORDERED_ACCESS)
    depth_view, view = DEPTH_VIEWS[scene.depth_format][1], TEXTURE_2D
    reads = [(form, image), (depth_view, scene.depth), *((depth_view, texture) for texture, _comparison in coverage)]
    tables = [(RANGE_SRV, slot, 0, 1, view, 0, ((0, *read),)) for slot, read in enumerate(reads)]
    tables.append((RANGE_UAV, 0, 0, 1, view, 0, ((0, DXGI_FORMAT_R32_UINT, shown),)))
    width, height = scene.size
    step = device.scene.dispatch({
        "compute_shader": shader(tuple(comparison for _texture, comparison in coverage)),
        "tables": tables, "heap": [], "groups": (-(-width // _GROUP), -(-height // _GROUP), 1)})
    return step, shown


def shown_step(scene: MeshScene, image: int, form: int) -> tuple[int, int]:
    """The shown texture is R32_UINT, red lowest. Make it after the frame's steps: it reads their coverage."""
    if form not in _TOPS:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The game's display pass writes texture format {format}, which the viewer does not "
                               "show."), {"format": form})
    return _step(scene, image, form, lambda comparisons: _shader(_TOPS[form], comparisons))


def target_step(scene: MeshScene, target: int, channels: str) -> tuple[int, int]:
    """Output as ``shown_step``'s. Make it after the frame's steps: it reads their coverage."""
    form = scene.target_formats[target]
    if form not in _CHANNELS:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "G-buffer target {target} is of texture format {format}, which the viewer does not "
                               "show."), {"target": target, "format": form})
    if channels != "rgb" and (len(channels) != 1 or channels not in target_lanes(form)):
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "G-buffer target {target}, of texture format {format}, has no channels {channels} "
                               "the viewer shows."), {"target": target, "format": form, "channels": channels})
    return _step(scene, scene.targets[target], form,
                 lambda comparisons: _target_shader(form, channels, comparisons))


class TargetImages:
    def __init__(self, scene: MeshScene):
        self.scene = scene
        self._steps: dict[tuple, tuple[int, int]] = {}

    def resize(self) -> None:
        """Call after ``MeshScene.resize``."""
        old = [texture for _step, texture in self._steps.values()]
        self._steps = {}
        self.scene.device.release(old)

    def drawn(self, target: int, channels: str, after: Callable[[int], list[int]]) -> None:
        scene = self.scene
        scene.begin()
        scene.device.pending += scene.gbuffer(apart=True)
        key = target, channels, tuple(scene.coverage)
        if key not in self._steps:
            self._steps[key] = target_step(scene, target, channels)
        step, texture = self._steps[key]
        scene.device.pending += [step, *after(texture)]
        scene.finish()


__all__ = ["TargetImages", "shown_step", "shown_value", "target_lanes", "target_step"]
