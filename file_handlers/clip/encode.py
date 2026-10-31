"""See docs/internals/clip-format.md."""

from __future__ import annotations

lazy from utils.hash_util import murmur3_hash

from .decode import MAGIC
from .layout import ClipFormatError
from .model import FLAG_NAMES, Key, Node, Property, ResourceKind, Timeline, walk_nodes
from .rules import control_tables, interpolation, profile, property_type
from .values import encode_value


def write_timeline(timeline: Timeline) -> bytes:
    from .validation import validate

    errors = [issue for issue in validate(timeline) if issue.error]
    if errors:
        raise ClipFormatError(f"{errors[0].message} ({len(errors)} problem{'s' if len(errors) > 1 else ''})")
    return _Encoder(timeline).write()


def _align(value: int, alignment: int) -> int:
    return -(-value // alignment) * alignment


class _Pool:
    def __init__(self, wide: bool, dedupe: bool):
        self.wide, self.dedupe = wide, dedupe
        self.data = bytearray()
        self.offsets: dict[str, int] = {}

    def add(self, text: str) -> int:
        if self.dedupe and (offset := self.offsets.get(text)) is not None:
            return offset
        offset = len(self.data) // 2 if self.wide else len(self.data)
        self.data += text.encode("utf-16-le") + b"\0\0" if self.wide else text.encode("utf-8") + b"\0"
        self.offsets.setdefault(text, offset)
        return offset


class _Encoder:
    def __init__(self, timeline: Timeline):
        self.timeline = timeline
        self.version = timeline.version
        self.rules = profile(timeline.version).rules
        dedupe = self.rules.string_dedupe
        self.pools = {"c8": _Pool(False, dedupe), "c16": _Pool(True, dedupe)}
        self.rows: dict[str, list] = {"path_points": [], "user_data": []}
        self._masks: dict[int, int] = {}

    def write(self) -> bytes:
        self._order()
        rules = self.rules
        node_ref = rules.records["node_ref"]
        tables = {"roots": self.roots, "track_children": self.track_children, "clip_roots": self.clip_roots}
        blobs, counts = {}, {}
        for section in rules.sections:
            if section.pool or section.user_curve:
                continue
            if section.record is node_ref:
                counts[section.table] = len(tables[section.table])
                continue
            records = self._records(section.table)
            counts[section.table] = len(records)
            blobs[section.table] = b"".join(self._encode(section.record, record) for record in records)
        for name, pool in self.pools.items():
            blobs[name] = bytes(pool.data)
        placement, position = {}, rules.header_size
        for section in rules.sections:
            if section.user_curve and self.timeline.kind is not ResourceKind.USER_CURVE:
                continue
            position = _align(position, section.align)
            placement[section.table] = position
            if section.record is node_ref:
                size = 8 * counts[section.table]
            elif section.user_curve:
                size = section.record.size
            else:
                size = len(blobs[section.table])
            position += size
        node_size = rules.records["node"].size
        for table, nodes in tables.items():
            if table in placement:
                blobs[table] = b"".join(
                    node_ref.encode({"node": placement["nodes"] + self.node_index[id(node)] * node_size})
                    for node in nodes)
        header = {"magic": int.from_bytes(MAGIC, "little"), "version": self.version,
                  "total_frame": self.timeline.total_frame, "header_guid": self.timeline.header_guid}
        for section in rules.sections:
            header[section.pointer] = placement.get(section.table, 0)
            if section.count:
                header[section.count] = counts[section.table]
            if section.user_curve and section.table in placement:
                blobs[section.table] = section.record.encode({"table_0": position, "table_1": position})
        out = bytearray(position)
        out[:rules.header_size] = rules.header.encode(header)
        for table, start in placement.items():
            out[start:start + len(blobs[table])] = blobs[table]
        return bytes(out)

    def _encode(self, codec, record: dict) -> bytes:
        for name, pool in codec.strings:
            record[name] = self.pools[pool].add(record[name])
        return codec.encode(record)

    def _intern(self, pool: str, text: str) -> int:
        return self.pools[pool].add(text)

    def _add_row(self, table: str, value) -> int:
        self.rows[table].append(value)
        return len(self.rows[table]) - 1

    def _order(self) -> None:
        timeline, rules = self.timeline, self.rules
        self.roots = timeline.roots
        self.track_children = [node for track in timeline.tracks for node in track.roots]
        self.clips = [clip for track in timeline.tracks for clip in track.clips]
        self.clip_roots = [node for clip in self.clips for node in clip.nodes]
        self.nodes = _grouped(([root] for root in self.roots), lambda node: node.children)
        self.node_index = {id(node): index for index, node in enumerate(self.nodes)}
        self.properties = _grouped((node.properties for node in walk_nodes(self.roots)), lambda prop: prop.children)
        self.property_index = {id(prop): index for index, prop in enumerate(self.properties)}
        self.clip_index = {id(clip): index for index, clip in enumerate(self.clips)}
        self.key_rows: dict[str, list[tuple[Key, Property]]] = {table: [] for table in rules.key_tables}
        self.key_start: dict[int, int] = {}
        for prop in self.properties:
            if self._keyed(prop):
                rows = self.key_rows[self._key_table(prop)]
                self.key_start[id(prop)] = len(rows)
                extras = [] if rules.last_key_table else [prop.extra_keys[slot] for slot in sorted(prop.extra_keys)]
                rows.extend((key, prop) for key in [*prop.keys, *extras])
        self.last_keys: list[tuple[Key, Property]] = []
        if rules.last_key_table:
            def walk(prop: Property) -> None:
                if 0 in prop.extra_keys:
                    self.last_keys.append((prop.extra_keys[0], prop))
                for child in prop.children:
                    walk(child)
            for node in walk_nodes(self.roots):
                for prop in node.properties:
                    walk(prop)
        self.last_key_index = {id(key): index for index, (key, _prop) in enumerate(self.last_keys)}
        self.speed_points = [point for prop in self.properties for point in prop.speed_points]
        self.control_rows: dict[str, list] = {"hermite": [], "hermite3d": []}
        self.control_index: dict[int, int] = {}
        owners = [*((key, "key") for key, _prop in self.key_rows[rules.key_tables[0]]),
                  *((key, "key") for key, _prop in self.last_keys),
                  *((point, "speed_point") for point in self.speed_points)]
        for item, owner in owners:
            info = interpolation(item.interpolation)
            if info is not None and info.control in control_tables(owner):
                rows = self.control_rows[info.control]
                self.control_index[id(item)] = len(rows)
                rows.append(item.control)

    def _keyed(self, prop: Property) -> bool:
        info = property_type(prop.type)
        return info is not None and info.owns_keys(self.version)

    def _key_table(self, prop: Property) -> str:
        return self.rules.key_table(property_type(prop.type), self.version, prop.compact_keys)

    def _records(self, table: str) -> list[dict]:
        match table:
            case "tracks":
                return self._track_records()
            case "clips":
                return self._clip_records()
            case "nodes":
                return [self._node_record(node, index) for index, node in enumerate(self.nodes)]
            case "properties":
                return self._property_records()
            case "speed_points":
                return [{"frame": point.frame, "rate": point.rate, "interpolation": point.interpolation,
                         "control_index": self.control_index.get(id(point), 0)} for point in self.speed_points]
            case "hermite" | "hermite3d":
                return [{"control": row} for row in self.control_rows[table]]
            case "last_keys":
                return [self._key_record(key, prop) for key, prop in self.last_keys]
            case "user_data":
                return [{"type": type_name, "path": path} for type_name, path in self.rows["user_data"]]
            case "path_points":
                return [{"value": value} for value in self.rows["path_points"]]
            case "path_point_aux":
                return []
        return [self._key_record(key, prop) for key, prop in self.key_rows[table]]

    def _track_records(self) -> list[dict]:
        records, child = [], 0
        for track in self.timeline.tracks:
            roots = track.roots
            records.append({"name": track.name, "name_c8": track.name, "group": track.group,
                            "enabled": track.enabled, "tool_flag": track.tool_flag, "clip_count": len(track.clips),
                            "clip_index": self.clip_index[id(track.clips[0])] if track.clips else 0,
                            "child_count": len(roots), "child_index": child if roots else 0})
            child += len(roots)
        return records

    def _clip_records(self) -> list[dict]:
        records, root = [], 0
        for clip in self.clips:
            records.append({"name": clip.name, "frame_in": clip.frame_in, "frame_out": clip.frame_out,
                            "source_in": clip.source_in, "source_out": clip.source_out,
                            "root_count": len(clip.nodes), "root_index": root if clip.nodes else 0})
            root += len(clip.nodes)
        return records

    def _node_record(self, node: Node, index: int) -> dict:
        return {"name": node.name, "name_c8": node.name, "node_type": node.node_type, "begin_frame": node.begin_frame,
                "end_frame": node.end_frame, "guid": node.guid, "node_id": node.node_id, "tag": node.tag,
                "editor_id": node.editor_id, "type_hash": node.type_hash, "unique_id": index,
                "extra_mask": self._extra_mask(node), "name_hash": murmur3_hash(node.name.encode("utf-8")),
                "unicode_hash": murmur3_hash(node.name.encode("utf-16-le")),
                "child_count": len(node.children),
                "child_index": self.node_index[id(node.children[0])] if node.children else 0,
                "property_count": len(node.properties),
                "property_index": self.property_index[id(node.properties[0])] if node.properties else 0}

    def _extra_mask(self, node: Node) -> int:
        mask = self._masks.get(id(node))
        if mask is None:
            mask = 0
            stack = list(node.properties)
            while stack:
                prop = stack.pop()
                mask |= sum(1 << slot for slot in prop.extra_keys if slot)
                stack.extend(prop.children)
            for child in node.children:
                mask |= self._extra_mask(child)
            self._masks[id(node)] = mask
        return mask

    def _property_records(self) -> list[dict]:
        records, speed = [], 0
        selectors = {table: index for index, table in enumerate(self.rules.key_tables)}
        for prop in self.properties:
            record = {"name": prop.name, "name_c16": prop.name, "type": prop.type, "begin_frame": prop.begin_frame,
                      "end_frame": prop.end_frame, "array_index": prop.array_index, "editor_id": prop.editor_id,
                      "name_hash": murmur3_hash(prop.name.encode("utf-8")),
                      "unicode_hash": murmur3_hash(prop.name.encode("utf-16-le")),
                      "speed_count": len(prop.speed_points), "speed_index": speed,
                      "key_table": selectors[self._key_table(prop)],
                      "extra_key_flags": sum(1 << slot for slot in prop.extra_keys),
                      "last_key_flag": 0 in prop.extra_keys,
                      "last_key_index": self.last_key_index.get(id(prop.extra_keys.get(0)), 0)}
            record.update((flag, getattr(prop, flag)) for flag in FLAG_NAMES)
            if self._keyed(prop):
                has_keys = prop.keys or (prop.extra_keys and not self.rules.last_key_table)
                record["key_count"], record["key_index"] = len(prop.keys), self.key_start[id(prop)] if has_keys else 0
            else:
                record["key_count"] = len(prop.children)
                record["key_index"] = self.property_index[id(prop.children[0])] if prop.children else 0
            speed += len(prop.speed_points)
            records.append(record)
        return records

    def _key_record(self, key: Key, prop: Property) -> dict:
        return {"frame": key.frame, "rate": key.rate, "interpolation": key.interpolation, "width": key.width,
                "instance_value": key.instance_value, "control_index": self.control_index.get(id(key), 0),
                "value": encode_value(property_type(prop.type).value, key.value, self._intern, self._add_row)}


def _grouped(groups, children) -> list:
    out: list = []
    seen: set[int] = set()

    def visit(group) -> None:
        group = list(group)
        for item in group:
            if id(item) not in seen:
                seen.add(id(item))
                out.append(item)
        for item in group:
            if members := children(item):
                visit(members)

    for group in groups:
        visit(group)
    return out
