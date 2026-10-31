"""See docs/internals/clip-format.md."""

from __future__ import annotations

import struct
from collections import Counter

from .layout import ClipFormatError
from .model import FLAG_NAMES, Clip, Key, Node, Property, ResourceKind, SpeedPoint, Timeline, Track
from .rules import PropertyTypeInfo, control_tables, interpolation, profile, property_type
from .values import decode_value, default_value

MAGIC = b"CLIP"
_VERSION = struct.Struct("<I")


def read_timeline(data: bytes, kind: ResourceKind | None = None) -> Timeline:
    return _Decoder(data).read(kind)


def _align(value: int, alignment: int) -> int:
    return -(-value // alignment) * alignment


class _Decoder:
    def __init__(self, data: bytes):
        if len(data) < 8 or data[:4] != MAGIC:
            raise ClipFormatError("Not a CLIP file")
        self.data = data
        self.profile = profile(_VERSION.unpack_from(data, 4)[0])
        self.rules = self.profile.rules
        if len(data) < self.rules.header_size:
            raise ClipFormatError("CLIP header is truncated")
        self.header = self.rules.header.decode(data, 0)
        self.tables: dict[str, list[dict]] = {}
        self.pools: dict[str, tuple[int, int]] = {}
        self._strings: dict[tuple[str, int], str] = {}

    def read(self, kind: ResourceKind | None) -> Timeline:
        self._read_sections()
        nodes = [self._node(record) for record in self.tables["nodes"]]
        tracks = self._tracks(nodes)
        if "data" in self.tables:
            if kind not in (None, ResourceKind.USER_CURVE):
                raise ClipFormatError("A user-curve data table in a file that is not a .ucurve")
            kind = ResourceKind.USER_CURVE
        elif kind is ResourceKind.USER_CURVE:
            raise ClipFormatError("The .ucurve file has no data table")
        timeline = Timeline(self.profile.version, kind or ResourceKind.CLIP, self.header["total_frame"],
                            self.header.get("header_guid", bytes(16)), tracks)
        roots = self._node_refs("roots", nodes)
        held = timeline.roots
        if [id(node) for node in roots] != [id(node) for node in held]:
            if self.rules.clip_roots or Counter(map(id, roots)) != Counter(map(id, held)):
                raise ClipFormatError("The root table and the tracks hold different nodes")
            timeline.root_order = roots
        self._link(held, nodes)
        return timeline

    def _link(self, roots: list[Node], nodes: list[Node]) -> None:
        node_records, property_records = self.tables["nodes"], self.tables["properties"]
        index_of = {id(node): index for index, node in enumerate(nodes)}
        pending = list(dict.fromkeys(index_of[id(root)] for root in roots))
        claimed, claimed_properties = set(pending), set()
        properties: list[Property | None] = [None] * len(property_records)

        def claim(start: int, count: int, table: list, taken: set, what: str) -> range:
            if start + count > len(table):
                raise ClipFormatError(f"The {what} range {start}+{count} leaves its table of {len(table)}")
            span = range(start, start + count)
            if any(index in taken for index in span):
                raise ClipFormatError(f"A {what[:-1] if what.endswith('s') else what} is listed in two places")
            taken.update(span)
            return span

        while pending:
            node = nodes[index := pending.pop()]
            record = node_records[index]
            children = claim(record["child_index"], record["child_count"], nodes, claimed, "node children")
            node.children = [nodes[child] for child in children]
            pending.extend(children)
            stack = list(claim(record["property_index"], record["property_count"], property_records,
                               claimed_properties, "node properties"))
            for item in stack:
                properties[item] = self._property(property_records[item])
            node.properties = [properties[item] for item in stack]
            while stack:
                item = stack.pop()
                prop = properties[item]
                if (children := self._link_property(prop, property_records[item])) is not None:
                    span = claim(*children, property_records, claimed_properties, "property children")
                    for child in span:
                        properties[child] = self._property(property_records[child])
                    prop.children = [properties[child] for child in span]
                    stack.extend(span)

    def _read_sections(self) -> None:
        data, header, size = self.data, self.header, len(self.data)
        placed = [(section, header[section.pointer]) for section in self.rules.sections
                  if not (section.user_curve and header[section.pointer] == 0)]
        position = self.rules.header_size
        for index, (section, start) in enumerate(placed):
            if start != _align(position, section.align) or any(data[position:start]):
                raise ClipFormatError(f"The {section.table} table is not where the previous table ends")
            end = placed[index + 1][1] if index + 1 < len(placed) else size
            if end < start or end > size:
                raise ClipFormatError(f"The {section.table} table overruns the file")
            if section.pool:
                self.pools[section.pool] = (start, end)
                position = end
                continue
            record = section.record
            rows = header[section.count] if section.count else 1 if section.user_curve else (end - start) // record.size
            position = start + rows * record.size
            if position > end:
                raise ClipFormatError(f"The {section.table} table overruns the next table")
            if section.empty and rows:
                raise ClipFormatError(f"The {section.empty} holds rows; no shipped file uses it")
            self.tables[section.table] = record.decode_table(data, start, rows)
            if section.user_curve and any(self.tables[section.table][0][name] != size
                                          for name in record.names if name.startswith("table_")):
                raise ClipFormatError("The user-curve data table holds entries")
        if position != size:
            raise ClipFormatError("Bytes follow the last table")
        self.node_table = header["node_table"]

    def _string(self, pool: str, offset: int) -> str:
        text = self._strings.get((pool, offset))
        if text is not None:
            return text
        start, end = self.pools[pool]
        data = self.data
        try:
            if pool == "c8":
                at = start + offset
                stop = data.find(b"\0", at, end)
                if at >= end or stop < 0:
                    raise ClipFormatError(f"c8 string at {offset} leaves its pool")
                text = data[at:stop].decode("utf-8")
            else:
                at = start + offset * 2
                stop = data.find(b"\0\0", at, end)
                while stop >= 0 and (stop - at) % 2:
                    stop = data.find(b"\0\0", stop + 1, end)
                if at >= end or stop < 0:
                    raise ClipFormatError(f"c16 string at {offset} leaves its pool")
                text = data[at:stop].decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise ClipFormatError(f"{pool} string at {offset} is not valid text: {exc}") from None
        self._strings[(pool, offset)] = text
        return text

    def _same_name(self, record: dict, field: str, pool: str, name: str) -> None:
        if field in record and self._string(pool, record[field]) != name:
            raise ClipFormatError(f"The two copies of the name {name!r} differ")

    @staticmethod
    def _range(items: list, start: int, count: int, what: str) -> list:
        if start + count > len(items):
            raise ClipFormatError(f"The {what} range {start}+{count} leaves its table of {len(items)}")
        return items[start:start + count]

    def _node_refs(self, table: str, nodes: list[Node]) -> list[Node]:
        size = self.rules.records["node"].size
        refs = []
        for record in self.tables.get(table, ()):
            index, rest = divmod(record["node"] - self.node_table, size)
            if rest or not 0 <= index < len(nodes):
                raise ClipFormatError(f"A {table} entry does not point at a node")
            refs.append(nodes[index])
        return refs

    def _node(self, record: dict) -> Node:
        name = self._string("c16", record["name"])
        self._same_name(record, "name_c8", "c8", name)
        return Node(name, record["node_type"], record["begin_frame"], record["end_frame"], record["guid"],
                    record.get("node_id", bytes(16)), self._string("c16", record["tag"]) if "tag" in record else "",
                    record.get("editor_id", 0), record.get("type_hash", 0))

    def _property(self, record: dict) -> Property:
        name = self._string("c8", record["name"])
        self._same_name(record, "name_c16", "c16", name)
        prop = Property(name, record["type"], record["begin_frame"], record["end_frame"], record["array_index"],
                        record.get("editor_id", 0))
        for flag in FLAG_NAMES:
            setattr(prop, flag, bool(record.get(flag, 0)))
        return prop

    def _link_property(self, prop: Property, record: dict) -> tuple[int, int] | None:
        info = property_type(prop.type)
        if info is None or not info.owns_keys(self.profile.version):
            if record.get("key_table", 0) != self._selector(self.rules.container_key_table) or record["speed_count"] \
                    or record.get("extra_key_flags") or record.get("last_key_flag"):
                raise ClipFormatError(f"The container property {prop.name!r} holds key data")
            return record["key_index"], record["key_count"]
        table = self._key_table(prop, info, record)
        slots = [slot for slot in range(self.rules.extra_key_slots) if record.get("extra_key_flags", 0) >> slot & 1]
        count = record["key_count"]
        keys = [self._key(info, item) for item in
                self._range(self.tables[table], record["key_index"], count + len(slots), f"{prop.name} key")]
        prop.keys, prop.extra_keys = keys[:count], dict(zip(slots, keys[count:]))
        if record.get("last_key_flag"):
            prop.extra_keys[0] = self._key(info, self._range(self.tables["last_keys"], record["last_key_index"], 1,
                                                             f"{prop.name} last key")[0])
        prop.speed_points = [SpeedPoint(item["frame"], item["rate"], item["interpolation"],
                                        self._control("speed_point", item))
                             for item in self._range(self.tables["speed_points"], record["speed_index"],
                                                     record["speed_count"], f"{prop.name} speed point")]

    def _selector(self, table: str) -> int:
        return self.rules.key_tables.index(table)

    def _key_table(self, prop: Property, info: PropertyTypeInfo, record: dict) -> str:
        rules, version = self.rules, self.profile.version
        tables, selector = rules.key_tables, record.get("key_table", 0)
        table = tables[selector] if selector < len(tables) else f"#{selector}"
        if table not in {rules.key_table(info, version, False), rules.key_table(info, version, True)}:
            raise ClipFormatError(f"{info.name} keys of {prop.name!r} are in the {table} key table")
        prop.compact_keys = table != tables[0]
        return table

    def _key(self, info: PropertyTypeInfo, record: dict) -> Key:
        value = (decode_value(info.value, record["value"], self._string, self._row) if "value" in record
                 else default_value(info.value))
        return Key(record["frame"], value, record["interpolation"], record.get("width", 0), record.get("rate", 0.0),
                   bool(record.get("instance_value", 0)), self._control("key", record))

    def _row(self, table: str, index: int) -> object:
        row = self._range(self.tables[table], index, 1, f"{table} row")[0]
        return row["value"] if table == "path_points" else (self._string("c8", row["type"]),
                                                            self._string("c16", row["path"]))

    def _control(self, owner: str, record: dict) -> tuple[float, ...] | None:
        info = interpolation(record["interpolation"])
        table = info.control if info is not None else ""
        index = record.get("control_index", 0)
        if not table or table not in control_tables(owner) or "control_index" not in record:
            if index:
                raise ClipFormatError(f"A {owner} without control points indexes control row {index}")
            return None
        return self._range(self.tables[table], index, 1, f"{table} row")[0]["control"]

    def _tracks(self, nodes: list[Node]) -> list[Track]:
        children = self._node_refs("track_children", nodes)
        clip_roots = self._node_refs("clip_roots", nodes)
        clips = []
        for record in self.tables.get("clips", ()):
            clip = Clip(self._string("c16", record["name"]), record["frame_in"], record["frame_out"],
                        record["source_in"], record["source_out"])
            if "root_count" in record:
                clip.nodes = self._range(clip_roots, record["root_index"], record["root_count"], "clip roots")
            clips.append(clip)
        tracks = []
        for record in self.tables["tracks"]:
            track = Track(self._string("c16", record["name"]), self._string("c16", record["group"]),
                          bool(record["enabled"]), record.get("tool_flag", 0))
            self._same_name(record, "name_c8", "c8", track.name)
            if "clip_count" in record:
                track.clips = self._range(clips, record["clip_index"], record["clip_count"], "track clips")
            roots = self._range(children, record["child_index"], record["child_count"], "track children")
            if self.rules.clip_roots:
                if [id(node) for node in roots] != [id(node) for node in track.roots]:
                    raise ClipFormatError(f"The children of track {track.name!r} are not its clips' roots")
            else:
                track.nodes = roots
            tracks.append(track)
        return tracks
