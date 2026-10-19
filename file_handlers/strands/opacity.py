"""See docs/internals/mesh-preview.md, Temporal anti-aliasing."""
from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache

from file_handlers.mmtr.material_preview import RANGE_SRV, RANGE_UAV
from file_handlers.mmtr.resident import TEXTURE_2D, UNORDERED_ACCESS, Resident
from file_handlers.sdf.hlsl import compile_hlsl_source
from file_handlers.tex.dxgi import DXGI_FORMAT_R32G32_UINT, DXGI_FORMAT_R32_FLOAT

_GROUP = 8
_OPACITY = f"""Texture2D<uint2> layers : register(t0);
RWTexture2D<float> opacity : register(u0);

[numthreads({_GROUP}, {_GROUP}, 1)]
void main(uint3 id : SV_DispatchThreadID)
{{
    uint width, height;
    opacity.GetDimensions(width, height);
    if (id.x < width && id.y < height)
        opacity[id.xy] = 1.0 - (layers[id.xy].y & 255u) / 255.0;
}}
"""


@lru_cache
def _shader() -> bytes:
    return bytes(compile_hlsl_source(_OPACITY, entry_point="main", target="cs_5_0"))


class StrandOpacity:
    """``texture`` holds 0 to 1."""

    def __init__(self, device: Resident, size: Sequence[int]):
        self.device, self.size = device, tuple(size)
        self.texture = (DXGI_FORMAT_R32_FLOAT, device.texture(DXGI_FORMAT_R32_FLOAT, self.size, UNORDERED_ACCESS))
        self._steps: dict[int, int] = {}

    def release(self) -> None:
        self.device.release([self.texture[1]])
        self._steps = {}

    def steps(self, layers: int) -> list[int]:
        if layers not in self._steps:
            width, height = self.size
            self._steps[layers] = self.device.scene.dispatch({
                "compute_shader": _shader(),
                "tables": [(RANGE_SRV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32G32_UINT, layers),)),
                           (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 0, ((0, *self.texture),))],
                "heap": [], "groups": (-(-width // _GROUP), -(-height // _GROUP), 1)})
        return [self._steps[layers]]


__all__ = ["StrandOpacity"]
