"""See docs/internals/mot-format.md, MotTree."""

from __future__ import annotations

import struct

from .decode import _U32, _Reader
from .encode import _utf16z, _Writer
from .errors import MotionFormatError
from .model import MotionTree, TreeLink, TreeMotion, TreeNode, TreeParam, TreeTag
from .rules import tree_param_kinds, tree_rules

_U64 = struct.Struct("<Q")
_F32 = struct.Struct("<f")


def read_tree(data: bytes, base: int = 0) -> MotionTree:
    return _TreeReader(data, base).read()


def write_tree(tree: MotionTree) -> bytes:
    return _TreeWriter(tree).write()


def _param_kind(code: int) -> str:
    kind = tree_param_kinds().get(code)
    if kind is None:
        raise MotionFormatError(f"MotTree parameter type {code} is not supported")
    return kind


class _TreeReader(_Reader):
    def __init__(self, data: bytes, base: int):
        self.rules = tree_rules(_U32.unpack_from(data, base)[0])
        super().__init__(data, base, self.rules.records)
        self.header = self.rules.header.decode(data, base)

    def _utf8(self, offset: int) -> tuple[str, int]:
        start = self.base + offset
        end = self.data.find(b"\0", start)
        if end < 0:
            raise MotionFormatError("A MotTree string runs past the data")
        return self.data[start:end].decode("utf-8"), end + 1 - self.base

    def read(self) -> MotionTree:
        h, rules = self.header, self.rules
        raw_nodes = self._table("node", h["nodes"], h["node_count"])
        cursor = h["nodes"] + h["node_count"] * rules.records["node"].size
        nodes = []
        for raw in raw_nodes:
            if rules.class_names == "pointer":
                class_name = self._utf8(raw["class"])[0]
            else:
                class_name, cursor = self._utf8(cursor)
            nodes.append(TreeNode(class_name, self._string(raw["name"]), raw["name_hash"], raw.get("id", 0),
                                  (raw["class_hash"], raw.get("class_check", 0)), raw["node_type"], raw["flags"],
                                  self._tags(raw), self._params(raw)))
        links = []
        for raw in self._table("link", h["links"], h["link_count"]):
            guid = bytes(self.data[self.base + raw["guid"]:self.base + raw["guid"] + 16]) if raw["guid"] else None
            links.append(TreeLink(nodes[raw["parent"]], raw["pin"], nodes[raw["child"]], raw["child_pin"],
                                  raw["type"], guid))
        tree = MotionTree(rules.version, self._string(h["name"]), nodes, links, nodes[h["root"]] if nodes else None)
        rows = self._table("remap", h["remaps"], h["remap_count"])
        if rules.remaps == "pairs":
            tree.remaps = [(row["target"], row["source"]) for row in rows]
        elif rules.remaps == "triples":
            tree.motions = [TreeMotion(nodes[row["target"]], [(row["bank"], row["motion"])]) for row in rows]
        else:
            if (rows != sorted(rows, key=lambda row: (row["count"] != 1, row["node"]))
                    or h["child_motion_count"] != sum(row["count"] == 1 for row in rows)):
                raise MotionFormatError("MotTree motion groups are not single-choice first in node order")
            total = sum(row["count"] for row in rows)
            choices = [(row["bank"], row["motion"]) for row in self._table("choice", h["choices"], total)]
            start = 0
            for row in rows:
                if row["start"] != start:
                    raise MotionFormatError("MotTree motion choices are not stored in node order")
                tree.motions.append(TreeMotion(nodes[row["node"]], choices[start:start + row["count"]]))
                start += row["count"]
        return tree

    def _tags(self, raw: dict) -> list[TreeTag] | None:
        if not raw["tags"]:
            return None
        count = raw["tag_count"]
        pointers = self._pointers(raw["tags"], count)
        hashes = self._unpack(f"{count}I", raw["tag_hashes"])
        return [TreeTag(self._string(pointer), value) for pointer, value in zip(pointers, hashes)]

    def _params(self, raw: dict) -> list[TreeParam] | None:
        if not raw["params"] and not raw["param_count"]:
            return None
        params = []
        for row in self._table("param", raw["params"], raw["param_count"]):
            kind, value = _param_kind(row["type"]), row["value"]
            if kind == "string16":
                value = self._string(value)
            elif kind == "blob":
                value = bytes(self.data[self.base + value:self.base + value + row["key"]])
            elif value >> 32:
                raise MotionFormatError(f"MotTree {kind} parameter has data above 32 bits")
            elif kind == "f32":
                value = _F32.unpack(_U32.pack(value))[0]
            elif kind == "i32":
                value -= (value & 0x80000000) << 1
            name = self._utf8(row["name"])[0] if "name" in row else None
            params.append(TreeParam(row["type"], row["key"], value, name))
        return params


class _TreeWriter(_Writer):
    def __init__(self, tree: MotionTree):
        self.tree, self.rules = tree, tree_rules(tree.version)
        super().__init__(self.rules.records, self.rules.header.size)
        self.index = {id(node): i for i, node in enumerate(tree.nodes)}

    def _node_index(self, node: TreeNode) -> int:
        found = self.index.get(id(node))
        if found is None:
            raise MotionFormatError("A MotTree link or motion names a node outside the tree")
        return found

    def write(self) -> bytes:
        tree, rules, records = self.tree, self.rules, self.rules.records
        h = {"version": tree.version, "node_count": len(tree.nodes), "link_count": len(tree.links),
             "root": self._node_index(tree.root) if tree.root is not None else 0, "remaps": 0, "choices": 0}
        h.update(self._remaps())
        h["name"] = self._put(_utf16z(tree.name), 8)
        h["nodes"] = self._pad(8)
        self.out.extend(bytes(len(tree.nodes) * records["node"].size))
        rows = [{"name_hash": node.name_hash, "id": node.id, "class_hash": node.class_hashes[0],
                 "class_check": node.class_hashes[1], "node_type": node.node_type, "flags": node.flags,
                 "tag_count": len(node.tags or ()), "param_count": len(node.params or ()),
                 "tags": 0, "tag_hashes": 0, "params": 0} for node in tree.nodes]
        for row, node in zip(rows, tree.nodes):
            row["class"] = self._put(node.class_name.encode("utf-8") + b"\0")
        self._tags(rows)
        for row, node in zip(rows, tree.nodes):
            row["name"] = self._put(_utf16z(node.name), 2)
        self._params(rows)
        h["links"] = self._pad(16)
        self.out.extend(bytes(len(tree.links) * records["link"].size))
        guids = []
        for link in tree.links:
            guids.append(self._put(link.guid, 16) if link.guid is not None else 0)
        for i, (link, guid) in enumerate(zip(tree.links, guids)):
            self._set(h["links"] + i * records["link"].size, self._record("link", {
                "parent": self._node_index(link.parent), "pin": link.pin, "child": self._node_index(link.child),
                "child_pin": link.child_pin, "type": link.type, "guid": guid}))
        for i, row in enumerate(rows):
            self._set(h["nodes"] + i * records["node"].size, self._record("node", row))
        self._pad(16)
        self._set(0, rules.header.encode(h))
        return bytes(self.out)

    def _remaps(self) -> dict:
        tree, records = self.tree, self.rules.records
        pairs = self.rules.remaps == "pairs"
        if tree.motions if pairs else tree.remaps:
            raise MotionFormatError(f"MotTree {tree.version} stores only "
                                    f"{'ID remaps' if pairs else 'motions per node'}")
        if pairs:
            rows = [{"target": target, "source": source} for target, source in tree.remaps]
        elif self.rules.remaps == "triples":
            rows = []
            for motion in tree.motions:
                if len(motion.choices) != 1:
                    raise MotionFormatError(f"MotTree {tree.version} nodes play exactly one motion each")
                rows.append({"target": self._node_index(motion.node), "bank": motion.choices[0][0],
                             "motion": motion.choices[0][1]})
        else:
            motions = sorted(tree.motions, key=lambda motion: (len(motion.choices) != 1, self._node_index(motion.node)))
            rows, start = [], 0
            for motion in motions:
                rows.append({"node": self._node_index(motion.node), "count": len(motion.choices), "start": start})
                start += len(motion.choices)
        found = {"remap_count": len(rows)}
        if rows:
            found["remaps"] = self._put(b"".join(records["remap"].encode(row) for row in rows))
        if self.rules.remaps == "groups":
            found["child_motion_count"] = sum(row["count"] == 1 for row in rows)
            if rows:
                found["choices"] = self._put(b"".join(records["choice"].encode({"bank": bank, "motion": motion})
                                                      for motion in motions for bank, motion in motion.choices), 8)
        return found

    def _tags(self, rows: list[dict]) -> None:
        tagged = [(row, node) for row, node in zip(rows, self.tree.nodes) if node.tags is not None]
        for row, node in tagged:
            row["tags"] = self._pad(8)
            self.out.extend(bytes(8 * len(node.tags)))
            row["tag_hashes"] = len(self.out)
            self.out.extend(struct.pack(f"<{len(node.tags)}I", *(tag.hash for tag in node.tags)))
        for row, node in tagged:
            for i, tag in enumerate(node.tags):
                _U64.pack_into(self.out, row["tags"] + 8 * i, self._put(_utf16z(tag.name), 8))

    def _params(self, rows: list[dict]) -> None:
        record = self.rules.records["param"]
        self._pad(8)
        for row, node in zip(rows, self.tree.nodes):
            if node.params is not None:
                row["params"] = len(self.out)
                self.out.extend(bytes(len(node.params) * record.size))
        for row, node in zip(rows, self.tree.nodes):
            for i, param in enumerate(node.params or ()):
                fields = {"type": param.type, "key": param.key}
                if "name" in record.names:
                    if param.name is None:
                        raise MotionFormatError(f"MotTree {self.tree.version} parameters need names")
                    fields["name"] = self._put(param.name.encode("utf-8") + b"\0")
                kind = _param_kind(param.type)
                if kind == "string16":
                    fields["value"] = self._put(_utf16z(param.value), 2)
                elif kind == "blob":
                    fields["key"] = len(param.value)
                    fields["value"] = self._put(param.value, 16)
                elif kind == "f32":
                    fields["value"] = _U32.unpack(_F32.pack(param.value))[0]
                else:
                    fields["value"] = param.value & 0xFFFFFFFF
                self._set(row["params"] + i * record.size, record.encode(fields))
