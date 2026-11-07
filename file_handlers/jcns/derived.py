"""See docs/internals/jcns-format.md, "Derived tables"."""

from __future__ import annotations

from functools import cached_property
from itertools import groupby

lazy from utils.hash_util import murmur3_hash_ascii, murmur3_hash_utf16le

from .model import JointConstraints, Record, Text
from .rules import Enums, new_record, rules_for

Assignment = tuple[Record, str, object]


def derived_changes(model: JointConstraints, enums: Enums) -> dict[str, list[Assignment]]:
    """Per derived table of the model's version: the (record, field or link, value) assignments that make it what the
    records mean; none when the model already stores it so."""
    return _Derivation(model, enums).tables()


def same(a, b) -> bool:
    if isinstance(a, list) or isinstance(b, list):
        a, b = a or [], b or []
        return isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and all(map(same, a, b))
    if isinstance(a, Record) and isinstance(b, Record):
        return a.kind == b.kind and a.values == b.values and a.links.keys() == b.links.keys() and all(
            same(link, b.links[name]) for name, link in a.links.items())
    if isinstance(a, Text) and isinstance(b, Text):
        return a.value == b.value
    return a == b


class Joints:
    def __init__(self, root: Record):
        self.names = [row.values["hash"] for row in rows(root, "name_hashes")]
        self.parents = rows(root, "multi_parents") if "multi_parents" in root.links else None

    def named(self, index: int) -> int | None:
        return self.names[index] if 0 <= index < len(self.names) else None

    def of(self, record: Record, prefix: str = "joint_name") -> int | None:
        values = record.values
        if prefix + "_hash" in values:
            return values[prefix + "_hash"]
        if record.kind == "constraint_weight" and self.parents is not None:
            slot = values["parent_name_index"]
            return self.named(self.parents[slot].values["joint_name_index"]) if 0 <= slot < len(self.parents) else None
        return self.named(values.get(prefix + "_index", -1))


def rows(record: Record, name: str) -> list[Record]:
    return record.links.get(name) or []


def walk(record: Record):
    yield record
    for link in record.links.values():
        if isinstance(link, list):
            for child in link:
                yield from walk(child)


class _Derivation:
    def __init__(self, model: JointConstraints, enums: Enums):
        rules = rules_for(model.version)
        self.version, self.root, self.rules, self.layouts = model.version, model.root, rules.derived, rules.records
        self.enums = enums
        self.joints, self.cones = Joints(self.root), rows(self.root, "cone_inputs")
        self.found: list[Assignment] = []

    def members(self, kind: str, field: str, wanted) -> dict[str, int]:
        found = self.enums.of_field(self.layouts[kind], field)
        return {member: found[member] for member in wanted if member in found}

    def assign(self, record: Record, name: str, value) -> None:
        if not same(record.links.get(name) if name in record.links else record.values.get(name), value):
            self.found.append((record, name, value))

    def row(self, layout: str, **values) -> Record:
        record = new_record(self.version, layout)
        record.values.update(values)
        return record

    def tables(self) -> dict[str, list[Assignment]]:
        links = {link.name: link for link in self.layouts[self.root.kind].links}
        steps = {"string_hashes": self.string_hashes}
        if "constraint_order" in links:
            steps["constraint_order"] = lambda: self.constraint_kinds(links["constraint_order"].fixed)
        if "pack_key" in self.rules:
            steps["packs"] = self.packs
        if "output_target_infos" in links:
            steps["output_target_infos"] = self.target_infos
        if "name_hashes" in links:
            steps.update(name_hashes=self.name_table, rotation_joints=self.rotation_joints,
                         multi_parents=self.multi_parents)
        tables = {}
        for name, step in steps.items():
            self.found = []
            step()
            tables[name] = self.found
        return tables

    def string_hashes(self) -> None:
        ascii_types = set(self.members("output", "output_type", self.rules["ascii_name_outputs"]).values())
        for record in walk(self.root):
            hashed = murmur3_hash_ascii if record.values.get("output_type") in ascii_types else murmur3_hash_utf16le
            for name, link in record.links.items():
                if name + "_hash" in record.values:
                    self.assign(record, name + "_hash", hashed(link.value) if link else 0)

    def constraint_kinds(self, fixed: int) -> None:
        tables = self.rules["constraint_tables"]
        kinds = self.members("constraint_kind", "kind", tables)
        wanted = {kind for member, kind in kinds.items()
                  if (tables[member] in self.root.links if fixed else rows(self.root, tables[member]))}
        order = [kind for kind in dict.fromkeys(row.values["kind"] for row in rows(self.root, "constraint_order"))
                 if kind in wanted]
        for kind in sorted(wanted - set(order)):
            order.insert(next((at for at, listed in enumerate(order) if listed > kind), len(order)), kind)
        self.assign(self.root, "constraint_order", [self.row("constraint_kind", kind=kind) for kind in order])

    def packs(self) -> None:
        outputs = rows(self.root, "outputs")
        free = sum(self.members("output", "flags", self.rules["pack_free_flags"]).values())

        def key(output: Record) -> tuple:
            return *(output.values[name] for name in self.rules["pack_key"]), output.values["flags"] & ~free

        sizes = [len(list(run)) for _, run in groupby(outputs, key)]
        for output, number in zip(outputs, (0 if at else size - 1 for size in sizes for at in range(size))):
            self.assign(output, "pack_flags", 0)
            self.assign(output, "out_pack_num", number)

    def target_infos(self) -> None:
        names = self.rules["target_names"]
        separators = {kind: names[member] for member, kind in self.members("output", "output_type", names).items()}
        infos = {}
        for output in rows(self.root, "outputs"):
            separator = separators.get(output.values["output_type"])
            target = output.values["joint_name_hash"] if separator is None else murmur3_hash_utf16le(
                output.links["joint_name"].value + separator + output.links["variable_name"].value)
            drivers = self.driver_cones(output) + rows(output, "joint_drivers")
            infos.setdefault(target, {}).update(dict.fromkeys(map(self.joints.of, drivers)))
        self.assign(self.root, "output_target_infos", [Record("output_target_info", {}, {"hashes": [
            self.row("name_hash", hash=value) for value in (target, *drivers)]}) for target, drivers in infos.items()])

    def driver_cones(self, output: Record) -> list[Record]:
        numbers = (driver.values["input_cone_no"] for driver in rows(output, "cone_drivers"))
        return [self.cones[number] for number in numbers if number < len(self.cones)]

    def uses(self):
        jointed = set(self.members("output", "output_type", self.rules["joint_outputs"]).values())

        def use(record: Record, prefix: str = "joint_name") -> tuple:
            return record, prefix + "_index", self.joints.of(record, prefix)

        def cone(record: Record):
            return (use(record, prefix) for prefix in ("joint_name", "parent_name", "aim_name"))

        for output in rows(self.root, "outputs"):
            yield output, "joint_name_index", (output.values["joint_name_hash"]
                                               if output.values["output_type"] in jointed else None)
            yield from map(use, rows(output, "joint_drivers"))
            for record in self.driver_cones(output):
                yield from cone(record)
        for record in self.cones:
            yield from cone(record)
        for rotation in rows(self.root, "rotations"):
            yield None, None, rotation.values["from_joint_name_hash"]
            yield None, None, rotation.values["target_joint_name_hash"]
        for multi in rows(self.root, "multi_constraints"):
            yield use(multi)
            yield from ((None, None, self.joints.of(weight, "parent_name")) for weight in rows(multi, "weights"))
        yield from map(use, rows(self.root, "multi_parents"))
        for aim in rows(self.root, "aim_constraints"):
            yield use(aim)
            yield use(aim, "world_up_object_name")
            yield from map(use, rows(aim, "targets"))
        yield from map(use, rows(self.root, "material_constraints"))

    @cached_property
    def used(self) -> tuple[list[tuple], dict[int, int]]:
        uses = list(self.uses())
        return uses, {joint: at for at, joint in enumerate(dict.fromkeys(
            joint for _, _, joint in uses if joint is not None))}

    def name_table(self) -> None:
        uses, at = self.used
        self.assign(self.root, "name_hashes", [self.row("name_hash", hash=joint) for joint in at])
        for record, field, joint in uses:
            if record is not None:
                self.assign(record, field, at.get(joint, -1))

    def rotation_joints(self) -> None:
        at = self.used[1]
        for name, field in (("rotation_from_joints", "from_joint_name_hash"),
                            ("rotation_target_joints", "target_joint_name_hash")):
            self.assign(self.root, name, [self.row("name_index", index=at[rotation.values[field]])
                                          for rotation in rows(self.root, "rotations")])

    def multi_parents(self) -> None:
        weights = [(multi.values["lod"], weight) for multi in rows(self.root, "multi_constraints")
                   for weight in rows(multi, "weights")]
        joints = [self.joints.of(weight, "parent_name") for _, weight in weights]
        lods: dict[int | None, int] = {}
        for (lod, _), joint in zip(weights, joints):
            lods[joint] = max(lods.get(joint, lod), lod)
        slots, at = {joint: slot for slot, joint in enumerate(lods)}, self.used[1]
        self.assign(self.root, "multi_parents", [self.row("multi_parent", joint_name_index=at.get(joint, -1), lod=lod)
                                                 for joint, lod in lods.items()])
        for (_, weight), joint in zip(weights, joints):
            self.assign(weight, "parent_name_index", slots[joint])
