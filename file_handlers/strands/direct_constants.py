"""See docs/internals/strands-rendering.md, RE4's render constants."""
from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

lazy from .chain_catalog import RenderSection
from .chain_constants import ConstantBlocks
lazy from .direct_render import LAYOUT, ibl_count
lazy from .render_constants import (EUMELANIN, FOUR_PI, LEAST_SHARE, LEAST_THROUGH, LINEAR, LOGF, PHEOMELANIN,
                               SQUARE, THOUSAND, TWENTIETH)
lazy from .strands_file import StrandsFile

F = np.float32
_ONE = F(1)
# Chiang's azimuthal polynomial factors.
_A_SQUARE, _A_LINEAR, _A_TWENTY_SECOND = F(1.194000005722046), F(0.26499998569488525), F(5.372000217437744)
_ROOT_PI_EIGHTHS = F(0.6266570687294006)
_LEAST_ROUGHNESS = F(0.0010000000474974513)
_VOXEL_SIZE, _PAGE_RESOLUTION = F(0.30000001192092896), 32
_HALF = F(0.5)


@dataclass(frozen=True, slots=True)
class DirectFrame:
    """``box`` is in world space; ``view`` is (position, forward axis)."""
    lod_factor: float
    segments: int
    drawn: int
    box: tuple[Sequence[float], Sequence[float]]
    transform: Sequence
    own_joint: Callable[[str], Sequence | None]
    moved: bool
    view: tuple[Sequence[float], Sequence[float]]
    view_tiles: int
    projection_lights: int


def drawn_segments(factor: float, segments: int) -> int:
    product = F(segments) * F(factor)
    return int(product) & 0xffffffff if np.isfinite(product) and product >= 0 else 0


def injected_segments(segments: int, drawn: int, rate: float) -> int:
    by_rate = F(segments) * F(rate)
    return min(drawn, int(by_rate) & 0xffffffff if np.isfinite(by_rate) and by_rate >= 0 else 0)


def _absorption(melanin: F, redness: F) -> list:
    through = _ONE - melanin
    length = -F(LOGF(through if LEAST_THROUGH <= through else LEAST_THROUGH))
    rest, red = (_ONE - redness) * length, length * redness
    return [red * pheo + rest * eu for pheo, eu in zip(PHEOMELANIN, EUMELANIN)]


def _longitudinal(roughness: F) -> F:
    """Chiang's longitudinal variance."""
    fifth = roughness * roughness * roughness * roughness * roughness
    tenth = fifth * fifth
    inner = roughness * roughness * SQUARE + roughness * LINEAR + tenth * tenth * TWENTIETH
    return inner * inner


class DirectConstants(ConstantBlocks):
    def __init__(self, render: RenderSection, layouts: Mapping):
        super().__init__(layouts)
        self.render = render
        self._world: np.ndarray | None = None
        self._before: np.ndarray | None = None

    def frame(self, fields: Mapping[str, object], strands: StrandsFile, frame: DirectFrame) -> Mapping[str, bytes]:
        """``fields`` by serialized name."""
        fields = {**self.render.unserialized, **fields}
        values = {name: fields[field] for name, field in self.render.arguments.items()}
        if frame.moved or self._world is None:
            joint = frame.own_joint(values["constraint_joint"])
            world = np.asarray(frame.transform if joint is None else joint, F).reshape(4, 4)
            self._before, self._world = world if self._world is None else self._world, world
        made = self._material(values, frame) | self._volume(values, strands, frame) | {
            "worldMat": self._world, "prevWorldMat": self._before} | self._boundary(frame) | self._ibl(values, frame)
        for name, value in made.items():
            if name in self._places:
                self._write(name, value)
        return {name: bytes(block) for name, block in self.blocks.items()}

    @staticmethod
    def _material(values: dict, frame: DirectFrame) -> dict:
        melanin, redness = F(values["melanin"]), F(values["redness"])
        tip = (F(values["tip_melanin"]) - melanin) * F(values["tip_rate"]) + melanin
        varied, ior = F(values["random_melanin"]), F(values["ior"])
        root, tipped = _absorption(melanin, redness), _absorption(tip, redness)
        product = F(values["longitudinal_roughness"]) * F(values["roughness"])
        product = (_ONE if _ONE <= product else product) if _LEAST_ROUGHNESS <= product else _LEAST_ROUGHNESS
        azimuthal = F(values["azimuthal_roughness"])
        fifth = azimuthal * azimuthal * azimuthal * azimuthal * azimuthal
        eleventh = fifth * fifth * azimuthal
        spread = (azimuthal * azimuthal * _A_SQUARE + azimuthal * _A_LINEAR
                  + eleventh * eleventh * _A_TWENTY_SECOND) * _ROOT_PI_EIGHTHS
        variance, rough = _longitudinal(F(values["longitudinal_roughness"])), _longitudinal(product)
        reflected = (_ONE - ior) / (ior + _ONE)
        lod = F(frame.lod_factor)
        scale = _ONE / (LEAST_SHARE if lod <= LEAST_SHARE else lod)
        return {"hm_sigma": root, "hm_IOR": ior, "hm_cuticleTiltAngle": F(values["tilt"]), "hm_s": spread,
                "hm_v": variance, "hm_m0_roughness": rough, "hm_f0": reflected * reflected,
                "hm_sqrt_s": F(np.sqrt(spread)), "hm_sqrt_v": F(np.sqrt(variance)),
                "hm_sqrt_roughness": F(np.sqrt(rough)),
                "hm_fakeMultipleScatteringFactor": F(values["fake_scattering"]) / FOUR_PI,
                **dict(zip(("hm_sigmaRndX", "hm_sigmaRndY", "hm_sigmaRndZ"),
                           (a - b for a, b in zip(_absorption(varied + melanin, redness), root)))),
                "hm_sigmaTip": tipped, "hm_tipRoughness": F(values["tip_roughness"]),
                "hm_sigmaTipRnd": [a - b for a, b in zip(_absorption(varied + tip, redness), tipped)],
                "hm_rndRoughness": F(values["random_roughness"]), "hm_scaleWidth": scale * F(values["width_scale"]),
                "hm_minWidth": F(values["least_width"]) / THOUSAND * scale,
                "hm_maxWidth": F(values["most_width"]) / THOUSAND * scale,
                "hm_backscatterScale": F(values["backscatter"]), "hm_global_transparency": F(values["opacity"]),
                "hm_shadowDensity": F(values["shadow_density"]), "hm_shadowPow": F(values["shadow_pow"]),
                "hm_guideShadingQualityLightMaxNum": int(values["light_count"]),
                "hm_guideShadingAdjustShading": F(values["adjust_shading"]),
                "hm_guideShadingAdjustDensity": F(values["adjust_density"]),
                "hm_guideShadingQualityLightMaxNumOverDensity": F(values["light_count_over_density"]),
                "hm_depthwriteWithAlphaBlendingThrehold": F(values["depth_write_threshold"]),
                "hm_projLightNum": frame.projection_lights}

    @staticmethod
    def _volume(values: dict, strands: StrandsFile, frame: DirectFrame) -> dict:
        segments = F(frame.segments)
        injected = injected_segments(frame.segments, frame.drawn, values["injection_rate"])
        share = F(injected) / segments
        share = LEAST_SHARE if share <= LEAST_SHARE else share
        theta, roughness, absorption = LAYOUT.set_textures["ds.lut"][1]
        return {"DSInfo_VoxelWorldSize": _VOXEL_SIZE, "DSInfo_VolumeResolution": LAYOUT.set_textures["ds.volume"][1][0],
                "DSInfo_VolumePageResolution": _PAGE_RESOLUTION,
                "DSInfo_RasterDepthThreshold": F(values["depth_threshold"]), "DSInfo_LUTThetaCount": theta,
                "DSInfo_LUTRoughnessCount": roughness, "DSInfo_LUTAbsorptionCount": absorption,
                "DSInfo_VolumeTracingOffsetScale": F(values["trace_offset"]),
                "DSInfo_VolumeTracingDelta": F(values["trace_delta"]),
                "DSInfo_RasterStrandWidthScale": _ONE / share * F(values["volume_width_scale"]),
                "DSInfo_StrandWidthMin": F(strands.strand_width_min),
                "DSInfo_StrandWidthMax": F(strands.strand_width_max),
                "DSInfo_StrandWidthAve": F(strands.strand_width_ave),
                "DSInfo_VolumeTracingIBLDelta": F(values["ibl_delta"])}

    @staticmethod
    def _ibl(values: dict, frame: DirectFrame) -> dict:
        first, count = int(values["ibl_first"]), int(values["ibl_count"])
        low, high = (np.asarray(corner, F) for corner in frame.box)
        return {"IBLDMTInfo_IBLDirectionStart": max(first, 0),
                "IBLDMTInfo_IBLDirectionNum": first + 1 if first >= 0 else ibl_count(count),
                "IBLDMTInfo_IBLRouhness": F(values["ibl_roughness"]), "IBLDMTInfo_CenterPos": (low + high) * _HALF,
                "IBLDMTInfo_InterpRate": F(values["ibl_interp_rate"]),
                "IBLDMTInfo_IndirectLightingMultiplier": F(values["indirect_multiplier"]),
                "IBLDMTInfo_AlbedoBlendRate": F(values["albedo_blend"])}

    @staticmethod
    def _boundary(frame: DirectFrame) -> dict:
        low, high = (np.asarray(corner, F) for corner in frame.box)
        position, forward = (np.asarray(axis, F) for axis in frame.view)
        depths = [abs((forward[0] * x + forward[1] * y) + forward[2] * z)
                  for x, y, z in (np.where(pick, high, low) - position for pick in itertools.product((0, 1), repeat=3))]
        near, far = min(depths), max(depths)
        return {"TargetWidth": frame.view_tiles, "TargetWidthHeight": 0, "StrandBoundaryScale": _ONE / (far - near),
                "StrandBoundaryOffset": near / (far - near)}


__all__ = ["DirectConstants", "DirectFrame", "drawn_segments", "injected_segments"]
