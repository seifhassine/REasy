"""See docs/internals/startup-draw-states.md, Transforms at start."""
from __future__ import annotations

from typing import Mapping

lazy import numpy as np

lazy from file_handlers.engine_math import axes_quaternion, axis_rotation, euler_quaternion, euler_rotation

lazy from .scene_time import radians
lazy from .scn_scene_graph import ScnComponent, ScnObjectId, _bool_field, _numeric_field, _object_ref_id, _strip_value


# _UNKNOWN: an object a write names that is not known here; _NONE: no parent to move the target under.
_UNKNOWN, _NONE = object(), object()


class StartupTransforms:
    def transform(self, spec: Mapping, component: ScnComponent, object_id: ScnObjectId) -> dict | None:
        if (node := self.node(spec, component, object_id)) is _UNKNOWN:
            return None
        if spec.get("context") and not self.own_context(object_id):
            return None
        if spec.get("with parent") and self.node("parent", component, object_id) is None:
            return {"targets": []}
        if (kind := spec.get("when parent holds")) is not None and not self.holds(
                self.node("parent", component, object_id), kind):
            return {"targets": []}
        write: dict = {"targets": [] if node is None else [node]}
        if spec.get("queued"):
            write["queued"] = True
        if (moved := spec.get("parent")) is not None:
            if (destination := self.destination(moved, component, object_id)) is _UNKNOWN:
                return None
            if destination is not _NONE:
                write["parent"] = destination
        if (source := spec.get("world position")) is not None:
            if (found := self.node(source, component, object_id)) is _UNKNOWN:
                return None
            if found is not None:
                write["world position"] = found
        if (point := spec.get("world point")) is not None:
            if (found := self.node(point["of"], component, object_id)) is _UNKNOWN:
                return None
            entries = getattr(component.fields.get(point["list"]), "values", None) or ()
            if found is not None and point["entry"] < len(entries):
                offset = self.ref_fields(entries[point["entry"]]).get(point["field"])
                write["world point"] = (found, (offset.x, offset.y, offset.z))
        if (gate := spec.get("gate")) is not None:
            if (joint := self.gate_joint(gate, component, object_id)) is None:
                return None
            if joint is False:
                return {"targets": []}
            if joint:
                write["parent joint"] = joint
        if not self._write_vectors(spec, write, component, object_id, node):
            return None
        if not self._write_parent_joint(spec, write, component, node):
            return None
        if not self._write_rotation(spec, write, component):
            return None
        if (turn := spec.get("euler add")) is not None:
            if (angle := self.angle(turn, component)) is None:
                return None
            write["euler add"] = (turn["axis"], angle)
        if (source := spec.get("world rotation")) is not None:
            if (found := self.node(source, component, object_id)) is _UNKNOWN:
                return None
            if found is not None:
                write["world rotation"] = found
        if (copied := spec.get("parent joint pose")) is not None and node is not None:
            kind, name = copied
            if (held := self.held(node, kind)) is None:
                return {"targets": []}
            if not isinstance(key := getattr(held.fields.get(name), "value", None), int):
                return None
            write["parent joint pose"] = key
        if (joint := spec.get("joint")) is not None and node is not None:
            write["joint"] = joint
            if (value := spec.get("joint position")) is not None:
                if (vector := self.vector(value, component, node)) is None:
                    return None
                write["joint position"] = vector
            if (value := spec.get("joint rotation")) is not None:
                write["joint rotation"] = axes_quaternion(value["axes"])
        return write

    def _write_vectors(self, spec: Mapping, write: dict, component: ScnComponent, object_id: ScnObjectId, node) -> bool:
        for key in ("position", "scale"):
            if isinstance(value := spec.get(key), Mapping) and "by" in value:
                value = value["cases"].get(str(getattr(component.fields.get(value["by"]), "value", None)))
            elif isinstance(value, Mapping) and "by stratum" in value:
                kind, name = value["by stratum"]
                option = (self.inputs(object_id).get(kind) or [None])[-1]
                if (held := False if option is None else self.stratum(option.fields.get(name))) is None:
                    return False
                value = value["cases"][str(held).lower()]
            if value is not None:
                if (vector := self.vector(value, component, node)) is None:
                    return False
                write[key] = vector
        return True

    def _write_parent_joint(self, spec: Mapping, write: dict, component: ScnComponent, node) -> bool:
        if (joint := spec.get("parent joint")) is not None:
            held = isinstance(joint, Mapping) and joint.get("held")
            if isinstance(joint, Mapping):
                joint = _strip_value(getattr(self.builder._field_at(self.document, component.fields, joint["field"]),
                                             "value", "") or "") or joint.get("else", "")
            if joint and held and node is not None:
                holder = write["parent"] if "parent" in write else self.parent.get(node)
                joints, unread = (None, None) if holder is None else self.builder._object_joints(
                    self.document, self.document.objects[holder])
                if unread is not None:
                    return False
                joint = joint if joints is not None and joint in joints.names else ""
            if joint:
                write["parent joint"] = joint
        return True

    def _write_rotation(self, spec: Mapping, write: dict, component: ScnComponent) -> bool:
        if (rotation := spec.get("rotation")) is not None:
            if not isinstance(rotation, Mapping):
                write["rotation"] = tuple(float(np.float32(value)) for value in rotation)
            elif (degrees := rotation.get("euler degrees")) is not None:
                if (vector := self.vector(degrees, component)) is None:
                    return False
                write["rotation"] = euler_quaternion(
                    [float(np.float32(value * rotation["per degree"])) for value in vector])
            elif (path := rotation.get("field path")) is not None:
                found = self.builder._field_at(self.document, component.fields, path)
                if any(not isinstance(getattr(found, name, None), float) for name in "xyzw"):
                    return False
                write["rotation"] = tuple(float(np.float32(getattr(found, name))) for name in "xyzw")
            else:
                if (half := rotation.get("half angle")) is None:
                    if (angle := self.angle(rotation, component)) is None:
                        return False
                    half = np.float32(angle) * np.float32(rotation["half"])
                write["rotation"] = axis_rotation(rotation["axis"], float(np.float32(half)))
        return True

    def holds(self, object_id: ScnObjectId | None, kind: str) -> bool:
        return object_id is not None and self.held(object_id, kind) is not None

    def held(self, object_id: ScnObjectId, kind: str) -> ScnComponent | None:
        return next((other for other in map(self.document.components.__getitem__,
                                            self.document.objects[object_id].components)
                     if self.builder._type_is_a(other.type_name, kind)), None)

    def node(self, spec, component: ScnComponent, object_id: ScnObjectId):
        if spec in ("parent", "grandparent"):
            node: ScnObjectId | None = object_id
            for _step in range(1 if spec == "parent" else 2):
                node = None if node is None else self.parent.get(node)
                node = None if node is None or self.document.objects[node].kind == "folder" else node
            return node
        if not isinstance(spec, Mapping):
            return object_id
        if "child" in spec:
            node = object_id
            for name in spec["child"].split("/"):
                node = None if node is None else self.child(node, name)
            return node
        if "find" in spec:
            node = object_id
            for step in spec["find"] if isinstance(spec["find"], list) else [spec["find"]]:
                if isinstance(step, Mapping):
                    step = _strip_value(getattr(component.fields.get(step["field"]), "value", "") or "")
                node = None if node is None or not step else self.find(node, step)
            return node
        if "child holding" in spec:
            return next((node for node in self.children.get(object_id, ()) if any(
                self.builder._type_is_a(self.document.components[other].type_name, spec["child holding"])
                for other in self.document.objects[node].components)), None)
        if "ref" in spec:
            return self.ref_node(self.builder._field_at(self.document, component.fields, spec["ref"]))
        return object_id

    def ref_node(self, value):
        raw = getattr(value, "raw_bytes", None)
        node = None if self.prefab else self.by_guid.get(raw)
        return _UNKNOWN if node is None and (self.prefab or raw and any(raw)) else node

    def destination(self, spec, component: ScnComponent, object_id: ScnObjectId):
        choices = [spec]
        if isinstance(spec, Mapping) and "to" in spec:
            if spec.get("with parent") and self.node("parent", component, object_id) is None:
                return _NONE
            choices = [spec["to"], spec.get("else")]
        for choice in choices:
            if choice == "root":
                return None
            if choice is not None and (found := self.node(choice, component, object_id)) is not None:
                return found
        return _NONE

    def vector(self, spec, component: ScnComponent, target: ScnObjectId | None = None) -> tuple | None:
        if not isinstance(spec, Mapping):
            parts = []
            for value in spec:
                if value == "keep":
                    parts.append(None)
                    continue
                if isinstance(value, Mapping):
                    if "target field" in value:
                        kind, name = value["target field"]
                        held = None if target is None else self.held(target, kind)
                        field = None if held is None else getattr(held.fields.get(name), "value", None)
                    else:
                        field = getattr(component.fields.get(value["field"]), "value", None)
                    if not isinstance(field, float):
                        return None
                    value = float(np.float32(field)) * value.get("times", 1.0) + value.get("add", 0.0)
                parts.append(float(np.float32(value)))
            return tuple(parts)
        if "field path" in spec:
            found = self.builder._field_at(self.document, component.fields, spec["field path"])
            if any(not isinstance(getattr(found, name, None), float) for name in "xyz"):
                return None
            return tuple(float(np.float32(getattr(found, name))) for name in "xyz")
        fields = self.user_fields(spec, component)
        value = None if fields is None else fields.get(spec["field"])
        found = tuple(getattr(value, name, None) for name in "xyz")
        return None if any(not isinstance(item, float) for item in found) else tuple(
            float(np.float32(item)) for item in found)

    def gate_joint(self, spec: Mapping, component: ScnComponent, object_id: ScnObjectId) -> str | bool | None:
        parent = self.parent.get(object_id)
        gate = None if parent is None else next(
            (other for other in map(self.document.components.__getitem__, self.document.objects[parent].components)
             if self.builder._type_is_a(other.type_name, spec["base"])), None)
        if gate is None:
            return False
        preferred = next((joint for type_name, joint in spec["joints"].items()
                          if self.builder._type_is_a(gate.type_name, type_name)), None)
        if preferred is None:
            return ""
        joints, unread = self.builder._object_joints(self.document, self.document.objects[parent])
        if unread is not None:
            return None
        names = joints.names if joints is not None else ()
        if preferred in names:
            return preferred
        named = _strip_value(getattr(component.fields.get(spec["else"]), "value", "") or "")
        return named if named and named in names else ""

    def angle(self, spec: Mapping, component: ScnComponent) -> float | None:
        value = getattr(self.builder._field_at(self.document, component.fields, spec["degrees"]), "value", None)
        return None if type(value) is not int else float(np.float32(value)) * spec["per degree"]

    def elevator_doors(self, spec: Mapping, component: ScnComponent, object_id: ScnObjectId) -> list[dict] | None:
        if spec.get("context") and not self.own_context(object_id):
            return []
        closes = _bool_field(component.fields, spec["unless"], False)
        if not closes and (below := spec.get("chapter below")) is not None:
            # Its init sets the unless flag from that chapter on.
            if self.campaign is None:
                return None
            closes = self.campaign.chapter >= below
        if closes:
            force, option = spec["force open"], None
            for node in (object_id, self.child(object_id, self.options["param object"])):
                held = () if node is None else map(self.document.components.__getitem__,
                                                    self.document.objects[node].components)
                option = next((other for other in held if other.type_name == force["option"]), option)
            if not (forced := option is not None and self.stratum(option.fields.get(force["strata"]))):
                return None if forced is None else []
        doors, start = [], spec["start floor"]
        box = self.child(object_id, spec["box"])
        if box is not None and not _bool_field(component.fields, spec["inner unless"], False):
            for node in self.children.get(box, ()):
                door = self.held(node, spec["door"])
                floor = None if door is None else int(_numeric_field(door.fields, spec["floor"], -1))
                if floor is not None and (floor < 0 or floor == start):
                    doors.append((node, door))
        floors = getattr(component.fields.get(spec["floors"]), "values", None) or ()
        first = self.ref_fields(floors[start]) if len(floors) > start else {}
        for ident in getattr(first.get(spec["outside"]), "values", None) or ():
            node = self.gimmick(self.context_key(self.ref_fields(ident)))
            if node is not None and (door := self.held(node, spec["door"])) is not None:
                doors.append((node, door))
        writes = []
        for node, door in doors:
            transform = self.document.objects[node].transform
            axis = spec["directions"].get(str(int(_numeric_field(door.fields, spec["direction"], -1))))
            # Read for a door that starts at its local origin unturned and moves its own transform (no joint).
            if axis is None or transform is None or any(transform.position) or \
                    tuple(transform.rotation) != (0.0, 0.0, 0.0, 1.0) or \
                    _strip_value(getattr(door.fields.get(spec["joint"]), "value", "") or ""):
                return None
            size = np.float32(_numeric_field(door.fields, spec["size"], 0.0))
            writes.append({"targets": [node], "position": tuple(float(np.float32(value) * size) for value in axis)})
        return writes

    def books(self, spec: Mapping, object_id: ScnObjectId,
              inputs: Mapping[str, list[ScnComponent]]) -> tuple[list, list] | None:
        if not self.own_context(object_id):
            return None
        option = next((other for other in reversed(inputs.get(spec["option"], ())) if len(
            getattr(other.fields.get(spec["list"]), "values", None) or ()) == spec["count"]), None)
        if option is None:
            return None
        requests, writes = [], []
        for index, value in enumerate(option.fields[spec["list"]].values):
            data, book = self.ref_fields(value), self.child(object_id, spec["child"].format(index))
            if book is None or not self.holds(book, spec["updater"]):
                return None
            slot = int(_numeric_field(data, spec["slot"], 0)) & spec["slot mask"]
            writes.append({"targets": [book], "parent joint": "", "slot": (object_id, spec["joint"].format(slot + 1)),
                           "flip": None if _bool_field(data, spec["face"], False) else spec["flip"]})
            if not _bool_field(data, spec["set"], False):
                requests.append({"targets": [book], "layer": spec["layer"], "draw": False})
        return requests, writes

    def linked_doors(self, spec: Mapping, component: ScnComponent) -> tuple[list, list] | None:
        current = getattr(component.fields.get(spec["start"]), "value", None)
        requests, writes = [], []
        for value in getattr(component.fields.get(spec["params"]), "values", None) or ():
            param = self.ref_fields(value)
            if (holder := self.ref_node(param.get(spec["object"]))) is _UNKNOWN:
                return None
            opened = getattr(param.get(spec["item"]), "value", None) == current
            for child in self.children.get(holder, ()) if holder is not None else ():
                for door in map(self.document.components.__getitem__, self.document.objects[child].components):
                    if door.type_name != spec["door"]:
                        continue
                    if (target := self.ref_node(door.fields.get(spec["ref"]))) is _UNKNOWN:
                        return None
                    if target is None:
                        continue
                    write = {"targets": [target], "world position": child}
                    if opened:
                        offset = door.fields.get(spec["offset"])
                        write["world offset"] = (offset.x, offset.y, offset.z)
                        requests.append({"targets": [target], "layer": spec["layer"], "draw": False})
                    writes.append(write)
        return requests, writes

    def door_lock(self, spec: Mapping, component: ScnComponent, object_id: ScnObjectId) -> list[dict] | None:
        door = self.node("parent", component, object_id)
        if door is None or not any(self.builder._type_is_a(self.document.components[other].type_name, spec["door"])
                                   for other in self.document.objects[door].components):
            return []
        named = _strip_value(getattr(component.fields.get(spec["field"]), "value", "") or "")
        joints, unread = self.builder._object_joints(self.document, self.document.objects[door])
        if not named and unread is not None:
            return None
        names = () if joints is None else joints.names
        writes = []
        for part in spec["parts"]:
            if (target := self.child(object_id, part["child"])) is None:
                continue
            if part.get("world position"):
                writes.append({"targets": [target], "world position": door})
            writes.append({"targets": [target], "parent": door, "queued": True})
            choice = {"joint": named} if named else next(
                (choice for choice in part["joints"] if choice["joint"] in names), part["joints"][-1])
            if "position" in choice:
                writes.append({"targets": [target],
                               "position": tuple(float(np.float32(v)) for v in choice["position"])})
            writes.append({"targets": [target], "parent joint": choice["joint"], "queued": True})
        if (above := self.destination(spec.get("self", "self"), component, object_id)) is _UNKNOWN:
            return None
        if "self" in spec and above is not _NONE:
            writes.append({"targets": [object_id], "parent": above, "queued": True})
        return writes

    def panel_stand(self, spec: Mapping, component: ScnComponent, object_id: ScnObjectId) -> tuple[list, list] | None:
        requests = []
        light = _strip_value(getattr(component.fields.get(spec["light"]), "value", "") or "")
        if light and (found := self.find(object_id, light)) is not None:
            requests.append({"targets": [found], "layer": spec["layer"], "draw": False})
        stand, holder = self.find(object_id, spec["stand"]), self.find(object_id, spec["holder"])
        if stand is None or holder is None:
            return None
        held, unread = self.builder._object_joints(self.document, self.document.objects[holder])
        own, unknown = self.builder._object_joints(self.document, self.document.objects[stand])
        if unread is not None or unknown is not None or held is None or len(held.names) <= spec["joint index"]:
            return None
        writes = [{"targets": [stand], "parent joint": held.names[spec["joint index"]], "queued": True}]
        index = 0
        while (point := self.find(object_id, spec["point"].format(index))) is not None:
            transform = self.document.objects[point].transform
            name = transform.parent_joint if transform is not None else ""
            if own is None or name not in own.names:
                return None
            writes.append({"targets": [stand], "joint": name, "joint rotation": euler_rotation(spec["euler"])})
            index += 1
        return requests, writes

    def panel_frames(self, spec: Mapping, component: ScnComponent, object_id: ScnObjectId) -> tuple[list, list] | None:
        if self.campaign is None or (bit := spec["difficulty bits"].get(str(self.campaign.difficulty))) is None:
            return None
        param = next((fields for fields in map(self.ref_fields, getattr(component.fields.get(spec["params"]), "values",
                                                                         None) or ())
                      if int(_numeric_field(fields, spec["mask"], 0)) & bit), None)
        path = "" if param is None else _strip_value(getattr(param.get(spec["user"]), "string", "") or "")
        user = self.builder._user_file(path, self.document.document_id) if path else None
        root = self.builder._user_fields(path, self.document.document_id) if user is not None else None
        if root is None:
            return None
        panels: list[ScnObjectId] = []
        while (panel := self.find(object_id, spec["panel"].format(len(panels)))) is not None:
            panels.append(panel)
        left, frames, waiting = list(range(len(self.enums.get(spec["types"], {})))), [], []
        for value in getattr(root.get(spec["frames"]), "values", None) or ():
            fields = self.builder._fields(user, _object_ref_id(value))
            kind = int(_numeric_field(fields, spec["type"], -1))
            if kind in left:
                left.remove(kind)
                frames.append((fields, kind))
            else:
                waiting.append(fields)
        if len(waiting) > len(left):
            return None
        frames += [(fields, left[index]) for index, fields in enumerate(waiting)]
        if any(not 0 <= kind < len(panels) for _fields, kind in frames):
            return None
        items: dict[int, int] = {}
        for name, attr in spec["items"].items():
            items.setdefault(int(_numeric_field(component.fields, name, -1)), attr)
        requests = [{"targets": [panels[kind]], "layer": spec["layer"],
                     "draw": not _bool_field(fields, spec["miss"], False)} for fields, kind in frames]
        for fields, kind in frames:
            if _bool_field(fields, spec["miss"], False):
                if int(_numeric_field(fields, spec["item"], -1)) in items and self.own_context(object_id) is not True:
                    return None
                requests.append({"targets": [panels[kind]], "layer": spec["layer"], "draw": False})
        writes = []
        for index, (_fields, kind) in enumerate(frames, 1):
            writes.append({"targets": [panels[kind]], "position": tuple(spec["position"])})
            writes.append({"targets": [panels[kind]], "parent joint": spec["frame joint"].format(index),
                           "queued": True})
        groups = {member: group for group, members in enumerate(spec["groups"]) for member in members}
        for index, (_fields, kind) in enumerate(frames):
            joints, unread = self.builder._object_joints(self.document, self.document.objects[panels[kind]])
            if unread is not None or joints is None or spec["panel joint"] not in joints.names:
                return None
            degrees = spec["degrees"][groups[kind]][groups[index]] if kind in groups and index in groups else 0.0
            angle = radians(degrees, spec["radians"])
            writes.append({"targets": [panels[kind]], "euler": tuple(spec["euler"])})
            writes.append({"targets": [panels[kind]], "joint": spec["panel joint"],
                           "joint rotation": euler_rotation((0.0, float(angle), 0.0))})
        return requests, writes

    def relation(self, spec: Mapping, inputs: Mapping[str, list[ScnComponent]]) -> tuple[list, list] | None:
        option = (inputs.get(spec["option"]) or [None])[-1]
        ids = getattr(option.fields.get(spec["ids"]), "values", None) or () if option is not None else ()
        if not ids:
            return [], []
        related = self.context_key(self.ref_fields(ids[0]))
        door = self.gimmick(related)
        if door is None:
            return None
        if not any(self.builder._type_is_a(self.document.components[other].type_name, spec["door"])
                   for other in self.document.objects[door].components):
            return [], []
        bro, pairs = spec["bro"], []
        for key, node in self.cores.items():
            for other in self.inputs(node).get(bro["option"], ()):
                named = self.context_key(self.ref_fields(other.fields.get(bro["id"])))
                if node == door or named == related:
                    pairs.append((list(key), named, named if node == door else list(key)))
        found = [{self.context_listed(key) for key in pair[:2]} for pair in pairs]
        if not any(states == {True} for states in found):
            return None if any(None in states for states in found) else ([], [])
        for type_name, name in spec.get("sleep", {}).items():
            for other in inputs.get(type_name, ()):
                if (asleep := self.stratum(other.fields.get(name))) is not False:
                    return None if asleep is None else ([], [])
        if (moved := self.child(door, spec["moved"])) is None:
            return list(spec["requests"]), []
        bros = {self.gimmick(pair[2]) for pair, states in zip(pairs, found) if states == {True}}
        if len(bros) != 1 or None in bros:
            return None
        return list(spec["requests"]), [{"targets": [moved], "parent": bros.pop(), "queued": True},
                                        {"targets": [moved], "parent joint": spec["joint"], "queued": True}]
