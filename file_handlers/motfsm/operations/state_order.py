"""Reorder existing states while preserving identities and relative semantics."""
from dataclasses import dataclass

from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.serialization import serialize_nodes
from file_handlers.motfsm.selection import resolve_node, integer


@dataclass(frozen=True, kw_only=True)
class StateOrderPlan:
    node: str
    order: str


def build(source: bytes, plan: StateOrderPlan) -> bytes:
    document = MotfsmFile()
    document.read(source)
    node = document.bhvt.nodes[resolve_node(document, plan.node)]
    order = [integer(part) for part in plan.order.split(',')]
    if sorted(order) != list(range(len(node.states))):
        raise ValueError('--order must contain every state position exactly once')
    start, end = document.bhvt.offsets['nodes'], document.bhvt.node_data_end
    if serialize_nodes(document) != document.source[start:end]:
        raise ValueError('Node writer does not reproduce the source')
    node.states = [node.states[index] for index in order]
    data = serialize_nodes(document)
    if len(data) != end-start:
        raise ValueError('Reordering changed the node table size')
    output = document.source[:start] + data + document.source[end:]
    reopened = MotfsmFile()
    reopened.read(output)
    if reopened.bhvt.nodes != document.bhvt.nodes or reopened.rebuild() != output:
        raise ValueError('Reordered state table did not roundtrip')
    return output
