"""Layout: docs/internals/collision-shapes.md."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

import numpy as np

_SHAPE = 0x70
TAG_BITS = 64
EVERY_TAG = (1 << TAG_BITS) - 1


class ShapeKind(IntEnum):
    """``via.character.CollisionShapeType``."""
    SPHERE = 0
    CAPSULE = 1
    BOX = 2
    TAPERED_CAPSULE = 3
    PLANE = 4


@dataclass(frozen=True, slots=True)
class CollisionShape:
    """``joint`` and ``end_joint`` are joint hashes; ``values`` is 20 floats."""
    kind: int
    joint: int
    end_joint: int
    tag: int
    values: np.ndarray
    read_from: tuple[str, int] = ("", -1)


def read_clsp(data: bytes, path: str = "") -> tuple[CollisionShape, ...]:
    if len(data) < 16 or data[:4] != b"CLSP":
        raise ValueError("not a collision shape preset")
    count, start = struct.unpack_from("<IQ", data, 4)
    if start < 16 or start + count * _SHAPE > len(data):
        raise ValueError("a collision shape preset's shapes run past its end")
    shapes = []
    for at in range(start, start + count * _SHAPE, _SHAPE):
        kind, joint, end_joint = struct.unpack_from("<3I", data, at)
        values = np.frombuffer(data, np.float32, 20, at + 0x20).copy()
        shapes.append(CollisionShape(kind, joint, end_joint, struct.unpack_from("<Q", data, at + 0x10)[0], values,
                                     (path, len(shapes))))
    return tuple(shapes)


def write_clsp(shapes) -> bytes:
    out = bytearray(struct.pack("<4sIQ", b"CLSP", len(shapes), 16))
    for shape in shapes:
        out += struct.pack("<3I4xQ8x", shape.kind, shape.joint, shape.end_joint, shape.tag)
        out += np.asarray(shape.values, "<f4").reshape(20).tobytes()
    return out.take_bytes()


__all__ = ["CollisionShape", "ShapeKind", "read_clsp", "write_clsp"]
