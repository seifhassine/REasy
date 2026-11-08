import fnmatch
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional, Callable

from utils.type_registry import TypeRegistry

ValidSignature = (b"SCN\x00", b"USR\x00", b"PFB\x00")


def _ext_matches(filename_lower: str, allow_scn: bool, allow_pfb: bool, allow_user: bool) -> bool:
    """Check if file extension matches allowed types."""
    return (
        (allow_scn and ".scn" in filename_lower)
        or (allow_pfb and ".pfb" in filename_lower)
        or (allow_user and ".user" in filename_lower)
    )


def _matches_pattern(filename: str, pattern: str) -> bool:
    """Check if filename matches the given pattern using fnmatch."""
    return not pattern or pattern == "*" or fnmatch.fnmatch(filename, pattern)


def _check_constraints(fields: Dict[str, Any], constraints: List[dict], debug: bool = False) -> bool:
    """Check if fields satisfy all constraints."""
    compare_map = {
        "Greater than": lambda v, c: float(v) > float(c),
        "Less than": lambda v, c: float(v) < float(c),
        "Equal to": lambda v, c: str(v) == str(c),
        "Not equal to": lambda v, c: str(v) != str(c),
    }
    string_map = {
        "Not empty (strings)": lambda v, _: isinstance(v, str) and bool(v.strip()),
        "Is empty (strings)": lambda v, _: isinstance(v, str) and not v.strip(),
    }
    for c in constraints or []:
        v = fields.get(c['field'])
        if v is None:
            return False
        if hasattr(v, 'value'):
            v = v.value
        if isinstance(v, str):
            v = v.rstrip('\x00')
        func = string_map.get(c['type']) or compare_map.get(c['type'])
        try:
            if func and not func(v, c['value']):
                return False
        except Exception:
            return False
    return True


def normalize_key_value(value: Any, debug: bool = False) -> Any:
    """Normalize a value for use as a matching key."""
    if value is None:
        return None
    if hasattr(value, 'raw_bytes') and getattr(value, 'raw_bytes') is not None:
        try:
            return ("bytes", bytes(getattr(value, 'raw_bytes')))
        except Exception:
            return None
    if hasattr(value, 'value'):
        return normalize_key_value(getattr(value, 'value'), debug)
    if isinstance(value, bool):
        return ("bool", 1 if value else 0)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        return ("int", int(value)) if value.is_integer() else ("float", round(value, 6))
    if isinstance(value, str):
        return ("str", value.rstrip('\x00'))
    seq = getattr(value, 'values', value)
    if isinstance(seq, (list, tuple)):
        parts = [normalize_key_value(v, debug) for v in seq]
        if any(p is None for p in parts):
            return None
        return ("arr", tuple(parts))
    if hasattr(value, 'x') and hasattr(value, 'y'):
        coords = [getattr(value, n) for n in "xyzw" if hasattr(value, n)]
        norm = [normalize_key_value(c, debug) for c in coords]
        if any(n is None for n in norm):
            return None
        return ("vec", tuple(norm))
    if hasattr(value, 'guid') or 'guid' in str(type(value)).lower():
        try:
            guid_str = str(value)
            if guid_str:
                return ("str", guid_str)
        except Exception:
            pass
    if hasattr(value, '__dict__'):
        try:
            s = str(value)
            if s and not s.startswith(f"<{type(value).__name__} object at 0x"):
                return ("str", s.rstrip('\x00'))
        except Exception:
            pass
    return None


def build_key(fields: Dict[str, Any], field_names: List[str], debug: bool = False, context: str = "") -> Optional[Tuple[Any, ...]]:
    """Build a key tuple from field values for matching."""
    out: List[Any] = []
    for name in field_names:
        v = fields.get(name)
        if v is None:
            return None
        sk = normalize_key_value(v, debug)
        if sk is None:
            return None
        out.append(sk)
    return tuple(out)


def scan_directory_single_pass(
    root_dir: str,
    pattern_a: str,
    pattern_b: str,
    recursive: bool,
    type_id_a: int,
    type_id_b: int,
    key_pairs: List[Tuple[str, str]],
    cap_fields_a: List[str],
    cap_fields_b: List[str],
    type_registry: TypeRegistry,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
    file_list: Optional[List[Path]] = None,
    allow_scn_a: bool = True,
    allow_pfb_a: bool = True,
    allow_user_a: bool = True,
    allow_scn_b: bool = True,
    allow_pfb_b: bool = True,
    allow_user_b: bool = True,
    debug: bool = False,
    constraints_a: List[dict] = None,
    constraints_b: List[dict] = None,
) -> Tuple[Dict[Tuple[Any, ...], List[dict]], Dict[Tuple[Any, ...], List[dict]]]:
    """
    Walk the tree once and collect A/B entries without reading files twice.
    """
    from file_handlers.rsz.rsz_file import RszFile

    a_map: Dict[Tuple[Any, ...], List[dict]] = {}
    b_map: Dict[Tuple[Any, ...], List[dict]] = {}

    constraints_a = constraints_a or []
    constraints_b = constraints_b or []

    root = Path(root_dir)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Root directory does not exist or is not a directory: {root_dir}")

    if file_list is None:
        iter_paths = root.rglob('*') if recursive else root.glob('*')
        file_list = [
            p for p in iter_paths if p.is_file() and (
                (_matches_pattern(p.name, pattern_a) and _ext_matches(p.name.lower(), allow_scn_a, allow_pfb_a, allow_user_a))
                or (_matches_pattern(p.name, pattern_b) and _ext_matches(p.name.lower(), allow_scn_b, allow_pfb_b, allow_user_b))
            )
        ]

    total = len(file_list)
    for idx, p in enumerate(file_list):
        if cancel_cb and cancel_cb():
            break
        if progress_cb:
            try:
                progress_cb(idx + 1, total)
            except Exception:
                pass

        name = p.name
        name_lower = name.lower()
        is_a = _matches_pattern(name, pattern_a) and _ext_matches(name_lower, allow_scn_a, allow_pfb_a, allow_user_a)
        is_b = _matches_pattern(name, pattern_b) and _ext_matches(name_lower, allow_scn_b, allow_pfb_b, allow_user_b)
        if not (is_a or is_b):
            continue

        try:
            data = p.read_bytes()
        except Exception:
            continue
        if len(data) < 4 or data[:4] not in ValidSignature:
            continue

        r = RszFile()
        r.type_registry = type_registry
        r.filepath = str(p)
        try:
            r.read(data)
        except Exception:
            continue

        for inst_index, inst in enumerate(r.instance_infos):
            fields = r.parsed_elements.get(inst_index)
            if not fields:
                continue
            if is_a and inst.type_id == type_id_a and _check_constraints(fields, constraints_a, debug):
                k_a = build_key(fields, [ap for ap, _ in key_pairs], debug, f"A instance {inst_index} in {name}")
                if k_a is not None:
                    a_map.setdefault(k_a, []).append({
                        "path": str(p),
                        "instance_id": inst_index,
                        "all_fields": fields,
                        "fields": {f: fields.get(f) for f in cap_fields_a if f in fields},
                    })
            if is_b and inst.type_id == type_id_b and _check_constraints(fields, constraints_b, debug):
                k_b = build_key(fields, [bp for _, bp in key_pairs], debug, f"B instance {inst_index} in {name}")
                if k_b is not None:
                    b_map.setdefault(k_b, []).append({
                        "path": str(p),
                        "instance_id": inst_index,
                        "all_fields": fields,
                        "fields": {f: fields.get(f) for f in cap_fields_b if f in fields},
                    })

    return a_map, b_map
