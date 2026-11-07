from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace

import fastgraph


def _immutable(*_args, **_kwargs):
    raise TypeError('Compiler snapshots are immutable; copy them for editing.')


class _Object(dict):
    __slots__ = ()
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo):
        memo[id(self)] = self
        return self


class _Array(list):
    __slots__ = ()
    __setitem__ = __delitem__ = append = extend = insert = pop = remove = reverse = sort = clear = __iadd__ = __imul__ = _immutable

    def __deepcopy__(self, memo):
        memo[id(self)] = self
        return self


IMMUTABLE_JSON = (_Object, _Array)
MAX_GRAPH_NODES = 32768
MAX_GRAPH_LINKS = 131072
_SEMANTIC_TEXT = frozenset(('semantic_ir', 'semantic_contract'))
_ROUTE_TABLES = frozenset(('program_routes', 'semantic_programs', 'routes'))
_GRAPH_KEYS = frozenset(('stage', 'contract_fingerprint', 'nodes', 'links'))


_INVALID = sentinel("_INVALID")
_MEMO_OWNERS = sentinel("_MEMO_OWNERS")
_property_validation = ContextVar('shader_property_validation', default=None)
fastgraph.configure(_Object, _Array, _INVALID, _SEMANTIC_TEXT, _ROUTE_TABLES, _GRAPH_KEYS)


@contextmanager
def immutable_property_validation(validated=None):
    token = _property_validation.set({} if validated is None else validated)
    try:
        yield
    finally:
        _property_validation.reset(token)


def _invalid_properties():
    from .shader_graph import ShaderGraphError
    raise ShaderGraphError('Compiler properties must be finite JSON within their declared size limits.')


def _checked_properties(value, memo):
    return fastgraph.checked_properties(value, memo, _property_validation.get(), MAX_GRAPH_NODES, MAX_GRAPH_LINKS)


def freeze_properties(value, *, memo=None):
    copied = {} if memo is None else memo
    result = _checked_properties(value, copied)
    if result is _INVALID:
        _invalid_properties()
    if memo is not None:
        memo.setdefault(_MEMO_OWNERS, []).append(value)
    return result


@dataclass(frozen=True, slots=True)
class _CheckedGraphSnapshot:
    document: dict
    graph: object

    def graph_for(self, document):
        from .shader_graph import ShaderGraphError
        if document is not self.document:
            raise ShaderGraphError('A checked graph belongs to a different serialized document.')
        return self.graph


def checked_graph_snapshot(graph):
    from .shader_graph import (
        ShaderGraph, ShaderGraphNode, ShaderGraphLink, ShaderGraphError, _NODE_KINDS, shader_graph_node_extension,
        _check_graph_tables, _check_graph_header,
    )
    if (type(graph) is not ShaderGraph or type(graph.nodes) is not list
            or type(graph.links) is not list or type(graph.stage) is not str
            or type(graph.contract_fingerprint) is not str):
        return None
    _check_graph_tables(graph.nodes, graph.links)
    _check_graph_header(graph.stage, graph.contract_fingerprint)
    with immutable_property_validation():
        parts = fastgraph.snapshot_graph(graph.nodes, graph.links, ShaderGraphNode, ShaderGraphLink, _NODE_KINDS,
                                         shader_graph_node_extension, ShaderGraphError, MAX_GRAPH_NODES,
                                         MAX_GRAPH_LINKS)
    if parts is None:
        return None
    nodes, node_documents, links, link_documents = parts
    owned = ShaderGraph(graph.stage, graph.contract_fingerprint, nodes, links)
    document = _Object({
        'stage': owned.stage,
        'contract_fingerprint': owned.contract_fingerprint,
        'nodes': _Array(node_documents),
        'links': _Array(link_documents),
    })
    return _CheckedGraphSnapshot(document, owned)


def compiler_graph_copy(graph):
    memo = {}
    with immutable_property_validation():
        return replace(graph, nodes=[replace(node, properties=dict(freeze_properties(node.properties, memo=memo)))
                                     for node in graph.nodes], links=list(graph.links))


def thaw_graph(graph):
    memo = {}
    for node in graph.nodes:
        node.properties = fastgraph.copy_properties(node.properties, memo)
    return graph
