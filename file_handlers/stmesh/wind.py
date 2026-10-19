"""See docs/internals/speedtree.md, Wind."""
from __future__ import annotations

import struct
from dataclasses import dataclass, fields

CURVE = 20
SIZE = 0x3d8


def _floats(data: bytes, at: int, count: int) -> tuple[float, ...]:
    return struct.unpack_from(f"<{count}f", data, at)


@dataclass(frozen=True, slots=True)
class BranchWindLevel:
    frequency: tuple[float, ...]
    bend: tuple[float, ...]
    turbulence: tuple[float, ...]
    flexibility: tuple[float, ...]
    turbulence_independence: float

    @classmethod
    def read(cls, data: bytes, at: int) -> "BranchWindLevel":
        curves = [_floats(data, at + 4 * CURVE * index, CURVE) for index in range(4)]
        return cls(*curves, _floats(data, at + 16 * CURVE, 1)[0])

    def pack(self) -> bytes:
        return struct.pack(f"<{4 * CURVE + 1}f", *self.frequency, *self.bend, *self.turbulence, *self.flexibility,
                           self.turbulence_independence)


@dataclass(frozen=True, slots=True)
class RippleGroup:
    bend: tuple[float, ...]
    turbulence_frequency: tuple[float, ...]
    turbulence: tuple[float, ...]
    turbulence_independence: float

    @classmethod
    def read(cls, data: bytes, at: int) -> "RippleGroup":
        curves = [_floats(data, at + 4 * CURVE * index, CURVE) for index in range(3)]
        return cls(*curves, _floats(data, at + 12 * CURVE, 1)[0])

    def pack(self) -> bytes:
        return struct.pack(f"<{3 * CURVE + 1}f", *self.bend, *self.turbulence_frequency, *self.turbulence,
                           self.turbulence_independence)


@dataclass(frozen=True, slots=True)
class WindConfig:
    """``SConfigSdk`` (its ``SConfigCommon`` base first)."""
    strength_response: float
    direction_response: float
    gust_frequency: float
    gust_strength_min: float
    gust_strength_max: float
    gust_duration_min: float
    gust_duration_max: float
    gust_rise_scalar: float
    gust_fall_scalar: float
    global_height: float
    full_branch_move: float
    full_ripple: float
    bounding_box_min: tuple[float, float, float]
    bounding_box_max: tuple[float, float, float]
    branch_stiffness_amount: float
    branch_stiffness_up: float
    branch_stretch: float
    shimmer: float
    shared: BranchWindLevel
    branch: BranchWindLevel
    ripple: RippleGroup
    is_grass: float

    @classmethod
    def read(cls, data: bytes) -> "WindConfig":
        if len(data) != SIZE:
            raise ValueError(f"a SpeedTree wind config is {SIZE:#x} bytes, not {len(data):#x}")
        head = _floats(data, 0, 12)
        return cls(*head, _floats(data, 0x30, 3), _floats(data, 0x3c, 3), *_floats(data, 0x48, 4),
                   BranchWindLevel.read(data, 0x58), BranchWindLevel.read(data, 0x19c), RippleGroup.read(data, 0x2e0),
                   _floats(data, 0x3d4, 1)[0])

    def pack(self) -> bytes:
        values = [getattr(self, field.name) for field in fields(self)]
        head = struct.pack("<12f", *values[:12])
        boxes = struct.pack("<6f", *self.bounding_box_min, *self.bounding_box_max)
        stiffness = struct.pack("<4f", self.branch_stiffness_amount, self.branch_stiffness_up, self.branch_stretch,
                                self.shimmer)
        return (head + boxes + stiffness + self.shared.pack() + self.branch.pack() + self.ripple.pack()
                + struct.pack("<f", self.is_grass))


STATE_SIZE = 0xd0


def _normalized(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = (vector[0] ** 2 + vector[1] ** 2 + vector[2] ** 2) ** 0.5
    return (0.0, 0.0, 0.0) if length == 0 else tuple(component / length for component in vector)


def _cross(a, b) -> tuple[float, float, float]:
    return a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]


def rest_state(config: WindConfig, direction: tuple[float, float, float] = (1.0, 0.0, 0.0)) -> bytes:
    """An ``SWindStateSdk``, as the engine uploads it with no wind."""
    height = config.global_height
    up = _normalized(_cross(_normalized(_cross(direction, (0.0, 0.0, 1.0))), direction))
    right = _normalized(_cross(direction, (0.0, 0.0, 1.0)))

    def level(source: BranchWindLevel) -> bytes:
        return struct.pack("<8f", 0.0, 0.0, 0.0, source.turbulence_independence, source.bend[0], source.turbulence[0],
                           source.flexibility[0], 0.0)

    ripple_independence = (config.ripple.turbulence_independence / height if height > 0
                           else config.ripple.turbulence_independence * 0.05)
    return (struct.pack("<3ff", *direction, 0.0)
            + struct.pack("<4f", 1.0 / height if height != 0 else 1.0, config.full_branch_move, config.full_ripple, 0.0)
            + struct.pack("<3ff3ff", *config.bounding_box_min, config.branch_stiffness_amount, *config.bounding_box_max,
                          config.branch_stiffness_up)
            + level(config.shared) + level(config.branch)
            + struct.pack("<8f", 0.0, 0.0, 0.0, ripple_independence, config.ripple.bend[0],
                          config.ripple.turbulence[0], 0.0, 0.0)
            + struct.pack("<3ff3ff3ff", *direction, config.branch_stretch, *right, config.shimmer, *up,
                          config.is_grass))


__all__ = ["BranchWindLevel", "RippleGroup", "STATE_SIZE", "WindConfig", "rest_state"]
