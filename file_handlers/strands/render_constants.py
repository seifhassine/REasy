"""See docs/internals/strands-rendering.md, Render constants."""
from __future__ import annotations

import ctypes
import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from file_handlers.mmtr.engine_buffers import engine_inverse

from .chain_catalog import RenderSection
from .chain_constants import ConstantBlocks
from .render_passes import SET_TEXTURES, TILE_BYTES
from .strands_file import StrandsFile

F = np.float32
_PI, _HALF_TURN = F(3.1415927410125732), F(180)
_MOST_TILT = F(0.1745329350233078)
LEAST_SHARE, _NO_SHARE = F(9.999999747378752e-06), F(100000)
THOUSAND = F(1000)
_LEAST_SPREAD, _LEAST_RATE, _LEAST_STEP = F(0.009999999776482582), F(0.009999999776482582), F(0.10000000149011612)
_DEPTH_WRITE = F(0.949999988079071)
SQUARE, LINEAR, TWENTIETH = F(0.8119999766349792), F(0.7260000109672546), F(3.700000047683716)
FOUR_PI = F(12.566370964050293)
# The fillers' logarithm: the C runtime's ``logf``.
LOGF = ctypes.CDLL("ucrtbase").logf
LOGF.restype, LOGF.argtypes = ctypes.c_float, (ctypes.c_float,)
# Eumelanin and pheomelanin absorption per RGB channel.
LEAST_THROUGH = F(9.999999747378752e-05)
EUMELANIN, PHEOMELANIN = np.array((0.506, 0.841, 1.653), F), np.array((0.343, 0.733, 1.924), F)
_JITTER_SCALE, _TRACE_LIMIT, _ALL_SAMPLES = F(0.009999999776482582), 32, 100000
# The main view's render type (``via.SceneView.RenderType`` Default).
_RENDER_TYPE = 0
_RANGE_END = 0x12345678


@dataclass(frozen=True, slots=True)
class DrawFrame:
    """``box`` is in world space; ``view`` is (position, forward axis); ``offset`` holds the origin positions;
    ``physics_bounds`` is the bounds handoff."""
    lod_factor: float
    drawn: int
    segments: int
    box: tuple[Sequence[float], Sequence[float]]
    transform: Sequence
    own_joint: Callable[[str], Sequence | None]
    offset: tuple[Sequence[float], Sequence[float]]
    view: tuple[Sequence[float], Sequence[float]]
    camera: Sequence[float]
    options: Mapping[str, object] | None
    view_scale: float
    view_tiles: int
    shared_tiles: tuple[int, int]
    vertex_bytes: int
    physics_bounds: bool
    projection_lights: int
    punctual_limit: int
    stamp_resolution: float


def _absorption(melanin, redness) -> np.ndarray:
    through = F(1) - melanin
    length = -F(LOGF(LEAST_THROUGH if LEAST_THROUGH > through else through))
    return (F(1) - redness) * length * EUMELANIN + redness * length * PHEOMELANIN


class RenderConstants(ConstantBlocks):
    def __init__(self, render: RenderSection, layouts: Mapping):
        super().__init__(layouts)
        self.render = render
        self._world: np.ndarray | None = None

    def frame(self, fields: Mapping[str, object], strands: StrandsFile, frame: DrawFrame) -> Mapping[str, bytes]:
        """``fields`` by serialized name."""
        values = {name: fields[field] for name, field in self.render.arguments.items()}
        options = None if frame.options is None else {
            name: frame.options[field] for name, field in self.render.options.items()}
        joint = frame.own_joint(values["constraint_joint"])
        world = np.asarray(frame.transform if joint is None else joint, F).reshape(4, 4)
        before, self._world = world if self._world is None else self._world, world
        made = self._material(values, options, frame) | self._volume(values, strands, frame)
        for name, value in (made | self._placed(values, frame, world, before)).items():
            if name in self._places:
                self._write(name, value)
        return {name: bytes(block) for name, block in self.blocks.items()}

    def _material(self, values: dict, options: dict | None, frame: DrawFrame) -> dict:
        ior, lod = F(values["ior"]), F(frame.lod_factor)
        if values["tilt_in_degrees"]:
            tilt = F(values["tilt_degrees"]) * _PI / _HALF_TURN
        else:
            tilt = F(values["tilt"])
            if self.render.version >= 3 and not tilt < _MOST_TILT:
                tilt = _MOST_TILT
        reflected = (F(1) - ior) / (ior + F(1))
        scale = F(1) / lod if LEAST_SHARE < lod else _NO_SHARE
        spread = F(values["shading_distribution"])
        spread = _LEAST_SPREAD if spread < _LEAST_SPREAD else spread if spread < 1 else F(1)
        threshold, resolution = _DEPTH_WRITE, F(1)
        longitudinal = F(values["longitudinal_roughness"])
        square = longitudinal * longitudinal
        twentieth = longitudinal * (square * square)
        twentieth *= twentieth
        twentieth *= twentieth
        variance = twentieth * TWENTIETH + (longitudinal * LINEAR + square * SQUARE)
        melanin, redness, varied = (F(values[name]) for name in ("melanin", "redness", "random_melanin"))
        tip = (F(values["tip_melanin"]) - melanin) * F(values["tip_rate"]) + melanin
        root, tipped = _absorption(melanin, redness), _absorption(tip, redness)
        if options is not None:
            threshold, resolution = F(options["depth_write_threshold"]), F(options["render_resolution"])
            if options["resolution_limit"]:
                resolution /= F(frame.view_scale)
                resolution = resolution if resolution < 1 else F(1)
        return {"hm_IOR": ior, "hm_cuticleTiltAngle": tilt, "hm_f0": reflected * reflected,
                "hm_azimuthalRoughness": F(values["azimuthal_roughness"]),
                "hm_scaleWidth": scale * F(values["width_scale"]),
                "hm_minWidth": scale * (F(values["least_width"]) / THOUSAND),
                "hm_maxWidth": scale * (F(values["most_width"]) / THOUSAND),
                "hm_global_transparency": F(values["opacity"]), "hm_depthwriteWithAlphaBlendingThrehold": threshold,
                "hm_rootBlendRate": F(values["root_blend"]), "hm_shadingLodFactor": (spread, F(1) / spread),
                "hm_segmentCount": frame.drawn,
                "hm_offsetCamera": np.asarray(frame.offset[0], F) - np.asarray(frame.camera, F),
                "hm_strandRenderScale": resolution, "hm_strandRenderInvserseScale": F(1) / resolution,
                "hm_renderType": _RENDER_TYPE, "hm_v": variance * variance,
                "hm_fakeMultipleScatteringFactor": F(values["fake_scattering"]) / FOUR_PI,
                "hm_backscatterScale": F(values["backscatter"]), "hm_shadowPow": F(values["shadow_pow"]),
                "hm_projLightNum": frame.projection_lights, "hm_albedoRate": F(values["albedo_rate"]),
                "hm_indirectLightingMultiplier": F(values["indirect_multiplier"]),
                "hm_primaryRoughness": F(values["roughness"]), "hm_punctuallightLimit": frame.punctual_limit,
                "hm_sigma": root, "hm_sigmaTip": tipped, "hm_sigmaTipRnd": _absorption(tip + varied, redness) - tipped,
                **dict(zip(("hm_sigmaRndX", "hm_sigmaRndY", "hm_sigmaRndZ"),
                           _absorption(melanin + varied, redness) - root)),
                "hm_stampRTTResolution": F(frame.stamp_resolution)}

    def _volume(self, values: dict, strands: StrandsFile, frame: DrawFrame) -> dict:
        rate = F(values["injection_rate"])
        by_rate = min(int((_LEAST_RATE if _LEAST_RATE > rate else rate) * F(frame.segments)) & 0xffffffff,
                      frame.drawn)
        injected = by_rate if values["rate_from_lod"] or self.render.version < 2 else frame.drawn
        share, step = F(injected) / F(frame.segments), F(values["trace_step"])
        samples = int(values["trace_samples"])
        theta, roughness, absorption = SET_TEXTURES["ds.lut"][1]
        return {"DSInfo_VoxelWorldSize": F(values["voxel_size"]),
                "DSInfo_VolumeResolution": SET_TEXTURES["ds.volume"][1][0],
                "DSInfo_RasterDepthThreshold": F(values["depth_threshold"]), "DSInfo_LUTThetaCount": theta,
                "DSInfo_LUTRoughnessCount": roughness, "DSInfo_LUTAbsorptionCount": absorption,
                "DSInfo_RasterStrandWidthScale": (F(1) / share if LEAST_SHARE < share else _NO_SHARE)
                * F(values["volume_width_scale"]),
                "DSInfo_StrandWidthAve": F(strands.strand_width_ave), "DSInfo_InjectionSegmentCount": injected,
                "DSInfo_RasterTraceStepScale": _LEAST_STEP if step < _LEAST_STEP else step if step < 1 else F(1),
                "DSInfo_VolumeTraceLimit": _TRACE_LIMIT, "DSInfo_VolumeTracingOffsetScale": F(values["trace_offset"]),
                "DSInfo_VolumeTracingDelta": F(values["trace_delta"]),
                "DSInfo_DSVolumeTracingOriginJitterScale": F(values["origin_jitter"]) * _JITTER_SCALE,
                "DSInfo_DSVolumeTracingDirectionJitterRadian": F(values["direction_jitter"]) / _HALF_TURN * _PI,
                "DSInfo_DSTraceInterpolationSampleNum": samples if samples >= 0 else _ALL_SAMPLES}

    @staticmethod
    def _placed(values: dict, frame: DrawFrame, now: np.ndarray, before: np.ndarray) -> dict:
        world = np.column_stack((now[:, :3], np.array((0, 0, 0, 1), F)))
        inverse = engine_inverse(world)
        low, high = (np.asarray(corner, F) for corner in frame.box)
        position, forward = (np.asarray(axis, F) for axis in frame.view)
        depths = [abs((forward[0] * x + forward[1] * y) + z * forward[2])
                  for x, y, z in (np.where(pick, high, low) - position for pick in itertools.product((0, 1), repeat=3))]
        near, far = min(depths), max(depths)
        words = frame.shared_tiles[0] * frame.shared_tiles[1] * TILE_BYTES // 4
        return {"worldMatWorldCB": now[:, :3].T, "prevWorldMatWorldCB": before[:, :3].T,
                "StrandShadingCB_WorldMatrix": world, "StrandShadingCB_InvWorldMatrix": inverse,
                "StrandShadingCB_UsePhysicsAABB": frame.physics_bounds, "DSInfo_InvWorldMatrix": inverse,
                "hm_offsetToWorld": np.asarray(frame.offset[0], F),
                "hm_prevOffsetToWorld": np.asarray(frame.offset[1], F),
                "TargetWidth": frame.view_tiles, "StrandBoundaryScale": F(1) / (far - near),
                "StrandBoundaryOffset": near / (far - near),
                "BufferRange": (frame.vertex_bytes, words, words, _RANGE_END),
                "StrandIndirectLightCB_Roughness": F(values["indirect_roughness"]),
                "StrandIndirectLightCB_TextureSize": SET_TEXTURES["indirect_light.image"][1],
                "StrandIndirectLightCB_IndirectLightingMultiplier": F(values["indirect_multiplier"])}


__all__ = ["DrawFrame", "RenderConstants"]
