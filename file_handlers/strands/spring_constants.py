"""See docs/internals/strands-chain.md, RE4's spring system."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.clsp.clsp_file import ShapeKind
from file_handlers.clsp.placement import point
from file_handlers.clsp.wrappers import placed
from file_handlers.mmtr.engine_buffers import engine_inverse
from file_handlers.mmtr.material_preview import MaterialPreviewError

from .chain_catalog import ConstantSources
from .chain_constants import ConstantBlocks, matrix_rotation
from .strands_file import StrandsFile

F = np.float32
# The node mass the physics object's constructor sets.
_NODE_MASS = F(1)
_FLAGS = ("advect_stretch", "advect_bending", "solve_stretch", "solve_bending", "ftl_stretch", "ftl_bending",
          "velocity_field")
# The game's filler stops at the ninth collider of a kind.
_SLOTS = 8
_IDENTITY = np.eye(4, dtype=F)
_MATRICES = ("capsuleWorldMat", "capsuleWorldInvMat", "prevCapsuleWorldMat", "sphereWorldMat", "sphereWorldInvMat",
             "prevSphereWorldMat", "boxWorldMat", "boxWorldInvMat", "prevBoxWorldMat")
_UP = np.array([0, 1, 0], F)
_TINY = F(1.1920928955078125e-07)
_OPPOSITE, _NEAR = F(-0.9998999834060669), F(9.999999747378752e-05)
_X, _Z = np.array([1, 0, 0], F), np.array([0, 0, 1], F)
_HALF_TURN = F(1.5707963705062866)


@dataclass(frozen=True, slots=True)
class SpringFrame:
    origin: Sequence[float]
    lod: int
    delta: float
    own_joint: Callable[[str], Sequence[float] | None]
    mesh_offsets: tuple[int, int]


def quaternion_between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``a`` and ``b`` must be unit vectors; follows RE4's helper operation for operation."""
    ax, ay, az = a
    bx, by, bz = b
    dot = F(F(bx * ax + by * ay) + bz * az)
    if dot >= _OPPOSITE:
        s = np.sqrt(F(F(dot + F(1)) * F(2)))
        inverse = F(1) / s
        return np.array([F(bz * ay - az * by) * inverse, F(az * bx - bz * ax) * inverse,
                         F(ax * by - bx * ay) * inverse, s * F(.5)], F)
    c = _X
    if _NEAR <= abs(ax):
        c = _UP if abs(ay) < _NEAR else _Z
    cx, cy, cz = c
    axis = np.array([ay * cz - az * cy, az * cx - ax * cz, ax * cy - ay * cx], F)
    length = np.sqrt(F(F(axis[0] * axis[0] + axis[1] * axis[1]) + axis[2] * axis[2]))
    if _TINY <= length:
        axis = axis * (F(1) / length)
    return np.array([*(axis * np.sin(_HALF_TURN)), np.cos(_HALF_TURN)], F)


def quaternion_matrix(q: np.ndarray, translation: np.ndarray) -> np.ndarray:
    x, y, z, w = q
    y2, z2, xx = y * F(2), z * F(2), x * F(2) * x
    xw = x * F(2) * w
    return np.array([[F(1) - F(z2 * z + y2 * y), y2 * x + z2 * w, z2 * x - y2 * w, 0],
                     [y2 * x - z2 * w, F(1) - F(z2 * z + xx), z2 * y + xw, 0],
                     [z2 * x + y2 * w, z2 * y - xw, F(1) - F(y2 * y + xx), 0],
                     [*translation, 1]], F)


def capsule_slot(ends: Sequence[np.ndarray], radius: F, inverse: np.ndarray) -> tuple[np.ndarray, list]:
    first, second = (point(end, inverse) for end in ends)
    d = first - second
    middle = (second + first) * F(.5)
    length = np.sqrt(F(F(d[0] * d[0] + d[1] * d[1]) + d[2] * d[2]))
    direction = d * (F(1) / length) if _TINY <= length else d
    size = np.sqrt(F(F(direction[0] * direction[0] + direction[1] * direction[1]) + direction[2] * direction[2]))
    if size < _TINY:
        direction = _UP
    return quaternion_matrix(quaternion_between(_UP, direction), middle), [F(radius), length, F(0), F(0)]


class SpringConstants(ConstantBlocks):
    """Seeded as the game's constructor seeds the block."""

    def __init__(self, sources: ConstantSources, layouts: Mapping):
        super().__init__(layouts)
        self.sources = sources
        self._write("boundingBoxOffset", [0, 1, 2, 3])
        for name in _MATRICES:
            self._write(name, np.tile(_IDENTITY, (_SLOTS, 1, 1)))

    def frame(self, fields: Mapping[str, object], strands: StrandsFile, frame: SpringFrame) -> Mapping[str, bytes]:
        sources = self.sources
        values = {name: fields[field] for name, field in sources.arguments.items()}
        if values["own_spheres"] or values["own_boxes"]:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The strands collide with spheres or boxes of their own, which no RE4 file holds."))
        made = {name: fields[field] for name, field in sources.copies.items()}
        guided, posed = bool(values["guided"]), strands.lods[frame.lod]
        made |= {
            "strandSize": 8 << int(values["strand_size"]),
            "strandCount": strands.guide_count if guided else strands.strand_count,
            "vertexCount": strands.lod_guide_vertex_counts[posed.index] if guided else posed.vertex_count,
            "renderStrandCount": strands.strand_count, "renderStrandVertexCount": posed.vertex_count,
            "DeltaT": F(frame.delta), "strandNodeMass": _NODE_MASS,
            "updateControlFlag": sum(bool(values[name]) << bit for bit, name in enumerate(_FLAGS)),
            "meshUVOffset": frame.mesh_offsets[0], "deformedVertexOffset": frame.mesh_offsets[1],
        }
        origin, before = np.asarray(frame.origin, F).reshape(4, 4), self._read("strandWM").reshape(4, 4)
        inverse = engine_inverse(origin)
        ring = self._read("boundingBoxOffset", "<u4").tolist()
        made |= {"strandPrevWM": before, "strandPrevInvWM": engine_inverse(before), "strandWM": origin,
                 "strandInvWM": inverse, "worldQuaternion": matrix_rotation(origin),
                 "boundingBoxOffset": [ring[1], ring[2], ring[3], ring[0]]}
        made |= self._capsules(values["own_capsules"], frame, origin, inverse.reshape(4, 4))
        for name, value in made.items():
            self._write(name, value)
        return {name: bytes(block) for name, block in self.blocks.items()}

    def _capsules(self, group: Sequence[Mapping], frame: SpringFrame, origin: np.ndarray,
                  inverse: np.ndarray) -> dict:
        fields = self.sources.wrappers["own_capsules"]
        current = self._read("capsuleWorldMat").reshape(_SLOTS, 4, 4)
        made = {"capsuleWorldMat": current, "capsuleWorldInvMat": self._read("capsuleWorldInvMat").reshape(_SLOTS, 4, 4),
                "prevCapsuleWorldMat": self._read("prevCapsuleWorldMat").reshape(_SLOTS, 4, 4),
                "capsuleExt": self._read("capsuleExt").reshape(_SLOTS, 4), "capsuleCount": len(group)}
        for slot, wrapper in enumerate(group[:_SLOTS]):
            if not wrapper[fields.enabled]:
                for name in ("capsuleWorldMat", "capsuleWorldInvMat", "prevCapsuleWorldMat"):
                    made[name][slot] = _IDENTITY
                made["capsuleExt"][slot] = 0
                continue
            made["prevCapsuleWorldMat"][slot] = current[slot]
            made["prevCapsuleCount"] = len(group)
            matrix = None
            if wrapper[fields.follow]:
                joint = frame.own_joint(wrapper[fields.joint])
                matrix = origin if joint is None else joint
            shape = placed(ShapeKind.CAPSULE, wrapper[fields.world], [wrapper[name] for name in fields.local], matrix)
            world, made["capsuleExt"][slot] = capsule_slot((shape[0:3], shape[4:7]), shape[8], inverse)
            made["capsuleWorldMat"][slot] = world
            made["capsuleWorldInvMat"][slot] = engine_inverse(world).reshape(4, 4)
        return made


__all__ = ["SpringConstants", "SpringFrame", "capsule_slot", "quaternion_between", "quaternion_matrix"]
