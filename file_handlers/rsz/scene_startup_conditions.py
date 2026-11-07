"""See docs/internals/startup-draw-states.md, Inputs on a new game and Campaign state."""
from __future__ import annotations

import operator
from typing import Mapping

lazy from utils.hash_util import murmur3_hash_utf16le

lazy from .campaign_state import three_valued
lazy from .scn_scene_graph import ScnComponent, ScnObjectId, _bool_field, _numeric_field, _object_ref_id, _strip_value


class StartupConditions:
    def turned_on(self, spec: Mapping, component: ScnComponent, object_id: ScnObjectId) -> bool | None:
        if int(_numeric_field(component.fields, spec["mode"], 0)) != spec["flags"]:
            dynamic = self.context_key(self.ref_fields(component.fields.get(spec["target"])))[0]
            return None if dynamic == self.options["contexts"]["dynamic"] else False
        own = {spec["type"]: [self.document.components[other] for other in self.document.objects[object_id].components
                              if self.document.components[other].type_name == spec["type"]]}
        on, off = (self.param_state(own, spec["type"], _strip_value(
            getattr(component.fields.get(spec[name]), "value", ""))) for name in ("on", "off"))
        if off == "holds" or on in ("missing", "fails"):
            return False
        return None if on is None or off is None else True

    def port_trigger(self, fields: Mapping[str, object]) -> bool | None:
        spec = self.options["port triggers"]

        def container(item) -> bool | None:
            data = _object_ref_id(self.ref_fields(item).get(spec["data"]))
            if not data:
                return False
            if self.builder._type_name(self.document.rsz_file, data) != spec["flag"]["type"]:
                return None
            condition = self.builder._fields(self.document.rsz_file, data)
            held = self.check_condition(condition.get(spec["flag"]["field"]))
            return None if held is None else held != _bool_field(condition, spec["not"], False)

        def general_rule(item) -> bool | None:
            entry = self.ref_fields(item) if _object_ref_id(item) else {}
            matters = getattr(entry.get(spec["matters"]), "values", None) or ()
            return three_valued([container(matter) for matter in matters],
                                _numeric_field(entry, spec["rule logic"], 0) == spec["or"]) if matters else False

        rules = getattr(fields.get(spec["rules"]), "values", None) or ()
        return three_valued([general_rule(item) for item in rules],
                            _numeric_field(fields, spec["logic"], 0) == spec["or"]) if rules else False

    def condition(self, fields: Mapping[str, object]) -> bool | None:
        return self.check_condition(fields.get(self.options["flag condition"]["field"]))

    def check_condition(self, value) -> bool | None:
        spec = self.options["flag condition"]
        instance = _object_ref_id(value)
        condition = self.builder._fields(self.document.rsz_file, instance)
        if not condition:
            return None
        if checks := getattr(condition.get(spec["flags"]), "values", None):
            if self.campaign is None:
                return None
            pairs = [(bytes(getattr(fields.get(spec["flag"]), "raw_bytes", b"")),
                      _bool_field(fields, spec["compare"], False)) for fields in map(self.ref_fields, checks)]
            return self.campaign.check(pairs, int(_numeric_field(condition, spec["logic"], 0)))
        return spec["unset"].get(self.builder._type_name(self.document.rsz_file, instance))

    def flag(self, inputs: Mapping[str, list[ScnComponent]], type_name: str, name: str) -> bool | None:
        state = self.param_state(inputs, type_name, name)
        return None if state is None else state == "holds"

    def param_state(self, inputs: Mapping[str, list[ScnComponent]], type_name: str, name: str) -> str | None:
        key = murmur3_hash_utf16le(name)
        for other in inputs.get(type_name, ()):
            for _instance, fields in self.params(other):
                if self.key(fields) == key:
                    held = self.condition(fields)
                    return None if held is None else "holds" if held else "fails"
        return "missing"

    def interact(self, inputs: Mapping[str, list[ScnComponent]], names: list[str]) -> bool | None:
        spec = self.options["interact"]
        found = []
        for holder in inputs.get(spec["holder"], ()):
            for item in getattr(holder.fields.get(spec["triggers"]), "values", None) or ():
                instance = _object_ref_id(self.ref_fields(item).get(spec["trigger"]))
                trigger = self.builder._fields(self.document.rsz_file, instance)
                if (self.builder._type_name(self.document.rsz_file, instance) == spec["type"]
                        and _strip_value(getattr(trigger.get(spec["name"]), "value", "")) in names):
                    found.append(self.flag(inputs, spec["flags"],
                                           _strip_value(getattr(trigger.get(spec["flag"]), "value", ""))))
        return three_valued(found, True)

    def stratum(self, field) -> bool | None:
        spec = self.options["strata"]
        for item in reversed(getattr(field, "values", None) or ()):
            stratum = self.ref_fields(item)
            if not _object_ref_id(stratum.get(spec["enable"])):
                continue
            holds = self.rule(self.ref_fields(stratum.get(spec["enable"])))
            if holds is None or holds:
                return holds and _bool_field(stratum, spec["value"], False)
        return False

    def rule(self, fields: Mapping[str, object]) -> bool | None:
        spec = self.options["strata"]
        matters = getattr(fields.get(spec["matters"]), "values", None) or ()
        either = getattr(fields.get(spec["logic"]), "value", None) == spec["or"]
        values = []
        for matter in matters:
            particle = _object_ref_id(self.ref_fields(matter).get(spec["particle"]))
            if particle:
                type_name = self.builder._type_name(self.document.rsz_file, particle)
                if type_name == spec.get("gm flag", {}).get("particle"):
                    values.append(self.gm_flag(particle))
                elif type_name == spec.get("flag", {}).get("particle"):
                    values.append(self.flag_particle(particle))
                elif type_name in spec["constants"]:
                    values.append(spec["constants"][type_name])
                else:
                    values.append(self.campaign_particle(type_name, particle))
            elif not either:
                return False
        if not matters:
            return False
        return three_valued(values, either)

    def flag_particle(self, particle: int) -> bool | None:
        fields = self.builder._fields(self.document.rsz_file, particle)
        return self.check_condition(fields.get(self.options["strata"]["flag"]["condition"]))

    def campaign_particle(self, type_name: str, particle: int) -> bool | None:
        spec = self.rules.get("campaign")
        if self.campaign is None or spec is None:
            return None
        if type_name == spec["played through"]["particle"]:
            return self.campaign.played_through
        for name, current in (("chapter", self.campaign.chapter), ("difficulty", self.campaign.difficulty)):
            if type_name == spec[name]["particle"]:
                fields = self.builder._fields(self.document.rsz_file, particle)
                compare = int(_numeric_field(fields, spec[name]["compare"], -1))
                if not 0 <= compare < len(spec["compares"]):
                    return False
                return getattr(operator, spec["compares"][compare])(
                    current, int(_numeric_field(fields, spec[name]["value"], 0)))
        return None

    def gm_flag(self, particle: int) -> bool | None:
        spec = self.options["strata"]["gm flag"]
        fields = self.builder._fields(self.document.rsz_file, particle)
        key = self.context_key(self.ref_fields(fields.get(spec["id"])))
        if not (found := self.context_listed(key)):
            return found
        flag = self.start_flag(key)
        return None if flag is None else flag == _bool_field(fields, spec["compare"], False)

    def start_flag(self, key: list[int], seen: frozenset = frozenset()) -> bool | None:
        spec = self.options["strata"]["gm flag"]
        target = self.gimmick(key)
        drivers = self.drivers.get(target, ()) if target is not None and tuple(key) not in seen else ()
        if not drivers or any(driver.type_name not in spec["unset"] for driver in drivers):
            return None
        held = {self.document.components[component_id].type_name
                for component_id in self.document.objects[target].components}
        inputs = self.inputs(target)
        for driver in drivers:
            rule = spec["unset"][driver.type_name]
            if held & set(rule.get("unless held", ())) or any(_numeric_field(driver.fields, name, value) != value
                                                              for name, value in rule.get("fields", {}).items()):
                return None
            for type_name, wanted in rule.get("flags", {}).items():
                by_key = {murmur3_hash_utf16le(name): value for name, value in wanted.items()}
                for other in inputs.get(type_name, ()):
                    for _instance, fields in self.params(other):
                        if (value := by_key.get(self.key(fields))) is not None and self.condition(fields) is not value:
                            return None
            for type_name, wanted in rule.get("strata", {}).items():
                for other in inputs.get(type_name, ()):
                    if any(self.stratum(other.fields.get(name)) is not value for name, value in wanted.items()):
                        return None
            if (related := rule.get("until related")) is not None:
                option = (self.inputs(target).get(related["option"]) or [None])[-1]
                states = []
                for ident in () if option is None else getattr(option.fields.get(related["ids"]), "values", None) or ():
                    other = self.context_key(self.ref_fields(ident))
                    if (found := self.context_listed(other)) is not False:
                        states.append(found and self.start_flag(other, seen | {tuple(key)}))
                if False not in states:
                    return None
        return False

    def attr_calls(self, component: ScnComponent, names):
        spec = self.options.get("attr calls", {})
        for name in names:
            for entry in getattr(self.ref_fields(component.fields.get(name)).get(spec["list"]), "values", None) or ():
                fields = self.ref_fields(entry)
                compare = spec["compares"][getattr(fields.get(spec["compare"]), "value", 0)]
                if getattr(operator, compare)(0, getattr(fields.get(spec["value"]), "value", 0)):
                    yield spec["calls"][str(getattr(fields.get(spec["call"]), "value", 0))], _strip_value(
                        getattr(fields.get(spec["key"]), "value", ""))

    def read_file_drawn(self, spec: Mapping, component: ScnComponent,
                        inputs: Mapping[str, list[ScnComponent]]) -> bool | None:
        path = _strip_value(getattr(component.fields.get(spec["user"]), "string", "") or "")
        if not path:
            return True
        user = self.builder._user_file(path, self.document.document_id)
        root = user.parsed_elements.get(user.object_table[0]) if user is not None and user.object_table else None
        if not isinstance(root, Mapping):
            return None
        if (option := (inputs.get(spec["option"]) or [None])[-1]) is not None:
            if (hidden := self.stratum(option.fields.get(spec["rule"]))) is None:
                return None
            if hidden:
                return False
        browse = int(_numeric_field(root, spec["browse"], 0))
        if browse not in spec["checked"]:
            return True
        file_id, quest = int(_numeric_field(root, spec["id"], -1)), spec["quest"]
        if browse == quest["type"] and quest["ids"][0] <= file_id < quest["ids"][1]:
            impossible = None if self.campaign is None else self.campaign.quest_impossible(file_id - quest["ids"][0])
            if impossible is None:
                return None
            if impossible:
                return False
        setting = user.parsed_elements.get(_object_ref_id(root.get(spec["setting"])))
        if not _bool_field(setting if isinstance(setting, Mapping) else {}, spec["off after"], False):
            return True
        return None if self.campaign is None else file_id not in self.campaign.files

    def user_value(self, spec: Mapping, component: ScnComponent) -> float | None:
        fields = self.user_fields(spec, component)
        value = None if fields is None else getattr(fields.get(spec["field"]), "value", None)
        return None if isinstance(value, bool) or not isinstance(value, (int, float)) else float(value)

    def user_fields(self, spec: Mapping, component: ScnComponent) -> Mapping[str, object] | None:
        path = _strip_value(getattr(component.fields.get(spec["user"]), "string", "") or "")
        return self.builder._user_fields(path, self.document.document_id) if path else None

    def powered(self, spec: Mapping, object_id: ScnObjectId) -> bool | None:
        source = next((other for other in map(self.document.components.__getitem__,
                                              self.document.objects[object_id].components)
                       if other.type_name == spec["component"]), None)
        if source is None:
            return True
        found = self.context_found(self.ref_fields(source.fields.get(spec["context"])))
        return None if found is None else not found or not _bool_field(source.fields, spec["enabled"], False)

    def context_found(self, ident: Mapping[str, object]) -> bool | None:
        return self.context_listed(self.context_key(ident))

    def context_key(self, ident: Mapping[str, object]) -> list[int]:
        spec = self.options["contexts"]
        return [int(_numeric_field(ident, name, spec["empty"])) for name in spec["id"]]

    def context_listed(self, key: list[int]) -> bool | None:
        spec = self.options["contexts"]
        if key[0] == spec["dynamic"] or key[0] == spec["static"] and key not in spec["checked"]:
            return None
        if key[0] != spec["static"]:
            return False
        path = str(self.document.source_path).replace("\\", "/").lower()
        listed = next((contexts for marker, contexts in spec["campaigns"].items() if marker in path), None)
        return None if listed is None else key in listed

    def gimmick(self, key: list[int]) -> ScnObjectId | None:
        if self.cores is None:
            spec = self.options["contexts"]
            self.cores = {}
            for item in self.document.objects.values():
                for component_id in item.components:
                    component = self.document.components[component_id]
                    if component.type_name == spec["core"]:
                        self.cores.setdefault(tuple(self.context_key(self.ref_fields(
                            component.fields.get(spec["core id"])))), item.id)
        return self.cores.get(tuple(key))

    def own_context(self, object_id: ScnObjectId) -> bool | None:
        spec = self.options["contexts"]
        core = next((other for other in map(self.document.components.__getitem__,
                                            self.document.objects[object_id].components)
                     if other.type_name == spec["core"]), None)
        return None if core is None else self.context_found(self.ref_fields(core.fields.get(spec["core id"])))
