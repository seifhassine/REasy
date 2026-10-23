"""See docs/internals/mesh-preview.md, Temporal anti-aliasing."""
from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import replace
from functools import lru_cache

lazy from file_handlers.sdf.hlsl import compile_hlsl_source
lazy from file_handlers.tex.dxgi import DXGI_FORMAT_R32G32B32A32_FLOAT, DXGI_FORMAT_R32_FLOAT, DXGI_FORMAT_R32_UINT

lazy from .engine_buffers import Camera
lazy from .material_preview import RANGE_CBV, RANGE_SRV, RANGE_UAV
lazy from .resident import TEXTURE_2D, UNORDERED_ACCESS, Resident

FRAMES = 8
_GROUP = 8
# Blend flags: show the kept average where unchanged; keep the average; the frame is its average's first.
_KEPT, _WHOLE, _FIRST = 1, 2, 4
_BLEND = f"""Texture2D<uint> shown : register(t0);
Texture2D<float4> before : register(t1);
Texture2D<float> responsive : register(t2);
RWTexture2D<float4> history : register(u0);
RWTexture2D<uint> image : register(u1);
RWTexture2D<uint> kept : register(u2);
RWTexture2D<uint> reference : register(u3);
cbuffer Blend : register(b0) {{ float weight; float3 background; uint flags; }};

[numthreads({_GROUP}, {_GROUP}, 1)]
void main(uint3 id : SV_DispatchThreadID)
{{
    uint width, height;
    image.GetDimensions(width, height);
    if (id.x >= width || id.y >= height)
        return;
    uint texel = shown[id.xy];
    float3 colour = float3(texel & 255u, (texel >> 8) & 255u, (texel >> 16) & 255u) / 255.0;
    float moving = responsive.Load(int3(id.xy, 0));
    float3 now = texel >> 24 != 0u ? colour : background;
    float4 earlier = before[id.xy];
    bool live = moving != 0.0 || earlier.a != 0.0;
    float taken = live ? 1.0 : weight;
    float4 average = float4(taken >= 1.0 ? now : lerp(earlier.rgb, now, taken), moving);
    history[id.xy] = average;
    uint3 bytes = (uint3)round(saturate(average.rgb) * 255.0);
    uint made = bytes.r | (bytes.g << 8) | (bytes.b << 16) | 0xff000000u;
    uint held = kept[id.xy];
    image[id.xy] = (flags & {_KEPT}u) != 0u && held >> 24 != 0u && !live ? held : made;
    if ((flags & {_WHOLE}u) != 0u)
        kept[id.xy] = made;
    if ((flags & {_FIRST}u) != 0u)
        reference[id.xy] = texel;
}}
"""
# Where a first frame or its neighbours differ from the first frame before, the kept average's alpha is cleared.
_MARK = f"""Texture2D<uint> shown : register(t0);
Texture2D<uint> reference : register(t1);
RWTexture2D<uint> kept : register(u0);

[numthreads({_GROUP}, {_GROUP}, 1)]
void main(uint3 id : SV_DispatchThreadID)
{{
    uint width, height;
    kept.GetDimensions(width, height);
    if (id.x >= width || id.y >= height)
        return;
    int2 last = int2(width, height) - 1;
    bool same = true;
    for (int y = -1; y <= 1; ++y)
        for (int x = -1; x <= 1; ++x) {{
            int3 at = int3(clamp(int2(id.xy) + int2(x, y), int2(0, 0), last), 0);
            same = same && shown.Load(at) == reference.Load(at);
        }}
    if (!same)
        kept[id.xy] &= 0xffffffu;
}}
"""


@lru_cache
def _shader(source: str) -> bytes:
    return bytes(compile_hlsl_source(source, entry_point="main", target="cs_5_0"))


def _halton(index: int, base: int) -> float:
    fraction, result = 1.0, 0.0
    while index:
        fraction /= base
        result += fraction * (index % base)
        index //= base
    return result


def jittered(camera: Camera, frame: int) -> Camera:
    if frame >= FRAMES:
        return camera
    index = frame + 1
    width, height = camera.size
    return replace(camera, jitter=((_halton(index, 2) - 0.5) * 2 / width, (_halton(index, 3) - 0.5) * 2 / height))


class TemporalImage:
    def __init__(self, device: Resident, size: Sequence[int]):
        self.device, self.size = device, tuple(size)
        self._history = [device.texture(DXGI_FORMAT_R32G32B32A32_FLOAT, self.size, UNORDERED_ACCESS) for _ in "ab"]
        self.image = device.texture(DXGI_FORMAT_R32_UINT, self.size, UNORDERED_ACCESS)
        # The last whole average, its alpha cleared where a first frame since differed; the last first frame.
        self._kept, self._reference = (device.texture(DXGI_FORMAT_R32_UINT, self.size, UNORDERED_ACCESS)
                                       for _ in "ab")
        # The ``framed`` of each, and whether the average drawn shows the kept one where unchanged.
        self._kept_for = self._reference_for = None
        self._partial = False
        # D3D loads outside a texture read zero, so a 1x1 zero texture is an empty mask for any frame size.
        self._unmasked = (DXGI_FORMAT_R32_FLOAT, device.texture(DXGI_FORMAT_R32_FLOAT, (1, 1), 0, bytes(4)))
        self._blend = device.scene.buffer(bytes(32), 0)
        self._written = bytes(32)
        self._steps: dict[tuple, int] = {}

    def release(self) -> None:
        """Steps returned earlier become invalid."""
        self.device.release([*self._history, self.image, self._kept, self._reference, self._unmasked[1],
                             self._blend])
        self._steps = {}

    def steps(self, shown: int, background: Sequence[int], frame: int,
              responsive: tuple[int, int] | None = None, framed=None) -> list[int]:
        """``framed``: what places the frame's texels (camera, size, light), None if unknown. An average begun again
        framed as the last whole one shows that one on until it is whole, but where its first frame differs from the
        first frame before."""
        if frame == 0:
            self._partial = framed is not None and framed == self._kept_for == self._reference_for
            self._reference_for = framed
        if frame == FRAMES - 1:
            self._kept_for = framed
        flags = (_KEPT if self._partial and frame < FRAMES - 1 else 0) | (_WHOLE if frame == FRAMES - 1 else 0) \
            | (_FIRST if frame == 0 else 0)
        blend = struct.pack("<4fI12x", 1.0 / (frame + 1) if frame < FRAMES else 0.0,
                            *(value / 255.0 for value in background), flags)
        if blend != self._written:
            self.device.scene.write(self._blend, blend)
            self._written = blend
        mask = responsive or self._unmasked
        key = (shown, mask, frame % 2)
        width, height = self.size
        groups = (-(-width // _GROUP), -(-height // _GROUP), 1)
        if key not in self._steps:
            before, history = self._history[frame % 2], self._history[1 - frame % 2]
            self._steps[key] = self.device.scene.dispatch({
                "compute_shader": _shader(_BLEND),
                "tables": [(RANGE_SRV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, shown),)),
                           (RANGE_SRV, 1, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32G32B32A32_FLOAT, before),)),
                           (RANGE_SRV, 2, 0, 1, TEXTURE_2D, 0, ((0, *mask),)),
                           (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32G32B32A32_FLOAT, history),)),
                           (RANGE_UAV, 1, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self.image),)),
                           (RANGE_UAV, 2, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self._kept),)),
                           (RANGE_UAV, 3, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self._reference),)),
                           (RANGE_CBV, 0, 0, 1, 0, 0, ((0, self._blend),))],
                "heap": [], "groups": groups})
        if not (frame == 0 and self._partial):
            return [self._steps[key]]
        if ("mark", shown) not in self._steps:
            self._steps["mark", shown] = self.device.scene.dispatch({
                "compute_shader": _shader(_MARK),
                "tables": [(RANGE_SRV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, shown),)),
                           (RANGE_SRV, 1, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self._reference),)),
                           (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self._kept),))],
                "heap": [], "groups": groups})
        return [self._steps["mark", shown], self._steps[key]]


__all__ = ["FRAMES", "TemporalImage", "jittered"]
