from collections import deque

from .shader_graph import ShaderGraphError, node_pins


def arrange_shader_graph(graph, contract=None, *, width_provider=lambda _node: 300.0):
    nodes = {node.node_id: node for node in graph.nodes}
    children = {identity: set() for identity in nodes}
    parents = {identity: set() for identity in nodes}
    for link in graph.links:
        if link.source_node not in nodes or link.target_node not in nodes:
            raise ShaderGraphError("Cannot arrange a graph with dangling connections.")
        children[link.source_node].add(link.target_node)
        parents[link.target_node].add(link.source_node)
    remaining = {identity: len(items) for identity, items in parents.items()}
    order = {identity: index for index, identity in enumerate(nodes)}
    ready = deque(identity for identity, count in remaining.items() if not count)
    ranks = dict.fromkeys(ready, 0)
    while ready:
        identity = ready.popleft()
        for child in sorted(children[identity], key=order.__getitem__):
            ranks[child] = max(ranks.get(child, 0), ranks[identity] + 1)
            remaining[child] -= 1
            if not remaining[child]:
                ready.append(child)
    if any(remaining.values()):
        raise ShaderGraphError("Cannot arrange a cyclic value graph.")
    columns = {}
    for identity, node in nodes.items():
        pins = node_pins(node, contract)
        rows = max(1, sum(pin.direction == "input" for pin in pins), sum(pin.direction == "output" for pin in pins))
        columns.setdefault(ranks[identity], []).append((node, 90.0 + 25.0 * rows))
    x = 0.0
    for rank in sorted(columns):
        column = columns[rank]
        y = -sum(height for _node, height in column) / 2.0
        for node, height in column:
            node.x, node.y = x, y
            y += height
        x += max(width_provider(node) for node, _height in column) + 70.0
