"""See docs/internals/jntexprgraph-format.md."""

from __future__ import annotations

import struct

from .errors import ExprGraphFormatError
from .model import ExprGraphResource, Graph, Node, Param
from .rules import layouts, value_struct


def read_jntexprgraph(data: bytes) -> ExprGraphResource:
    records = layouts().records
    header = records["header"].decode(data, 0)
    if layouts().versions.get(header["version"]) != struct.pack("<I", header["magic"]):
        raise ExprGraphFormatError(f"not a supported .jntexprgraph (version {header['version']})")
    resource = records["resource"].decode(data, 16)
    if any(data[40:resource["graphs_at"]]):
        raise ExprGraphFormatError("nonzero bytes after the resource header")
    graphs = records["graph"].decode_table(data, resource["graphs_at"], resource["graph_count"])
    develop = records["graph_dev"].decode_table(data, resource["dev_at"], len(graphs)) if resource["dev_at"] else None
    result = ExprGraphResource(header["version"], resource["name_id"], resource["kind_id"], [], develop is not None)
    for number, graph in enumerate(graphs):
        infos = records["output_info"].decode_table(data, graph["output_infos_at"], graph["output_count"])
        ids = struct.unpack_from(f"<{max((i['id_offset'] + i['id_count'] for i in infos), default=0)}H", data,
                                 graph["output_ids_at"])
        count = graph["native_count"] + graph["managed_count"]
        nodes = records["node"].decode_table(data, graph["nodes_at"], count)
        dev = None
        if develop is not None:
            if (develop[number]["graph_id"], develop[number]["data_hash"]) != (graph["graph_id"], graph["data_hash"]):
                raise ExprGraphFormatError(f"graph {number}: develop data names another graph")
            dev = records["node_dev"].decode_table(data, develop[number]["nodes_at"], count)
        decoded = [_node(data, node, dev[index] if dev else None) for index, node in enumerate(nodes)]
        result.graphs.append(Graph(graph["graph_id"], graph["name_hash"], graph["attr_flags"], graph["data_hash"],
                                   [(i["name_hash"], list(ids[i["id_offset"]:i["id_offset"] + i["id_count"]]))
                                    for i in infos], decoded[:graph["native_count"]], decoded[graph["native_count"]:]))
    return result


def _node(data: bytes, node: dict, dev: dict | None) -> Node:
    records = layouts().records
    if node["range_count"] or node["ranges_at"] or node["range_values_at"]:
        raise ExprGraphFormatError(f"node {node['index']}: range tables, which no sample file has")
    params, at = [], node["values_at"]
    for info in records["param_info"].decode_table(data, node["params_at"], node["param_count"]):
        layout = value_struct(info["set_type"])
        value = layout.unpack_from(data, at)
        at += layout.size
        params.append(Param(info["pin"], info["recv_type"], info["set_type"], value[0] if len(value) == 1 else value))
    return Node(node["class_hash"], node["index"], node["flags"], node["user_hash"], params,
                records["value_link"].decode_table(data, node["value_links_at"], node["value_link_count"]),
                records["exe_link"].decode_table(data, node["exe_links_at"], node["exe_link_count"]),
                _extra(data, node), None if dev is None else dev["node_id"],
                [] if dev is None else records["send_pin"].decode_table(data, dev["send_pins_at"],
                                                                        dev["send_pin_count"]))


def _extra(data: bytes, node: dict):
    size, at = node["extra_size"], node["extra_at"]
    if not size:
        if at:
            raise ExprGraphFormatError(f"node {node['index']}: extra data offset without data")
        return None
    spec = layouts().extras.get(node["class_hash"])
    if spec is None:
        raise ExprGraphFormatError(f"node {node['index']}: extra data of class 0x{node['class_hash']:08X}, whose "
                                   "layout is unknown")
    blob = data[at:at + size]
    if "code" in spec:
        program, offset = [], 0
        while offset < size:
            word, operands = spec["code"].get(blob[offset], (blob[offset], struct.Struct("<")))
            program.append((word, *operands.unpack_from(blob, offset + 1)))
            offset += 1 + operands.size
        if offset != size:
            raise ExprGraphFormatError(f"node {node['index']}: code runs past its data")
        return program
    head = layouts().records[spec["record"]]
    extra = head.decode(blob, 0)
    end = head.size
    if "items" in spec:
        item = layouts().records[spec["items"]]
        extra["items"] = item.decode_table(blob, end, extra.pop(spec["count"]))
        end += item.size * len(extra["items"])
    if end != size:
        raise ExprGraphFormatError(f"node {node['index']}: extra data of {size} bytes, its layout reads {end}")
    return extra
