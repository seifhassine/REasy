"""A read-only projection of native BHVT identities and relationships."""
from dataclasses import dataclass


@dataclass(frozen=True)
class GraphEdge:
    source: int
    target: int | None
    kind: str
    position: int
    condition: int
    error: str = ''

    @property
    def key(self):
        return self.source, self.kind, self.position


class MotfsmGraph:
    def __init__(self, document):
        self.document = document
        self.nodes = document.bhvt.nodes
        self.identities = {}
        for index, node in enumerate(self.nodes):
            identity = node.id_hash, node.ex_id
            if identity in self.identities:
                raise ValueError(f'Duplicate BHVT node identity: {identity}')
            self.identities[identity] = index
        self.parents = {}
        self.issues = {i: [] for i in range(len(self.nodes))}
        self.outgoing = {i: [] for i in range(len(self.nodes))}
        self.incoming = {i: [] for i in range(len(self.nodes))}
        self.edges = []
        self._paths = {}
        for index, node in enumerate(self.nodes):
            # Parent 0 is the real root identity; only UINT32_MAX is absent.
            try:
                self.parents[index] = document.references.parent_index(node)
            except (ValueError, IndexError) as exc:
                self.parents[index] = None
                self.issues[index].append(str(exc))
            for position, child in enumerate(node.children):
                self._edge(index, 'child', position, child.condition_id,
                           lambda child=child: document.references.node_index(child.id_hash, child.ex_id))
            for position, state in enumerate(node.states):
                self._edge(index, 'state', position, state.TransitionConditions,
                           lambda state=state: document.references.state_target(state.mTransitions))
            for position, transition in enumerate(node.transitions):
                self._edge(index, 'start', position, transition.mStartStateTransition,
                           lambda transition=transition: document.references.node_index(transition.mStartState, transition.mStartStateEx))
            for position, state in enumerate(node.all_states):
                self._edge(index, 'all', position, state.mAllTransition,
                           lambda state=state: document.references.node_index(state.mAllState, state.mAllStateEx))

    def _edge(self, source, kind, position, condition, resolver):
        error = ''
        try:
            target = resolver()
        except (ValueError, IndexError) as exc:
            target, error = None, str(exc)
            self.issues[source].append(error)
        if target is None and not error:
            return
        edge = GraphEdge(source, target, kind, position, condition, error)
        self.edges.append(edge)
        self.outgoing[source].append(edge)
        if target is not None:
            self.incoming[target].append(edge)

    def identity(self, index):
        node = self.nodes[index]
        return f'{node.id_hash:08X}:{node.ex_id}'

    def path(self, index):
        if index not in self._paths:
            seen, names, current = set(), [], index
            while current is not None:
                if current in seen:
                    names.append('[cyclic parent]')
                    break
                seen.add(current)
                names.append(self.nodes[current].name)
                current = self.parents[current]
            self._paths[index] = '.'.join(reversed(names))
        return self._paths[index]

    def root(self):
        return next((i for i, parent in self.parents.items() if parent is None), None)

    def children(self, index):
        return tuple(dict.fromkeys(edge.target for edge in self.outgoing[index]
                                   if edge.kind == 'child' and edge.target is not None))

    def subgraph(self, focus, mode):
        visible = {focus}
        if mode == 'children':
            visible.update(self.children(focus))
            edges = [edge for edge in self.edges if edge.source in visible and
                     (edge.target in visible or (edge.target is None and edge.source == focus)) and
                     (edge.kind != 'child' or edge.source == focus)]
        else:
            edges = list(dict.fromkeys(edge for edge in [*self.outgoing[focus], *self.incoming[focus]]
                                      if edge.kind != 'child'))
            for edge in edges:
                visible.add(edge.source)
                if edge.target is not None:
                    visible.add(edge.target)
        return sorted(visible), edges

    def columns(self, focus, visible, edges, mode):
        if mode == 'neighbors':
            result = {focus: 1}
            for edge in edges:
                if edge.target == focus and edge.source != focus:
                    result[edge.source] = 0
            for edge in edges:
                if edge.source == focus and edge.target is not None and edge.target != focus:
                    result[edge.target] = 2
            return result
        result, pending = {focus: 0}, [focus]
        links = {}
        for edge in edges:
            if edge.kind != 'child' and edge.target is not None:
                links.setdefault(edge.source, []).append(edge.target)
        for source in pending:
            for target in links.get(source, ()):
                if target not in result:
                    result[target] = result[source]+1
                    pending.append(target)
        for index in visible:
            result.setdefault(index, 1)
        return result
