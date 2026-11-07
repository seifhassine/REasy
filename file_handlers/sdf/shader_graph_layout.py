import fastgraph

from .shader_graph import ShaderGraphError, node_pins


def arrange_shader_graph(graph, contract=None, *, width_provider=300.0, pins=None):
    fastgraph.arrange_graph(graph.nodes, graph.links,
                            (lambda node: node_pins(node, contract)) if pins is None else pins, width_provider,
                            ShaderGraphError)
