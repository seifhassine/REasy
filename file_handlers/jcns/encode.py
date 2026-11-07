"""See docs/internals/jcns-format.md, The writer's placement."""

from __future__ import annotations

import struct

from .errors import JcnsFormatError
from .model import JointConstraints, Record, Text
from .rules import FormatRules, Link, header_codec, rules_for


def _align(value: int, alignment: int) -> int:
    return -(-value // alignment) * alignment


def _string(text: Text) -> bytes:
    return text.value.encode("utf-16-le", "surrogatepass") + b"\0\0"


class _Writer:
    def __init__(self, rules: FormatRules, pooled: bool):
        self.rules, self.pooled = rules, pooled
        self.out = bytearray()
        self.after_table = False
        self.offsets: dict[int, int] = {}
        self.strings: dict[int, int] = {}
        self.pool: list[Text] = []
        self.patches: list[tuple[int, Text | int]] = []

    def place(self, blob: bytes, alignment: int, table: bool = True) -> int:
        if not table and self.after_table:
            alignment = max(alignment, 8)
        self.after_table = table
        self.out.extend(bytes(_align(len(self.out), alignment) - len(self.out)))
        self.out.extend(blob)
        return len(self.out) - len(blob)

    def encode(self, record: Record) -> bytes:
        layout = self.rules.records[record.kind]
        values = dict(record.values)
        for link in layout.links:
            value = record.links.get(link.name)
            values[link.name] = 0
            if not link.record or not isinstance(value, list) and link.fixed:
                continue
            length = len(value) if isinstance(value, list) else 0
            if link.fixed:
                if length != link.fixed:
                    raise JcnsFormatError(f"{record.kind}.{link.name} holds {length} entries, not {link.fixed}")
                continue
            count = max(length - link.add, 0)
            if values.setdefault(link.count, count) != count:
                raise JcnsFormatError(f"{record.kind}.{link.count} counts tables of different lengths")
        return layout.codec.encode(values)

    def place_link(self, record: Record, link: Link) -> list[Record]:
        value = record.links.get(link.name)
        slot = self.offsets[id(record)] + link.at
        if value is None:
            return []
        if isinstance(value, Text):
            if id(value) not in self.strings:
                if self.pooled and (record.kind, link.name) in self.rules.pooled:
                    self.strings[id(value)] = -1
                    self.pool.append(value)
                else:
                    self.strings[id(value)] = self.place(_string(value), link.align or 2, table=False)
            self.patches.append((slot, value))
            return []
        at = self.place(b"".join(self.encode(element) for element in value), link.align or 16)
        size = self.rules.records[link.record].codec.size
        for index, element in enumerate(value):
            self.offsets[id(element)] = at + index * size
        self.patches.append((slot, at))
        return value

    def place_level(self, level: list[Record]) -> list[Record]:
        following = []
        for kind in dict.fromkeys(record.kind for record in level):
            for link in self.rules.records[kind].order:
                children = [child for record in level if record.kind == kind
                            for child in self.place_link(record, link)]
                if self.rules.depth_first and children:
                    self.place_level(children)
                else:
                    following.extend(children)
        return following

    def block(self, record: Record) -> None:
        layout = self.rules.records[record.kind]
        if not layout.links:
            return
        self.out.extend(bytes(_align(len(self.out), layout.block_align) - len(self.out)))
        level = [record]
        while level:
            level = self.place_level(level)

    def write(self, model: JointConstraints) -> bytes:
        header = header_codec()
        self.out.extend(bytes(header.size))
        type_table = self.place(bytes((model.expression_type,)), 1)
        data_table = self.place(bytes(8), 16)
        root = model.root
        self.offsets[id(root)] = self.place(self.encode(root), 16)
        layout = self.rules.records[root.kind]
        by_name = {link.name: link for link in layout.links}
        for link in [by_name[name] for name in self.rules.root_order] or layout.links:
            for element in self.place_link(root, link):
                self.block(element)
            for text in self.pool:
                self.strings[id(text)] = self.place(_string(text), 8, table=False)
            self.pool.clear()
        for slot, target in self.patches:
            struct.pack_into("<Q", self.out, slot, self.strings[id(target)] if isinstance(target, Text) else target)
        struct.pack_into("<Q", self.out, data_table, self.offsets[id(root)])
        self.out[:header.size] = header.encode({
            "version": model.version, "err_flags": model.err_flags, "master_size": model.master_size,
            "type_table": type_table, "lod_table": 0, "data_table": data_table, "count": 1})
        return bytes(self.out)


def write_jcns(model: JointConstraints) -> bytes:
    rules = rules_for(model.version)
    if rules.expressions.get(model.expression_type) != model.root.kind:
        raise JcnsFormatError(f"expression type {model.expression_type} does not hold a {model.root.kind} record")
    return _Writer(rules, model.pooled).write(model)
