"""See docs/internals/clip-format.md."""

from __future__ import annotations

import math
import struct
from collections import Counter
from dataclasses import dataclass

from .layout import ClipFormatError
from .model import FLAG_NAMES, Key, Node, Property, SpeedPoint, Timeline, walk_nodes
from .rules import FormatRules, control_tables, control_width, interpolation, profile, property_type
from .values import value_problem

_FLOAT = struct.Struct("<f")


@dataclass(frozen=True, slots=True)
class Issue:
    target: object
    message: str
    error: bool = True


def validate(timeline: Timeline) -> list[Issue]:
    try:
        rules = profile(timeline.version).rules
    except ClipFormatError as exc:
        return [Issue(timeline, str(exc))]
    return _Validator(timeline, rules).run()


def _f32(value) -> bool:
    if type(value) is not float or not math.isfinite(value):
        return False
    try:
        _FLOAT.pack(value)
    except OverflowError:
        return False
    return True


class _Validator:
    def __init__(self, timeline: Timeline, rules: FormatRules):
        self.timeline, self.rules = timeline, rules
        self.version = timeline.version
        self.records = rules.records
        self.issues: list[Issue] = []

    def run(self) -> list[Issue]:
        timeline, rules = self.timeline, self.rules
        if not _f32(timeline.total_frame):
            self._add(timeline, "The total frame is not a finite 32-bit float")
        if timeline.header_guid != bytes(16) and "header_guid" not in rules.header.names:
            self._add(timeline, "This version stores no header GUID")
        clip_roots = rules.clip_roots
        for track in timeline.tracks:
            self._text(track, track.name, "name")
            self._text(track, track.group, "group")
            self._stored("track", track, "tool_flag", track.tool_flag, 0)
            if not self._fits("track", "tool_flag", track.tool_flag):
                self._add(track, "The tool flag does not fit its field")
            if track.clips and "clip" not in self.records:
                self._add(track, "This version has no clips")
            if track.nodes and clip_roots:
                self._add(track, "In this version a track's nodes belong to its clips")
            for clip in track.clips:
                self._text(clip, clip.name, "name")
                if clip.nodes and not clip_roots:
                    self._add(clip, "In this version a clip holds no nodes")
                for name in ("frame_in", "frame_out", "source_in", "source_out"):
                    if not _f32(getattr(clip, name)):
                        self._add(clip, f"{name} is not a finite 32-bit float")
        held = [node for track in timeline.tracks for node in track.roots]
        if timeline.root_order is not None and (clip_roots or Counter(map(id, timeline.root_order))
                                                != Counter(map(id, held))):
            self._add(timeline, "The root order does not list the tracks' root nodes")
        self._ownership(held)
        nodes = list(walk_nodes(timeline.roots))
        if not self._fits("node", "unique_id", max(len(nodes) - 1, 0)):
            self._add(timeline, "More nodes than the node ID field can number")
        for node in nodes:
            self._node(node)
        return self.issues

    def _add(self, target, message: str, error: bool = True) -> None:
        self.issues.append(Issue(target, message, error))

    def _text(self, target, text, what: str) -> None:
        if not isinstance(text, str) or "\0" in text:
            self._add(target, f"The {what} must be text without NUL characters")

    def _stored(self, record: str, target, field: str, value, default) -> None:
        if value != default and field not in self.records[record].names:
            self._add(target, f"This version stores no {field.replace('_', ' ')}")

    def _fits(self, record: str, field: str, value: int) -> bool:
        low, high = self.records[record].limits.get(field, (0, (1 << 64) - 1))
        return low <= value <= high

    def _ownership(self, roots: list[Node]) -> None:
        clips = [clip for track in self.timeline.tracks for clip in track.clips]
        if len(set(map(id, clips))) != len(clips):
            self._add(self.timeline, "A clip is listed in two places")
        root_ids = set(map(id, roots))
        parents = Counter(id(child) for node in walk_nodes(roots) for child in node.children)
        for node in walk_nodes(roots):
            if parents[id(node)] != (id(node) not in root_ids):
                self._add(node, "The node is listed in two places")
        seen: set[int] = set()
        stack = [prop for node in walk_nodes(roots) for prop in node.properties]
        while stack:
            prop = stack.pop()
            if id(prop) in seen:
                self._add(prop, "The property is listed in two places")
                continue
            seen.add(id(prop))
            stack.extend(prop.children)
            for item in [*prop.keys, *prop.extra_keys.values(), *prop.speed_points]:
                if id(item) in seen:
                    self._add(item, "The key is listed in two places")
                seen.add(id(item))

    def _node(self, node: Node) -> None:
        self._text(node, node.name, "name")
        self._text(node, node.tag, "tag")
        self._stored("node", node, "tag", node.tag, "")
        self._stored("node", node, "editor_id", node.editor_id, 0)
        self._stored("node", node, "type_hash", node.type_hash, 0)
        self._stored("node", node, "node_id", node.node_id, bytes(16))
        if not self._fits("node", "node_type", node.node_type):
            self._add(node, "The node type does not fit a byte")
        if not (isinstance(node.guid, bytes) and len(node.guid) == 16 and isinstance(node.node_id, bytes)
                and len(node.node_id) == 16):
            self._add(node, "GUIDs must be 16 bytes")
        if not (self._fits("node", "editor_id", node.editor_id) and self._fits("node", "type_hash", node.type_hash)):
            self._add(node, "IDs must fit 32 bits")
        if not (_f32(node.begin_frame) and _f32(node.end_frame)):
            self._add(node, "Frames must be finite 32-bit floats")
        for prop in node.properties:
            self._property(prop, None)

    def _property(self, prop: Property, parent: Property | None) -> None:
        rules = self.rules
        info = property_type(prop.type)
        self._text(prop, prop.name, "name")
        self._stored("property", prop, "editor_id", prop.editor_id, 0)
        for flag in FLAG_NAMES:
            self._stored("property", prop, flag, getattr(prop, flag), False)
        if not self._fits("property", "type", prop.type):
            self._add(prop, "The property type does not fit a byte")
        if not self._fits("property", "editor_id", prop.editor_id):
            self._add(prop, "The editor ID must fit 32 bits")
        if not self._fits("property", "array_index", prop.array_index):
            self._add(prop, "The array index is out of range")
        elif prop.array_index != -1 and (parent is None or property_type(parent.type) is None
                                         or property_type(parent.type).children != "elements"):
            self._add(prop, "Only array elements have an array index")
        if not (_f32(prop.begin_frame) and _f32(prop.end_frame)):
            self._add(prop, "Frames must be finite 32-bit floats")
        keyed = info is not None and info.owns_keys(self.version)
        count = len(prop.keys) if keyed else len(prop.children)
        if not self._fits("property", "key_count", count):
            self._add(prop, "The property holds more keys or children than its count field fits")
        if not keyed:
            if prop.keys or prop.extra_keys or prop.speed_points:
                self._add(prop, "A property of this type holds child properties, not keys")
            for child in prop.children:
                self._property(child, prop)
            return
        if prop.children:
            self._add(prop, "A property of this type holds keys, not child properties")
        if prop.compact_keys and not rules.compact_keys:
            self._add(prop, "This version has no compact keys")
        if not self._fits("property", "speed_count", len(prop.speed_points)):
            self._add(prop, "The property holds more speed points than its count field fits")
        slots = 1 if rules.last_key_table else rules.extra_key_slots
        if any(not 0 <= slot < slots for slot in prop.extra_keys):
            self._add(prop, f"This version has {slots} extra key slot{'s' if slots > 1 else ''}")
        for key in prop.keys:
            self._key(key, info, rules.key_record(info, self.version, prop.compact_keys).name)
        for slot, key in prop.extra_keys.items():
            last = rules.last_key_table and slot == 0
            self._key(key, info, rules.key_record(info, self.version, prop.compact_keys, last).name)
        for point in prop.speed_points:
            self._control(point, "speed_point", point.interpolation, point.control)
            if not (_f32(point.frame) and _f32(point.rate)):
                self._add(point, "Speed point frame and rate must be finite 32-bit floats")
            if not self._fits("speed_point", "interpolation", point.interpolation):
                self._add(point, "The interpolation does not fit")

    def _key(self, key: Key, info, record: str) -> None:
        fields = self.records[record].names
        if not _f32(key.frame) or not _f32(key.rate):
            self._add(key, "Key frame and rate must be finite 32-bit floats")
        if problem := value_problem(info.value, key.value):
            self._add(key, f"The value {problem}")
        self._stored(record, key, "rate", key.rate, 0.0)
        self._stored(record, key, "width", key.width, 0)
        self._stored(record, key, "instance_value", key.instance_value, False)
        if not self._fits(record, "interpolation", key.interpolation):
            self._add(key, "The interpolation does not fit a byte")
        if not self._fits(record, "width", key.width):
            self._add(key, "The width does not fit its field")
        self._control(key, "key" if "control_index" in fields else "", key.interpolation, key.control)

    def _control(self, item: Key | SpeedPoint, owner: str, code: int, control) -> None:
        info = interpolation(code)
        table = info.control if info is not None else ""
        if not table:
            if control is not None:
                self._add(item, "Only Hermite and Bezier interpolations hold control points")
            return
        if not owner or table not in control_tables(owner):
            self._add(item, f"{info.name} needs control points this record cannot hold")
            return
        width = control_width(table)
        if not (isinstance(control, tuple) and len(control) == width and all(_f32(value) for value in control)):
            self._add(item, f"{info.name} needs {width} finite 32-bit control values")
