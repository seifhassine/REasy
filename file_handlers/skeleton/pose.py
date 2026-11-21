"""See docs/internals/skeleton-editor.md, Placement."""
from __future__ import annotations

import functools
import math

import numpy as np

from file_handlers.engine_math import joint_descriptor, make_trs_matrix, transform_product

lazy from file_handlers.layout_codec import load_table


@functools.cache
def rules() -> dict:
    return load_table("resources/data/skeletons", "editor", ValueError)


def model_matrices(joints) -> np.ndarray:
    rows, out = {id(joint): row for row, joint in enumerate(joints)}, np.empty((len(joints), 4, 4), np.float32)
    for row, joint in enumerate(joints):
        local = make_trs_matrix(joint.position, joint.rotation, joint.scale)
        out[row] = local if joint.parent is None else transform_product(out[rows[id(joint.parent)]], local)
    return out


def mirrored(matrix: np.ndarray) -> np.ndarray:
    normal, flip = np.array(rules()["mirror"]["normal"]), np.eye(4)
    flip[:3, :3] -= 2.0 * np.outer(normal, normal)
    return flip @ matrix @ flip


def euler_degrees(rotation) -> tuple[float, float, float]:
    x, y, z, w = np.divide(rotation, math.hypot(*rotation))
    sine = min(max(2.0 * (w * x - y * z), -1.0), 1.0)
    if abs(sine) > 1.0 - rules()["angles"]["pole"]:
        return (math.degrees(math.copysign(math.pi / 2, sine)), 0.0, math.degrees(math.copysign(1.0, sine) * math.atan2(
            2.0 * (x * z - w * y), 1.0 - 2.0 * (y * y + z * z))))
    return (math.degrees(math.asin(sine)), math.degrees(math.atan2(2.0 * (x * z + w * y), 1.0 - 2.0 * (x * x + y * y))),
            math.degrees(math.atan2(2.0 * (x * y + w * z), 1.0 - 2.0 * (x * x + z * z))))


def local_fields(world: np.ndarray, base: np.ndarray, stored: dict) -> dict:
    position, rotation, scale = joint_descriptor(np.linalg.solve(base, world).T)
    if np.dot(rotation, stored.get("rotation", rotation)) < 0:
        rotation = tuple(-value for value in rotation)
    found, kept = {"position": position, "rotation": rotation, "scale": scale}, rules()["kept"]
    return {name: found[name] if np.abs(np.subtract(found[name], value)).max() > kept else value
            for name, value in stored.items()}
