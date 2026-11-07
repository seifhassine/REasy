"""See docs/internals/mot-format.md."""

from __future__ import annotations

import functools
from dataclasses import dataclass

lazy from file_handlers.layout_codec import RecordCodec, game_enums, load_table, merged
lazy from utils.enum_manager import game_enum_definitions
lazy from utils.game_keys import game_key

from .errors import MotionFormatError

DATA_DIRECTORY = "resources/data/motion"
_DEEP = ("records", "rules", "align", "payload_align")


@dataclass(frozen=True, slots=True)
class Encoding:
    kind: str
    code: int
    size: int
    params: int
    decode: str
    axis: int
    axes: tuple[int, ...]
    bits: int
    endian: str
    order: str
    blend: str
    blend_order: str


@dataclass(frozen=True, slots=True)
class MotRules:
    header: RecordCodec
    order: tuple[str, ...]
    align: dict[str, int]
    payload_order: tuple[str, ...]
    payload_align: dict[str, int]
    tracks: str
    master_size: bool
    sequence_clip: int
    records: dict[str, RecordCodec]


@dataclass(frozen=True, slots=True)
class MotProfile:
    version: int
    keys: str
    games: tuple[str, ...]
    rules: MotRules


@functools.cache
def _load(name: str) -> dict:
    return load_table(DATA_DIRECTORY, name, MotionFormatError)


def _codec(name: str, spec: dict) -> RecordCodec:
    return RecordCodec(name, spec["size"], spec["fields"], MotionFormatError)


@functools.cache
def mot_rules(family: str) -> MotRules:
    data = merged(_load("layouts")["mot"], family, _DEEP)
    rules = data["rules"]
    return MotRules(_codec("MOT header", data["header"]), tuple(data["order"]), dict(data["align"]),
                    tuple(data["payload_order"]), dict(data["payload_align"]), rules["tracks"],
                    rules["master_size"], rules["sequence_clip"],
                    {name: _codec(name, spec) for name, spec in data["records"].items()})


@functools.cache
def mot_versions() -> dict[int, dict]:
    return {int(version): entry for version, entry in _load("versions")["mot"].items()}


@functools.cache
def mot_profile(version: int) -> MotProfile:
    entry = mot_versions().get(version)
    if entry is None:
        raise MotionFormatError(f"MOT version {version} is not supported "
                                f"(supported: {', '.join(map(str, sorted(mot_versions())))})")
    return MotProfile(version, entry["keys"], tuple(entry["games"]), mot_rules(entry["layout"]))


@dataclass(frozen=True, slots=True)
class ListProfile:
    version: int
    mot: int
    header: RecordCodec
    records: dict[str, RecordCodec]
    override_base: str
    source_key: str
    reverse: bool
    row_sequences: str


@functools.cache
def list_versions() -> dict[int, dict]:
    return {int(version): entry for version, entry in _load("versions")["motlist"].items()}


@functools.cache
def list_profile(version: int) -> ListProfile:
    entry = list_versions().get(version)
    if entry is None:
        raise MotionFormatError(f"MOTLIST version {version} is not supported "
                                f"(supported: {', '.join(map(str, sorted(list_versions())))})")
    data = merged(_load("layouts")["motlist"], entry["layout"], _DEEP)
    rules = data["rules"]
    return ListProfile(version, entry["mot"], _codec("MOTLIST header", data["header"]),
                       {name: _codec(name, spec) for name, spec in data["records"].items()},
                       rules["override_base"], rules["source_key"], rules["reverse"], rules["row_sequences"])


@functools.cache
def pack_profile(version: int) -> tuple[int, RecordCodec]:
    entry = _load("versions")["motpack"].get(str(version))
    if entry is None:
        raise MotionFormatError(f"MOTPACK version {version} is not supported "
                                f"(supported: {', '.join(sorted(_load('versions')['motpack'], key=int))})")
    return entry["mot"], _codec("MOTPACK header", _load("layouts")["motpack"]["header"])


@functools.cache
def jmap_layout(version: int) -> tuple[int, int, RecordCodec] | None:
    entry = _load("versions")["jmap"].get(str(version), {})
    return None if "groups" not in entry else (entry["groups"], entry["count"],
                                               _codec("JMAP mask group", _load("layouts")["jmap"]["group"]))


def controller_rules() -> dict:
    return {kind: rule for kind, rule in _load("controllers").items() if kind != "format"}


@functools.cache
def sequence_track_rules(game: str) -> dict:
    def indexed(value):
        return {key: indexed(item) for key, item in (enumerate(value) if isinstance(value, list) else value.items())} \
            if isinstance(value, (list, dict)) else value

    found = _load("sequence_tracks")["games"].get(game_key(game), {})
    return found and {**found, "classes": {kind: rule | {"defaults": indexed(rule["defaults"])} | ({"enums": {field: {
        item["name"]: item["value"] for item in game_enum_definitions(game)[enum]}
        for field, enum in rule["enums"].items()}} if "enums" in rule else {})
        for kind, rule in found["classes"].items()}}


def jmap_properties(version: int) -> tuple[int, RecordCodec] | None:
    entry = _load("versions")["jmap"].get(str(version), {})
    return None if "properties" not in entry else (
        entry["properties"], _codec("JMAP property joint", _load("layouts")["jmap"][entry["property"]]))


def joint_attributes(game: str) -> dict[str, int]:
    return {name: value for value, name in _game_enums(game)["JointAttr"].items()}


def jmap_extras(version: int) -> tuple[int, int, RecordCodec] | None:
    entry = _load("versions")["jmap"].get(str(version))
    return None if entry is None else (entry["extras"], entry["type"],
                                       _codec("JMAP extra joint", _load("layouts")["jmap"][entry["extra"]]))


def jmap_mirror(version: int) -> tuple[int, int] | None:
    entry = _load("versions")["jmap"].get(str(version), {})
    return None if "mirror" not in entry else (entry["mirror"], entry["mirror_record"])


@functools.cache
def solver_kinds() -> dict[int, str]:
    return {int(code): kind for code, kind in _load("layouts")["solvers"].items()}


@functools.cache
def solver_codes() -> dict[str, int]:
    return {kind: code for code, kind in solver_kinds().items()}


@functools.cache
def frame_types() -> dict[int, str]:
    return {int(code): kind for code, kind in _load("layouts")["frame_types"].items()}


@functools.cache
def append_types() -> dict[str, str]:
    return _load("layouts")["append_types"]


@functools.cache
def append_primitives() -> dict[str, str]:
    return _load("layouts")["append_primitives"]


@functools.cache
def gpu_format(stream: str, fmt: int) -> dict:
    spec = _load("layouts")["gpu_formats"][stream].get(str(fmt))
    if spec is None:
        raise MotionFormatError(f"GPU {stream} format {fmt} is not known")
    return spec


@functools.cache
def gpu_range_floats() -> int:
    return _load("layouts")["gpu_range_floats"]


@functools.cache
def pose_flags() -> dict[str, str]:
    return _load("layouts")["pose_flags"]


def _key_family(name: str) -> tuple[dict, dict]:
    family = _load("tracks")["families"][name]
    if parent := family.get("extends"):
        rules, keys = _key_family(parent)
        return family.get("rules", rules), {kind: {**keys.get(kind, {}), **family.get("keys", {}).get(kind, {})}
                                            for kind in {*keys, *family.get("keys", {})}}
    return family["rules"], family["keys"]


@functools.cache
def encodings(family: str) -> dict[tuple[str, int], Encoding]:
    rules, keys = _key_family(family)
    found = {}
    for kind, codes in keys.items():
        for code, spec in codes.items():
            rule = {**rules.get(kind, {}), **rules.get(f"{kind}.{spec['decode']}", {})}
            order = rule.get("order", "")
            found[kind, int(code)] = Encoding(
                kind, int(code), spec["size"], spec.get("params", 0), spec["decode"], spec.get("axis", -1),
                tuple(spec.get("axes", ())), spec.get("bits", 0), spec.get("endian", "little"), order, rule["blend"],
                rule.get("blend_order", order))
    return found


def encoding(family: str, kind: str, code: int) -> Encoding:
    found = encodings(family).get((kind, code))
    if found is None:
        raise MotionFormatError(f"{kind} track encoding 0x{code:02X} is not known for {family} tracks")
    return found


@functools.cache
def _game_enums(game: str) -> dict[str, dict[int, str]]:
    return game_enums(_load("versions")["enums"], game)


def layer_enums(game: str) -> tuple[dict[int, str], dict[int, str]]:
    enums = _game_enums(game)
    return enums["BlendMode"], enums["JointMaskMode"]


@functools.cache
def display_names(enum: str) -> dict[int, str]:
    """The newest version's games know every value."""
    versions = mot_versions()
    names: dict[int, str] = {}
    for game in versions[max(versions)]["games"]:
        names.update(_game_enums(game)[enum])
    return dict(sorted(names.items()))


@dataclass(frozen=True, slots=True)
class TreeRules:
    version: int
    header: RecordCodec
    records: dict[str, RecordCodec]
    class_names: str
    remaps: str


@functools.cache
def tree_rules(version: int) -> TreeRules:
    family = _load("versions")["tree"].get(str(version))
    if family is None:
        raise MotionFormatError(f"MotTree version {version} is not supported "
                                f"(supported: {', '.join(sorted(_load('versions')['tree'], key=int))})")
    data = merged(_load("layouts")["tree"], family, _DEEP)
    return TreeRules(version, _codec("MotTree header", data["header"]),
                     {name: _codec(name, spec) for name, spec in data["records"].items()},
                     data["rules"]["class_names"], data["rules"]["remaps"])


@functools.cache
def tree_param_kinds() -> dict[int, str]:
    return {int(code): kind for code, kind in _load("layouts")["tree_params"].items()}


@functools.cache
def tree_param_names() -> dict[int, str]:
    """MotTree parameter names by key (murmur3 of the name's bytes); an ExtraData parameter's key is its size."""
    return {int(key): name for key, name in _load("layouts")["tree_param_names"].items()}


@functools.cache
def tree_classes(version: int) -> dict[str, dict]:
    return _load("tree_classes").get(str(version), {})


@functools.cache
def rotation_offset_head(version: int) -> RecordCodec | None:
    """A RotationGlobalOffsetNode blob's head in trees of ``version``, its chain's weights following it (RE4's build
    reads no root joint); None: no build of that version holds the node."""
    data = _load("layouts")["rotation_offset"]
    name = data["versions"].get(str(version))
    return None if name is None else _codec("Rotation offset", data["heads"][name])


@functools.cache
def pose_blend_head() -> RecordCodec:
    return _codec("Pose blend", _load("layouts")["pose_blend"])


@functools.cache
def joint_pose_layout() -> tuple[dict[str, RecordCodec], dict[str, tuple], frozenset[str]]:
    data = _load("layouts")["joint_pose"]
    return ({name: _codec(f"Joint pose {name}", data[name]) for name in ("data", "pose", "frame", "table")},
            {name: tuple(spec) for name, spec in data["rotations"].items()}, frozenset(data["table_calcs"]))
