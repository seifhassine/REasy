"""Reorder existing states while preserving identities and relative semantics."""
from file_handlers.motfsm.serialization import serialize_nodes
from tools.fsm.common import resolve_node, integer
from tools.fsm.editing import open_document


def configure(parser):
    parser.add_argument('--node', required=True)
    parser.add_argument('--order', required=True, help='complete state permutation, e.g. 2,0,1')


def run(args):
    document = open_document(args.source)
    node = document.bhvt.nodes[resolve_node(document, args.node)]
    order = [integer(part) for part in args.order.split(',')]
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
    from file_handlers.motfsm.motfsm_file import MotfsmFile
    reopened = MotfsmFile()
    reopened.read(output)
    if reopened.bhvt.nodes != document.bhvt.nodes or reopened.rebuild() != output:
        raise ValueError('Reordered state table did not roundtrip')
    return output
