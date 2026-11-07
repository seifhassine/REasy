"""See docs/internals/startup-draw-states.md, Time after the start."""
from __future__ import annotations

import operator
from typing import Mapping

lazy import numpy as np

lazy from file_handlers.clip.model import FLAG_NAMES
lazy from file_handlers.engine_math import CurveRule, curve_key, normalized

lazy from .scene_time import CurveAnimation, CurveProcess, CurveWork, Ease, FanParam, PendulumParam, TimelineKeys
lazy from .scn_scene_graph import (
    ScnComponent, ScnObjectId, ScnSceneObject, ScnTimeline, ScnTimelineNode, _bool_field, _numeric_field,
    _object_ref_id, _strip_value, _vector3, normalize_scene_path,
)


def _clip_flagged(prop) -> bool:
    return (any(getattr(prop, flag) for flag in FLAG_NAMES) or bool(prop.extra_keys or prop.speed_points)
            or prop.compact_keys or prop.array_index != -1)


def _clip_keys(spec: Mapping, prop) -> TimelineKeys | None:
    if prop.type != spec["float"] or prop.children or _clip_flagged(prop):
        return None
    keys = []
    for key in prop.keys:
        if key.interpolation != spec["event key"] or key.instance_value:
            return None
        keys.append((float(key.frame), key.value))
    if any(later[0] <= earlier[0] for earlier, later in zip(keys, keys[1:])):
        return None
    return TimelineKeys(float(prop.begin_frame), float(prop.end_frame), tuple(keys))


class StartupTime:
    def curve_animations(self, names) -> tuple[list[CurveAnimation], int]:
        spec = self.rules.get("curve animators")
        play = None if spec is None or names is None else spec.get("play")
        animations, refused = [], 0
        for item in self.document.objects.values() if play else ():
            for component in map(self.document.components.__getitem__, item.components):
                request = self.curve_request(item.id)
                if component.type_name != spec["component"] or not _bool_field(component.fields, self.enabled, True) \
                        or request != "start" and (
                            request != "" or not _bool_field(component.fields, spec["auto"], True)) \
                        or not self.runs(item.id, spec) or item.id in self.curve_started \
                        or self.controlled(spec, item.id) or self.forced_rate(spec, item.id) is not False \
                        or self.asleep(item.id, component.type_name):
                    continue
                found = self.curve_animation(spec, play, component, item.id)
                if found is False:
                    refused += 1
                elif found is not None:
                    animations.append(found)
        return animations, refused

    def simple_animations(self) -> list[tuple[ScnObjectId, ScnComponent, float, list | None]]:
        spec = self.rules.get("simple animations")
        found = []
        for item in self.document.objects.values() if spec else ():
            for component in map(self.document.components.__getitem__, item.components):
                rule = spec["types"].get(component.type_name)
                if rule is None or not _bool_field(component.fields, self.enabled, True) \
                        or not self.runs(item.id, spec) or self.asleep(item.id, component.type_name):
                    continue
                fields = component.fields
                distance = _numeric_field(fields, spec["distance"], 0.0) if _bool_field(
                    fields, spec["own distance"], False) else spec["common distance"]
                params = [self.simple_param(spec, rule, self.ref_fields(value))
                          for value in getattr(fields.get(spec["params"]), "values", None) or ()]
                found.append((item.id, component, float(distance), None if None in params else params))
        return found

    @staticmethod
    def simple_param(spec: Mapping, rule: Mapping, fields: Mapping) -> tuple[str, FanParam | PendulumParam] | None:
        def vector(name, size):
            value = fields.get(rule.get(name, spec.get(name)))
            return None if value is None or not all(hasattr(value, axis) for axis in "xyz"[:size]) else tuple(
                float(getattr(value, axis)) for axis in "xyz"[:size])

        names = [spec["joint"], spec["axis"],
                 *(rule[key] for key in rule if key not in ("kind", "speed ease", "gravity"))]
        axis = vector("axis", 3)
        if axis is None or any(name not in fields for name in names):
            return None
        joint = _strip_value(getattr(fields[spec["joint"]], "value", ""))
        if rule["kind"] == "fan":
            ranges = vector("speed range", 2), vector("cycle range", 2)
            return None if None in ranges else (joint, FanParam(
                None, axis, _numeric_field(fields, rule["speed"], 0.0), rule["speed ease"],
                _bool_field(fields, rule["random"], False), *ranges))
        ranges = (vector("degree range", 2), vector("cycle range", 2), vector("twist axis", 3),
                  vector("twist range", 2), vector("twist period range", 2))
        if None in ranges:
            return None
        degrees, cycles, twist_axis, twists, periods = ranges
        return joint, PendulumParam(
            None, axis, _numeric_field(fields, rule["length"], 0.0), _numeric_field(fields, rule["degree"], 0.0),
            rule["gravity"], _bool_field(fields, rule["random"], False), degrees, cycles,
            _bool_field(fields, rule["infinite"], False),
            _numeric_field(fields, rule["per second"], 0.0), _bool_field(fields, rule["twist"], False), twist_axis,
            _numeric_field(fields, rule["twist start"], 0.0), twists, periods)

    def asleep(self, object_id: ScnObjectId, type_name: str) -> bool:
        if self.sleeping is None:
            self.sleeping = {}
            spec = self.rules.get("sleepers") or {"types": {}}
            for item in self.document.objects.values():
                for component in map(self.document.components.__getitem__, item.components):
                    rule = spec["types"].get(component.type_name)
                    if rule is None or not _bool_field(component.fields, self.enabled, True) \
                            or not self.runs(item.id, spec) or not self.sleeps(rule, component):
                        continue
                    objects, folders = self.sleeping.setdefault(
                        _strip_value(getattr(component.fields.get(rule["component"]), "value", "")), (set(), set()))
                    if int(_numeric_field(component.fields, rule["target"], 0)) != rule["folder"]:
                        objects.add(item.id)
                        continue
                    folder = self.parent.get(item.id)
                    while folder is not None and self.document.objects[folder].kind != "folder":
                        folder = self.parent.get(folder)
                    if folder is not None:
                        folders.add(folder)
        objects, folders = self.sleeping.get(type_name, ((), ()))
        node = object_id
        while node is not None and node not in folders:
            node = self.parent.get(node)
        return object_id in objects or node is not None

    def sleeps(self, rule: Mapping, component: ScnComponent) -> bool:
        conditions = [self.ref_fields(value) for value in getattr(
            component.fields.get(rule["conditions"]), "values", None) or ()]
        if not conditions:
            return True
        if self.campaign is None:
            return False
        compares = self.rules["campaign"]["compares"]
        held = []
        for fields in conditions:
            compare = int(_numeric_field(fields, rule["compare"], -1))
            held.append(0 <= compare < len(compares) and getattr(operator, compares[compare])(
                self.campaign.chapter, int(_numeric_field(fields, rule["chapter"], 0))))
        return any(held) if int(_numeric_field(component.fields, rule["logic"], 0)) == rule["any"] else all(held)

    def controlled(self, spec: Mapping, object_id: ScnObjectId) -> bool:
        node = object_id
        while node is not None:
            if any(self.document.components[other].type_name in spec["controllers"]
                   for other in self.document.objects[node].components):
                return True
            node = self.parent.get(node)
        return False

    def curve_animation(self, spec: Mapping, play: Mapping, component: ScnComponent,
                        object_id: ScnObjectId) -> CurveAnimation | None | bool:
        group = self.ref_fields(component.fields.get(spec["group"]))
        path = _strip_value(getattr(group.get(play["data"]), "string", "") or "")
        if not path:
            return None
        user = self.builder._user_file(path, self.document.document_id)
        if user is None or not user.object_table:
            return False
        data = user.parsed_elements.get(user.object_table[0], {})
        works = {}
        for phase, (has, held) in play["phases"].items():
            container = self.builder._fields(user, _object_ref_id(data.get(held)))
            instance = _object_ref_id(container.get(play["container"]))
            if not _bool_field(data, has, False) or not instance:
                continue
            rule = play["works"].get(self.builder._type_name(user, instance))
            fields = self.builder._fields(user, instance)
            if rule is None:
                return False
            time, random_time = np.float32(_numeric_field(fields, rule["time"], 0.0)), None
            if _bool_field(fields, rule["random"], False):
                random_time = (_numeric_field(fields, rule["min time"], 0.0),
                               _numeric_field(fields, rule["max time"], 0.0))
            elif not time > np.float32(play["least time"]):
                continue
            if "curve" in rule:
                curve, held_curve = self.builder._fields(user, _object_ref_id(fields.get(rule["curve"]))), play["curve"]
                works[phase] = CurveWork(float(time), tuple(curve_key((key.x, key.y, key.z, key.w)) for key in getattr(
                    curve.get(held_curve["keys"]), "values", None) or ()), held_curve["clamp"],
                    _numeric_field(curve, held_curve["min"], 0.0), _numeric_field(curve, held_curve["max"], 0.0),
                    random_time=random_time, curve=CurveRule.of(held_curve))
                continue
            ease = self.ease(play["eases"].get(str(int(_numeric_field(fields, rule["ease"], -1)))))
            if ease is None:
                return False
            if "min span" in rule:
                works[phase] = CurveWork(float(time), ease=ease, random_time=random_time, spans=(
                    _numeric_field(fields, rule["min span"], 0.0), _numeric_field(fields, rule["max span"], 0.0)))
            else:
                works[phase] = CurveWork(float(time), ease=ease, start=_numeric_field(fields, rule["start"], 0.0),
                                         end=_numeric_field(fields, rule["end"], 0.0), random_time=random_time)
        if not works:
            return None
        processes = []
        for container in getattr(group.get(spec["processes"]), "values", None) or ():
            process = self.ref_fields(container).get(spec["process"])
            type_name = self.builder._type_name(self.document.rsz_file, _object_ref_id(process))
            kind, turn = spec["kinds"].get(type_name), play["rotations"].get(type_name)
            if kind is None and turn is None:
                return False
            fields = self.ref_fields(process)
            target = object_id
            if int(_numeric_field(fields, spec["target"], 0)) == spec["child target"]:
                if self.prefab:
                    return False
                target = self.by_guid.get(getattr(fields.get(spec["child"]), "raw_bytes", None))
                if target is None or self.parent.get(target) != object_id:
                    return None
            low, high = _numeric_field(fields, spec["min"], 0.0), _numeric_field(fields, spec["max"], 0.0)
            if turn is not None:
                offset = None
                if _bool_field(fields, turn["offset"], False):
                    transform = self.document.objects[target].transform
                    if transform is None:
                        return False
                    offset = tuple(float(np.float32(value)) for value in transform.rotation)
                x, y, z = (np.float32(value) for value in _vector3(fields.get(turn["axis"])))
                axis = turn["default axis"] if np.float32(turn["least square"]) > (y * y + x * x) + z * z \
                    else normalized((x, y, z))
                processes.append(CurveProcess(target, low, high, axis=tuple(float(value) for value in axis),
                                              offset=offset, radians=tuple(turn["radians"])))
            elif "material" in kind:
                material, variable = self.curve_variable(kind, fields)
                mesh = self.mesh(target)
                materials = None if mesh is None else self.mdf_materials(mesh)
                if mesh is None or materials is not None and variable not in next(
                        (held for name, held in materials if name == material), ()):
                    return None
                # Its writes every frame land after the mesh's own list entry for the variable.
                if materials is None or (target, material, variable) in self.other_writes:
                    return False
                processes.append(CurveProcess(target, low, high, material, variable))
        return CurveAnimation(object_id, works.get("start"), works.get("loop"), tuple(processes),
                              float(play["least time"])) if processes else None

    @staticmethod
    def curve_variable(kind: Mapping, fields: Mapping[str, object]) -> tuple[str, str]:
        variable = kind.get("variable") or _strip_value(getattr(fields.get(kind["variable field"]), "value", ""))
        return _strip_value(getattr(fields.get(kind["material"]), "value", "")), variable

    def timelines(self) -> tuple[list[ScnTimeline], list[tuple[ScnObjectId, str]]]:
        spec, played, refused = self.rules.get("timelines"), [], []
        for item in self.document.objects.values() if spec else ():
            for component in map(self.document.components.__getitem__, item.components):
                fields = component.fields
                if component.type_name != spec["component"] or not _bool_field(fields, self.enabled, True) \
                        or not _bool_field(fields, spec["scene start"], False) or not self.runs(item.id, spec):
                    continue
                found = self.timeline(spec, component, item)
                if isinstance(found, str):
                    refused.append((item.id, found))
                elif found is not None:
                    played.append(found)
        return played, refused

    def timeline(self, spec: Mapping, component: ScnComponent, item: ScnSceneObject) -> ScnTimeline | str | None:
        fields = component.fields
        path = _strip_value(getattr(fields.get(spec["resource"]), "value", ""))
        if not path:
            return None
        if self.prefab:
            return "a prefab's timeline binds in the scene it is spawned in"
        if self.asleep(item.id, component.type_name):
            return "a sleeper stops it after its first update"
        if _bool_field(fields, spec["loop"], False):
            return "it loops"
        if spec["binds"].get(str(int(_numeric_field(fields, spec["bind"], -1)))) != "fix":
            return "its bind type is not read"
        speed = _numeric_field(fields, spec["speed"], 1.0)
        if speed < 0:
            return "it plays backwards"
        clip = self.builder._clip(path, self.document.document_id)
        if clip is None:
            return "its clip is not read"
        end = clip.total_frame
        if _numeric_field(fields, spec["end"], end) != end:
            return "its end frame is not its clip's"
        # The player evaluates each section of a clip apart (its sections' start frames).
        if any(info.frame_in != 0 for track in clip.tracks for info in track.clips):
            return "its clip has sections"
        nodes = []
        for node in clip.roots:
            if node.node_type != spec["object node"] or node.properties:
                return "its clip sets a node kind REasy does not play"
            tracks = []
            for child in node.children:
                drawn = spec["drawn"].get(child.name)
                if drawn is None or child.children:
                    return f"its clip sets {child.name}, which REasy does not play"
                for prop in child.properties:
                    names = spec["vectors"].get(str(prop.type))
                    parts = {part.name: _clip_keys(spec, part) for part in prop.children}
                    if prop.name not in drawn["fields"] or names is None or _clip_flagged(prop) \
                            or not parts.keys() <= set(names) or None in parts.values():
                        return f"its clip sets {child.name}.{prop.name} in a way REasy does not play"
                    tracks.append((drawn["type"], prop.name, tuple(parts.get(name) for name in names)))
            nodes.append(ScnTimelineNode(bytes(node.guid), node.name, tuple(tracks)))
        time_scale = _numeric_field(item.fields, spec["time scale"], -1.0)
        return ScnTimeline(item.id, normalize_scene_path(path), _numeric_field(fields, spec["start"], 0.0), end, speed,
                           _bool_field(fields, spec["event"], False), spec["frames a second"],
                           time_scale if time_scale >= 0 else spec["scene time scale"], tuple(nodes),
                           float(spec["first back"]), float(spec["zero back"]))

    def forced_rate(self, spec: Mapping, object_id: ScnObjectId) -> float | bool | None:
        starters, rates, node = spec.get("flag starters", {}), set(), object_id
        while node is not None:
            held = [self.document.components[other] for other in self.document.objects[node].components]
            if self.runs(node, spec) and any(other.type_name == spec["component"] for other in held):
                for other in held:
                    rule = starters.get(other.type_name)
                    if rule is not None and _bool_field(other.fields, self.enabled, True):
                        conditions = [self.check_condition(other.fields.get(rule[name])) for name in ("start", "ended")]
                        rates.add(1.0 if True in conditions else None if None in conditions else 0.0)
            node = self.parent.get(node)
        if not rates:
            return False
        return rates.pop() if len(rates) == 1 else None

    def curve_request(self, object_id: ScnObjectId) -> str | None:
        node = object_id
        while node is not None:
            if any(not self.applied(node, params) for _component, _rule, params in self.curve_settings.get(node, ())):
                return None
            node = self.parent.get(node)
        return self.curve_requests.get(object_id, "")

    def ease(self, name: str | None) -> Ease | None:
        return None if name is None else Ease.of(name, self.rules["ease constants"][name])
