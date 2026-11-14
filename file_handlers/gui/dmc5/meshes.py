"""See docs/internals/gui-rendering.md, Meshes."""
from __future__ import annotations

from functools import cache

import numpy as np

lazy from file_handlers.engine_math import (affine_inverse, crt, make_trs_matrix, normalized_row, quaternion_product,
                                            transform_product)
lazy from file_handlers.mesh.mesh_file import VertexBufferType


def _turn(angles) -> np.ndarray:
    (sx, sy, sz), (cx, cy, cz) = ([np.float32(crt(name)(float(angle))) for angle in angles]
                                  for name in ("sinf", "cosf"))
    return np.array([[cz * cy, cy * sz, -sy, 0], [cz * sy * sx - sz * cx, cz * cx + sz * sy * sx, cy * sx, 0],
                     [cz * sy * cx + sz * sx, sz * sy * cx - cz * sx, cy * cx, 0], [0, 0, 0, 1]], np.float32)


@cache
def entity_world(holder: tuple, rotation: tuple) -> np.ndarray:
    f = np.float32
    holder, zero = f(holder).reshape(4, 4), f(0)
    inverse = affine_inverse(holder)
    position = tuple(float(value) for value in (((inverse[1] * zero + inverse[0] * zero) + inverse[2] * zero)
                                                + inverse[3])[:3])
    x, y, z, w = (f(value) for value in quaternion_product((0.0, 0.0, 0.0, 1.0), (
        -rotation[0], -rotation[1], -rotation[2], rotation[3])))
    unit = f(1) / np.sqrt(((y * y + x * x) + z * z) + w * w)
    turned, scale = tuple(float(value * unit) for value in (x, y, z, w)), []
    for row in transform_product(make_trs_matrix(position, turned, (1.0, 1.0, 1.0)).T, holder)[:3]:
        part = normalized_row(row)
        out = (inverse[3] * part[3] + inverse[2] * part[2]) + (inverse[1] * part[1] + inverse[0] * part[0])
        scale.append(float(np.sqrt(((out[0] * out[0] + out[1] * out[1]) + out[2] * out[2]) + out[3] * out[3])))
    return transform_product(make_trs_matrix(position, turned, tuple(scale)).T, holder)


def mesh_world(node, names, rule, degree_factors, view: np.ndarray, entity: np.ndarray) -> np.ndarray:
    held = node.render_properties
    position, scale = (np.float32(held[names[key]]) for key in ("position", "scale"))
    turn = _turn(np.float32(held[names["rotation"]]) * np.float32(degree_factors[0]) * np.float32(degree_factors[1]))
    turn[3] = -position[0], position[1], -position[2], 1
    whole = transform_product(transform_product(turn, _turn(np.float32(rule["turn"]))), transform_product(
        np.float32(node.parent.world_matrix).reshape(4, 4), view))
    whole[:3] *= (np.float32(rule["scale"]) * scale)[:, None]
    return transform_product(whole, entity)


def listed_lights(lights, rule) -> tuple[list[dict], dict[int, int]]:
    rows, masks, one = [], {}, np.float32(1)
    for light in reversed(lights):
        radius, intensity, rough = (np.float32(light[name]) for name in ("Radius", "Intensity", "MinRoughness"))
        reach = (np.sqrt(intensity / np.float32(light["IlluminanceThreshold"])) - one) * radius * np.float32(
            rule["range_scale"])
        if not (light["Enabled"] and light["Draw"] and reach > 0):
            continue
        inverse = one / max(radius, np.float32(rule["least_radius"]))
        masks[light["LightGroup"]] = masks.get(light["LightGroup"], 0) | 1 << len(rows)
        rows.append({"position": light["Position"], "boundingRadius": [reach],
                     "attenuation": [inverse, rough * rough, 1, 0], "color": intensity * np.float32(light["Color"]),
                     "tolerance": [min(one / (inverse * reach * inverse * reach),
                                       np.float32(rule["largest_tolerance"]))],
                     "shadowIndex": [rule["shadow"]], "iesId": [rule["ies"]], "reserved": rule["reserved"]})
    return rows, masks


def _front(corners, edges, near: float) -> list[tuple[float, float]]:
    flat = sorted({(point[0] / point[3], point[1] / point[3]) for point in [
        corner for corner in corners if corner[3] >= near] + [
        a + (b - a) * ((near - a[3]) / (b[3] - a[3])) for a, b in edges if (a[3] < near) != (b[3] < near)]})

    def chain(row):
        kept = []
        for point in row:
            while len(kept) > 1 and ((kept[-1][0] - kept[-2][0]) * (point[1] - kept[-2][1])
                                     <= (kept[-1][1] - kept[-2][1]) * (point[0] - kept[-2][0])):
                kept.pop()
            kept.append(point)
        return kept[:-1]

    return chain(flat) + chain(flat[::-1])


def box_outline(mesh, shown: np.ndarray, near: float) -> list[tuple[float, float]]:
    low, high = (side([held.bounding_box[at] for held in mesh.meshes], 0) for at, side in enumerate((np.min, np.max)))
    corners = np.array([[x, y, z, 1.0] for x in (low[0], high[0]) for y in (low[1], high[1])
                        for z in (low[2], high[2])]) @ shown
    return _front(corners, [(corners[at], corners[at | bit]) for at in range(8) for bit in (1, 2, 4) if not at & bit],
                  near)


def mesh_triangles(mesh) -> np.ndarray:
    rows = []
    for _name, part in mesh_parts(mesh):
        held = mesh.mesh_buffer.buffer_payloads[part.buffer_index]
        data, stride = next((bytes(stream.data), int(stream.stride)) for stream in held.attribute_streams
                            if stream.type == VertexBufferType.Position)
        faces = np.frombuffer(bytes(held.face_bytes), np.uint16 if held.integer_faces is None else np.uint32)[
            part.faces_index_offset:part.faces_index_offset + part.indices_count].astype(np.int64)
        rows.append(np.frombuffer(data, np.float32).reshape(-1, stride // 4)[faces + part.verts_index_offset, :3])
    return np.concatenate(rows).reshape(-1, 3, 3)


def _inside(shapes: np.ndarray, x: float, y: float) -> bool:
    a = shapes - (x, y)
    b = np.roll(a, -1, 1)
    turns = a[:, :, 0] * b[:, :, 1] - a[:, :, 1] * b[:, :, 0]
    return bool(((turns > 0).all(1) | (turns < 0).all(1)).any())


def covers(triangles: np.ndarray, shown: np.ndarray, near: float, x: float, y: float) -> bool:
    clip = np.concatenate([triangles, np.ones((*triangles.shape[:2], 1))], 2) @ shown
    front = clip[:, :, 3] >= near
    whole = clip[front.all(1)]
    return _inside(whole[:, :, :2] / whole[:, :, 3:], x, y) or any(
        _inside(np.array([_front(row, zip(row, np.roll(row, -1, 0)), near)]), x, y)
        for row in clip[front.any(1) & ~front.all(1)])


def mesh_parts(mesh) -> list[tuple[str, object]]:
    return [(mesh.material_names[submesh.material_index], submesh) for held in mesh.meshes
            for group in held.lods[0].mesh_groups for submesh in group.submeshes]
