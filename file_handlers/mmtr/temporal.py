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
_BLEND = f"""Texture2D<uint> shown : register(t0);
Texture2D<float4> before : register(t1);
Texture2D<float> responsive : register(t2);
RWTexture2D<float4> history : register(u0);
RWTexture2D<uint> image : register(u1);
cbuffer Blend : register(b0) {{ float weight; float3 background; }};

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
    float taken = moving != 0.0 || earlier.a != 0.0 ? 1.0 : weight;
    float4 average = float4(taken >= 1.0 ? now : lerp(earlier.rgb, now, taken), moving);
    history[id.xy] = average;
    uint3 bytes = (uint3)round(saturate(average.rgb) * 255.0);
    image[id.xy] = bytes.r | (bytes.g << 8) | (bytes.b << 16) | 0xff000000u;
}}
"""


@lru_cache
def _shader() -> bytes:
    return bytes(compile_hlsl_source(_BLEND, entry_point="main", target="cs_5_0"))


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
        # D3D loads outside a texture read zero, so a 1x1 zero texture is an empty mask for any frame size.
        self._unmasked = (DXGI_FORMAT_R32_FLOAT, device.texture(DXGI_FORMAT_R32_FLOAT, (1, 1), 0, bytes(4)))
        self._blend = device.scene.buffer(bytes(16), 0)
        self._written = bytes(16)
        self._steps: dict[tuple, int] = {}

    def release(self) -> None:
        """Steps returned earlier become invalid."""
        self.device.release([*self._history, self.image, self._unmasked[1], self._blend])
        self._steps = {}

    def steps(self, shown: int, background: Sequence[int], frame: int,
              responsive: tuple[int, int] | None = None) -> list[int]:
        blend = struct.pack("<4f", 1.0 / (frame + 1) if frame < FRAMES else 0.0,
                            *(value / 255.0 for value in background))
        if blend != self._written:
            self.device.scene.write(self._blend, blend)
            self._written = blend
        mask = responsive or self._unmasked
        key = (shown, mask, frame % 2)
        if key not in self._steps:
            before, history = self._history[frame % 2], self._history[1 - frame % 2]
            width, height = self.size
            self._steps[key] = self.device.scene.dispatch({
                "compute_shader": _shader(),
                "tables": [(RANGE_SRV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, shown),)),
                           (RANGE_SRV, 1, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32G32B32A32_FLOAT, before),)),
                           (RANGE_SRV, 2, 0, 1, TEXTURE_2D, 0, ((0, *mask),)),
                           (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32G32B32A32_FLOAT, history),)),
                           (RANGE_UAV, 1, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self.image),)),
                           (RANGE_CBV, 0, 0, 1, 0, 0, ((0, self._blend),))],
                "heap": [], "groups": (-(-width // _GROUP), -(-height // _GROUP), 1)})
        return [self._steps[key]]


__all__ = ["FRAMES", "TemporalImage", "jittered"]
