"""See docs/internals/startup-draw-states.md."""
from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping

lazy import numpy as np

lazy from file_handlers.engine_math import (
    affine_inverse, euler_rotation, make_trs_matrix, mathex_rotation, normalized_row, quaternion_euler,
    quaternion_product, rotation_euler, transform_coord, transform_product,
)
lazy from file_handlers.mmtr.engine_buffers import engine_inverse
lazy from file_handlers.rsz.utils.rsz_instance_defaults import type_table
lazy from utils.hash_util import murmur3_hash_utf16le

lazy from .campaign_state import three_valued
lazy from .scene_time import SimpleBody
lazy from .scn_scene_graph import (
    ScnComponent, ScnComponentId, ScnForeignRequest, ScnObjectId, ScnSceneDocument, ScnSceneGraphBuilder, ScnTransform,
    _bool_field, _identity, _numeric_field, _object_ref_id, _object_runs, _object_scale, _strip_value, joint_rotation,
    joint_world, object_world, parent_frame, parent_of, root_location, root_world,
)

from .scene_startup_conditions import StartupConditions
from .scene_startup_materials import StartupMaterials
from .scene_startup_time import StartupTime
from .scene_startup_transforms import StartupTransforms


STARTUP_DRAW_RESOURCE = "resources/data/startup_draw_requests.json"


def startup_transform(transform: ScnTransform, write: Mapping) -> ScnTransform:
    position, rotation, scale = (
        tuple(old if new is None else new for new, old in zip(write[key], current)) if key in write else current
        for key, current in (("position", transform.position), ("rotation", transform.rotation),
                             ("scale", transform.scale)))
    if (angles := write.get("euler")) is not None:
        rotation = euler_rotation(angles)
    if (turn := write.get("euler add")) is not None:
        axis, angle = turn
        angles = list(rotation_euler(rotation))
        angles[axis] = float(np.float32(angle + angles[axis]))
        rotation = euler_rotation(angles)
    return replace(transform, position=position, rotation=rotation, scale=scale,
                   parent_joint=write.get("parent joint", transform.parent_joint),
                   local_matrix=make_trs_matrix(position, rotation, scale))


_LOCAL_WRITES = ("position", "rotation", "scale", "parent joint", "euler", "euler add")


class StartupWorld:
    def __init__(self, document, collect_joints: Callable | None = None, collect_own: Callable | None = None):
        self.document, self.collect_joints, self.collect_own = document, collect_joints, collect_own
        self.written: set[ScnObjectId] = set()
        self.worlds: dict[ScnObjectId, tuple[np.ndarray, tuple[float, float, float, float]]] = {}

    def write(self, item, write: Mapping) -> None:
        if item.transform is None:
            return
        if "parent" in write:
            self.reparent(item, write["parent"])
        if (slot := write.get("slot")) is not None:
            if self.collect_joints is not None:
                self.collect_joints(item)
            if (coord := self.joint_coord(self.document.objects[slot[0]], slot[1])) is None:
                return
            position, (x, y, z) = coord
            write = {**write, "position": position, "euler": (x, y, z if write["flip"] is None else write["flip"])}
        if any(key in write for key in _LOCAL_WRITES):
            parent = parent_of(self.document, item)
            if "position" in write and (parent is None or parent.kind == "folder"):
                # A root's local position is its world one (its folders' offsets were added at load).
                self.document.startup_rooted.add(item.id)
            self.update(item, startup_transform(item.transform, write))
        if (source := write.get("world position")) is not None:
            position = self.position(self.document.objects[source])
            if (offset := write.get("world offset")) is not None:
                position = tuple(float(np.float32(a) + np.float32(b)) for a, b in zip(position, offset))
            self.set_position(item, position)
        if (point := write.get("world point")) is not None:
            rows = np.asarray(self.world(self.document.objects[point[0]])[0], dtype=np.float32).T
            self.set_position(item, transform_coord(point[1], rows))
        if (source := write.get("world rotation")) is not None:
            matrix = np.asarray(self.world(self.document.objects[source])[0], dtype=np.float32).T
            self.set_rotation(item, mathex_rotation(matrix))
        if (name := write.get("joint")) is not None:
            self.pose(item, name, write.get("joint position"), write.get("joint rotation"))
        if (key := write.get("parent joint pose")) is not None:
            self.take_parent_joint(item, key)

    def take_parent_joint(self, item, key: int) -> None:
        parent = parent_of(self.document, item)
        if parent is None or parent.kind == "folder":
            return
        if self.collect_own is not None:
            self.collect_own(parent)
        joints = self.document.joints.get(parent.id)
        if (index := None if joints is None else joints.find(key)) is None:
            return
        self.update(item, replace(item.transform, position=joints.positions[index], rotation=joints.rotations[index]))

    def pose(self, item, name: str, position, rotation) -> None:
        if self.collect_own is not None:
            self.collect_own(item)
        joints = self.document.joints.get(item.id)
        if (index := None if joints is None else joints.index(name)) is None:
            return
        self.document.joints[item.id] = joints.posed(index, position, rotation)
        self.document.startup_posed.add(item.id)
        self.written.add(item.id)
        self.worlds.clear()

    def joint_coord(self, holder, joint: str):
        joints = self.document.joints.get(holder.id)
        if (index := None if joints is None else joints.index(joint)) is None:
            return None
        world = np.asarray(self.world(holder)[0], dtype=np.float32).T
        at = np.asarray(joint_world(self.document, holder, index, lambda owner: self.world(owner)[0]), np.float32).T
        local = transform_product(at, engine_inverse(np.ascontiguousarray(world)))
        return tuple(float(value) for value in local[3, :3]), quaternion_euler(mathex_rotation(local))

    def update(self, item, transform: ScnTransform) -> None:
        item.transform = replace(transform, local_matrix=make_trs_matrix(
            transform.position, transform.rotation, transform.scale))
        self.written.add(item.id)
        self.worlds.clear()
        if transform.parent_joint and self.collect_joints is not None:
            self.collect_joints(item)

    def world(self, item) -> tuple[np.ndarray, tuple[float, float, float, float]]:
        if (found := self.worlds.get(item.id)) is None:
            self.worlds[item.id] = root_world(self.document, item)
            found = self.worlds[item.id] = object_world(self.document, item, self.world)
        return found

    def base(self, item) -> tuple[np.ndarray, tuple[float, float, float, float]] | None:
        """The matrix (row vectors) and world rotation the transform hangs under; None for a root."""
        parent = parent_of(self.document, item)
        if parent is None or parent.kind == "folder":
            return None
        matrix, rotation = self.world(parent)
        joints = self.document.joints.get(parent.id) if item.transform.parent_joint else None
        if (index := None if joints is None else joints.index(item.transform.parent_joint)) is not None:
            matrix = joint_world(self.document, parent, index, lambda owner: self.world(owner)[0])
            rotation = joint_rotation(self.document, parent, index, lambda owner: self.world(owner)[1])
        return np.asarray(matrix, dtype=np.float32).T, rotation

    def dirty(self, item) -> bool:
        seen: set[ScnObjectId] = set()
        while item is not None and item.id not in seen:
            if item.id in self.written:
                return True
            seen.add(item.id)
            item = parent_of(self.document, item)
        return False

    def position(self, item) -> tuple[float, float, float]:
        base = None if not self.dirty(item) else self.base(item)
        if base is None:
            return tuple(float(value) for value in self.world(item)[0][:3, 3])
        rows, f = base[0], np.float32
        x, y, z = (f(value) for value in item.transform.position)
        return tuple(float(value) for value in (((rows[1] * y + rows[0] * x) + rows[2] * z) + rows[3])[:3])

    def set_position(self, item, world) -> None:
        f = np.float32
        if (base := self.base(item)) is None:
            self.document.startup_rooted.add(item.id)
            position = tuple(float(f(value)) for value in world)
        else:
            inverse = affine_inverse(base[0])
            x, y, z = (f(value) for value in world)
            position = tuple(float(value) for value in (((inverse[1] * y + inverse[0] * x) + inverse[2] * z)
                                                         + inverse[3])[:3])
        self.update(item, replace(item.transform, position=position))

    def set_rotation(self, item, rotation) -> None:
        if (base := self.base(item)) is None:
            local = tuple(rotation)
        else:
            f = np.float32
            x, y, z, w = (f(value) for value in base[1])
            q = [f(value) for value in quaternion_product(rotation, (-x, -y, -z, w))]
            scale = f(1) / np.sqrt(((q[1] * q[1] + q[0] * q[0]) + q[2] * q[2]) + q[3] * q[3])
            local = tuple(float(value * scale) for value in q)
        self.update(item, replace(item.transform, rotation=local))

    def set_scale(self, item, scale) -> None:
        f = np.float32
        base = self.base(item)
        if base is None or item.transform.absolute_scaling:
            self.update(item, replace(item.transform, scale=tuple(float(f(value)) for value in scale)))
            return
        inverse, rows, local = affine_inverse(base[0]), np.asarray(self.world(item)[0], dtype=f).T, []
        for row, part in zip(rows[:3], scale):
            unit = normalized_row(row) * f(part)
            out = (inverse[3] * unit[3] + inverse[2] * unit[2]) + (inverse[1] * unit[1] + inverse[0] * unit[0])
            local.append(float(np.sqrt(((out[0] * out[0] + out[1] * out[1]) + out[2] * out[2]) + out[3] * out[3])))
        self.update(item, replace(item.transform, scale=tuple(local)))

    def reparent(self, item, parent_id: ScnObjectId | None) -> None:
        """Keeps the world; a move to the scene root keeps the folder."""
        document, current = self.document, parent_of(self.document, item)
        target = None if parent_id is None else document.objects[parent_id]
        if (current is None or current.kind == "folder") if target is None else current is target:
            return
        world, rotation = self.world(item)
        position = self.position(item)
        scale = _object_scale(item, world)
        if target is None:
            target = item
            while (target := document.objects.get(document.object_by_local_id.get(target.parent_id))) is not None \
                    and target.kind != "folder":
                pass
        document.startup_parents[item.id] = None if target is None else target.id
        document.startup_rooted.discard(item.id)
        self.worlds.clear()
        if item.transform.parent_joint and self.collect_joints is not None:
            self.collect_joints(item)
        self.set_position(item, position)
        self.set_rotation(item, rotation)
        self.set_scale(item, scale)


def apply_startup_transforms(document, collect_joints: Callable | None = None,
                             collect_own: Callable | None = None) -> None:
    document.startup_parents, document.startup_rooted, document.startup_posed = {}, set(), set()
    world = StartupWorld(document, collect_joints, collect_own)
    writes = document.startup_transforms
    for object_id, write in [*(entry for entry in writes if not entry[1].get("queued")),
                             *(entry for entry in writes if entry[1].get("queued"))]:
        world.write(document.objects[object_id], write)


def start_bones(document, item) -> np.ndarray | None:
    return skin_bones(document, item) if item.id in document.startup_posed else None


def simple_body(document, item, joints) -> tuple[SimpleBody, tuple[float, float, float]]:
    frame = parent_frame(document, item, lambda owner: (owner.document_world_matrix, owner.document_world_rotation))
    transform = item.transform
    body = SimpleBody(item.id, root_location(document, item) if frame is None else transform.position,
                      transform.rotation, transform.scale, *(frame or (None, None)))
    if joints is not None:
        body = replace(body, joint_parents=joints.parents, joint_positions=joints.positions,
                       joint_rotations=joints.rotations, joint_scales=joints.scales)
    return body, tuple(float(value) for value in item.document_world_matrix[:3, 3])


def skin_bones(document, item) -> np.ndarray | None:
    """Skin matrices (3 x 4, bone remap order) at the ``document.joints`` pose; None without joints or for joints
    following another object's."""
    joints = document.joints.get(item.id)
    if joints is None or joints.inverse_binds is None or not joints.remap or item.transform.same_joints_constraint:
        return None
    identity = _identity()
    bones = np.tile(np.eye(3, 4, dtype=np.float32), (len(joints.remap), 1, 1))
    for row, index in enumerate(joints.remap):
        if 0 <= index < len(joints.names):
            model = joint_world(document, item, index, lambda _owner: identity)
            bones[row] = transform_product(model, joints.inverse_binds[index].T)[:3]
    return bones


def _int32_number(text: str) -> int | None:
    """Int32.TryParse with NumberStyles.Integer."""
    digits = text.strip()
    digits = digits[1:] if digits[:1] in ("+", "-") else digits
    return number if digits.isascii() and digits.isdigit() and -2**31 <= (number := int(text)) < 2**31 else None


def startup_draw_requests(game: str) -> dict:
    return type_table(STARTUP_DRAW_RESOURCE).get(str(game).upper(), {})


def _empty_start() -> dict:
    return {"requests": [], "trigger": [], "reset": [], "transforms": [], "meshes": [], "parts": []}


class StartupDraw(StartupConditions, StartupTransforms, StartupTime, StartupMaterials):
    """Methods return None where the outcome is not known, unless they say otherwise."""

    def __init__(self, builder: ScnSceneGraphBuilder, document: ScnSceneDocument, rules: Mapping[str, object]):
        self.builder, self.document, self.rules = builder, document, rules
        self.options, self.starts, self.runner = rules["options"], rules["starts"], rules["drivers"]
        self.enabled = builder.fields.get("enabled")
        self.campaign = builder._campaign_state(document, rules.get("campaign"))
        self.enums = {name: {int(value): text for value, text in table.items()}
                      for name, table in rules.get("enums", {}).items()}
        self.unbound = murmur3_hash_utf16le("")
        self.requests: dict[ScnObjectId, dict[int, bool]] = {}
        self.phases: dict[tuple[ScnObjectId, int], str] = {}
        self.foreign: list[ScnForeignRequest] = []
        self.curve_started: set[ScnObjectId] = set()
        self.sleeping: dict[str, tuple[set[ScnObjectId], set[ScnObjectId]]] | None = None
        self.awake: list[tuple[ScnObjectId, Mapping, ScnComponent]] = []
        self.transforms: list[tuple[ScnObjectId, Mapping]] = []
        self.meshes: dict[ScnObjectId, dict[int, bool]] = {}
        self.parts: dict[ScnObjectId, dict[int, bool]] = {}
        self.cores: dict[tuple[int, ...], ScnObjectId] | None = None
        self.self_hides: list[tuple[ScnObjectId, ScnComponent, Mapping]] = []
        self.flag_draws: list[tuple[ScnObjectId, ScnComponent, Mapping]] = []
        self.updates: list[tuple[ScnObjectId, Mapping, ScnComponent]] = []
        self.ready: dict[ScnComponentId, dict | None] = {}
        self.parent: dict[ScnObjectId, ScnObjectId] = {}
        self.children: dict[ScnObjectId, list[ScnObjectId]] = {}
        for item in document.objects.values():
            parent = document.object_by_local_id.get(item.parent_id)
            if parent is not None and parent != item.id:
                self.parent[item.id] = parent
                self.children.setdefault(parent, []).append(item.id)
        self.roots = [item.id for item in document.objects.values() if item.id not in self.parent]
        scn = document.rsz_file
        self.prefab = scn.is_pfb
        self.by_guid = builder._guid_objects(document)
        self.table_index = {instance: index for index, instance in enumerate(scn.object_table)}
        self.prefab_refs = {(info.object_id, info.property_id, info.array_index): info.target_id
                            for info in scn.gameobject_ref_infos}
        self.settings: dict[ScnObjectId, list[tuple[Mapping, list[tuple[int, Mapping]], ScnComponent]]] = {}
        self.material_settings: dict[ScnObjectId, list[tuple[ScnComponent, Mapping, list[tuple[int, Mapping]]]]] = {}
        self.touched: set[ScnComponentId] = set()
        self.finished: dict[ScnComponentId, list[int]] = {}
        self.portrayal: dict[ScnObjectId, dict[int, bool]] = {}
        curves = rules.get("curve animators", {}).get("settings", {})
        self.curve_settings: dict[ScnObjectId, list[tuple[ScnComponent, Mapping, list[tuple[int, Mapping]]]]] = {}
        self.curve_requests: dict[ScnObjectId, str | None] = {}
        self.drivers: dict[ScnObjectId, list[ScnComponent]] = {}
        self.mdfs: dict[str, list[tuple[str, dict[str, tuple]]] | None] = {}
        self.lit: list[ScnObjectId] = []
        lights = rules.get("light awakes")
        for item in document.objects.values():
            if lights and lights["holding"] in (kinds := [document.components[c].type_name for c in item.components]) \
                    and any(builder._type_is_a(kind, lights["type"]) for kind in kinds):
                self.lit.append(item.id)
            for component_id in item.components:
                component = document.components[component_id]
                # An awake's requests come besides the component's other start-up work.
                if (rule := rules.get("awake", {}).get(component.type_name)) is not None:
                    self.awake.append((item.id, rule, component))
                if (rule := curves.get(component.type_name)) is not None:
                    self.curve_settings.setdefault(item.id, []).append((component, rule, self.params(component)))
                if (rule := rules["settings"].get(component.type_name)) is not None:
                    self.settings.setdefault(item.id, []).append((rule, self.params(component), component))
                elif (rule := rules.get("material settings", {}).get(component.type_name)) is not None:
                    self.material_settings.setdefault(item.id, []).append((component, rule, self.params(component)))
                elif (rule := rules["portrayal"].get(component.type_name)) is not None:
                    if item.id not in self.portrayal:
                        keys: dict[int, bool] = {}
                        for _instance, fields in self.params(component):
                            keys.setdefault(self.key(fields), _bool_field(fields, rule["children"], False))
                        self.portrayal[item.id] = keys
                elif (rule := rules.get("self hides", {}).get(component.type_name)) is not None:
                    self.self_hides.append((item.id, component, rule))
                elif (rule := rules.get("flag draws", {}).get(component.type_name)) is not None:
                    self.flag_draws.append((item.id, component, rule))
                elif (rule := rules.get("updates", {}).get(component.type_name)) is not None:
                    self.updates.append((item.id, rule, component))
                elif any(builder._type_is_a(component.type_name, base) for base in self.runner["types"]):
                    self.drivers.setdefault(item.id, []).append(component)

    def ref_fields(self, value) -> Mapping[str, object]:
        return self.builder._fields(self.document.rsz_file, _object_ref_id(value))

    def params(self, component: ScnComponent) -> list[tuple[int, Mapping[str, object]]]:
        entries = getattr(self.builder._field_at(self.document, component.fields, self.options["params"]), "values",
                          None)
        return [(instance, self.builder._fields(self.document.rsz_file, instance))
                for instance in map(_object_ref_id, entries or ())]

    def key(self, fields: Mapping[str, object]) -> int | None:
        return getattr(fields.get(self.options["key"]), "value", None)

    def descendants(self, object_id: ScnObjectId) -> list[ScnObjectId]:
        found, stack = [], list(reversed(self.children.get(object_id, ())))
        while stack:
            found.append(node := stack.pop())
            stack.extend(reversed(self.children.get(node, ())))
        return found

    def child(self, object_id: ScnObjectId, name: str) -> ScnObjectId | None:
        return next((node for node in self.children.get(object_id, ()) if self.document.objects[node].name == name),
                    None)

    def find(self, object_id: ScnObjectId, name: str) -> ScnObjectId | None:
        return next((node for node in self.descendants(object_id) if self.document.objects[node].name == name), None)

    def runs(self, object_id: ScnObjectId, owner: Mapping) -> bool:
        return _object_runs(self.document, object_id, owner.get("when"), self.options["update"])

    def driver_ready(self, component: ScnComponent, object_id: ScnObjectId) -> bool:
        return not self.runs(object_id, self.runner) or self.start(component, object_id) is not None

    def start(self, component: ScnComponent, object_id: ScnObjectId) -> dict | None:
        if component.id not in self.ready:
            rule = self.starts.get(component.type_name)
            self.ready[component.id] = None if rule is None or "gap" in rule or not _bool_field(
                component.fields, self.enabled, True) else self.resolve(rule, component, object_id)
        return self.ready[component.id]

    def inputs(self, object_id: ScnObjectId) -> dict[str, list[ScnComponent]]:
        found: dict[str, list[ScnComponent]] = {}
        for node in (object_id, self.child(object_id, self.options["param object"])):
            for component_id in self.document.objects[node].components if node is not None else ():
                component = self.document.components[component_id]
                found.setdefault(component.type_name, []).append(component)
        return found

    def resolve(self, rule: Mapping, component: ScnComponent, object_id: ScnObjectId) -> dict | None:
        inputs = self.inputs(object_id)
        for name, cases in rule.get("cases", {}).items():
            case = cases.get(str(getattr(component.fields.get(name), "value", None)).lower())
            if case is None:
                return None
            rest = {key: value for key, value in rule["cases"].items() if key != name}
            return self.resolve({**rule, "cases": rest, **case}, component, object_id)
        if any(inputs.get(name) for name in rule.get("absent", ())) or any(
                not inputs.get(name) for name in rule.get("present", ())) or any(
                getattr(component.fields.get(name), "values", None) for name in rule.get("empty", ())) or any(
                any(getattr(component.fields.get(name), "raw_bytes", b"") or b"")
                for name in rule.get("empty guids", ())):
            return None
        if "with" in rule and not any(self.builder._type_is_a(self.document.components[other].type_name, kind)
                                      for other in self.document.objects[object_id].components
                                      for kind in rule["with"]):
            return _empty_start()
        if rule.get("context"):
            spec = self.options["contexts"]
            core = next((other for other in map(self.document.components.__getitem__,
                                                self.document.objects[object_id].components)
                         if other.type_name == spec["core"]), None)
            found = None if core is None else self.context_found(self.ref_fields(core.fields.get(spec["core id"])))
            if found is None:
                return None
            if not found:
                return _empty_start()
        for type_name, wanted in rule.get("flags", {}).items():
            by_key = {murmur3_hash_utf16le(name): value for name, value in wanted.items()}
            for other in inputs.get(type_name, ()):
                for _instance, fields in self.params(other):
                    default = by_key.get(self.key(fields), wanted.get("*"))
                    if default is not None and self.condition(fields) is not default:
                        return None
        if (entries := self._entries(rule, component, object_id, inputs)) is None:
            return None
        return self._start(rule, entries, component, object_id, inputs)

    def _entries(self, rule: Mapping, component: ScnComponent, object_id: ScnObjectId,
                 inputs: Mapping[str, list[ScnComponent]]) -> list[Mapping] | None:
        override = None
        for type_name, wanted in rule.get("strata", {}).items():
            for other in inputs.get(type_name, ()):
                for name, default in wanted.items():
                    value = self.stratum(other.fields.get(name))
                    if not isinstance(default, Mapping):
                        if value is not default:
                            return None
                    elif value is None or (case := str(value).lower()) not in default:
                        return None
                    elif default[case] is not None:
                        override = default[case]
        if "interact" in rule:
            if (fires := self.interact(inputs, rule["interact"])) is None or not fires and "interact else" not in rule:
                return None
            if not fires:
                override = rule["interact else"]
        for name, type_name in rule.get("refers", {}).items():
            target = None if self.prefab else self.by_guid.get(getattr(component.fields.get(name), "raw_bytes", None))
            if target is None or all(self.document.components[other].type_name != type_name
                                     for other in self.document.objects[target].components):
                return None
        for path in rule.get("children", ()):
            node = object_id
            for name in path.split("/"):
                node = None if node is None else self.child(node, name)
            if node is None:
                return None
        # Its rate controller's EmissiveRate fade-in from the first move is not followed.
        if "lit" in rule and self.turned_on(rule["lit"], component, object_id) is not False:
            return None
        entries = [rule if override is None else override]
        if (choose := rule.get("choose")) is not None:
            found = (inputs.get(choose["component"]) or [None])[-1]
            value = int(choose["default"] if (field := found.fields.get(choose["field"]) if found else None) is None
                        else getattr(field, "value", choose["default"]))
            case = choose["cases"].get(str(value & choose["mask"]))
            if case is None and (bits := choose.get("difficulty bits")) is not None and self.campaign is not None \
                    and (bit := bits.get(str(self.campaign.difficulty))) is not None:
                case = choose["cases"].get(str(choose["mask"] if value & bit else 0))
            if case is None:
                return None
            entries.append(case)
        if (listed := rule.get("chapters")) is not None:
            hidden = "only event" in listed and _bool_field(component.fields, listed["only event"], False)
            if not hidden:
                if self.campaign is None:
                    return None
                hidden = self.campaign.chapter not in {int(getattr(value, "value", value)) for value in getattr(
                    component.fields.get(listed["field"]), "values", None) or ()}
            if hidden:
                entries.append(listed["hidden"])
        if (read_file := rule.get("read file")) is not None:
            if (drawn := self.read_file_drawn(read_file, component, inputs)) is None:
                return None
            entries.append(read_file["drawn" if drawn else "hidden"])
        return entries

    def _start(self, rule: Mapping, entries: list[Mapping], component: ScnComponent, object_id: ScnObjectId,
               inputs: Mapping[str, list[ScnComponent]]) -> dict | None:
        start = _empty_start()
        for entry in entries:
            if not self._add_entry(entry, start, component, object_id, inputs):
                return None
        for kind, name in self.attr_calls(component, rule.get("attr calls", ())):
            start[kind].append(name)
        spec = self.options.get("port triggers", {})
        for item in getattr(component.fields.get(rule.get("port triggers", "")), "values", None) or ():
            entry = self.ref_fields(item)
            if (holds := self.port_trigger(entry)) is None:
                return None
            if holds:
                start["trigger"] += [_strip_value(getattr(key, "value", key))
                                     for key in getattr(entry.get(spec["keys"]), "values", None) or ()]
        if (item := rule.get("finish item")) is not None:
            source = item["start"] if "when" not in item or _bool_field(component.fields, item["when"], False) \
                else item["field"]
            value = getattr(component.fields.get(source), "value", None)
            if value is not None:
                start["trigger"].append(self.enums.get(item["enum"], {}).get(int(value), str(value)))
                if "then" in item and int(value) != item["none"]:
                    start["trigger"].append(item["then"])
        return start

    def _add_entry(self, entry: Mapping, start: dict, component: ScnComponent, object_id: ScnObjectId,
                   inputs: Mapping[str, list[ScnComponent]]) -> bool:
        if not self._add_requests(entry, start, component, inputs):
            return False
        if (relation := entry.get("relation")) is not None:
            if (related := self.relation(relation, inputs)) is None:
                return False
            start["requests"] += related[0]
            start["transforms"] += related[1]
        if (phases := entry.get("phases")) is not None:
            if (first := self.first_phase(phases, component)) is None:
                return False
            start["requests"] += first[0]
            start["trigger"] += first[1]
        for spec in entry.get("transforms", ()):
            if (transform := self.transform(spec, component, object_id)) is None:
                return False
            start["transforms"].append(transform)
        if (lock := entry.get("door lock")) is not None:
            if (writes := self.door_lock(lock, component, object_id)) is None:
                return False
            start["transforms"] += writes
        if (books := entry.get("books")) is not None:
            if (placed := self.books(books, object_id, inputs)) is None:
                return False
            start["requests"] += placed[0]
            start["transforms"] += placed[1]
        if (doors := entry.get("linked doors")) is not None:
            if (linked := self.linked_doors(doors, component)) is None:
                return False
            start["requests"] += linked[0]
            start["transforms"] += linked[1]
        if (frames := entry.get("panel frames")) is not None:
            if (laid := self.panel_frames(frames, component, object_id)) is None:
                return False
            start["requests"] += laid[0]
            start["transforms"] += laid[1]
        if (steps := entry.get("scenario steps")) is not None:
            if (stepped := self.scenario_steps(steps, component)) is None:
                return False
            start["requests"] += stepped
        start["meshes"] += entry.get("meshes", ())
        for spec in entry.get("parts", ()):
            if (writes := self.part_writes(spec, component, inputs)) is None:
                return False
            start["parts"].append({**spec, "writes": writes})
        for name in entry.get("trigger", ()):
            if isinstance(name, Mapping):
                if "when" in name and not _bool_field(component.fields, name["when"], False):
                    continue
                if "flag" in name and not (held := self.flag(inputs, *name["flag"])):
                    if held is None:
                        return False
                    continue
                name = None if self.unless(name, component) else name.get("key") or _strip_value(
                    getattr(component.fields.get(name.get("field")), "value", ""))
            if name:
                start["trigger"].append(name)
        start["reset"] += entry.get("reset", ())
        return True

    def _add_requests(self, entry: Mapping, start: dict, component: ScnComponent,
                      inputs: Mapping[str, list[ScnComponent]]) -> bool:
        for request in entry.get("requests", ()):
            if self.unless(request, component):
                continue
            if (param := request.get("param")) is not None:
                if (state := self.param_state(inputs, param["type"], param["key"])) is None:
                    return False
                if state not in param["is"]:
                    continue
            if isinstance(request.get("draw"), Mapping):
                draw = getattr(self.builder._field_at(self.document, component.fields, request["draw"]["field"]),
                               "value", None)
                if type(draw) is not bool:
                    return False
                request = {**request, "draw": draw}
            if "all refs" in request:
                if self.prefab:
                    return False
                raws = [getattr(self.builder._field_at(self.document, component.fields, path), "raw_bytes", None)
                        for path in request["all refs"]]
                targets = [self.by_guid.get(raw) for raw in raws]
                # Another scene's object: not known here.
                if any(target is None and raw and any(raw) for target, raw in zip(targets, raws)):
                    return False
                if None in targets:
                    continue
                request = {**request, "targets": targets}
            start["requests"].append(request)
        return True

    def by_ref(self, item, request: ScnForeignRequest) -> ScnObjectId | None:
        raw = getattr(item, "raw_bytes", None)
        found = self.by_guid.get(raw)
        if found is None and raw and any(raw):
            self.foreign.append(replace(request, guid=bytes(raw)))
        return found

    def request_targets(self, entry: Mapping, component: ScnComponent, object_id: ScnObjectId):
        if "targets" in entry:
            found = list(entry["targets"])
        elif "child" in entry:
            node: ScnObjectId | None = object_id
            for name in entry["child"].split("/"):
                node = None if node is None else self.child(node, name)
            found = [node]
        elif "child holding" in entry:
            found = [next((node for node in self.children.get(object_id, ()) if any(
                self.document.components[other].type_name == entry["child holding"]
                for other in self.document.objects[node].components)), None)]
        elif entry.get("children"):
            found = list(self.children.get(object_id, ()))
        elif "children numbered" in entry:
            prefix = entry["children numbered"]
            found = [node for node in self.children.get(object_id, ())
                     if (name := self.document.objects[node].name).startswith(prefix)
                     and (number := _int32_number(name[len(prefix):])) is not None
                     and entry.get("number", number) == number]
            if "number" in entry:
                found = found[-1:]
        elif "find" in entry:
            node = object_id
            for name in entry["find"].split("/"):
                node = None if node is None else self.find(node, name)
            found = [node]
        elif "ref" in entry or "refs" in entry or "ref value" in entry:
            field = entry["ref value"] if "ref value" in entry else component.fields.get(
                entry.get("ref") or entry["refs"])
            items = getattr(field, "values", None) or () if "refs" in entry else [field]
            request = ScnForeignRequest(b"", entry["layer"], entry["draw"], "start", bool(entry.get("descendants")),
                                        entry.get("unless name"), bool(entry.get("curves")))
            found = [] if self.prefab else [self.by_ref(item, request) for item in items]
        else:
            found = [object_id]
        for target in found:
            if target is None or (text := entry.get("unless name")) and text in self.document.objects[target].name:
                continue
            if "holding" in entry and not any(
                    self.builder._type_is_a(self.document.components[other].type_name, entry["holding"])
                    for other in self.document.objects[target].components):
                continue
            yield target
            if entry.get("descendants"):
                yield from self.descendants(target)

    def bound(self, object_id: ScnObjectId, fields: Mapping[str, object]) -> bool | None:
        bind = getattr(fields.get(self.options["bind"]), "value", self.unbound)
        if bind == self.unbound:
            return False
        spec = self.options["interact"]
        item = self.document.objects[object_id]
        holders = [object_id, *([parent] if item.name == self.options["param object"] and (
            parent := self.parent.get(object_id)) is not None else [])]
        found: list[bool | None] = []
        for holder_id in holders:
            for holder in map(self.document.components.__getitem__, self.document.objects[holder_id].components):
                if holder.type_name != spec["holder"]:
                    continue
                for entry in getattr(holder.fields.get(spec["triggers"]), "values", None) or ():
                    instance = _object_ref_id(self.ref_fields(entry).get(spec["trigger"]))
                    trigger = self.builder._fields(self.document.rsz_file, instance)
                    if murmur3_hash_utf16le(_strip_value(getattr(trigger.get(spec["name"]), "value", ""))) != bind:
                        continue
                    if self.builder._type_name(self.document.rsz_file, instance) != spec["type"]:
                        found.append(None)
                    elif self.runs(holder_id, self.runner):
                        found.append(self.flag(self.inputs(holder_id), spec["flags"],
                                               _strip_value(getattr(trigger.get(spec["flag"]), "value", ""))))
        return three_valued(found, True)

    def applied(self, object_id: ScnObjectId, params: list[tuple[int, Mapping]]) -> bool:
        if any(self.bound(object_id, fields) is None for _instance, fields in params):
            return False
        keys = {self.key(fields) for _instance, fields in params}
        node: ScnObjectId | None = object_id
        while node is not None:
            if node == object_id or any(self.portrayal.get(node, {}).get(key) for key in keys):
                if not all(self.driver_ready(driver, node) for driver in self.drivers.get(node, ())):
                    return False
            node = self.parent.get(node)
        return True

    def targets(self, instance: int, fields: Mapping[str, object], rule: Mapping, name: str,
                request: ScnForeignRequest):
        values = getattr(fields.get(name), "values", None) or ()
        if self.prefab:
            index, property_id = self.table_index.get(instance), rule.get("prefab ids", {}).get(name)
            found = (self.document.object_by_local_id.get(self.prefab_refs.get((index, property_id, position)))
                     for position in range(len(values)))
        else:
            found = (self.by_ref(item, request) for item in values)
        for target in found:
            if target is not None and target in self.document.objects:
                yield target

    def request(self, object_id: ScnObjectId, layer: int, draw: bool, phase: str = "start") -> None:
        self.requests.setdefault(object_id, {})[layer] = draw
        self.phases[object_id, layer] = phase

    def apply(self, instance: int, fields: Mapping[str, object], rule: Mapping, triggered: bool, phase: str) -> None:
        for name, draw in rule["draw"].items():
            request = ScnForeignRequest(b"", rule["layer"], draw != triggered, phase)
            for target in self.targets(instance, fields, rule, name, request):
                self.request(target, rule["layer"], draw != triggered, phase)

    def fire(self, object_id: ScnObjectId, name: str, triggered: bool) -> None:
        key = murmur3_hash_utf16le(name)
        collects = self.portrayal.get(object_id, {}).get(key)
        if collects is None:
            return
        for node in (object_id, *(self.descendants(object_id) if collects else ())):
            for rule, params, _component in self.settings.get(node, ()):
                param = next(((instance, fields) for instance, fields in params if self.key(fields) == key), None)
                if param is not None and self.applied(node, params):
                    self.apply(*param, rule, triggered, "start")
            for component, _rule, params in self.material_settings.get(node, ()):
                if any(self.key(fields) == key for _instance, fields in params):
                    if triggered:
                        self.finished.setdefault(component.id, []).append(key)
                    else:
                        self.touched.add(component.id)
            for _component, rule, params in self.curve_settings.get(node, ()):
                for _instance, fields in params:
                    if self.key(fields) == key:
                        request = int(_numeric_field(fields, rule["request"], -1))
                        kind = None if not triggered else rule["requests"].get(str(request))
                        for target in (node, *self.descendants(node)):
                            self.curve_requests[target] = kind

    def unless(self, entry, component: ScnComponent) -> bool:
        return isinstance(entry, Mapping) and "unless" in entry and _bool_field(component.fields, entry["unless"],
                                                                                 False)

    def run(self) -> dict[ScnObjectId, dict[int, bool]]:
        for object_id, rule, component in self.awake:
            for entry in rule.get("requests", ()):
                for target in self.request_targets(entry, component, object_id):
                    self.request(target, entry["layer"], entry["draw"], "awake")
            self.mesh_requests(rule.get("meshes", ()), component, object_id)
        for object_id, entries in self.settings.items():
            for rule, params, component in entries:
                if self.runs(object_id, rule) and self.applied(object_id, params) and (
                        "awake when" not in rule or _bool_field(component.fields, rule["awake when"], False)):
                    for param in params:
                        self.apply(*param, rule, False, "awake")
                    for param in params:
                        if self.bound(object_id, param[1]):
                            self.apply(*param, rule, True, "start")
        for object_id, entries in self.material_settings.items():
            for component, rule, params in entries:
                if self.runs(object_id, rule) and self.applied(object_id, params):
                    self.finished.setdefault(component.id, []).extend(
                        self.key(fields) for _instance, fields in params if self.bound(object_id, fields))
        for object_id, components in self.drivers.items():
            for component in components:
                if not self.runs(object_id, self.runner):
                    continue
                if _bool_field(component.fields, self.enabled, True):
                    always = (self.starts.get(component.type_name) or {}).get("always", {})
                    for spec in always.get("transforms", ()):
                        if (write := self.transform(spec, component, object_id)) is not None:
                            self.transforms += [(target, write) for target in write["targets"]]
                    if (stand := always.get("panel stand")) is not None and (
                            placed := self.panel_stand(stand, component, object_id)) is not None:
                        for entry in placed[0]:
                            for target in entry["targets"]:
                                self.request(target, entry["layer"], entry["draw"])
                        self.transforms += [(target, write) for write in placed[1] for target in write["targets"]]
                    if (doors := always.get("elevator doors")) is not None and (
                            opened := self.elevator_doors(doors, component, object_id)) is not None:
                        self.transforms += [(target, write) for write in opened for target in write["targets"]]
                if (start := self.start(component, object_id)) is None:
                    continue
                for entry in start["requests"]:
                    for target in self.request_targets(entry, component, object_id):
                        self.request(target, entry["layer"], entry["draw"])
                        if entry.get("curves"):
                            self.curve_started.add(target)
                for name in start["trigger"]:
                    self.fire(object_id, name, True)
                for name in start["reset"]:
                    self.fire(object_id, name, False)
                self.transforms += [(target, write) for write in start["transforms"] for target in write["targets"]]
                self.mesh_requests(start["meshes"], component, object_id)
                for entry in start["parts"]:
                    for target in self.request_targets(entry, component, object_id):
                        self.parts.setdefault(target, {}).update(entry["writes"])
        for object_id, rule, component in self.updates:
            if self.runs(object_id, rule) and _bool_field(component.fields, self.enabled, True):
                for entry in rule["requests"]:
                    for target in self.request_targets(entry, component, object_id):
                        self.request(target, entry["layer"], entry["draw"])
        for object_id, component, rule in self.self_hides:
            held = [self.document.components[other] for other in self.document.objects[object_id].components]
            option = component if "option" not in rule else next(
                (other for other in held if self.builder._type_is_a(other.type_name, rule["option"])), None)
            if not self.runs(object_id, rule) or not _bool_field(component.fields, self.enabled, True) \
                    or option is None or self.stratum(option.fields.get(rule["strata"])) is not True:
                continue
            target = self.parent.get(object_id) if any(
                other.type_name == rule.get("parent if") for other in held) else object_id
            if target is not None:
                self.request(target, rule["layer"], False)
        for object_id, component, rule in self.flag_draws:
            flag = bytes(getattr(component.fields.get(rule["flag"]), "raw_bytes", b"") or b"")
            held = (self.document.components[other].type_name for other in self.document.objects[object_id].components)
            if (not self.runs(object_id, rule) or not _bool_field(component.fields, self.enabled, True) or not any(flag)
                    or rule["unless held"] in held or self.campaign is None
                    or (reads := self.campaign.check([(flag, True)], 0)) is None):
                continue
            self.request(object_id, rule["layer"], reads == _bool_field(component.fields, rule["enable"], False))
        return self.requests

    def folders(self) -> dict[ScnObjectId, bool]:
        spec = self.rules.get("folders")
        if spec is None:
            return {}
        fields = self.builder.fields
        drawn = {item.id: _bool_field(item.fields, fields.get("folder draw"), True)
                 or not _bool_field(item.fields, fields.get("folder startup"), True)
                 for item in self.document.objects.values() if item.kind == "folder"}
        for item in self.document.objects.values():
            if not self.runs(item.id, spec):
                continue
            for component_id in item.components:
                component = self.document.components[component_id]
                switch = spec["switches"].get(component.type_name)
                if switch is None or "gap" in switch or "camera" in switch or "camera area" in switch \
                        or not _bool_field(component.fields, self.enabled, True):
                    continue
                switched = (self.platform_switch(component, switch, spec["platform"]) if "platforms" in switch
                            else self.flag_switch(component, switch, spec["platform"], item.id))
                for name, draw in switched:
                    if (folder := self.folder_at(item.id, name)) is not None:
                        drawn[folder] = draw
        return drawn

    def folder_at(self, object_id: ScnObjectId, path: str) -> ScnObjectId | None:
        node = self.parent.get(object_id)
        while node is not None and self.document.objects[node].kind != "folder":
            node = self.parent.get(node)
        for index, name in enumerate(path.split("/")):
            if node is None and index:
                return None
            candidates = self.roots if node is None else self.children.get(node, ())
            node = next((child for child in candidates if (folder := self.document.objects[child]).kind == "folder"
                         and folder.name == name), None)
        return node

    def platform_switch(self, component: ScnComponent, switch: Mapping, platform: str) -> list[tuple[str, bool]]:
        target = self.ref_fields(component.fields.get(switch["target"]))
        on = _bool_field(target, switch["platforms"][platform], False)
        found = []
        for fields, state in ((target, on), (self.ref_fields(component.fields.get(switch["other"])), not on)):
            name = _strip_value(getattr(fields.get(switch["name"]), "value", ""))
            if name and _bool_field(fields, switch["draw"], False):
                found.append((name, state))
        return found

    def flag_switch(self, component: ScnComponent, switch: Mapping, platform: str,
                    object_id: ScnObjectId) -> list[tuple[str, bool]]:
        held_by_key: dict[int | None, bool | None] = {}
        for component_id in self.document.objects[object_id].components:
            if (check := self.document.components[component_id]).type_name == switch["flags"]:
                for _instance, fields in self.params(check):
                    held_by_key.setdefault(self.key(fields), self.condition(fields))
                break
        controls: dict[str, list[tuple[int, bool | None, bool]]] = {}
        for value in getattr(component.fields.get(switch["params"]), "values", None) or ():
            param = self.ref_fields(value)
            priority = int(getattr(param.get(switch["priority"]), "value", 0) or 0)
            held = held_by_key.get(getattr(param.get(switch["key"]), "value", None), False)
            for control in getattr(param.get(switch["controls"]), "values", None) or ():
                fields = self.ref_fields(control)
                if name := _strip_value(getattr(fields.get(switch["name"]), "value", "")):
                    controls.setdefault(name, []).append((priority, held, _bool_field(fields, switch["draw"], False)))
        if (force := switch.get("force off")) is not None and force["targets"].get(platform) in {
                int(getattr(value, "value", value))
                for value in getattr(component.fields.get(force["field"]), "values", None) or ()}:
            return [(name, False) for name in controls]
        found = []
        for name, entries in controls.items():
            best = None
            for index, (priority, held, draw) in enumerate(entries):
                if held is True and (best is None or priority >= best[0]):
                    best = (priority, index, draw)
            if best is not None and not any(
                    held is None and draw != best[2] and (priority, index) > best[:2]
                    for index, (priority, held, draw) in enumerate(entries)):
                found.append((name, best[2]))
        return found

    def first_phase(self, spec: Mapping, component: ScnComponent) -> tuple[list, list] | None:
        settings = [self.ref_fields(value)
                    for value in getattr(component.fields.get(spec["list"]), "values", None) or ()]
        if not settings:
            return None
        requests, low = [], 0
        for fields in settings:
            points = int(_numeric_field(fields, spec["hit points"], 0))
            requests.append({"child": _strip_value(getattr(fields.get(spec["name"]), "value", "")),
                             "layer": spec["layer"], "draw": low <= 0 < low + points})
            low += points
        key = _strip_value(getattr(settings[0].get(spec["key"]), "value", ""))
        return requests, [key] if key else []

    def scenario_steps(self, spec: Mapping, component: ScnComponent) -> list[dict] | None:
        if self.campaign is None or self.prefab:
            return None
        entries = [self.ref_fields(item) for item in getattr(component.fields.get(spec["list"]), "values", None) or ()]
        reads = [self.campaign.check([(bytes(getattr(entry.get(spec["flag"]), "raw_bytes", b"") or b""), True)], 0)
                 for entry in entries]
        count = max((index + 1 for index, value in enumerate(reads) if value), default=0)
        if None in reads[count:]:
            return None
        drawn: dict[bytes, dict] = {}
        for entry in entries[:count]:
            if not _bool_field(entry, spec["enable"], False):
                continue
            for item in getattr(entry.get(spec["targets"]), "values", None) or ():
                target = self.ref_fields(item)
                raw = bytes(getattr(target.get(spec["ref"]), "raw_bytes", b"") or b"")
                if _bool_field(target, spec["enable"], False) and any(raw):
                    drawn[raw] = {"ref value": target[spec["ref"]], "layer": spec["layer"],
                                  "draw": _bool_field(target, spec["draw"], False)}
        return list(drawn.values())

    def part_writes(self, spec: Mapping, component: ScnComponent,
                    inputs: Mapping[str, list[ScnComponent]]) -> dict[int, bool] | None:
        writes: dict[int, bool] = {}
        for write in spec.get("writes", ()):
            draw = write["draw"]
            if isinstance(draw, Mapping):
                if (value := self.user_value(draw, component)) is None:
                    return None
                draw = value > draw["above"] if "above" in draw else value <= draw["at most"]
            for index in [write["index"]] if "index" in write else range(write["from"], write["to"] + 1):
                writes[index] = draw
        if (dial := spec.get("dial")) is not None:
            text = next((value for option in inputs.get(dial["option"], ()) if len(value := _strip_value(
                getattr(option.fields.get(dial["field"]), "value", "") or "")) == dial["digits"]), "")
            for digit, character in enumerate(text):
                group = next((index for index, letters in enumerate(dial["groups"]) if character in letters), None)
                if group is not None:
                    first = dial["first part"] + digit * dial["digit stride"]
                    writes.update({first + index: index == group for index in range(len(dial["groups"]))})
        return writes

    def mesh_requests(self, entries, component: ScnComponent, object_id: ScnObjectId) -> None:
        for entry in entries:
            for target in self.request_targets(entry, component, object_id):
                self.meshes.setdefault(target, {})[entry["layer"]] = entry["enabled"]
