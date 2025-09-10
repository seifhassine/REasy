#python pfb_refinfos_extractor.py <dir-or-file> --registry <path/to/rsz.json> --pretty --output result.json

#!/usr/bin/env python3
import os
import sys
import argparse
import json
from collections import defaultdict
from typing import Dict, Set, List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from file_handlers.rsz.rsz_file import RszFile
from utils.type_registry import TypeRegistry


def iter_pfb_like_files(root: str) -> List[str]:
    results: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            lower = name.lower()
            if lower.endswith('.pfb') or '.pfb.' in lower:
                results.append(os.path.join(dirpath, name))
    return results


def load_rsz(filepath: str, type_registry: TypeRegistry) -> RszFile:
    with open(filepath, 'rb') as f:
        data = f.read()
    r = RszFile()
    r.type_registry = type_registry
    r.filepath = filepath
    r.read(data)
    return r


def resolve_component_type_name(instance_id: int, rsz: RszFile, type_registry: TypeRegistry) -> str:
    if instance_id < 0 or instance_id >= len(rsz.instance_infos):
        return f"Instance[{instance_id}]"
    inst = rsz.instance_infos[instance_id]
    info = type_registry.get_type_info(inst.type_id) if type_registry else None
    if info and 'name' in info:
        return info['name']
    return f"0x{inst.type_id:08X}"


def build_property_map_for_file(filepath: str, type_registry: TypeRegistry):
    rsz = load_rsz(filepath, type_registry)
    if not rsz.is_pfb:
        return []

    pairs = []
    for gori in rsz.gameobject_ref_infos:
        obj_id = gori.object_id
        prop_id = gori.property_id
        array_idx = gori.array_index
        if obj_id < 0 or obj_id >= len(rsz.object_table):
            continue
        instance_id = rsz.object_table[obj_id]
        type_name = resolve_component_type_name(instance_id, rsz, type_registry)
        pairs.append((type_name, prop_id, array_idx))
    return pairs


def aggregate(root: str, registry_json: str):
    type_registry = TypeRegistry(registry_json) if registry_json else None

    mapping: Dict[str, Dict[int, dict]] = defaultdict(lambda: defaultdict(lambda: { 'files': set(), 'array_ids': set() }))

    files = iter_pfb_like_files(root)
    for fp in files:
        try:
            pairs = build_property_map_for_file(fp, type_registry)
            for (type_name, prop_id, array_idx) in pairs:
                mapping[type_name][prop_id]['files'].add(fp)
                mapping[type_name][prop_id]['array_ids'].add(array_idx)
        except Exception as ex:
            print(f"[warn] Failed {fp}: {ex}")

    result = {}
    for t, prop_map in mapping.items():
        result[t] = {}
        for pid, data in prop_map.items():
            result[t][str(pid)] = {
                'files': sorted(list(data['files'])),
                'array_ids': sorted(list(data['array_ids']))
            }
    return result


def main():
    parser = argparse.ArgumentParser(description='Map PFB GameObjectRefInfo property IDs to component type names and list file occurrences.')
    parser.add_argument('path', help='Directory to scan or a single PFB file')
    parser.add_argument('--registry', '-r', help='Path to RSZ type registry JSON for type name resolution')
    parser.add_argument('--output', '-o', help='Output JSON file; if omitted, prints to stdout')
    parser.add_argument('--pretty', action='store_true', help='Pretty-print JSON')

    args = parser.parse_args()

    if os.path.isfile(args.path):
        root = os.path.dirname(args.path)
        single_file = os.path.abspath(args.path)
        type_registry = TypeRegistry(args.registry) if args.registry else None
        mapping: Dict[str, Dict[int, dict]] = defaultdict(lambda: defaultdict(lambda: { 'files': set(), 'array_ids': set() }))
        try:
            pairs = build_property_map_for_file(single_file, type_registry)
            for (type_name, prop_id, array_idx) in pairs:
                mapping[type_name][prop_id]['files'].add(single_file)
                mapping[type_name][prop_id]['array_ids'].add(array_idx)
        except Exception as ex:
            print(f"[warn] Failed {single_file}: {ex}")
        result = {}
        for t, prop_map in mapping.items():
            result[t] = {}
            for pid, data in prop_map.items():
                result[t][str(pid)] = {
                    'files': sorted(list(data['files'])),
                    'array_ids': sorted(list(data['array_ids']))
                }
    else:
        result = aggregate(args.path, args.registry)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2 if args.pretty else None, ensure_ascii=False)
        print(f"Wrote {args.output}")
    else:
        print(json.dumps(result, indent=2 if args.pretty else None, ensure_ascii=False))


if __name__ == '__main__':
    main()