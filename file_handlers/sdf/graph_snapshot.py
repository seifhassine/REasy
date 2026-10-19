from dataclasses import dataclass, replace
import math


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
_JSON_CONTAINERS = (dict, list)
_ORDINARY_CONTAINERS = (dict, list, _Object, _Array)
_SEMANTIC_TEXT = frozenset(('semantic_ir', 'semantic_contract'))
_ROUTE_TABLES = frozenset(('program_routes', 'semantic_programs'))
_GRAPH_KEYS = frozenset(('stage', 'contract_fingerprint', 'nodes', 'links'))


_MISSING = object()
_INVALID = object()
_MEMO_OWNERS = object()


def _invalid_properties():
    from .shader_graph import ShaderGraphError
    raise ShaderGraphError('Compiler properties must be finite JSON within their declared size limits.')


def _copy(value, memo):
    pending = []

    def clone(item):
        if not isinstance(item, _JSON_CONTAINERS):
            return item
        identity = id(item)
        result = memo.get(identity, _MISSING)
        if result is _MISSING:
            ordinary = type(item) in _ORDINARY_CONTAINERS
            if isinstance(item, dict):
                result = dict(item) if ordinary else {}
            else:
                result = list(item) if ordinary else []
            memo[identity] = result
            pending.append((item, result, ordinary))
        return result

    result = clone(value)
    while pending:
        original, copied, ordinary = pending.pop()
        if isinstance(original, dict):
            for key, item in original.items():
                if not ordinary or isinstance(item, _JSON_CONTAINERS):
                    copied[key] = clone(item)
        else:
            for index, item in enumerate(original):
                if not ordinary:
                    copied.append(clone(item))
                elif isinstance(item, _JSON_CONTAINERS):
                    copied[index] = clone(item)
    return result


def _scalar_state(value):
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, str):
        return len(value) <= 4096
    if isinstance(value, float):
        return math.isfinite(value)
    return None if isinstance(value, _JSON_CONTAINERS) else False


def _checked_properties(value, memo):
    from .shader_graph import MAX_GRAPH_NODES, MAX_GRAPH_LINKS, _property_validation

    if not isinstance(value, _JSON_CONTAINERS):
        return value if _scalar_state(value) else _INVALID
    validated = _property_validation.get()
    completed = set() if validated is None else validated
    active, added = set(), []
    pending = [(value, 2048, None, None, False)]
    result, success = value, False
    try:
        while pending:
            item, limit, parent, key, leaving = pending.pop()
            identity = id(item)
            if leaving:
                active.remove(identity)
                if validated is None:
                    completed.add((identity, limit))
                else:
                    validated[identity, limit] = item
                continue
            mapping = isinstance(item, dict)
            if len(item) > (64 if mapping else limit) or identity in active:
                return _INVALID
            if isinstance(item, IMMUTABLE_JSON):
                owned = item
            elif memo is not None and identity in memo:
                # A shared container's members were checked where it was first copied; only its length is per use.
                owned = memo[identity]
            elif memo is None and (identity, limit) in completed:
                continue
            else:
                kind, owned = type(item), None
                entries = item.items() if mapping else item
                if memo is not None:
                    if kind is dict or kind is list:
                        owned = _Object(item) if mapping else _Array(item)
                    else:
                        entries = list(entries)
                        owned = _Object(entries) if mapping else _Array(entries)
                    memo[identity] = owned
                    added.append(identity)
                active.add(identity)
                pending.append((item, limit, None, None, True))
                embedded = mapping and ((len(item) == 4 and 'stage' in item and 'contract_fingerprint' in item
                                         and 'nodes' in item and 'links' in item) if kind is dict
                                        else set(item) == _GRAPH_KEYS)
                for child_key, child in entries if mapping else enumerate(entries):
                    if mapping:
                        if not isinstance(child_key, str) or len(child_key) > 256:
                            return _INVALID
                        if (child_key in _SEMANTIC_TEXT and isinstance(child, str)
                                and len(child.encode('utf-8')) <= 2 * 1024 * 1024):
                            continue
                    state = _scalar_state(child)
                    if state is None:
                        child_limit = (2048 if not mapping else
                                       65535 if child_key in _ROUTE_TABLES else
                                       MAX_GRAPH_NODES if embedded and child_key == 'nodes' else
                                       MAX_GRAPH_LINKS if embedded and child_key == 'links' else 2048)
                        pending.append((child, child_limit, owned, child_key, False))
                    elif not state:
                        return _INVALID
            if memo is not None:
                if parent is None:
                    result = owned
                elif type(parent) is _Object:
                    dict.__setitem__(parent, key, owned)
                else:
                    list.__setitem__(parent, key, owned)
        success = True
        return result
    finally:
        if not success:
            for identity in added:
                del memo[identity]


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
        ShaderGraph, ShaderGraphNode, ShaderGraphLink,
        _check_graph_tables, _check_graph_header, _checked_graph_node,
        _check_graph_link_fields, immutable_property_validation,
    )
    if (type(graph) is not ShaderGraph or type(graph.nodes) is not list
            or type(graph.links) is not list or type(graph.stage) is not str
            or type(graph.contract_fingerprint) is not str):
        return None
    _check_graph_tables(graph.nodes, graph.links)
    _check_graph_header(graph.stage, graph.contract_fingerprint)
    owned = ShaderGraph(graph.stage, graph.contract_fingerprint)
    nodes, links, identities, copied = [], [], set(), {}
    with immutable_property_validation():
        for source in graph.nodes:
            if (type(source) is not ShaderGraphNode or type(source.node_id) is not str
                    or not source.node_id or type(source.kind) is not str
                    or source.kind != source.kind.strip().lower()
                    or type(source.x) is not float or type(source.y) is not float
                    or type(source.properties) not in (dict, _Object)):
                return None
            if len(source.kind) > 4096:
                _invalid_properties()
            properties = freeze_properties(source.properties, memo=copied)
            node = _checked_graph_node(source.kind, source.x, source.y, properties,
                                       source.node_id, identities)
            owned.nodes.append(node)
            nodes.append(_Object({
                'id': node.node_id,
                'kind': node.kind,
                'position': _Array((node.x, node.y)),
                'properties': properties,
            }))
        for source in graph.links:
            if type(source) is not ShaderGraphLink:
                return None
            fields = source.source_node, source.source_pin, source.target_node, source.target_pin
            if any(type(part) is not str for part in fields):
                return None
            _check_graph_link_fields(fields)
            owned.links.append(source)
            links.append(_Object({
                'source': _Array((source.source_node, source.source_pin)),
                'target': _Array((source.target_node, source.target_pin)),
            }))
    document = _Object({
        'stage': owned.stage,
        'contract_fingerprint': owned.contract_fingerprint,
        'nodes': _Array(nodes),
        'links': _Array(links),
    })
    return _CheckedGraphSnapshot(document, owned)


def compiler_graph_copy(graph):
    from .shader_graph import immutable_property_validation

    memo = {}
    with immutable_property_validation():
        return replace(graph, nodes=[replace(node, properties=dict(freeze_properties(node.properties, memo=memo)))
                                     for node in graph.nodes], links=list(graph.links))


def thaw_graph(graph):
    memo = {}
    for node in graph.nodes:
        node.properties = _copy(node.properties, memo)
    return graph
