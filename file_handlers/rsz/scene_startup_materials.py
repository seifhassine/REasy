"""See docs/internals/startup-draw-states.md, Material values at start."""
from __future__ import annotations

from collections import Counter
from typing import Mapping

lazy import numpy as np

lazy from file_handlers.mdf.mdf_file import MdfFile
lazy from utils.hash_util import murmur3_hash_utf16le

lazy from .scene_time import CurveProcess, LightBlink, LightRise, UnitProcess
lazy from .scn_scene_graph import (
    VIA_RENDER_MESH, ScnComponent, ScnObjectId, _bool_field, _numeric_field, _object_ref_id, _strip_value,
)


class StartupMaterials:
    def materials(self, names) -> tuple[dict[ScnObjectId, tuple[dict[int, tuple], tuple[Mapping, ...]]], int]:
        found: dict[ScnObjectId, tuple[dict[int, tuple], list[Mapping]]] = {}
        base: dict[ScnObjectId, tuple[dict[int, tuple], list[Mapping]]] = {}
        self.base, self.curve_writers, self.names, self.rises, self.blinks = {}, {}, names, [], []
        refused = 0
        if names is None:
            return {}, refused

        def merge(written: dict | None) -> int:
            self.merge(base, written, names)
            return self.merge(found, written, names)

        for object_id, entries in self.material_settings.items():
            for component, rule, params in entries:
                if self.runs(object_id, rule):
                    written = None if component.id in self.touched or not self.applied(object_id, params) else {
                        object_id: ({}, self.settings_writes(rule, params, self.finished.get(component.id, ())))}
                    refused += merge(written)
        spec = self.rules.get("animators")
        for item in self.document.objects.values() if spec else ():
            for component in map(self.document.components.__getitem__, item.components):
                if component.type_name == spec["component"] and _bool_field(component.fields, self.enabled, True):
                    known = len(self.rises), len(self.blinks)
                    if merge(self.animator_writes(spec, component, item.id)):
                        refused += 1
                        del self.rises[known[0]:], self.blinks[known[1]:]
        spec = self.rules.get("curve animators")
        undecided: dict[ScnObjectId, set[tuple[str, str]]] = {}
        decided: dict[ScnObjectId, set[tuple[str, str]]] = {}
        for item in self.document.objects.values() if spec else ():
            for component in map(self.document.components.__getitem__, item.components):
                if component.type_name == spec["component"] and _bool_field(component.fields, self.enabled, True) \
                        and self.runs(item.id, spec):
                    written = self.curve_writes(spec, component, item.id)
                    for target, variables in self.curve_variables(spec, component, item.id).items():
                        (undecided if written is None else decided).setdefault(target, set()).update(variables)
                    kept = self.merge(found, written, names)
                    refused += kept
                    if not kept and written:
                        self.curve_writers.setdefault(item.id, set()).update(written)
        spec = self.rules.get("writers")
        for item in self.document.objects.values() if spec else ():
            for component in map(self.document.components.__getitem__, item.components):
                if (rule := spec["types"].get(component.type_name)) is not None and self.runs(item.id, spec):
                    refused += merge(self.writer_writes(spec, rule, component, item.id, names))
        self.other_writes = {(object_id, entry[names.material], entry[names.variable])
                             for object_id, (_replaced, appended) in found.items() for entry in appended}
        # Where a curve animator's variable is undecided the file's value stays: which write lands last is not known.
        refused += self.void(found, undecided, names)
        self.void(base, {target: undecided.get(target, set()) | decided.get(target, set())
                         for target in {*undecided, *decided}}, names)
        # A rise plays while its unit's write is the only one left of its variable, a blink while none is.
        writes = Counter((object_id, entry[names.material], entry[names.variable])
                         for object_id, (_replaced, appended) in found.items() for entry in appended)
        self.rises = [rise for rise in self.rises for process in rise.processes
                      if writes[process.target, process.material, process.variable] == 1]
        self.blinks = [blink for blink in self.blinks for process in blink.processes
                       if not writes[process.target, process.material, process.variable]]
        curved = set().union(*self.curve_writers.values())
        self.base = {key: (replaced, tuple(appended)) for key, (replaced, appended) in base.items() if key in curved}
        return {key: (replaced, tuple(appended)) for key, (replaced, appended) in found.items()}, refused

    def curve_writes(self, spec: Mapping, component: ScnComponent, object_id: ScnObjectId) -> dict | None:
        rate, request = self.forced_rate(spec, object_id), self.curve_request(object_id)
        if rate is None or request is None or object_id in self.curve_started:
            return None
        if rate is False:
            if request == "start" or request != "stop" and (
                    _bool_field(component.fields, spec["auto"], True) or self.controlled(spec, object_id)):
                return None
            rate = 0.0
        elif request == "start":
            # GroupData.applyStart clears the forced rate; which comes last is not known.
            return None
        group = self.ref_fields(component.fields.get(spec["group"]))
        processes = [self.ref_fields(container).get(spec["process"])
                     for container in getattr(group.get(spec["processes"]), "values", None) or ()]
        found: dict[ScnObjectId, tuple[dict[int, tuple], list]] = {}
        for process in processes:
            kind = spec["kinds"].get(self.builder._type_name(self.document.rsz_file, _object_ref_id(process)))
            if kind is None:
                return None
            fields = self.ref_fields(process)
            target = object_id
            if int(_numeric_field(fields, spec["target"], 0)) == spec["child target"]:
                if self.prefab:
                    return None
                target = self.by_guid.get(getattr(fields.get(spec["child"]), "raw_bytes", None))
                if target is None or self.parent.get(target) != object_id:
                    return {}
            if "material" not in kind:
                continue
            material, variable = self.curve_variable(kind, fields)
            if len(processes) > 1:
                mesh = self.mesh(target)
                materials = None if mesh is None else self.mdf_materials(mesh)
                if mesh is None or materials is not None and variable not in next(
                        (variables for name, variables in materials if name == material), ()):
                    return {}
                if materials is None:
                    return None
            low, high = _numeric_field(fields, spec["min"], 0.0), _numeric_field(fields, spec["max"], 0.0)
            found.setdefault(target, ({}, []))[1].append(
                (material, variable, float(np.float32((high - low) * rate + low)), False))
        return found

    def curve_variables(self, spec: Mapping, component: ScnComponent,
                        object_id: ScnObjectId) -> dict[ScnObjectId, set[tuple[str, str]]]:
        group = self.ref_fields(component.fields.get(spec["group"]))
        found: dict[ScnObjectId, set[tuple[str, str]]] = {}
        for container in getattr(group.get(spec["processes"]), "values", None) or ():
            process = self.ref_fields(container).get(spec["process"])
            kind = spec["kinds"].get(self.builder._type_name(self.document.rsz_file, _object_ref_id(process)), {})
            fields = self.ref_fields(process)
            target = object_id
            if int(_numeric_field(fields, spec["target"], 0)) == spec["child target"]:
                if self.prefab:
                    continue
                target = self.by_guid.get(getattr(fields.get(spec["child"]), "raw_bytes", None))
                if target is None or self.parent.get(target) != object_id:
                    return {}
            if "material" in kind:
                found.setdefault(target, set()).add(self.curve_variable(kind, fields))
        return found

    @staticmethod
    def void(found: dict, variables: Mapping[ScnObjectId, set[tuple[str, str]]], names) -> int:
        dropped = 0
        for object_id, held in variables.items():
            if object_id not in found or not held:
                continue
            replaced, appended = found[object_id]
            kept = [entry for entry in appended if (entry[names.material], entry[names.variable]) not in held]
            if len(kept) != len(appended):
                found[object_id] = (replaced, kept)
                dropped += 1
        return dropped

    def writer_writes(self, spec: Mapping, rule: Mapping, component: ScnComponent, object_id: ScnObjectId,
                      names) -> dict | None:
        if not (rule.get("awake") or _bool_field(component.fields, self.enabled, True)) or any(
                _numeric_field(component.fields, name, value) != value
                for name, value in rule.get("fields", {}).items()):
            return None
        held = {self.document.components[other].type_name for other in self.document.objects[object_id].components}
        if any(name not in held for name in rule.get("needs", ())):
            return {}
        writes = rule.get("writes", ())
        if "powered" in rule:
            powered = self.powered(spec["power"], object_id)
            if powered is None:
                return None
            writes = rule["powered" if powered else "unpowered"]
        found: dict[ScnObjectId, tuple[dict[int, Mapping], list]] = {}
        for write in writes:
            spec_object = write.get("object", {})
            if "ref" in spec_object:
                raw = getattr(component.fields.get(spec_object["ref"]), "raw_bytes", None)
                target = None if self.prefab else self.by_guid.get(raw)
                if target is None and (self.prefab or raw and any(raw)):
                    return None
            else:
                target = object_id if not spec_object else (
                    self.child(object_id, spec_object["child"]) if "child" in spec_object
                    else self.find(object_id, spec_object["find"]))
            mesh = None if target is None else self.mesh(target)
            if mesh is None:
                continue
            if "entries" in write:
                # Every entry whose variable's name hash is the name's (MaterialParam.get_VariableHash).
                source = write["scalar"]
                value = self.timer_progress(spec["timers"], component.fields.get(source["timer"])) \
                    if "timer" in source else getattr(component.fields.get(source["field"]), "value", None)
                if value is False:
                    continue
                if not isinstance(value, float):
                    return None
                wanted = murmur3_hash_utf16le(write["entries"])
                for index, entry in enumerate(map(self.ref_fields, getattr(
                        mesh.fields.get(names.list), "values", None) or ())):
                    if murmur3_hash_utf16le(_strip_value(getattr(entry.get(names.variable), "value", ""))) == wanted:
                        found.setdefault(target, ({}, []))[0][index] = {names.scalar: float(np.float32(value))}
                continue
            if "entry" in write:
                count = len(getattr(mesh.fields.get(names.list), "values", None) or ())
                if "scalar" in write:
                    # The script writes only the entries the list has.
                    if count <= write["entry"]:
                        continue
                    value = self.user_value(write["scalar"], component) if "user" in write["scalar"] else getattr(
                        component.fields.get(write["scalar"]["field"]), "value", None)
                    if not isinstance(value, float):
                        return None
                    found.setdefault(target, ({}, []))[0][write["entry"]] = {names.scalar: float(np.float32(value))}
                    continue
                color = write["vector"]
                colors = getattr(component.fields.get(color["field"]), "values", None) or ()
                if count <= write["entry"] or len(colors) <= color["index"]:
                    return None
                found.setdefault(target, ({}, []))[0][write["entry"]] = {names.vector: tuple(
                    float(np.float32(getattr(colors[color["index"]], channel) / color["scale"])) for channel in "rgba")}
                continue
            value = write["value"]
            if isinstance(value, Mapping):
                option = (self.inputs(object_id).get(value["option"]) or [None])[-1]
                if option is None:
                    continue
                value = _numeric_field(option.fields, value["field"], 0.0)
            if "index" in write:
                materials = self.mdf_materials(mesh)
                if materials is None:
                    return None
                material = materials[write["index"]][0] if write["index"] < len(materials) else ""
                if [name for name, _variables in materials].count(material) > 1:
                    return None
                if not material or write.get("contains", "") not in material:
                    continue
            else:
                material = _strip_value(getattr(component.fields.get(write["material"]), "value", ""))
            found.setdefault(target, ({}, []))[1].append((material, write["variable"], float(np.float32(value)), False))
        return found

    def timer_progress(self, spec: Mapping, value) -> float | bool | None:
        guid = bytes(getattr(value, "raw_bytes", b"") or b"")
        condition_spec = self.options["flag condition"]
        found = []
        for item in self.document.objects.values() if any(guid) else ():
            components = [self.document.components[other] for other in item.components]
            registrar = next((other for other in components if other.type_name == spec["component"]), None)
            flags = next((other for other in components if other.type_name == spec["flags"]), None)
            if registrar is None or flags is None or not _bool_field(registrar.fields, self.enabled, True) \
                    or not self.runs(item.id, spec):
                continue
            param = next((fields for _instance, fields in self.params(flags)
                          if self.key(fields) == murmur3_hash_utf16le(spec["key"])), None)
            checks = [] if param is None else getattr(self.ref_fields(param.get(condition_spec["field"])).get(
                condition_spec["flags"]), "values", None) or []
            if checks and bytes(getattr(self.ref_fields(checks[0]).get(condition_spec["flag"]), "raw_bytes", b"")
                                or b"") == guid:
                found.append((registrar, param))
        if not found:
            return False
        if len(found) > 1 or self.campaign is None:
            return None
        registrar, param = found[0]
        chapter = int(_numeric_field(registrar.fields, spec["chapter"], -1))
        if chapter != -1 and self.campaign.chapter != -1 and chapter < self.campaign.chapter:
            return 1.0
        if (started := self.campaign.check([(guid, True)], 0)) is not True:
            return started
        return 1.0 if self.condition(param) is True else None

    def mesh(self, object_id: ScnObjectId) -> ScnComponent | None:
        return next((other for other in map(self.document.components.__getitem__,
                                            self.document.objects[object_id].components)
                     if other.type_name == VIA_RENDER_MESH), None)

    def mdf_materials(self, mesh: ScnComponent) -> list[tuple[str, dict[str, tuple]]] | None:
        _mesh_path, path, _parts = self.builder._mesh_from_fields(mesh.fields)
        resolver = self.builder.resource_resolver
        if not path or resolver is None:
            return None
        if path not in self.mdfs:
            resolved, self.mdfs[path] = resolver(path, self.document.document_id), None
            if resolved is not None and resolved.data is not None:
                try:
                    mdf = MdfFile()
                    mdf.read(resolved.data, resolved.path)
                    self.mdfs[path] = [(_strip_value(material.header.mat_name),
                                        {parameter.name: parameter.parameter for parameter in material.parameters})
                                       for material in mdf.materials]
                except Exception as exc:
                    self.builder._unread(path, self.document.document_id, exc)
        return self.mdfs[path]

    def merge(self, found: dict, written: dict | None, names) -> int:
        if written is None or any(appended is None for _replaced, appended in written.values()):
            return 1
        for object_id, (_replaced, appended) in written.items():
            mesh = self.mesh(object_id)
            listed = {(_strip_value(getattr(entry.get(names.material), "value", "")),
                       _strip_value(getattr(entry.get(names.variable), "value", "")))
                      for entry in map(self.ref_fields, getattr(mesh.fields.get(names.list), "values", None) or ())
                      } if mesh is not None else set()
            if any((material, variable) in listed for material, variable, _value, _vector in appended):
                return 1
        for object_id, (replaced, appended) in written.items():
            entry = found.setdefault(object_id, ({}, []))
            entry[0].update(replaced)
            entry[1].extend(frozendict({names.material: material, names.variable: variable,
                                        names.vector if vector else names.scalar: value})
                            for material, variable, value, vector in appended)
        return 0

    def settings_writes(self, rule: Mapping, params: list[tuple[int, Mapping]], finished=()) -> list | None:
        appended = []
        for phase, index, fields in [("start", 0, fields) for _instance, fields in params] + [
                (phase, index, fields) for key in finished for _instance, fields in params if self.key(fields) == key
                for phase, index in (("start", 0), ("end", 1))]:
            detail = fields.get(rule["detail"])
            writes = rule["writes"].get(self.builder._type_name(self.document.rsz_file, _object_ref_id(detail)), {})
            if phase not in writes:
                return None
            detail = self.ref_fields(detail)
            for write in writes[phase]:
                rate = float(index)
                if "ease" in write:
                    rates = self.rules["eases"].get(str(int(_numeric_field(detail, write["ease"], -1))))
                    if rates is None:
                        return None
                    rate = rates[index]
                start, end = detail.get(write["from"]), detail.get(write.get("to", write["from"]))
                if write["vector"]:
                    # The work eases each byte, truncates it back to a byte and writes the bytes as floats.
                    value = tuple(float(int(np.float32(np.float32(getattr(start, channel)) + np.float32(
                        getattr(end, channel) - getattr(start, channel)) * np.float32(rate))) & 0xFF)
                        for channel in "rgba")
                else:
                    low, high = (float(getattr(item, "value", 0.0)) for item in (start, end))
                    value = float(np.float32(np.float32(low) + np.float32(high - low) * np.float32(rate)))
                appended.append((_strip_value(getattr(detail.get(write["material"]), "value", "")),
                                 write["variable"], value, write["vector"]))
        return appended

    def animator_writes(self, spec: Mapping, component: ScnComponent, object_id: ScnObjectId) -> dict | None:
        owners, node = [], object_id
        while node is not None and not owners:
            owners = [(other, node, spec["owners"][base]) for other in map(
                self.document.components.__getitem__, self.document.objects[node].components)
                for base in spec["owners"] if self.builder._type_is_a(other.type_name, base)]
            node = self.parent.get(node)
        if not owners or any("gap" in rule or not _bool_field(owner.fields, self.enabled, True)
                             for owner, _node, rule in owners):
            return None
        replaced: dict[int, tuple] = {}
        units = [self.ref_fields(container).get(spec["unit"])
                 for container in getattr(component.fields.get(spec["units"]), "values", None) or ()]
        on, moving = set(), {}
        for owner, node, rule in owners:
            if "child" in rule:
                node = self.child(node, rule["child"])
            elif "find" in rule:
                node = self.find(node, rule["find"])
            elif "ref" in rule:
                if self.prefab:
                    return None
                node = self.by_guid.get(getattr(owner.fields.get(rule["ref"]), "raw_bytes", None))
            if node != object_id:
                continue
            if "when" not in rule or _bool_field(owner.fields, rule["when"], False):
                on.update(rule.get("on", ()))
                if "moves after" in rule:
                    moving.update(dict.fromkeys(rule.get("on", ()), rule["moves after"]))
            if (color := rule.get("color")) is not None:
                colors = getattr(owner.fields.get(color["field"]), "values", None) or ()
                if len(colors) <= color["index"]:
                    return None
                value = colors[color["index"]]
                replaced[color["entry"]] = {self.names.vector: tuple(
                    float(np.float32(getattr(value, channel) / color["scale"])) for channel in "rgba")}
        appended, rises, blinks = [], [], []
        curves = spec["curve"]
        for index, unit in enumerate(units):
            fields = self.ref_fields(unit)
            kind = spec["kinds"].get(self.builder._type_name(self.document.rsz_file, _object_ref_id(unit)))
            if kind is None:
                return None
            name = _strip_value(getattr(fields.get(spec["key"]), "value", ""))
            enabled = index in on or name in on
            param = self.ref_fields(fields.get(spec["param"]) if _bool_field(fields, spec["own"], False)
                                    else component.fields.get(kind["shared"]))
            low, high = (float(_numeric_field(param, kind[bound], 0.0)) for bound in ("min", "max"))
            material = _strip_value(getattr(fields.get(spec["material"]), "value", ""))
            if "cycle" in kind:
                if enabled:
                    cycle = np.float32(_numeric_field(param, kind["cycle"], 0.0))
                    values = self.mdf_variable(object_id, material, spec["variable"])
                    if (delay := moving.get(index, moving.get(name))) is None or not cycle > 0 or values is None:
                        return None
                    if values:
                        blinks.append(LightBlink(object_id, (CurveProcess(object_id, low, high, material,
                                                                          spec["variable"]),),
                                                 float(cycle), float(kind["full turn"]), float(kind["quarter turn"]),
                                                 delay, float(np.float32(values[0]))))
                continue
            curve = self.ref_fields(param.get(kind["on" if enabled else "off"]))
            rates = None if int(_numeric_field(curve, curves["type"], -1)) == curves["keyframes"] else \
                self.rules["eases"].get(str(int(_numeric_field(curve, curves["ease"], -1))))
            if rates is None:
                return None
            ends = [float(np.float32(np.float32(rate) * np.float32(high - low) + np.float32(low))) for rate in rates]
            if not any(ends if enabled else ends[:1]):
                continue
            appended.append((material, spec["variable"], ends[-1] if enabled else ends[0], False))
            delay = moving.get(index, moving.get(name)) if enabled else None
            if delay is not None and (rise := self.light_rise(spec, kind, param, material, (low, high), object_id,
                                                              delay)) is not None:
                rises.append(rise)
        self.rises.extend(rises)
        self.blinks.extend(blinks)
        return {object_id: (replaced, appended)} if replaced or appended else {}

    def mdf_variable(self, object_id: ScnObjectId, material: str, variable: str) -> tuple | None:
        mesh = self.mesh(object_id)
        materials = () if mesh is None else self.mdf_materials(mesh)
        if materials is None:
            return None
        return next((held.get(variable, ()) for name, held in materials if name == material), ())

    def light_rise(self, spec: Mapping, kind: Mapping, param: Mapping, material: str, bounds: tuple[float, float],
                   object_id: ScnObjectId, delay: int) -> LightRise | None:
        curves, low, high = spec["curve"], *bounds
        on, off = (self.ref_fields(param.get(kind[name])) for name in ("on", "off"))
        time = np.float32(_numeric_field(param, kind["time"], 0.0))
        ease = self.ease(curves["eases"].get(str(int(_numeric_field(on, curves["ease"], -1)))))
        values = self.mdf_variable(object_id, material, spec["variable"])
        if ease is None or not time > 0 or not values:
            return None
        if delay:
            rates = None if int(_numeric_field(off, curves["type"], -1)) == curves["keyframes"] else \
                self.rules["eases"].get(str(int(_numeric_field(off, curves["ease"], -1))))
            if rates is None or np.float32(np.float32(rates[0]) * np.float32(high - low) + np.float32(low)) != 0:
                return None
        return LightRise(object_id, (UnitProcess(object_id, low, high, material, spec["variable"]),), float(time),
                         ease, delay, float(np.float32(values[0])) if delay else None)
