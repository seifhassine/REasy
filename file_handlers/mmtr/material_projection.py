from __future__ import annotations

from .surface_data import SURFACE_DATA_KIND

PLANAR_AXES = {"x": "yz", "y": "xz", "z": "xy"}


def _datum(graph, offer, x, y):
    return graph.add_node(SURFACE_DATA_KIND, x=x, y=y, properties={
        "datum": offer.key, "label": offer.label, "value_type": offer.value_type})


def insert_tiling_offset(graph, x: float, y: float):
    tiling = graph.add_node("constant", x=x, y=y, properties={"value_type": "float2", "values": [1.0, 1.0]})
    offset = graph.add_node("constant", x=x, y=y + 120, properties={"value_type": "float2", "values": [0.0, 0.0]})
    result = graph.add_node("multiply_add", x=x + 260, y=y, properties={"value_type": "float2"})
    graph.connect(tiling.node_id, "value", result.node_id, "b")
    graph.connect(offset.node_id, "value", result.node_id, "c")
    return result


def _scaled_position(graph, position, x, y):
    datum = _datum(graph, position, x, y)
    scale = graph.add_node("constant", x=x, y=y + 120, properties={"value_type": "float3", "values": [1.0, 1.0, 1.0]})
    scaled = graph.add_node("multiply", x=x + 260, y=y, properties={"value_type": "float3"})
    graph.connect(datum.node_id, "value", scaled.node_id, "a")
    graph.connect(scale.node_id, "value", scaled.node_id, "b")
    return scaled


def insert_planar(graph, position, axis: str, x: float, y: float):
    scaled = _scaled_position(graph, position, x, y)
    result = graph.add_node("swizzle", x=x + 520, y=y, properties={"source_type": "float3", "mask": PLANAR_AXES[axis]})
    graph.connect(scaled.node_id, "value", result.node_id, "value")
    return result


def insert_triplanar(graph, position, normal, x: float, y: float):
    scaled = _scaled_position(graph, position, x, y)
    direction = _datum(graph, normal, x, y + 520)
    absolute = graph.add_node("absolute", x=x + 260, y=y + 520, properties={"value_type": "float3"})
    sharpness = graph.add_node("constant", x=x + 260, y=y + 640, properties={"value_type": "float3",
                                                                            "values": [4.0, 4.0, 4.0]})
    weights = graph.add_node("power", x=x + 520, y=y + 520, properties={"value_type": "float3"})
    ones = graph.add_node("constant", x=x + 520, y=y + 640, properties={"value_type": "float3",
                                                                       "values": [1.0, 1.0, 1.0]})
    total = graph.add_node("dot", x=x + 780, y=y + 640, properties={"value_type": "float3"})
    spread = graph.add_node("swizzle", x=x + 1040, y=y + 640, properties={"source_type": "float", "mask": "xxx"})
    blend = graph.add_node("divide", x=x + 1300, y=y + 520, properties={"value_type": "float3"})
    graph.connect(direction.node_id, "value", absolute.node_id, "value")
    graph.connect(absolute.node_id, "result", weights.node_id, "a")
    graph.connect(sharpness.node_id, "value", weights.node_id, "b")
    graph.connect(weights.node_id, "value", total.node_id, "a")
    graph.connect(ones.node_id, "value", total.node_id, "b")
    graph.connect(total.node_id, "value", spread.node_id, "value")
    graph.connect(weights.node_id, "value", blend.node_id, "a")
    graph.connect(spread.node_id, "result", blend.node_id, "b")
    terms = []
    for row, (axis, mask) in enumerate(PLANAR_AXES.items()):
        top = y + row * 160
        coordinates = graph.add_node("swizzle", x=x + 520, y=top, properties={"source_type": "float3", "mask": mask})
        sample = graph.add_node("sample_texture", x=x + 780, y=top, properties={"dimension": "texture2d"})
        weight = graph.add_node("swizzle", x=x + 1560, y=y + 520 + row * 120,
                                properties={"source_type": "float3", "mask": axis * 4})
        term = graph.add_node("multiply", x=x + 1820, y=top, properties={"value_type": "float4"})
        graph.connect(scaled.node_id, "value", coordinates.node_id, "value")
        graph.connect(coordinates.node_id, "result", sample.node_id, "coordinates")
        graph.connect(blend.node_id, "value", weight.node_id, "value")
        graph.connect(sample.node_id, "rgba", term.node_id, "a")
        graph.connect(weight.node_id, "result", term.node_id, "b")
        terms.append(term)
    first = graph.add_node("add", x=x + 2080, y=y, properties={"value_type": "float4"})
    result = graph.add_node("add", x=x + 2340, y=y, properties={"value_type": "float4"})
    graph.connect(terms[0].node_id, "value", first.node_id, "a")
    graph.connect(terms[1].node_id, "value", first.node_id, "b")
    graph.connect(first.node_id, "value", result.node_id, "a")
    graph.connect(terms[2].node_id, "value", result.node_id, "b")
    return result


__all__ = ["PLANAR_AXES", "insert_planar", "insert_tiling_offset", "insert_triplanar"]
