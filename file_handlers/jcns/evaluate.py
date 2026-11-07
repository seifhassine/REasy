"""See docs/internals/jcns-evaluation.md."""

from __future__ import annotations

from dataclasses import dataclass, field

import fastjcns
lazy from utils.hash_util import murmur3_hash_utf16le

from .model import JointConstraints, Record
from .rig import Rig
from .rules import Enums, evaluation_rules, rules_for


@dataclass(slots=True)
class Results:
    channels: list[int] | None = None
    weights: list | None = None
    materials: fastjcns.Materials | None = None
    outputs: list = field(default_factory=list)
    weights_set: bool = False


class MeshMaterials:
    def __init__(self, surfaces):
        self.names: dict[int, tuple[str, dict]] = {}
        variables = []
        for surface in surfaces:
            material = murmur3_hash_utf16le(surface.material_name)
            if material in self.names:
                continue
            parameters = self.names[material] = (surface.material_name, {})
            for parameter in surface.parameters:
                variable = murmur3_hash_utf16le(parameter.name)
                parameters[1].setdefault(variable, parameter)
                variables.append((material, variable, len(parameter.values), (*parameter.values, 0.0, 0.0, 0.0)[:4],
                                  parameter.locked))
        self.table = fastjcns.Materials(variables)

    def drawn(self) -> dict[str, dict[str, float | tuple[float, ...]]]:
        drawn: dict[str, dict] = {}
        for (material, variable), (_count, value) in self.table.variables.items():
            name, parameters = self.names[material]
            if value[:len(parameters[variable].values)] != tuple(parameters[variable].values):
                drawn.setdefault(name, {})[parameters[variable].name] = value
        for material, variable, vector, scalar in self.table.params:
            name, parameters = self.names.get(material, ("", {}))
            parameter = parameters.get(variable)
            if parameter is not None and not parameter.locked and len(parameter.values) in (1, 4):
                drawn.setdefault(name, {})[parameter.name] = scalar if len(parameter.values) == 1 else vector
        return drawn


class Evaluation:
    def __init__(self, model: JointConstraints, rig: Rig, enums: Enums):
        self.model, self.rig = model, rig
        self.program = program(model, enums)

    @property
    def cone_values(self) -> list[float]:
        return self.program.cone_values

    def run(self, lod: int = 0, blend: float = 1.0, results: Results | None = None,
            kinds: tuple[str, ...] | None = None) -> Results:
        results = results or Results()
        outputs, weights, results.weights_set = self.program.run(
            self.rig, lod, blend, results.channels, results.weights, results.weights_set, kinds, results.materials)
        if outputs is not None:
            results.outputs = outputs
        if results.weights is not None:
            results.weights[:] = weights
        return results


def program(model: JointConstraints, enums: Enums) -> fastjcns.Program:
    return fastjcns.Program(evaluation_rules(model.version, enums.game), *_RecordPacker(model, enums).records())


def _links(record: Record, name: str):
    return record.links.get(name) or ()


def _ref(values: dict, name: str) -> tuple:
    return (False, values[name + "_hash"]) if name + "_hash" in values else (True, values[name + "_index"])


def _by_index(values: dict, name: str) -> tuple:
    return True, values[name + "_index"]


class _RecordPacker:
    def __init__(self, model: JointConstraints, enums: Enums):
        self.root, self.layouts = model.root, rules_for(model.version).records
        self.enums, self.cache = enums, {}
        self.vector_axis = self.members("cone_input", "axis").get("Vector")
        self.targets = self.root.links.get("rotation_target_joints")
        self.sources = self.root.links.get("rotation_from_joints")
        self.table = "multi_parents" in self.root.links
        self.multi_ref = _by_index if self.table else _ref

    def members(self, kind: str, name: str) -> dict[str, int]:
        if (kind, name) not in self.cache:
            self.cache[kind, name] = self.enums.of_field(self.layouts[kind], name) if kind in self.layouts else {}
        return self.cache[kind, name]

    def code(self, kind: str, name: str, category: str, value) -> int:
        names = fastjcns.NAMES[category]
        return next((names.index(member) for member, number in self.members(kind, name).items()
                     if number == value and member in names), -1)

    def bits(self, kind: str, name: str, category: str, value: int) -> int:
        names = fastjcns.NAMES[category]
        return sum(1 << names.index(member) for member, mask in self.members(kind, name).items()
                   if member in names and value & mask)

    def curve(self, kind: str, record: Record) -> tuple:
        d, keys = record.values, record.links.get("keyframes")
        return (self.code(kind, "curve_pre_infinity", "infinity", d.get("curve_pre_infinity")),
                self.code(kind, "curve_post_infinity", "infinity", d.get("curve_post_infinity")),
                None if keys is None else [
                    (*(key.values[name] for name in ("frame", "value", "ix", "iy", "ox", "oy")),
                     self.code("keyframe", "out_tangent_type", "tangent", key.values["out_tangent_type"]))
                    for key in keys])

    def cone_driver(self, record: Record) -> tuple:
        d = record.values
        curve_type = (self.code("cone_driver", "curve_type", "curve", d["curve_type"]) if "curve_type" in d
                      else fastjcns.NAMES["curve"].index("MinMax"))
        return (d["input_cone_no"], d["out_min"], d["out_max"],
                self.code("cone_driver", "interpolation", "interpolation", d["interpolation"]), curve_type,
                *self.curve("cone_driver", record))

    def joint_driver(self, record: Record) -> tuple:
        d = record.values
        return (_ref(d, "joint_name"), self.bits("joint_driver", "flags", "driver_flag", d["flags"]),
                *(self.code("joint_driver", name, category, d.get(name)) for name, category in (
                    ("input_type", "input"), ("axis", "axis"), ("order", "order"), ("interpolation", "interpolation"),
                    ("curve_type", "curve"))),
                *(d[name] for name in ("in_min", "in_mid", "in_max", "out_min", "out_mid", "out_max")),
                d["input_offset_rot"], *self.curve("joint_driver", record))

    def cone(self, record: Record) -> tuple:
        v = record.values
        return (_ref(v, "joint_name"), _ref(v, "parent_name"),
                _ref(v, "aim_name") if "aim_name_index" in v else (False, 0), v.get("parent_name_index", -1),
                v["axis"], v.get("up", -1), v["axis"] == self.vector_axis, "up" in v and v["up"] == self.vector_axis,
                bool(self.bits("cone_input", "flags", "cone_flag", v["flags"])), v["cone_half_angle_rad"],
                v.get("scale", 0.0), v["offset_rot"],
                *(v.get(name, (0.0, 0.0, 0.0)) for name in ("cone_vec", "target_vec", "up_vec")))

    def output(self, record: Record) -> tuple:
        v, names = record.values, (record.links.get("joint_name"), record.links.get("variable_name"))
        return ((True, v["joint_name_index"]) if "joint_name_index" in v else (False, v["joint_name_hash"]),
                v["joint_name_hash"], v.get("variable_name_hash", 0),
                *(self.code("output", name, category, v[name]) for name, category in (
                    ("output_type", "element"), ("mode", "mode"), ("axis", "axis"), ("order", "order"))),
                v["lod"], self.bits("output", "flags", "output_flag", v["flags"]),
                min(4, v["out_pack_num"] + 1) if "out_pack_num" in v else 1,
                v.get("min_value", 0.0), v.get("max_value", 0.0), v["output_offset_rot"],
                [self.cone_driver(driver) for driver in _links(record, "cone_drivers")],
                [self.joint_driver(driver) for driver in _links(record, "joint_drivers")],
                None if None in names else tuple(murmur3_hash_utf16le(name.value) for name in names))

    def expression(self, number: int, record: Record, lod: Record) -> tuple:
        v = record.values
        joints = (((False, v["target_joint_name_hash"]), (False, v["from_joint_name_hash"])) if self.targets is None
                  else ((True, self.targets[number].values["index"]), (True, self.sources[number].values["index"])))
        return (*joints, lod.values["lod"], *(v[name] for name in (
            "in_base_quat", "out_base_quat", "x", "y", "z", "in_base", "out_base", "inverse", "mirroring")))

    def multi(self, record: Record) -> tuple:
        v = record.values
        return (self.multi_ref(v, "joint_name"), v.get("lod", 0),
                self.code("multi_constraint", "property", "property", v.get("property")),
                bool(v.get("maintain_offset")),
                [(self.multi_ref(weight.values, "parent_name"), weight.values["weight"])
                 for weight in _links(record, "weights")])

    def aim(self, record: Record) -> tuple:
        v = record.values
        return (_ref(v, "joint_name"), _ref(v, "world_up_object_name"), v.get("lod", 0x7FFFFFFF),
                self.code("aim_constraint", "world_up_type", "world_up", v["world_up_type"]),
                self.code("aim_constraint", "rotate_offset_order", "order", v["rotate_offset_order"]),
                v["aim_vector"], v["up_vector"], v["world_up_vector"], v["rotate_offset"],
                [(_ref(target.values, "joint_name"), target.values["weight"]) for target in _links(record, "targets")])

    def material(self, record: Record) -> tuple:
        v = record.values
        return (_ref(v, "joint_name"), v["material_name_hash"], v["variable_name_hash"],
                self.code("material_constraint", "mode", "apply", v["mode"]),
                self.code("material_constraint", "order", "order", v["order"]), bool(v["world"]))

    def records(self) -> tuple:
        cones = _links(self.root, "cone_inputs")
        return ((bool(self.root.values.get("legacy_version")),
                 bool(self.root.values.get("legacy_multi_constraint_init")), self.table,
                 bool(cones) and "cone_vec" in cones[0].values,
                 bool(self.root.values.get("legacy_material_param"))),
                [entry.values["hash"] for entry in _links(self.root, "name_hashes")],
                [self.cone(record) for record in cones],
                [self.output(record) for record in _links(self.root, "outputs")],
                [self.expression(number, record, lod) for number, (record, lod) in
                 enumerate(zip(_links(self.root, "rotations"), _links(self.root, "lods")))],
                [self.multi(record) for record in _links(self.root, "multi_constraints")],
                [(entry.values["joint_name_index"], entry.values["lod"])
                 for entry in _links(self.root, "multi_parents")],
                [entry.values["hash"] for entry in _links(self.root, "multi_joint_hashes")],
                [self.aim(record) for record in _links(self.root, "aim_constraints")],
                [self.material(record) for record in _links(self.root, "material_constraints")],
                [(group.values["normalize_top_num"], [entry.values["hash"] for entry in _links(group, "hashes")])
                 for group in _links(self.root, "blend_shape_groups")],
                [self.code("constraint_kind", "kind", "kind", entry.values["kind"])
                 for entry in _links(self.root, "constraint_order")])
