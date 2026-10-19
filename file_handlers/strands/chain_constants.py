"""See docs/internals/strands-chain.md."""
from __future__ import annotations

import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from file_handlers.clsp.clsp_file import EVERY_TAG, CollisionShape, ShapeKind
lazy from file_handlers.clsp.placement import pairwise, point, row_lengths
lazy from file_handlers.clsp.wrappers import placed
lazy from file_handlers.mmtr.engine_buffers import engine_inverse

lazy from .chain_catalog import ConstantSources
lazy from .gpus import GpuStrandsPreset
lazy from .strands_file import StrandsFile

F = np.float32
_PI, _PER_DEGREE = F(3.1415927410125732), F(0.0055555556900799274)
_DISCRETE = F(1e8)
_TINY = F(1.1920928955078125e-07)

_PRESET = {
    "strandNodeRadius": "radius", "strandDensity": "density", "strandStretchYoungModulus": "stretch_stiffness",
    "strandAngularYoungModulus": "bend_stiffness", "strandStaticFriction": "static_friction",
    "strandKineticFriction": "kinetic_friction", "restitutionCoefficient": "restitution",
    "swingLimit": "swing_limit", "swingLimitStiffness": "swing_limit_stiffness", "twistLimit": "twist_limit",
    "twistLimitStiffness": "twist_limit_stiffness", "repulsiveStrength": "repulsive_strength",
    "interSmoothingStrength": "inter_smoothing_strength", "linearVelocityDamping": "linear_damping",
    "angularVelocityDamping": "angular_damping", "linearVelocityEMAWeight": "linear_ema",
    "angularVelocityEMAWeight": "angular_ema", "clumpStrayAmount": "clump_stray_amount",
    "clumpTightness": "clump_tightness", "clumpLimit": "clump_limit",
}
_ANGLES = frozenset({"swingLimit", "twistLimit"})
_UNSET = {"clumpStrayAmount": F(1), "clumpTightness": F(0), "clumpLimit": F(0)}
_SWITCHES = (("use_volume_preservation", "volume_preservation"), ("use_inter_smoothing", "inter_smoothing"),
             ("use_swing_limit", "swing_limited"), ("use_twist_limit", "twist_limited"),
             ("use_responsive_curve", None), ("use_damping_scaling_curve", None), ("use_static_friction_curve", None))
_FLAGS = (("advect_stretch", 0x1), ("advect_bending", 0x2), ("solve_stretch", 0x4), ("solve_bending", 0x8),
          ("solve_collision", 0x80), ("air_drag", 0x100), ("world_quaternion", 0x200), ("fast_collision", 0x400))
_SOLVER = {1: (6, 12, 24, 32), 2: (4, 12, 24, 32), 3: (5, 8, 16, 24)}
_SOLVER_4 = ((6, 6, 12, None), (5, None, None, 20))
_STRAND_SIZES = {1: 16, 2: 32}
_WET = (("wetMassMultiplier", "wet_mass", 1), ("wetStretchMultiplier", "wet_stretch", 1),
        ("wetBendMultiplier", "wet_bend", 1), ("wetFrictionMultiplier", "wet_friction", 1),
        ("wetLinearDamping", "wet_linear_damping", 0), ("wetAngularDamping", "wet_angular_damping", 0))
_CURVES = (("responsive_curve", 4, "numResponsiveCurve", "endpointResponsiveCurve", "weightsResponsiveCurve"),
           ("damping_scaling_curve", 5, "numDampingScalingWeight", "endDampingScalingSpeed", "weightsDampingScaling"))
_SAMPLES = 32
_COLLIDERS = {ShapeKind.SPHERE: "sphereColliders", ShapeKind.CAPSULE: "capsuleColliders",
              ShapeKind.TAPERED_CAPSULE: "capsuleColliders", ShapeKind.BOX: "boxColliders",
              ShapeKind.PLANE: "planeColliders"}
_COUNTS = {"sphereColliders": "sphereColliderCount", "capsuleColliders": "capsuleColliderCount",
           "boxColliders": "boxColliderCount", "planeColliders": "planeColliderCount"}
_SLOTS = 32
# Wet colliders kept; ``CastWetCollider`` drops the rest.
_WET_SLOTS = 8
_OWN = {"own_spheres": ShapeKind.SPHERE, "own_capsules": ShapeKind.CAPSULE, "own_boxes": ShapeKind.BOX}
_CLEARED = {ShapeKind.SPHERE: [(0, [0] * 4)], ShapeKind.CAPSULE: [(0, [0] * 8)],
            ShapeKind.BOX: [(0, [0] * 3), (16, [0, 0, 0, 1, 0, 0, 0])]}


@dataclass(frozen=True, slots=True)
class SetFrame:
    origin: Sequence[float]
    lod: int
    first_lod: int
    segments: int
    time_scale: float
    measured_rate: float
    simulated_frames: int
    anchor: tuple[Sequence[float], Sequence[float]] | None
    colliders: Sequence[CollisionShape]
    own_joint: Callable[[str], Sequence[float] | None]
    mesh_offsets: tuple[int, int]
    wet_colliders: Sequence[tuple[Sequence[float], float, float]]
    dry_out: bool
    wind: Sequence[float] | None = None
    turn: np.ndarray | None = None


def _cells(low: np.ndarray, high: np.ndarray, width: F) -> list[int]:
    if not width > 0:
        return [1, 1, 1]
    counts = [int(np.ceil((top + (top + bottom) * F(-.5)) * F(2) / width)) + 1 for bottom, top in zip(low, high)]
    return [count if 0 <= count <= 1024 else 1024 for count in counts]


def _radians(value) -> F:
    """In the order the engines' limit setters and preset sampler multiply."""
    return F(value) * _PER_DEGREE * _PI


def matrix_rotation(matrix: np.ndarray) -> np.ndarray:
    """Quaternion as (x, y, z, w)."""
    rows = []
    for row in matrix[:3]:
        a, b, c, d = row
        length = np.sqrt(F(F(F(a * a + b * b) + c * c) + d * d))
        rows.append(row[:3] * (F(1) if length < _TINY else F(1) / length))
    r = rows
    a, b, c = r[0][0], r[1][1], r[2][2]
    if F(F(a + b) + c) > 0:
        k = F(.5) / np.sqrt(F(F(F(a + b) + c) + F(1)))
        return np.array([(r[1][2] - r[2][1]) * k, (r[2][0] - r[0][2]) * k, (r[0][1] - r[1][0]) * k, F(.25) / k], F)
    if b < a and c < a:
        d = np.sqrt(F(F(F(a + F(1)) - b) - c))
        d = d + d
        return np.array([d * F(.25), (r[0][1] + r[1][0]) / d, (r[2][0] + r[0][2]) / d, (r[1][2] - r[2][1]) / d], F)
    if b > c:
        d = np.sqrt(F(F(F(b + F(1)) - a) - c))
        d = d + d
        return np.array([(r[0][1] + r[1][0]) / d, d * F(.25), (r[1][2] + r[2][1]) / d, (r[2][0] - r[0][2]) / d], F)
    d = np.sqrt(F(F(F(c + F(1)) - a) - b))
    d = d + d
    return np.array([(r[2][0] + r[0][2]) / d, (r[1][2] + r[2][1]) / d, d * F(.25), (r[0][1] - r[1][0]) / d], F)


def _times(matrix: np.ndarray, parent: np.ndarray) -> np.ndarray:
    """In the engines' summation order."""
    out = np.empty((4, 4), F)
    for i, (x, y, z, w) in enumerate(matrix[:3]):
        out[i] = ((x * parent[0] + y * parent[1]) + w * parent[3]) + z * parent[2]
    x, y, z, _ = matrix[3]
    out[3] = ((x * parent[0] + y * parent[1]) + z * parent[2]) + parent[3]
    return out


def _product(a: Sequence, b: Sequence) -> np.ndarray:
    """The engines' quaternion product b * a, in their summation order."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([((bw * ax + aw * bx) + by * az) + -bz * ay, ((bw * ay + aw * by) + bz * ax) + -bx * az,
                     ((bw * az + aw * bz) + bx * ay) + -by * ax, ((bw * aw + ax * -bx) + -by * ay) + -bz * az], F)


def _unrotate(position: np.ndarray, quaternion: np.ndarray) -> np.ndarray:
    x, y, z, w = quaternion
    size = ((x * x + y * y) + z * z) + w * w
    inverse = np.array([-x, -y, -z, w], F) * (F(1) / size) if size > 0 else np.array([0, 0, 0, 1], F)
    return _product(_product(inverse, [*position, F(0)]), quaternion)[:3]


def _slot(kind: ShapeKind, v: np.ndarray, inverse: np.ndarray, quaternion: np.ndarray | None,
          own: bool = False) -> list[tuple[int, list]]:
    def local(position):
        out = point(position, inverse)
        return out if quaternion is None else _unrotate(out, quaternion)

    if kind == ShapeKind.SPHERE:
        return [(0, [*local(v[0:3]), v[3]])]
    if kind == ShapeKind.CAPSULE:
        return [(0, [*local(v[0:3]), v[8], *local(v[4:7]), v[8]])]
    if kind == ShapeKind.TAPERED_CAPSULE:
        return [(0, [*local(v[0:3]), v[3], *local(v[4:7]), v[7]])]
    if kind == ShapeKind.BOX:
        world = v[:16].reshape(4, 4)
        held = pairwise(world, inverse)
        turn, centre = matrix_rotation(held), held[3, :3]
        if quaternion is not None:
            turn, centre = _product(turn, quaternion), _unrotate(centre, quaternion)
        return [(0, [*centre]), (16, [*turn, *(v[16:19] if own else row_lengths(world) * v[16:19])])]
    n = v[0:3]
    turned = (n[0] * inverse[0, :3] + n[1] * inverse[1, :3]) + n[2] * inverse[2, :3]
    size = np.sqrt((turned[0] * turned[0] + turned[1] * turned[1]) + turned[2] * turned[2])
    turned = turned * (F(1) if size < _TINY else F(1) / size)
    far = point(n * -v[3], inverse)
    return [(0, [*(n if quaternion is not None else turned),
                 -((turned[0] * far[0] + turned[1] * far[1]) + turned[2] * far[2])])]


def _rest(matrices: Sequence, parents: Sequence[int], joint: int) -> np.ndarray:
    m = np.asarray(matrices[joint], F).reshape(4, 4)
    x, y, z, w = matrix_rotation(m)
    x2, y2, z2, one = x + x, y + y, z + z, F(1)
    xx, yy, zz, xy, xz, yz, wx, wy, wz = x * x2, y * y2, z * z2, x * y2, x * z2, y * z2, w * x2, w * y2, w * z2
    own = np.array([[(one - yy) - zz, xy + wz, xz - wy, 0], [xy - wz, (one - xx) - zz, yz + wx, 0],
                    [xz + wy, yz - wx, (one - xx) - yy, 0], [*m[3, :3], 1]], F)
    own[:3] *= np.array([np.sqrt(F(F(F(a * a + b * b) + c * c) + d * d)) for a, b, c, d in m[:3]], F)[:, None]
    return own if parents[joint] < 0 else _times(own, _rest(matrices, parents, parents[joint]))


def rest_position(matrices: Sequence, parents: Sequence[int], joint: int) -> np.ndarray:
    """``matrices`` are parent-relative; rebuilt as the engines do."""
    return _rest(matrices, parents, joint)[3, :3]


COLLIDER_SLOTS = _SLOTS


def collider_slots(values: Mapping[str, object], shapes: Sequence[CollisionShape], tag: int
                   ) -> dict[ShapeKind, tuple[int, int]]:
    if not values["solver"]:
        return {}
    arrays: dict[str, ShapeKind] = {}
    for kind, name in _COLLIDERS.items():
        arrays.setdefault(name, kind)
    own, hit = dict.fromkeys(arrays, 0), dict.fromkeys(arrays, 0)
    if values["collision_target"] != 1:
        for group, kind in _OWN.items():
            own[_COLLIDERS[kind]] += min(len(values[group]), _SLOTS)
    if values["collision_target"] != 0:
        for shape in shapes:
            if shape.tag & tag:
                hit[_COLLIDERS[shape.kind]] += 1
    return {kind: (own[name], hit[name]) for name, kind in arrays.items()}


def _words(value) -> bytes:
    if isinstance(value, (bool, int, np.integer)):
        return struct.pack("<I", int(value) & 0xffffffff)
    if isinstance(value, (float, np.floating)):
        return struct.pack("<f", value)
    return b"".join(_words(item) for item in value)


class ConstantBlocks:
    """``layouts``: the blocks' shader reflections."""

    def __init__(self, layouts: Mapping):
        self.blocks = {name: bytearray(layout.byte_size) for name, layout in layouts.items()}
        self._places = {item.name: (name, item.byte_offset, item.byte_size)
                        for name, layout in layouts.items() for item in layout.variables}

    def _read(self, name: str, kind: str = "<f4") -> np.ndarray:
        block, at, size = self._places[name]
        return np.frombuffer(self.blocks[block], kind, size // 4, at).copy()

    def _write(self, name: str, value) -> None:
        block, at, size = self._places[name]
        data = _words(value)
        if len(data) != size:
            raise ValueError(f"the strand constant {name} takes {size} bytes, not {len(data)}")
        self.blocks[block][at:at + size] = data

    def _put(self, name: str, at: int, values: Sequence) -> None:
        block, start, size = self._places[name]
        data = np.asarray(values, F).tobytes()
        if at + len(data) > size:
            raise ValueError(f"the strand constant {name} has no bytes {at} to {at + len(data)}")
        self.blocks[block][start + at:start + at + len(data)] = data

    def words(self, name: str) -> tuple[int, ...]:
        return tuple(map(int, self._read(name, "<u4")))


class PhysicsConstants(ConstantBlocks):
    """The bounds ring is seeded as the engines' constructor does."""

    def __init__(self, sources: ConstantSources, layouts: Mapping):
        super().__init__(layouts)
        self.sources = sources
        self._write("boundingBoxOffset", [0, 1, 2, 0])

    def frame(self, fields: Mapping[str, object], load: Callable[[str], GpuStrandsPreset | None],
              strands: StrandsFile, frame: SetFrame) -> Mapping[str, bytes]:
        """``fields`` by serialized name."""
        sources = self.sources
        values = {name: fields[field] for name, field in sources.arguments.items()}
        made = {name: fields[field] for name, field in sources.copies.items()} | dict(sources.literals)
        if frame.wind is not None:
            air = np.array(made["strandAirVelocity"], F)
            air[:3] += np.asarray(frame.wind, F)[:3]
            made["strandAirVelocity"] = air
        if frame.turn is not None:
            gravity = np.array(made["strandGravity"], np.float64)
            gravity[:3] = np.asarray(frame.turn, np.float64) @ gravity[:3]
            made["strandGravity"] = gravity.astype(F)
        base = load(values["preset"]) if values["preset"] else None
        more = [(load(path) if path else None, weight)
                for path, weight in zip(values["more_presets"], values["more_preset_weights"], strict=True)]
        if base is None:
            made |= {name: F(fields[field]) for name, field in sources.own.items()} | _UNSET
            made |= {name: _radians(made[name]) for name in _ANGLES}
            switches = sum(bool(values[own]) << bit for bit, (_, own) in enumerate(_SWITCHES) if own)
        else:
            made |= self._blended(values, base, more)
            switches = sum(bool(getattr(base, name)) << bit for bit, (name, _) in enumerate(_SWITCHES))
        scale = F(frame.time_scale) if values["time_scaled"] else F(1)
        rate = min(max(F(values["least_rate"]), F(frame.measured_rate) / scale) if values["automatic_rate"]
                   else F(values["target_rate"]) / scale, F(values["most_rate"]))
        settled = frame.simulated_frames >= sources.settle_frames
        made |= self._ruled(values, base, made, switches, strands, frame, settled)
        made |= self._origin(values, frame, rate, settled and not values["warping"])
        made |= self._colliders(values, base, frame, made["strandWM"], made["strandInvWM"], made["worldQuaternion"])
        made |= self._wet(values, frame, made["strandInvWM"], made["worldQuaternion"])
        made |= {"meshUVOffset": frame.mesh_offsets[0], "deformedVertexOffset": frame.mesh_offsets[1]}
        if switches & 3:
            made |= self._grid(values, frame)
        made["DeltaT"] = max(F(0), F(1) / rate)
        if base is not None:
            made |= self._curves(values, base, more, switches)
        else:
            made |= {"modeWeight": 0, "numWeight": 0, "numResponsiveCurve": 0, "numDampingScalingWeight": 0}
        for name, value in made.items():
            self._write(name, value)
        return {name: bytes(block) for name, block in self.blocks.items()}

    @staticmethod
    def _weights(values: dict, more: list) -> tuple:
        more = [(preset, max(F(0), F(weight))) for preset, weight in more if preset is not None]
        more = [(preset, weight) for preset, weight in more if weight]
        first = max(F(0), F(values["preset_weight"])) if more else F(1)
        total = first
        for _, weight in more:
            total = total + weight
        return first, more, F(1) if total == 0 else F(1) / total

    def _blended(self, values: dict, base: GpuStrandsPreset, more: list) -> dict:
        first, more, scale = self._weights(values, more)
        out = {}
        for name, field in _PRESET.items():
            def term(preset, weight):
                value = F(getattr(preset, field))
                return _radians(value) * weight if name in _ANGLES else value * weight
            held = term(base, first)
            for preset, weight in more:
                held = term(preset, weight) + held
            out[name] = scale * held
        return out

    @staticmethod
    def _ruled(values: dict, base: GpuStrandsPreset | None, made: dict, switches: int, strands: StrandsFile,
               frame: SetFrame, settled: bool) -> dict:
        size = _STRAND_SIZES.get(values["strand_size"], 8)
        guided = values["guided"] and values["solver"]
        moving = settled and not values["warping"]
        out = {
            "occlusionMaxDistance": max(F(0), F(values["occlusion_distance"])),
            "strandSize": size,
            "strandCount": strands.guide_count if guided else strands.strand_count,
            "vertexCountLOD0": strands.lod_guide_vertex_counts[frame.first_lod] if guided
            else strands.lods[frame.first_lod].vertex_count,
            # Only the guided programs read these.
            "vertexCount": strands.lod_guide_vertex_counts[frame.lod] if guided
            else strands.lods[frame.lod].vertex_count,
            "renderVertexCountLOD0": strands.lods[frame.first_lod].vertex_count,
            "renderStrandCount": strands.strand_count,
            "renderStrandVertexCount": strands.lods[frame.lod].vertex_count,
            "executeRenderingSegmentCount": frame.segments,
            "secondaryGuideCount": strands.secondary_guide_count,
            "wetClumpTightness": max(F(0), F(values["clump_tightness"])),
            "skinMoveInfluence": F(values["skin_move_influence"]) if moving else F(0),
            "worldVelocityMultiplier": F(values["world_velocity_multiplier"])
            if moving and values["world_velocity_drag"] else F(0),
        }
        wet = values["wet_multipliers"]
        out |= {name: F(values[value]) if wet else F(dry) for name, value, dry in _WET}
        mass = made["strandNodeRadius"] * made["strandNodeRadius"] * made["strandDensity"]
        out["discretizer"] = _DISCRETE if base is not None and mass == 0 else _DISCRETE / mass
        low, high = (np.asarray(corner, F)[:3] for corner in values["local_grid"])
        for bit, kind, width, cells in ((1, "density", "densityCellWidth", "gridDimension"),
                                        (2, "velocity", "velocityCellWidth", "velocityGridDimension")):
            cell = F(getattr(base, f"{kind}_cell_width") if base is not None else values[f"{kind}_cell_width"])
            if switches & bit:
                out[width] = cell
            out[cells] = _cells(low, high, cell) if switches & bit else [0, 0, 0]
        option = values["solver_option"]
        out["subStep"], out["iterationTime"] = values["sub_steps"], values["iterations"]
        if option in _SOLVER or option == 4:
            table = _SOLVER.get(option) or _SOLVER_4[size >= 32]
            out["subStep"], out["iterationTime"] = table[1 + (size >= 16) + (size >= 32)], table[0]
        flag = sum(bit for name, bit in _FLAGS if values[name]) | (switches & 0x60) << 8 | (switches & 0xc) << 9
        if moving and values["adaptive_skin_move"]:
            flag |= 0x8000
        if values["clear_velocity"] or not settled or values["warping"] and values["static_warp"]:
            flag |= 0x10000
        out["updateControlFlag"] = flag | 0x48f * (option != 0)
        return out

    def _origin(self, values: dict, frame: SetFrame, rate: F, moving: bool) -> dict:
        origin, before = np.asarray(frame.origin, F).reshape(4, 4), self._read("strandWM").reshape(4, 4)
        speed = np.array([rate * (origin[3, axis] - before[3, axis]) for axis in range(3)], F)
        limit = rate * F(values["max_linear_speed"])
        gain = [rate * (now - then) for now, then in zip(speed, self._read("worldVelocity"))]
        gain = [-limit if value < -limit else min(value, limit) for value in gain]
        inertia = F(values["world_inertia_multiplier"]) if moving and values["world_inertia"] else None
        ring = self._read("boundingBoxOffset", "<u4").tolist()
        return {
            "strandPrevWM": before, "strandPrevInvWM": self._read("strandInvWM"), "strandWM": origin,
            "strandInvWM": engine_inverse(origin), "worldQuaternion": matrix_rotation(origin), "worldVelocity": speed,
            "worldAcceleration": [value * inertia if inertia is not None else F(0) for value in gain],
            "boundingBoxOffset": [ring[1], ring[2], ring[0], ring[3]],
        }

    def _colliders(self, values: dict, base: GpuStrandsPreset | None, frame: SetFrame, origin: np.ndarray,
                   inverse: np.ndarray, quaternion: np.ndarray) -> dict:
        counts = dict.fromkeys(_COUNTS.values(), 0)
        if not values["solver"]:
            return counts
        turn = quaternion if values["world_quaternion"] else None

        def fill(kind: ShapeKind, parts: list) -> None:
            name = _COLLIDERS[kind]
            for at, part in parts:
                self._put(name, counts[_COUNTS[name]] * (self._places[name][2] // _SLOTS) + at, part)
            counts[_COUNTS[name]] += 1

        if values["collision_target"] != 1:
            for group, kind in _OWN.items():
                fields = self.sources.wrappers[group]
                for wrapper in values[group][:_SLOTS]:
                    if not wrapper[fields.enabled]:
                        fill(kind, _CLEARED[kind])
                        continue
                    matrix = None
                    if wrapper[fields.follow]:
                        joint = frame.own_joint(wrapper[fields.joint])
                        matrix = origin if joint is None else joint
                    shape = placed(kind, wrapper[fields.world], [wrapper[name] for name in fields.local], matrix)
                    fill(kind, _slot(kind, shape, inverse, turn, own=True))
        if values["collision_target"] == 0:
            return counts
        tag = base.collision_tag if base is not None else EVERY_TAG
        for shape in frame.colliders:
            if shape.tag & tag and counts[_COUNTS[_COLLIDERS[shape.kind]]] < _SLOTS:
                fill(shape.kind, _slot(shape.kind, shape.values, inverse, turn))
        return counts

    def _wet(self, values: dict, frame: SetFrame, inverse: np.ndarray, quaternion: np.ndarray) -> dict:
        turn = quaternion if values["world_quaternion"] else None
        cast = frame.wet_colliders[:_WET_SLOTS]
        for slot, (box, wetness, decay) in enumerate(cast):
            for at, part in _slot(ShapeKind.BOX, np.asarray(box, F), inverse, turn):
                self._put("wetColliders", slot * (self._places["wetColliders"][2] // _WET_SLOTS) + at, part)
            self._put("_colliderWetness", slot * 4, [wetness])
            self._put("_colliderWetDecayDistance", slot * 4, [decay])
        return {"wetColliderCount": len(cast), "resetWetness": int(frame.dry_out)}

    def _grid(self, values: dict, frame: SetFrame) -> dict:
        low, high = (np.asarray(corner, F)[:3] for corner in values["local_grid"])
        centre = (low + high) * F(.5)
        half = high - centre
        if frame.anchor is None:
            offset = centre - half
        else:
            world, rest = (np.asarray(position, F) for position in frame.anchor)
            offset = ((world + (centre - rest)) - np.asarray(frame.origin, F).reshape(4, 4)[3, :3]) - half
        return {"prevGridOffset": self._read("gridOffset"), "gridOffset": offset}

    def _curves(self, values: dict, base: GpuStrandsPreset, more: list, switches: int) -> dict:
        first, more, scale = self._weights(values, more)
        presets = [(base, first), *more]
        used = presets if first else presets[1:]
        out = {"modeWeight": (1 + bool(base.use_relative_distance)) * bool(base.use_deformation_weight)}

        def sampled(name: str, end: F) -> list:
            rows = []
            for i in range(_SAMPLES):
                x, held = end * (F(i) / F(_SAMPLES - 1)), F(0)
                for preset, weight in used:
                    value = weight * getattr(preset, name).evaluate(x)
                    held = held + (value * scale if name == "strand_weight_curve" else value)
                rows.append(held)
            return rows

        def ends(name: str) -> F:
            end = F(getattr(base, name).endpoint) if first else F(0)
            for preset, _ in presets[1:]:
                end = max(F(getattr(preset, name).endpoint), end)
            return end

        if out["modeWeight"]:
            out["numWeight"], out["endpoint"] = _SAMPLES, ends("strand_weight_curve")
            rows = sampled("strand_weight_curve", F(1) if base.use_relative_distance else out["endpoint"])
            out["weights"] = [F(0) if F(1) - row < 0 else min(F(1) - row, F(1)) for row in rows]
        for name, bit, count, end, slot in _CURVES:
            out[count] = _SAMPLES * (switches >> bit & 1)
            if out[count]:
                out[end] = ends(name)
                out[slot] = [scale * row for row in sampled(name, out[end])]
        if switches >> 6 & 1:
            out["weightsStaticFriction"] = [
                scale * sum((weight * F(preset.static_friction_weights[i]) for preset, weight in used), F(0))
                for i in range(_SAMPLES)]
        return out


__all__ = ["COLLIDER_SLOTS", "ConstantBlocks", "PhysicsConstants", "SetFrame", "collider_slots", "rest_position"]
