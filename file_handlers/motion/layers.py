"""See docs/internals/motion-editor.md, Motion layers."""

from __future__ import annotations

import struct
from dataclasses import dataclass, replace

lazy import numpy as np

lazy from file_handlers.rsz.rsz_data_types import ObjectData
lazy from file_handlers.rsz.scn_scene_graph import scene_fields

from .pose import Rig, bound
from .rules import jmap_extras, jmap_layout, jmap_mirror, jmap_properties, joint_attributes, layer_enums
from .tree_pose import TreePose, blend_add, blend_layer, layer_base, mirror, mirror_rows

VIA_MOTION = "via.motion.Motion"

# A joint's mask: the components a layer binds on it (1 translation, 2 rotation, 4 scale) and its weight.
JointMask = tuple[int, float]


@dataclass(frozen=True, slots=True)
class MotionLayer:
    add: bool = False
    rate: float = 1.0
    bank: int = 0
    mask: int = 0
    mask_mode: str = "ID"
    motion: int = -1
    private: bool = False
    enabled: bool = True
    stopped: bool = False
    frame: float = 0.0
    speed: float = 1.0


def motion_component(document, item, game: str) -> tuple[tuple[MotionLayer, ...], str] | None:
    component = next((document.components[c] for c in item.components
                      if c in document.components and document.components[c].type_name == VIA_MOTION), None)
    if component is None:
        return None
    names, elements = scene_fields(game), document.rsz_file.parsed_elements
    blend, modes = layer_enums(game)

    def field(fields, meaning):
        name = (names.get(meaning) or "").casefold()
        return next((value for key, value in fields.items() if name and key.casefold() == name), None)

    def number(fields, meaning, default):
        return getattr(field(fields, meaning), "value", default)

    layers = tuple(MotionLayer(mode == "AddBlend", float(number(fields, "layer blend rate", 1.0)),
                               int(number(fields, "layer bank", 0)), int(number(fields, "layer joint mask", 0)),
                               modes.get(int(number(fields, "layer joint mask mode", 0)), "ID"),
                               int(number(fields, "layer motion", -1)), mode == "Private",
                               bool(number(fields, "layer enabled", True)),
                               bool(number(fields, "layer stop update", False)),
                               float(number(fields, "layer frame", 0.0)), float(number(fields, "layer speed", 1.0)))
                   for entry in getattr(field(component.fields, "motion layers"), "values", ())
                   if isinstance(entry, ObjectData) and isinstance(fields := elements.get(entry.value), dict)
                   for mode in (blend.get(int(number(fields, "layer blend mode", 0))),))
    return layers, str(document.resources.get(component.id, {}).get(names.get("joint map"))
                       or number(component.fields, "joint map", "") or "").rstrip("\0")


def joint_mask_groups(data: bytes) -> dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    layout = jmap_layout(struct.unpack_from("<I", data, 0)[0])
    if layout is None:
        return {}
    groups_at, count_at, record = layout
    found = {}
    for group in record.decode_table(data, struct.unpack_from("<Q", data, groups_at)[0],
                                     struct.unpack_from("<H", data, count_at)[0]):
        size = group["count"]
        found[group["id"]] = (np.frombuffer(data, np.uint32, size, group["hashes"]),
                              np.frombuffer(data, np.uint8, size, group["masks"]),
                              np.frombuffer(data, np.float32, size, group["weights"]) if group["weights"]
                              else np.ones(size, np.float32))
    return found


def joint_map_extras(data: bytes) -> tuple[bool, tuple[tuple[int, int], ...]]:
    """``(deform values are the extras' local translation x, ((name hash, AttrFlags), ...))``; see motion-editor.md."""
    layout = jmap_extras(struct.unpack_from("<I", data, 0)[0])
    if layout is None:
        return False, ()
    extras_at, type_at, record = layout
    table = struct.unpack_from("<Q", data, extras_at)[0]
    rows = record.decode_table(data, *struct.unpack_from("<QQ", data, table)) if table else []
    return bool(struct.unpack_from("<I", data, type_at)[0] & 1), tuple((row["hash"], row["extra_type"] & 0xFFFF)
                                                                        for row in rows)


def joint_map_mirror(data: bytes) -> tuple[dict[int, tuple[int, int]], tuple[int, int]] | None:
    """``({name hash: (translation flags, rotation flags)}, a missing joint's flags)``, None without the data; see
    motion-editor.md."""
    layout = jmap_mirror(struct.unpack_from("<I", data, 0)[0])
    at = 0 if layout is None else struct.unpack_from("<Q", data, layout[0])[0]
    if not at:
        return None
    hashes, flags, count = struct.unpack_from("<QQI", data, at)
    end, record = flags + layout[1] * count, layout[1]
    return dict(zip(struct.unpack_from(f"<{count}I", data, hashes), zip(data[flags:end:record],
                                                                       data[flags + 1:end:record]))), \
        (data[at + 20], data[at + 21])


def property_joints(data: bytes, game: str) -> frozenset[int]:
    layout = jmap_properties(struct.unpack_from("<I", data, 0)[0])
    flag = joint_attributes(game).get("PropertyAnimation", 0)
    table = struct.unpack_from("<Q", data, layout[0])[0] if layout is not None and flag else 0
    return frozenset(row["hash"] for row in layout[1].decode_table(data, *struct.unpack_from("<QQ", data, table))
                     if row["flags"] & flag) if table else frozenset()


def layer_joint_masks(groups: dict, layer: MotionLayer) -> dict[int, JointMask] | None:
    by_id = layer.mask_mode == "ID"
    chosen = [groups[layer.mask]] if by_id and layer.mask in groups else [
        groups[bit] for bit in (1 << shift for shift in range(32)) if not by_id and layer.mask & bit and bit in groups]
    if not chosen:
        return None if by_id or not layer.mask else {}
    held = [dict(zip(hashes.tolist(), zip(masks.tolist(), weights.tolist()))) for hashes, masks, weights in chosen]
    found: dict[int, JointMask] = {}
    if layer.mask_mode == "BitFlagOr":
        for group in held:
            for value, (bits, weight) in group.items():
                old = found.get(value, (0, 0.0))
                found[value] = (old[0] | bits, max(old[1], weight))
        return found
    for value in set(held[0]).intersection(*held[1:]):
        bits, weight = 0xFF, 1.0
        for group in held:
            bits, weight = bits & group[value][0], weight * group[value][1]
        found[value] = (bits, weight)
    return found


def joined_masks(first: dict[int, JointMask] | None, second: dict[int, JointMask] | None) -> dict | None:
    if first is None or second is None:
        return second if first is None else first
    return {value: (bits & second[value][0], weight * second[value][1]) for value, (bits, weight) in first.items()
            if value in second}


def mask_weights(hashes, masks: dict[int, JointMask] | None) -> np.ndarray:
    weights = np.ones((len(hashes), 3), np.float32)
    for at, value in enumerate(hashes if masks is not None else ()):
        bits, weight = masks.get(value, (0, 0.0))
        weights[at] = np.array([bits & 1, bits & 2, bits & 4], bool) * np.float32(weight)
    return weights


@dataclass(frozen=True, slots=True, eq=False)
class MotionJoints:
    hashes: tuple[int, ...]
    rest: tuple[np.ndarray, np.ndarray, np.ndarray]
    interpolated: np.ndarray
    root: int
    rig: Rig | None = None
    mirror_table: tuple | None = None

    @classmethod
    def of(cls, rig: Rig, extras: tuple = (), keyed=(), interpolating: int = 0,
           mirror_table: tuple | None = None) -> MotionJoints:
        held, flags = set(rig.hashes), dict(extras)
        added = tuple(dict.fromkeys(value for value in (*flags, *keyed) if value not in held))
        count = len(added)
        rest = tuple(np.concatenate([part, fill]) for part, fill in zip(rig.base, (
            np.zeros((count, 3), np.float32), np.tile(np.float32([0, 0, 0, 1]), (count, 1)),
            np.ones((count, 3), np.float32))))
        interpolated = np.array([False] * len(rig.hashes) + [bool(flags.get(value, 0) & interpolating)
                                                             for value in added], bool)
        return cls((*rig.hashes, *added), rest, interpolated, rig.root, rig, mirror_table)

    def bind(self, hashes: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray]:
        return bound(self.hashes, hashes)

    def mirror(self, pose: TreePose) -> None:
        mirror(pose, *mirror_rows(self.hashes, self.rig, self.mirror_table), self.rest[1])

    def layer_pose(self, sample, binding: tuple[np.ndarray, np.ndarray], weights: np.ndarray) -> TreePose:
        values, keyed = sample
        rows, slots = binding
        pose = TreePose(len(self.hashes))
        for component in range(3):
            pose.values[component][rows] = values[component][slots]
            pose.weights[rows, component] = weights[:, component] * keyed[component][slots]
        return pose

    def stacked(self, layers) -> TreePose:
        out = None
        rows = np.arange(len(self.hashes))
        for layer, pose_of in layers:
            if layer.private:
                continue
            pose = pose_of(out) if layer.enabled and not layer.stopped else None
            if out is None:
                out = TreePose(len(self.hashes))
                if pose is not None and not layer.add:
                    layer_base(out, pose, self.rest, self.interpolated)
                    continue
                for mine, rest in zip(out.values, self.rest):
                    mine[:] = rest
            if pose is None or layer.rate <= 0:
                continue
            if layer.add:
                blend_add(out, pose, np.float32(layer.rate), rows[rows != self.root], list(self.rest))
            else:
                blend_layer(out, pose, layer.rate, rows == self.root, self.interpolated)
        if out is None:
            out = TreePose(len(self.hashes))
            for mine, rest in zip(out.values, self.rest):
                mine[:] = rest
        return out


def posed(rig: Rig, sample, binding: tuple[np.ndarray, np.ndarray], add: bool = False) -> tuple[np.ndarray, ...]:
    """The root is posed too, so an export keeps what the motion keys."""
    joints = replace(MotionJoints.of(rig), root=-1)
    played = joints.layer_pose(sample, binding, np.ones((len(binding[0]), 3), np.float32))
    return tuple(joints.stacked([(MotionLayer(add=add), lambda _below: played)]).values)
