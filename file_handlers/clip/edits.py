"""See docs/internals/clip-editor.md."""

from __future__ import annotations

import copy
import uuid
from collections import Counter

lazy from utils.hash_util import murmur3_hash

from .commands import ReplaceContents, SetFields
from .document import ClipDocument
from .evaluation import NUMERIC_TYPES, sample
from .layout import RecordCodec
from .model import Clip, Key, Node, Property, SpeedPoint, Track, walk_nodes, walk_properties
from .rules import (
    InterpolationType,
    ValueCodec,
    control_tables,
    control_width,
    display_names,
    interpolation,
    property_type,
)
from .values import coerce, default_value, value_problem


def new_guid() -> bytes:
    return uuid.uuid4().bytes_le


def new_editor_id() -> int:
    return uuid.uuid4().int & 0xFFFFFFFF or 1


def stores(document: ClipDocument, record: str, field: str) -> bool:
    codec = document.rules.records.get(record)
    return codec is not None and field in codec.names


def set_fields(document: ClipDocument, target: object, text: str, merge_key=None, **values) -> None:
    edits = [(target, values)]
    if "frame" in values and isinstance(target, (Key, SpeedPoint)):
        edits += _in_frame_order(document, [target], {id(target): values["frame"]})
    document.push(SetFields(document, edits, text, merge_key))


def _in_frame_order(document: ClipDocument, moved: list, frames: dict[int, float]) -> list[tuple[object, dict]]:
    lists = {}
    for item in moved:
        found = document.parent(item)
        if found is not None and found[1] != "extra_keys":
            lists[id(found[0]), found[1]] = found
    return [(owner, {field: sorted(getattr(owner, field), key=lambda item: frames.get(id(item), item.frame))})
            for owner, field in lists.values()]


def _insert(document: ClipDocument, owner: object, field: str, index: int | None, item: object, text: str) -> None:
    items = list(getattr(owner, field))
    items.insert(len(items) if index is None else index, item)
    document.push(ReplaceContents(document, owner, field, items, text))


def _span(item: Clip | Node | Property) -> tuple[float, float]:
    if isinstance(item, Clip):
        return item.source_in, item.source_out
    return item.begin_frame, item.end_frame


def add_track(document: ClipDocument, index: int | None = None) -> Track:
    track = Track(name="Track")
    _insert(document, document.timeline, "tracks", index, track, "Add Track")
    return track


def add_clip(document: ClipDocument, track: Track) -> Clip:
    end = document.timeline.total_frame
    clip = Clip(frame_out=end, source_out=end)
    _insert(document, track, "clips", None, clip, "Add Clip")
    return clip


def make_node(document: ClipDocument, node_type: int, begin: float, end: float) -> Node:
    name = display_names("NodeType", document.timeline.version).get(node_type, "Node")
    node = Node(name=name, node_type=node_type, begin_frame=begin, end_frame=end, guid=new_guid())
    if stores(document, "node", "node_id"):
        node.node_id = new_guid()
    if stores(document, "node", "editor_id"):
        node.editor_id = new_editor_id()
    if stores(document, "node", "type_hash"):
        node.type_hash = murmur3_hash(name.encode("utf-8"))
    return node


def add_node(document: ClipDocument, parent: Track | Clip | Node, node_type: int) -> Node:
    begin, end = _span(parent) if not isinstance(parent, Track) else (0.0, document.timeline.total_frame)
    node = make_node(document, node_type, begin, end)
    with document.macro("Add Node"):
        _insert(document, parent, "children" if isinstance(parent, Node) else "nodes", None, node, "Add Node")
        _sync_root_order(document)
    return node


def _sync_root_order(document: ClipDocument) -> None:
    timeline = document.timeline
    if timeline.root_order is None:
        return
    held = [node for track in timeline.tracks for node in track.roots]
    left, order = Counter(map(id, held)), []
    for node in [*timeline.root_order, *held]:
        if left[id(node)]:
            left[id(node)] -= 1
            order.append(node)
    if list(map(id, order)) != list(map(id, timeline.root_order)):
        document.push(ReplaceContents(document, timeline, "root_order", order, "Root Order"))


def make_property(document: ClipDocument, name: str, kind: int, begin: float, end: float,
                  array_index: int = -1) -> Property:
    info = property_type(kind)
    prop = Property(name=name, type=kind, begin_frame=begin, end_frame=end, array_index=array_index)
    prop.compact_keys = document.rules.compact_keys and info is not None and info.owns_keys(document.timeline.version)
    if stores(document, "property", "editor_id"):
        prop.editor_id = new_editor_id()
    return prop


def add_property(document: ClipDocument, owner: Node | Property, name: str, kind: int,
                 array_index: int = -1) -> Property:
    begin, end = _span(owner)
    prop = make_property(document, name, kind, begin, end, array_index)
    field = "properties" if isinstance(owner, Node) else "children"
    _insert(document, owner, field, None, prop, "Add Property")
    return prop


def missing_members(container: Property) -> list[tuple[str, int]]:
    info = property_type(container.type)
    present = {child.name for child in container.children}
    return [member for member in (info.members if info else ()) if member[0] not in present]


def add_member(document: ClipDocument, container: Property, name: str) -> Property:
    members = property_type(container.type).members
    kind = dict(members)[name]
    order = [member for member, _ in members]
    prop = make_property(document, name, kind, container.begin_frame, container.end_frame)
    index = sum(1 for child in container.children
                if child.name in order and order.index(child.name) < order.index(name))
    _insert(document, container, "children", index, prop, "Add Member")
    return prop


def add_element(document: ClipDocument, array: Property, kind: int) -> Property:
    index = max((child.array_index for child in array.children), default=-1) + 1
    return add_property(document, array, f"{array.name}[{index}]", kind, index)


def rename(document: ClipDocument, item: object, name: str) -> None:
    values = {"name": name}
    if isinstance(item, Node) and stores(document, "node", "type_hash") and \
            item.type_hash == murmur3_hash(item.name.encode("utf-8")):
        values["type_hash"] = murmur3_hash(name.encode("utf-8"))
    set_fields(document, item, "Rename", **values)


def remove(document: ClipDocument, items: list) -> None:
    with document.macro("Delete"):
        for item in items:
            found = document.parent(item)
            if found is None:
                continue
            owner, field = found
            container = getattr(owner, field)
            if isinstance(container, dict):
                contents = {slot: key for slot, key in container.items() if key is not item}
            else:
                contents = [entry for entry in container if entry is not item]
            document.push(ReplaceContents(document, owner, field, contents, "Delete"))
        _sync_root_order(document)


def duplicate(document: ClipDocument, items: list) -> list:
    """Nodes in copied tracks and clips keep their IDs, as in clips the authoring tool reuses."""
    copies = []
    with document.macro("Duplicate"):
        for item in items:
            found = document.parent(item)
            if found is None or found[1] == "extra_keys":
                continue
            owner, field = found
            twin = copy.deepcopy(item)
            container = getattr(owner, field)
            index = container.index(item) + 1
            if isinstance(twin, (Key, SpeedPoint)):
                twin.frame += 1.0
                index = sum(1 for existing in container if existing.frame <= twin.frame)
            elif isinstance(twin, Property):
                _renew_properties(document, [twin])
            elif isinstance(twin, Node):
                for node in walk_nodes([twin]):
                    _renew(document, node)
            _insert(document, owner, field, index, twin, "Duplicate")
            copies.append(twin)
        _sync_root_order(document)
    return copies


def _renew(document: ClipDocument, node: Node) -> None:
    node.guid = new_guid()
    if stores(document, "node", "node_id"):
        node.node_id = new_guid()
    if stores(document, "node", "editor_id"):
        node.editor_id = new_editor_id()
    _renew_properties(document, node.properties)


def _renew_properties(document: ClipDocument, properties: list[Property]) -> None:
    if stores(document, "property", "editor_id"):
        for prop in walk_properties(properties):
            prop.editor_id = new_editor_id()


def move(document: ClipDocument, item: object, delta: int) -> bool:
    found = document.parent(item)
    if found is None or found[1] == "extra_keys":
        return False
    owner, field = found
    items = list(getattr(owner, field))
    index = items.index(item)
    target = index + delta
    if not 0 <= target < len(items):
        return False
    items.insert(target, items.pop(index))
    document.push(ReplaceContents(document, owner, field, items, "Move"))
    return True


def shift(document: ClipDocument, items: list, delta: float, merge_key=None) -> None:
    """A clip's content follows its source range on its own."""
    edits: dict[int, tuple[object, dict]] = {}

    def frames(target, *names) -> None:
        if id(target) not in edits:
            edits[id(target)] = (target, {name: getattr(target, name) + delta for name in names})

    for item in items:
        if isinstance(item, Clip):
            frames(item, "frame_in", "frame_out")
            continue
        nodes = list(walk_nodes([item])) if isinstance(item, Node) else []
        for node in nodes:
            frames(node, "begin_frame", "end_frame")
        roots = [prop for node in nodes for prop in node.properties] if nodes else \
            [item] if isinstance(item, Property) else []
        for prop in walk_properties(roots):
            frames(prop, "begin_frame", "end_frame")
            for key in [*prop.keys, *prop.extra_keys.values(), *prop.speed_points]:
                frames(key, "frame")
        if isinstance(item, (Key, SpeedPoint)):
            frames(item, "frame")
    moved = [item for item in items if isinstance(item, (Key, SpeedPoint))]
    frames_after = {key: values["frame"] for key, (_, values) in edits.items() if "frame" in values}
    for owner, values in _in_frame_order(document, moved, frames_after):
        edits.setdefault(id(owner), (owner, {}))[1].update(values)
    if edits:
        document.push(SetFields(document, list(edits.values()), "Move in Time", merge_key))


def trim(document: ClipDocument, item: Clip | Node | Property, end: bool, delta: float, merge_key=None) -> None:
    if isinstance(item, Clip):
        names = ("frame_out", "source_out") if end else ("frame_in", "source_in")
    else:
        names = ("end_frame",) if end else ("begin_frame",)
    set_fields(document, item, "Trim", merge_key, **{name: getattr(item, name) + delta for name in names})


def slip(document: ClipDocument, clip: Clip, delta: float, merge_key=None) -> None:
    set_fields(document, clip, "Slip", merge_key, source_in=clip.source_in + delta, source_out=clip.source_out + delta)


def default_interpolation(kind: int) -> int:
    return InterpolationType.LINEAR if kind in NUMERIC_TYPES else InterpolationType.DISCRETE


def default_control(code: int) -> tuple[float, ...] | None:
    info = interpolation(code)
    return (0.0,) * control_width(info.control) if info is not None and info.control else None


def make_key(document: ClipDocument, prop: Property, frame: float) -> Key:
    keys = prop.keys
    before = [key for key in keys if key.frame <= frame]
    mode = (before[-1] if before else keys[0]).interpolation if keys else default_interpolation(prop.type)
    value = sample(prop, frame) if keys else None
    if value is None:
        value = default_value(property_type(prop.type).value)
    key = Key(frame=frame, value=value, interpolation=mode, width=document.rules.width_default,
              control=default_control(mode))
    for name, value in fitted(document, key_record(document, prop), key, prop.type).items():
        setattr(key, name, value)
    return key


def key_record(document: ClipDocument, prop: Property, key: Key | None = None) -> RecordCodec:
    rules = document.rules
    last = rules.last_key_table and key is not None and prop.extra_keys.get(0) is key
    return rules.key_record(property_type(prop.type), document.timeline.version, prop.compact_keys, last)


def fitted(document: ClipDocument, record: RecordCodec, key: Key, kind: int) -> dict:
    names = record.names
    values = {"width": (key.width or document.rules.width_default) if "width" in names else 0,
              "rate": key.rate if "rate" in names else 0.0,
              "instance_value": key.instance_value if "instance_value" in names else False}
    info = interpolation(key.interpolation)
    if info is not None and info.control and not ("control_index" in names and info.control in control_tables("key")):
        values.update(interpolation=default_interpolation(kind), control=None)
    return {name: value for name, value in values.items() if getattr(key, name) != value}


def add_key(document: ClipDocument, prop: Property, frame: float) -> Key:
    key = make_key(document, prop, frame)
    index = sum(1 for existing in prop.keys if existing.frame <= frame)
    _insert(document, prop, "keys", index, key, "Add Key")
    return key


def set_extra_key(document: ClipDocument, prop: Property, slot: int) -> Key:
    key = make_key(document, prop, prop.end_frame)
    document.push(ReplaceContents(document, prop, "extra_keys", {**prop.extra_keys, slot: key}, "Add Extra Key"))
    return key


def add_speed_point(document: ClipDocument, prop: Property, frame: float) -> SpeedPoint:
    point = SpeedPoint(frame=frame, rate=1.0, interpolation=InterpolationType.LINEAR)
    index = sum(1 for existing in prop.speed_points if existing.frame <= frame)
    _insert(document, prop, "speed_points", index, point, "Add Speed Point")
    return point


def interpolations_for(document: ClipDocument, items: list) -> dict[int, str]:
    allowed, names = None, display_names("InterpolationType", document.timeline.version)
    for item in items:
        prop = document.owner(item, Property)
        info = property_type(prop.type) if prop is not None else None
        owner = "speed_point" if isinstance(item, SpeedPoint) else "key"
        dedicated = info is not None and document.rules.compact_keys and bool(info.key_storage)
        codes = set()
        for code in names:
            mode = interpolation(code)
            if mode.numeric and owner == "key" and (prop is None or prop.type not in NUMERIC_TYPES):
                continue
            if mode.control and (mode.control not in control_tables(owner) or (owner == "key" and dedicated)):
                continue
            codes.add(code)
        codes.add(item.interpolation)
        allowed = codes if allowed is None else allowed & codes
    return {code: names.get(code, f"0x{code:X}") for code in sorted(allowed or ())}


def set_interpolation(document: ClipDocument, items: list, code: int) -> None:
    info = interpolation(code)
    edits, full = [], []
    for item in items:
        owner = "speed_point" if isinstance(item, SpeedPoint) else "key"
        needs = info is not None and info.control in control_tables(owner)
        control = item.control if needs and item.control is not None and \
            len(item.control) == control_width(info.control) else default_control(code) if needs else None
        edits.append((item, {"interpolation": code, "control": control}))
        prop = document.owner(item, Property)
        if needs and isinstance(item, Key) and prop is not None and prop.compact_keys and \
                not property_type(prop.type).key_storage and prop not in full:
            full.append(prop)
    with document.macro("Set Interpolation"):
        if full:
            document.push(SetFields(document, [(prop, {"compact_keys": False}) for prop in full], "Full Keys"))
        document.push(SetFields(document, edits, "Set Interpolation"))


def set_key_storage(document: ClipDocument, prop: Property, compact: bool) -> None:
    record = document.rules.key_record(property_type(prop.type), document.timeline.version, compact)
    edits = [(key, values) for key in [*prop.keys, *prop.extra_keys.values()]
             if (values := fitted(document, record, key, prop.type))]
    document.push(SetFields(document, [(prop, {"compact_keys": compact}), *edits], "Key Storage"))


def paste_keys(document: ClipDocument, prop: Property, keys: list[Key], start: float) -> list[Key]:
    """``keys`` hold frames relative to ``start``."""
    codec, record = property_type(prop.type).value, key_record(document, prop)
    pasted = []
    for key in keys:
        if not value_problem(codec, key.value):
            twin = copy.copy(key)
            twin.frame += start
            for name, value in fitted(document, record, twin, prop.type).items():
                setattr(twin, name, value)
            pasted.append(twin)
    if pasted:
        document.push(ReplaceContents(document, prop, "keys", sorted([*prop.keys, *pasted], key=lambda key: key.frame),
                                      "Paste Keys"))
    return pasted


def retarget(document: ClipDocument, prop: Property, kind: int) -> None:
    if kind == prop.type:
        return
    rules, version = document.rules, document.timeline.version
    old, new = property_type(prop.type), property_type(kind)
    keyed = new is not None and new.owns_keys(version)
    kept = keyed and old is not None and old.owns_keys(version)
    compact = prop.compact_keys if kept else rules.compact_keys and keyed
    edits = []
    with document.macro("Change Type"):
        if not kept:
            document.push(ReplaceContents(document, prop, "keys", [], "Change Type"))
            document.push(ReplaceContents(document, prop, "extra_keys", {}, "Change Type"))
            document.push(ReplaceContents(document, prop, "speed_points", [], "Change Type"))
            if keyed:
                document.push(ReplaceContents(document, prop, "children", [], "Change Type"))
        else:
            for key in [*prop.keys, *prop.extra_keys.values()]:
                record = rules.key_record(new, version, compact, rules.last_key_table and prop.extra_keys.get(0) is key)
                edits.append((key, {"value": _convert(key.value, old.value, new.value),
                                    **fitted(document, record, key, kind)}))
            if not new.speed_points and prop.speed_points:
                document.push(ReplaceContents(document, prop, "speed_points", [], "Change Type"))
        document.push(SetFields(document, [(prop, {"type": kind, "compact_keys": compact}), *edits], "Change Type"))


def _convert(value, old: ValueCodec, new: ValueCodec) -> object:
    if old.numeric and new.numeric and isinstance(value, (int, float)):
        return coerce(new, value)
    if (old.kind, old.table) == (new.kind, new.table) and not value_problem(new, value):
        return value
    return default_value(new)

