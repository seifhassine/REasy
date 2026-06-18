#!/usr/bin/env python3
"""
Add property_id metadata to RSZ JSON dumps.

"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


GAMEOBJECT_REF = "via.GameObjectRef"
GUID_OR_GAMEOBJECT_REF = {"via.Guid", "System.Guid", GAMEOBJECT_REF}
NATIVE_NAME_RE = re.compile(r"^v\d+_(.+)$")


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def save_json(path: Path, data) -> None:
    with path.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent="\t")
        fp.write("\n")


def parse_order(value):
    try:
        return int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None


def is_array(value) -> bool:
    return value is True or value == 1


def index_il2cpp(il2cpp_dump):
    by_name = {}
    for key, entry in il2cpp_dump.items():
        if not isinstance(entry, dict):
            continue
        for name in (key, entry.get("name"), entry.get("fqn")):
            if isinstance(name, str) and name:
                by_name.setdefault(name, entry)
    return by_name


def reflection_props(entry):
    value = entry.get("reflection_properties")
    return value if isinstance(value, dict) else {}


def type_chain(il2cpp, type_name):
    seen = set()
    while isinstance(type_name, str) and type_name and type_name not in seen:
        seen.add(type_name)
        entry = il2cpp.get(type_name)
        if not isinstance(entry, dict):
            return
        yield type_name, entry
        type_name = entry.get("parent")


def direct_gameobject_ref_orders(entry, defining_type):
    result = {}
    for name, prop in reflection_props(entry).items():
        if not isinstance(prop, dict) or prop.get("type") != GAMEOBJECT_REF:
            continue
        order = parse_order(prop.get("order"))
        if order is not None:
            result[str(name)] = (order, defining_type)
    return result


def inherited_gameobject_ref_orders(il2cpp, type_name):
    result = {}
    for defining_type, entry in type_chain(il2cpp, type_name):
        for name, match in direct_gameobject_ref_orders(entry, defining_type).items():
            result.setdefault(name, match)
    return result


def native_position_orders(entry, defining_type):
    ordered = []
    for prop in reflection_props(entry).values():
        if not isinstance(prop, dict):
            continue
        order = parse_order(prop.get("order"))
        if order is not None:
            ordered.append((order, prop.get("type")))

    result = {}
    position = 0
    for order, prop_type in sorted(ordered):
        if prop_type not in GUID_OR_GAMEOBJECT_REF:
            continue
        if prop_type == GAMEOBJECT_REF:
            result[position] = (order, defining_type)
        position += 1
    return result


def array_element_type(il2cpp, type_name):
    entry = il2cpp.get(type_name) if isinstance(type_name, str) else None
    if not isinstance(entry, dict):
        return type_name

    element_type = entry.get("element_type_name")
    if isinstance(element_type, str) and element_type:
        return element_type

    args = entry.get("generic_arg_types") if entry.get("is_generic_type") else None
    if isinstance(args, list) and len(args) == 1 and isinstance(args[0], dict):
        return args[0].get("type", type_name)
    return type_name


def rsz_value_type(il2cpp, rsz_field):
    field_type = rsz_field.get("type")
    return array_element_type(il2cpp, field_type) if is_array(rsz_field.get("array")) else field_type


def generated_rsz_orders(il2cpp, type_name, prefix="", field_index=0, seen=()):
    if type_name in seen:
        return {}, field_index

    entry = il2cpp.get(type_name)
    fields = entry.get("RSZ") if isinstance(entry, dict) else None
    if not isinstance(fields, list):
        return {}, field_index

    direct_orders = direct_gameobject_ref_orders(entry, type_name)
    result = {}

    for rsz_field in fields:
        if not isinstance(rsz_field, dict):
            continue

        has_name = "potential_name" in rsz_field
        name = str(rsz_field.get("potential_name")) if has_name else f"v{field_index}"
        field_type = rsz_field.get("type")

        if (
            rsz_field.get("code") == "Struct"
            and not is_array(rsz_field.get("array"))
            and isinstance(field_type, str)
            and field_type in il2cpp
        ):
            nested, field_index = generated_rsz_orders(
                il2cpp, field_type, f"{prefix}STRUCT_{name}_", field_index, seen + (type_name,)
            )
            result.update(nested)
            continue

        if has_name and rsz_value_type(il2cpp, rsz_field) == GAMEOBJECT_REF and name in direct_orders:
            result[f"{prefix}{name}"] = direct_orders[name]
        field_index += 1

    return result, field_index


def dump_type_name(key, entry, il2cpp):
    for candidate in (entry.get("name"), entry.get("fqn"), key):
        if isinstance(candidate, str) and candidate in il2cpp:
            return candidate
    return None


def inheritance_depth(il2cpp, type_name, cache):
    if type_name not in cache:
        cache[type_name] = sum(1 for _ in type_chain(il2cpp, type_name)) or 1
    return cache[type_name]


def make_property_id(il2cpp, match, depth_cache):
    order, defining_type = match
    return (inheritance_depth(il2cpp, defining_type, depth_cache) << 16) | (order & 0xFFFF)


def is_dump_gameobject_ref(field) -> bool:
    return field.get("original_type") == GAMEOBJECT_REF or field.get("type") == "GameObjectRef"


def is_native_8_16(field) -> bool:
    return field.get("native") is True and field.get("align") == 8 and field.get("size") == 16


def set_field_value(field, key, value, overwrite) -> bool:
    if key in field and not overwrite:
        return False
    if field.get(key) == value:
        return False
    field[key] = value
    return True


def match_field(field, generated, direct, native_by_position, native_position):
    name = field.get("name")
    if isinstance(name, str):
        match = generated.get(name) or direct.get(name)
        native_name = NATIVE_NAME_RE.match(name)
        if match is None and native_name:
            match = direct.get(native_name.group(1))
        if match is not None:
            return match, native_position

    if is_native_8_16(field):
        return native_by_position.get(native_position), native_position + 1
    return None, native_position


def patch_dump(rsz_dump, il2cpp_dump, *, overwrite=False):
    il2cpp = index_il2cpp(il2cpp_dump)
    depth_cache = {}
    type_cache = {}
    stats = {
        "types_seen": 0,
        "types_with_il2cpp": 0,
        "matches": 0,
        "property_ids_written": 0,
    }

    for key, entry in rsz_dump.items():
        fields = entry.get("fields") if isinstance(entry, dict) else None
        if not isinstance(fields, list):
            continue
        stats["types_seen"] += 1

        type_name = dump_type_name(str(key), entry, il2cpp)
        if type_name is None:
            continue
        stats["types_with_il2cpp"] += 1

        if type_name not in type_cache:
            type_cache[type_name] = (
                generated_rsz_orders(il2cpp, type_name)[0],
                inherited_gameobject_ref_orders(il2cpp, type_name),
                native_position_orders(il2cpp[type_name], type_name),
            )
        generated, direct, native_by_position = type_cache[type_name]

        native_position = 0
        for field in fields:
            if not isinstance(field, dict):
                continue

            match, native_position = match_field(field, generated, direct, native_by_position, native_position)
            if match is None or not (is_dump_gameobject_ref(field) or field.get("native") is True):
                continue

            stats["matches"] += 1
            if set_field_value(field, "property_id", make_property_id(il2cpp, match, depth_cache), overwrite):
                stats["property_ids_written"] += 1

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Add via.GameObjectRef property_id metadata to an RSZ dump.")
    parser.add_argument("dump", type=Path, help="RSZ dump JSON")
    parser.add_argument("il2cpp", type=Path, help="Matching il2cpp.json")
    parser.add_argument("-o", "--output", type=Path, help="Output path")
    parser.add_argument("--in-place", action="store_true", help="Write back to the input dump")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing property_id values")
    parser.add_argument("--dry-run", action="store_true", help="Report only")
    args = parser.parse_args()

    if args.in_place and args.output:
        parser.error("--in-place and --output are mutually exclusive")

    rsz_dump = load_json(args.dump)
    stats = patch_dump(
        rsz_dump,
        load_json(args.il2cpp),
        overwrite=args.overwrite,
    )

    print(f"Types scanned: {stats['types_seen']}")
    print(f"Types matched in il2cpp: {stats['types_with_il2cpp']}")
    print(f"GameObjectRef fields matched: {stats['matches']}")
    print(f"Property IDs written: {stats['property_ids_written']}")

    if args.dry_run:
        print("Dry run: no file written")
        return 0

    output = args.dump if args.in_place else args.output
    output = output or args.dump.with_name(f"{args.dump.stem}_with_orders{args.dump.suffix}")
    save_json(output, rsz_dump)
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
