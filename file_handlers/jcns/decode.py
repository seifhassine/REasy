"""See docs/internals/jcns-format.md."""

from __future__ import annotations

lazy from .encode import write_jcns
from .errors import JcnsFormatError
from .model import JointConstraints, Record, Text
from .rules import header_codec, rules_for


def read_jcns(data: bytes) -> JointConstraints:
    if len(data) < header_codec().size:
        raise JcnsFormatError("too short for a .jcns header")
    header = header_codec().decode(data, 0)
    rules = rules_for(header["version"])
    if header["count"] != 1 or header["lod_table"]:
        raise JcnsFormatError(f"{header['count']} expressions{' with a LOD table' if header['lod_table'] else ''}; "
                              "every known file holds one")
    expression_type = data[header["type_table"]]
    if expression_type not in rules.expressions:
        raise JcnsFormatError(f"expression type {expression_type} has no layout")
    texts: dict[int, Text] = {}

    def text(at: int) -> Text:
        if at not in texts:
            end = data.find(b"\0\0", at)
            while end >= 0 and (end - at) % 2:
                end = data.find(b"\0\0", end + 1)
            if end < 0:
                raise JcnsFormatError(f"string at 0x{at:X} has no terminator")
            texts[at] = Text(data[at:end].decode("utf-16-le", "surrogatepass"))
        return texts[at]

    def read(kind: str, at: int) -> Record:
        layout = rules.records[kind]
        values = layout.codec.decode(data, at)
        links = {}
        for link in layout.links:
            pointer = values.pop(link.name)
            if not pointer:
                links[link.name] = None
            elif not link.record:
                links[link.name] = text(pointer)
            else:
                count = link.fixed or values[link.count] + link.add
                size = rules.records[link.record].codec.size
                links[link.name] = [read(link.record, pointer + index * size) for index in range(count)]
        for name in layout.counts:
            del values[name]
        return Record(kind, values, links)

    root_at = int.from_bytes(data[header["data_table"]:header["data_table"] + 8], "little")
    model = JointConstraints(header["version"], expression_type, read(rules.expressions[expression_type], root_at),
                             header["err_flags"], header["master_size"])
    if rules.pooled:
        model.pooled = True
        model.pooled = write_jcns(model) == data
    return model
