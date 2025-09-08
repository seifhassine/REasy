import json
import os
from typing import Dict, List, Optional, Set
import copy
import fire

Field = Dict

def typename_for_key(k: str, entry: Dict) -> str:
    """
    When use_hashkeys=True, the dict key is a hash and 'name' holds the logical typename.
    Otherwise the key itself is the typename.
    """
    return entry.get("name") or k

def build_name_index(data: Dict[str, Dict]) -> Dict[str, Dict]:
    return { typename_for_key(k, v): v for k, v in data.items() }

def build_parent_map(data: Dict[str, Dict]) -> Dict[str, Optional[str]]:
    parent: Dict[str, Optional[str]] = {}
    for k, v in data.items():
        parent[typename_for_key(k, v)] = v.get("parent")
    return parent

def ancestors_chain(t: str, parent_map: Dict[str, Optional[str]], existing: Set[str]) -> List[str]:
    """
    Return ancestors from root → immediate parent.
    Stops at None or missing types.
    """
    chain: List[str] = []
    seen: Set[str] = {t}
    cur = parent_map.get(t)
    while cur and cur not in seen:
        if cur not in existing:
            break
        chain.append(cur)
        seen.add(cur)
        cur = parent_map.get(cur)
    chain.reverse()
    return chain

def own_fields_count_mode(
    t: str,
    idx: Dict[str, Dict],
    parent_map: Dict[str, Optional[str]],
    cache: Dict[str, List[Field]],
    visiting: Set[str],
    existing_types: Set[str],
) -> List[Field]:
    if t in cache:
        return cache[t]
    if t in visiting:
        cache[t] = list(idx.get(t, {}).get("fields", []) or [])
        return cache[t]

    visiting.add(t)

    entry = idx.get(t)
    if not entry:
        cache[t] = []
        visiting.remove(t)
        return cache[t]

    child_fields: List[Field] = list(entry.get("fields", []) or [])
    ancs = ancestors_chain(t, parent_map, existing_types)

    total_anc_own_len = 0
    for anc in ancs:
        anc_own = own_fields_count_mode(anc, idx, parent_map, cache, visiting, existing_types)
        total_anc_own_len += len(anc_own)

    k = len(child_fields) - total_anc_own_len
    if k < 0:
        k = 0

    own = child_fields[-k:] if k > 0 else []
    cache[t] = own
    visiting.remove(t)
    return own

def strip_file(input_json: str, out: Optional[str] = None):
    """
      For each type, remove exactly the number of ancestor fields (sum of OWN(ancestors)) from the begining.
      Assumes inherited fields are listed first in the child!!
    """
    with open(input_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = copy.deepcopy(data)
    idx_in  = build_name_index(data)
    idx_out = build_name_index(result)
    parent_map = build_parent_map(data)
    existing_types = set(idx_in.keys())

    own_cache: Dict[str, List[Field]] = {}

    for t in idx_out.keys():
        own = own_fields_count_mode(t, idx_in, parent_map, own_cache, visiting=set(), existing_types=existing_types)
        idx_out[t]["fields"] = own

    if not out:
        base, ext = os.path.splitext(input_json)
        out = f"{base}_strip{ext or '.json'}"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent='\t', sort_keys=True)
    print(f"Wrote stripped JSON to: {out}")

def propagate_file(input_json: str, out: Optional[str] = None):
    """
    assumes input is already STRIPPED:
      - Treat CURRENT fields of each type as its OWN fields.
      - Rebuild: concat OWN(ancestors root→parent) + OWN(child).
    """
    with open(input_json, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = copy.deepcopy(data)
    idx_in  = build_name_index(data)
    idx_out = build_name_index(result)
    parent_map = build_parent_map(data)
    existing_types = set(idx_in.keys())

    own_cache: Dict[str, List[Field]] = {
        t: list(idx_in[t].get("fields", []) or []) for t in idx_in.keys()
    }

    for t in idx_out.keys():
        ancs = ancestors_chain(t, parent_map, existing_types)
        rebuilt: List[Field] = []
        for anc in ancs:
            rebuilt.extend(own_cache.get(anc, []))
        rebuilt.extend(own_cache.get(t, []))
        idx_out[t]["fields"] = rebuilt

    if not out:
        abs_input = os.path.abspath(input_json)
        in_dir = os.path.dirname(abs_input)
        parent_dir = os.path.dirname(in_dir) if in_dir else os.path.abspath(os.path.join(abs_input, os.pardir))
        base_name = os.path.basename(abs_input)
        base, ext = os.path.splitext(base_name)
        if base.endswith("_strip"):
            base = base[:-6]
        out = os.path.join(parent_dir, f"{base}{ext or '.json'}")

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent='\t', sort_keys=True)
    print(f"Wrote propagated JSON to: {out}")

def main():
    fire.Fire({
        "strip": strip_file,
        "propagate": propagate_file,
    })

if __name__ == "__main__":
    main()
