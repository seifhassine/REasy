"""See docs/internals/bhvt-graph.md."""

from __future__ import annotations

import functools
from collections import Counter, defaultdict

lazy from file_handlers.rsz.rsz_data_types import (ArrayData, GuidData, MaybeObject, ObjectData, RawBytesData,
                                                   StructData, UserDataData)
lazy from file_handlers.rsz.rsz_file import RszInstanceInfo
lazy from file_handlers.rsz.utils.rsz_field_utils import create_field_value_from_definition, is_type_assignable
lazy from file_handlers.rsz.utils.rsz_instance_defaults import with_value
lazy from file_handlers.uvar.expression_nodes import expression_parameter_names
lazy from utils.enum_manager import game_enum_definitions
lazy from utils.hash_util import murmur3_hashes

from .model import Hfsm, Tree
from .rules import activator_refused, hfsm_rules, rules_of, tree_rules
from .tree_codec import NO_ID


def base_name(key: str) -> str:
    prefix, _, rest = key.partition("_")
    return rest if rest and prefix[:1] == "v" and prefix[1:].isdigit() else key


def field_key(fields: dict, name: str) -> str | None:
    return name if name in fields else next((key for key in fields if base_name(key) == name), None)


def field_value(fields: dict, name: str):
    item = fields[field_key(fields, name)]
    return item.raw_bytes if isinstance(item, GuidData) else item.value


def asset(user, name: str) -> tuple[int, str] | None:
    path = field_value(user.embedded_instances[user.embedded_object_table[0]], name).rstrip("\0")
    return (user.type_id, path) if path else None


def user_data(block, base: str, name: str) -> list:
    registry = block.type_registry
    return [user for user in block.rsz_userdata_infos if asset(user, name) and is_type_assignable(
        registry, registry.get_type_info(user.type_id)["name"], base)]


def references(fields: dict):
    for value in fields.values():
        for item in value.values if isinstance(value, (ArrayData, StructData)) else (value,):
            if isinstance(item, dict):
                yield from references(item)
            elif isinstance(item, (ObjectData, UserDataData)):
                yield item


def reached(rsz, index: int) -> set[int]:
    found, stack = set(), [index]
    while stack:
        if (at := stack.pop()) not in found:
            found.add(at)
            stack.extend(ref.value for ref in references(rsz.parsed_elements[at]) if ref.value > 0)
    return found


def uses(rsz) -> Counter:
    return Counter(ref.value for fields in rsz.parsed_elements.values() for ref in references(fields))


def blank_value(value, size: int):
    return RawBytesData(bytes(size), size, value.orig_type) if isinstance(value, MaybeObject) else value


def blank_fields(block, info: dict) -> dict:
    return {name: blank_value(value, int(definition["size"])) for definition in info["fields"]
            for name, value, *_ in [create_field_value_from_definition(definition,
                                                                       has_embedded_rsz=block.has_embedded_rsz)]}


def object_place(model: Tree | Hfsm, field: str, value: int) -> tuple[str, int] | None:
    graph = rules_of(model).graph
    if isinstance(model, Hfsm):
        block, base = graph["objects"][field]
        return block, value - base
    dynamic, static = graph["indexes"][field]
    if value in (-1, NO_ID):
        return None
    return (static, value & ~graph["static_flag"]) if static is not None and value & graph["static_flag"] \
        else (dynamic, value)


def object_ref(model: Tree | Hfsm, field: str, value: int) -> tuple[str, int] | None:
    ref = object_place(model, field, value)
    return ref if ref is not None and 0 <= ref[1] < len(model.blocks[ref[0]].object_table) else None


def object_value(model: Tree | Hfsm, block: str, index: int) -> int:
    graph = rules_of(model).graph
    if isinstance(model, Hfsm):
        return index + dict(graph["objects"].values())[block]
    return index | graph["static_flag"] if block in {pair[1] for pair in graph["indexes"].values()} else index


def action_objects(tree: Tree) -> dict[tuple[int, int], tuple[str, int]]:
    graph = tree_rules(tree.version).graph["actions"]
    found = {}
    for block, ex_field in graph["blocks"].items():
        rsz = tree.blocks[block]
        for index, (instance, ex_id) in enumerate(zip(rsz.object_table, getattr(tree, ex_field), strict=True)):
            found.setdefault((field_value(rsz.parsed_elements[instance], graph["id_field"]), ex_id), (block, index))
    return found


def callbacks(tree: Tree) -> list[tuple[object, list[tuple[str, int]]]]:
    graph = tree_rules(tree.version).graph["callback"]
    objects: dict[tuple, list[tuple[str, int]]] = {}
    for block in graph["blocks"]:
        rsz = tree.blocks[block]
        for index, instance in enumerate(rsz.object_table):
            guid = field_value(rsz.parsed_elements[instance], graph["guid_field"])
            objects.setdefault((rsz.instance_infos[instance].type_id, guid), []).append((block, index))
    found, names = [], expression_parameter_names()
    for uvar in (tree.variable, *tree.base_variables):
        for variable in uvar.variables:
            for node in [] if variable.expression is None else variable.expression.nodes:
                params = {names.get(parameter.name_hash): parameter.value for parameter in node.parameters}
                if node.name == graph["node"] and params[graph["function_parameter"]] == graph["function"]:
                    guid = params.get(graph["guid_parameter"])
                    key = (params[graph["type_parameter"]] & 0xFFFFFFFF, None if guid is None else guid.bytes)
                    found.append((node, objects.get(key, [])))
    return found


def own_variables(model: Tree | Hfsm) -> list:
    return [model.variable, *model.base_variables] if isinstance(model, Tree) else [model.variable]


def variables(model: Tree | Hfsm, owners=()) -> dict[bytes, tuple[object, object]]:
    found = {}

    def visit(uvar, source) -> None:
        for variable in uvar.variables:
            found.setdefault(variable.guid.bytes, (source, variable))
        for embed in uvar.embedded_uvars:
            visit(embed, source)

    for uvar in (*own_variables(model), *owners):
        visit(uvar, uvar)
    return found


def variable_references(model: Tree | Hfsm) -> list[bytes]:
    graph = rules_of(model).graph["variables"]
    names, found = expression_parameter_names(), []
    if "condition" in graph:
        base, field = graph["condition"]
        for rsz in model.blocks.values():
            found += [field_value(rsz.parsed_elements[instance], field) for instance in rsz.object_table
                      if is_type_assignable(rsz.type_registry, rsz.type_registry.get_type_info(
                          rsz.instance_infos[instance].type_id)["name"], base)]
    node_name, parameter_name = graph["node"]
    for uvar in own_variables(model):
        for variable in uvar.variables:
            for node in [] if variable.expression is None else variable.expression.nodes:
                found += [parameter.value.bytes for parameter in node.parameters
                          if node.name == node_name and names.get(parameter.name_hash) == parameter_name]
    return list(dict.fromkeys(found))


def hfsm_action_objects(hfsm: Hfsm) -> dict[tuple[int, int], int]:
    names = hfsm_rules(hfsm.version).graph["action_fields"]
    rsz = hfsm.blocks["actions"]
    return {(field_value(rsz.parsed_elements[instance], names["id"]),
             field_value(rsz.parsed_elements[instance], names["list_no"])): index
            for index, instance in enumerate(rsz.object_table)}


def slot_base(model: Tree | Hfsm, block: str) -> str:
    return rules_of(model).graph["slots"][block]


def slot_types(block, base: str) -> list[str]:
    return _slot_types(block.type_registry, block.game_version, base)


@functools.cache
def _families(registry) -> tuple[dict, dict, dict]:
    named, children, parents = defaultdict(list), defaultdict(list), defaultdict(set)
    for entry in registry.registry.values():
        if isinstance(entry, dict) and entry.get("name") and registry.find_type_by_name(entry["name"])[0] is entry:
            named[entry["name"].casefold()].append(entry["name"])
            if (parent := entry.get("parent")) and isinstance(parent, str):
                children[parent].append(entry["name"])
                parents[parent.casefold()].add(parent)
    return named, children, parents


@functools.cache
def _slot_types(registry, game: str, base: str) -> list[str]:
    named, children, parents = _families(registry)
    key = base.strip().casefold()
    found, stack = set(named.get(key, ())), list(parents.get(key, ()))
    while stack:
        for name in children.get(stack.pop(), ()):
            if name not in found:
                found.add(name)
                stack.append(name)
    return sorted((found if key else {name for names in named.values() for name in names}) - activator_refused(game))


@functools.cache
def tag_names(game: str) -> dict[int, str]:
    names = [f"{enum}.{member['name']}" for enum, members in game_enum_definitions(game).items() for member in members]
    return dict(zip(murmur3_hashes(name.encode("utf-16le") for name in names), names))


def tag_hash(name: str) -> int:
    return next(murmur3_hashes([name.encode("utf-16le")]))


def new_instances(block, type_name: str, values: dict) -> list[tuple[object, dict]]:
    created, made = [], {}

    def build(type_name: str, values: dict, path: str = "") -> int:
        info, type_id = block.type_registry.find_type_by_name(type_name)
        instance = RszInstanceInfo()
        instance.type_id, instance.crc = type_id, int(info["crc"], 16)
        definitions = {definition["name"]: definition for definition in info["fields"]}
        fields = blank_fields(block, info)

        def element(field, value, definition: dict, at: str):
            if value is None:
                return field
            if not isinstance(value, dict):
                return with_value(field, value, definition)
            if "same" not in value:
                made[at] = build(value["type"], value["fields"], at + ".")
            field.value = made[value.get("same", at)]
            return field

        for name, value in values.items():
            field = fields[name]
            if isinstance(field, ArrayData):
                field.values = [element(field.element_class(orig_type=field.orig_type), item, definitions[name],
                                        f"{path}{name}.{index}") for index, item in enumerate(value)]
            else:
                fields[name] = element(field, value, definitions[name], path + name)
        created.append((instance, fields))
        return len(created)

    build(type_name, values)
    return created
