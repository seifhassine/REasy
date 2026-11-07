"""See docs/internals/jntexprgraph-format.md."""

from __future__ import annotations

import struct

from .errors import ExprGraphFormatError
from .model import ExprGraphResource, Node
from .rules import layouts, value_struct


def write_jntexprgraph(resource: ExprGraphResource) -> bytes:
    found = layouts()
    records, align = found.records, found.align
    magic = found.versions.get(resource.version)
    if magic is None:
        raise ExprGraphFormatError(f".jntexprgraph version {resource.version} is not supported")
    out = bytearray(40)

    def place(alignment: int) -> int:
        out.extend(bytes(-len(out) % alignment))
        return len(out)

    graphs_at = place(align["graphs"])
    out.extend(bytes(records["graph"].size * len(resource.graphs)))
    tables, nodes = [], []
    for graph in resource.graphs:
        members = graph.native + graph.managed
        ids_at = place(align["output_ids"])
        for _name, ids in graph.outputs:
            out.extend(struct.pack(f"<{len(ids)}H", *ids))
        infos_at, offset = place(align["output_infos"]), 0
        for name, ids in graph.outputs:
            out.extend(records["output_info"].encode({"name_hash": name, "id_offset": offset, "id_count": len(ids)}))
            offset += len(ids)
        tables.append((ids_at, infos_at, place(align["nodes"])))
        out.extend(bytes(records["node"].size * len(members)))
        nodes.extend(members)
    fields = [{"params_at": 0, "values_at": 0, "extra_at": 0, "extra_size": 0} for _ in nodes]
    for node, entry in zip(nodes, fields):
        entry["params_at"] = len(out) if node.params else 0
        for param in node.params:
            out.extend(records["param_info"].encode({"pin": param.pin, "recv_type": param.recv_type,
                                                     "set_type": param.set_type}))
    place(align["values"])
    for node, entry in zip(nodes, fields):
        entry["values_at"] = len(out) if node.params else 0
        for param in node.params:
            value = param.value if isinstance(param.value, tuple) else (param.value,)
            out.extend(value_struct(param.set_type).pack(*value))
    for node, entry in zip(nodes, fields):
        entry["value_links_at"] = place(align["links"])
        out.extend(b"".join(map(records["value_link"].encode, node.value_links)))
        entry["exe_links_at"] = len(out)
        out.extend(b"".join(map(records["exe_link"].encode, node.exe_links)))
    for node, entry in zip(nodes, fields):
        if node.extra is not None:
            entry["extra_at"] = place(align["extra"])
            blob = _extra(node)
            entry["extra_size"] = len(blob)
            out.extend(blob)
    dev_at = 0
    if resource.develop:
        dev_at = place(align["dev"])
        out.extend(bytes(records["graph_dev"].size * len(resource.graphs)))
        dev_tables = []
        for graph in resource.graphs:
            dev_tables.append(len(out))
            out.extend(bytes(records["node_dev"].size * (len(graph.native) + len(graph.managed))))
        pins = []
        for node in nodes:
            pins.append(place(align["send_pins"]))
            out.extend(b"".join(map(records["send_pin"].encode, node.send_pins)))
        number = 0
        for index, (graph, table) in enumerate(zip(resource.graphs, dev_tables)):
            _put(out, dev_at + index * records["graph_dev"].size, records["graph_dev"].encode(
                {"graph_id": graph.graph_id, "nodes_at": table, "data_hash": graph.data_hash}))
            for slot, node in enumerate(graph.native + graph.managed):
                if node.node_id is None:
                    raise ExprGraphFormatError(f"node {node.index}: develop data without a node id")
                _put(out, table + slot * records["node_dev"].size, records["node_dev"].encode(
                    {"node_id": node.node_id, "send_pins_at": pins[number], "send_pin_count": len(node.send_pins)}))
                number += 1
    number = 0
    for graph_index, (graph, (ids_at, infos_at, nodes_at)) in enumerate(zip(resource.graphs, tables)):
        members = graph.native + graph.managed
        _put(out, graphs_at + graph_index * records["graph"].size, records["graph"].encode(
            {"graph_id": graph.graph_id, "output_ids_at": ids_at, "output_infos_at": infos_at, "nodes_at": nodes_at,
             "name_hash": graph.name_hash, "output_count": len(graph.outputs), "native_count": len(graph.native),
             "managed_count": len(graph.managed), "attr_flags": graph.attr_flags, "data_hash": graph.data_hash}))
        for slot, node in enumerate(members):
            _put(out, nodes_at + slot * records["node"].size, records["node"].encode(
                {**fields[number], "ranges_at": 0, "range_values_at": 0, "index": node.index, "range_count": 0,
                 "value_link_count": len(node.value_links), "param_count": len(node.params),
                 "exe_link_count": len(node.exe_links), "flags": node.flags, "class_hash": node.class_hash,
                 "user_hash": node.user_hash}))
            number += 1
    _put(out, 0, records["header"].encode({"version": resource.version, "magic": struct.unpack("<I", magic)[0]}))
    _put(out, 16, records["resource"].encode({"graphs_at": graphs_at, "dev_at": dev_at, "name_id": resource.name_id,
                                              "kind_id": resource.kind_id, "graph_count": len(resource.graphs)}))
    return bytes(out)


def _put(out: bytearray, at: int, data: bytes) -> None:
    out[at:at + len(data)] = data


def _extra(node: Node) -> bytes:
    spec = layouts().extras.get(node.class_hash)
    if spec is None:
        raise ExprGraphFormatError(f"node {node.index}: extra data of class 0x{node.class_hash:08X}, whose layout is "
                                   "unknown")
    if "code" in spec:
        blob = bytearray()
        for word, *operands in node.extra:
            op = spec["opcodes"].get(word, word)
            blob.append(op)
            blob.extend(spec["code"][op][1].pack(*operands) if op in spec["code"] else b"")
        return bytes(blob)
    records = layouts().records
    values = dict(node.extra)
    if "items" not in spec:
        return records[spec["record"]].encode(values)
    items = values.pop("items")
    values[spec["count"]] = len(items)
    return records[spec["record"]].encode(values) + b"".join(map(records[spec["items"]].encode, items))
