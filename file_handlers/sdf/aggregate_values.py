from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import re

from .shader_graph import (
    ShaderGraphError, ShaderGraphNodeExtension, ShaderPin,
    _hlsl_variable_parts, register_shader_graph_node_extension,
)
from PySide6.QtCore import QT_TRANSLATE_NOOP
from utils.hex_util import align


AGGREGATE_KINDS = frozenset({"aggregate_construct", "aggregate_extract", "aggregate_replace"})
_VALUE = re.compile(r"(float|int|uint|bool)([1-4])?")


@dataclass(frozen=True)
class AggregateField:
    path: str
    value_type: str
    offsets: tuple[int, ...]

    def to_dict(self):
        return dict(path=self.path, value_type=self.value_type, offsets=list(self.offsets))


@lru_cache(maxsize=1024)
def constant_value_fields(variable):
    def visit(value, path, offset, recurse):
        base, suffix = _hlsl_variable_parts(value)
        dimensions = tuple(int(count) for count in re.findall(r"\[(\d+)\]", suffix))
        if suffix != "".join(f"[{count}]" for count in dimensions) or any(count < 1 for count in dimensions):
            raise ShaderGraphError("Renderer array has an invalid declared dimension")

        def element(path, offset):
            if value.members:
                fields, end = [], 0
                for member in value.members:
                    children, size = recurse(member, path + "." + member.name, offset + member.byte_offset, recurse)
                    fields.extend(children)
                    end = max(end, member.byte_offset + size)
                return fields, align(end, 16)
            matrix = re.fullmatch(r"(row_major|column_major) (float|int|uint|bool)([1-4])x([1-4])", base)
            if matrix:
                orientation, scalar, rows, columns = matrix.groups()
                rows, columns = int(rows), int(columns)
                fields = [AggregateField(path + f"[{row}]", scalar + (str(columns) if columns > 1 else ""),
                                         tuple(offset + (row * 16 + col * 4 if orientation == "row_major"
                                                         else col * 16 + row * 4) for col in range(columns)))
                          for row in range(rows)]
                return fields, (rows if orientation == "row_major" else columns) * 16
            scalar = _VALUE.fullmatch(base)
            if scalar is None:
                raise ShaderGraphError(f"Renderer value has no declared 32-bit graph type: {base}")
            width = int(scalar[2] or 1)
            return [AggregateField(path, base, tuple(offset + 4 * index for index in range(width)))], width * 4

        def array(axis, path, offset, descend):
            if axis == len(dimensions):
                return element(path, offset)
            first, size = descend(axis + 1, path + "[0]", offset, descend)
            stride = align(size, 16)
            fields = list(first)
            for index in range(1, dimensions[axis]):
                children, _ = descend(axis + 1, path + f"[{index}]", offset + index * stride, descend)
                fields.extend(children)
            return fields, dimensions[axis] * stride

        return array(0, path, offset, array)

    fields, _ = visit(variable, "", variable.byte_offset, visit)
    end = variable.byte_offset + variable.byte_size
    if any(offset < variable.byte_offset or offset + 4 > end for field in fields for offset in field.offsets):
        raise ShaderGraphError(f"Declared renderer layout exceeds {variable.name}'s reflected extent")
    return tuple(fields)


def aggregate_fields(properties):
    fields = tuple(AggregateField(str(row["path"]), str(row["value_type"]), tuple(row["offsets"]))
                   for row in properties["value_layout"])
    paths, offsets = set(), set()
    for field in fields:
        value_type = _VALUE.fullmatch(field.value_type)
        if (value_type is None or len(field.offsets) != int(value_type[2] or 1) or field.path in paths
                or not re.fullmatch(r"(?:\.[A-Za-z_]\w*|\[\d+\])*", field.path)
                or any(not isinstance(offset, int) or offset < 0 or offset % 4 or offset in offsets
                       for offset in field.offsets)):
            raise ShaderGraphError("Invalid typed aggregate field layout")
        paths.add(field.path)
        offsets.update(field.offsets)
    if not fields:
        raise ShaderGraphError("A graph aggregate needs declared fields")
    return fields


def aggregate_type(fields):
    shape = tuple((field.path, field.value_type) for field in fields)
    return "REasyAggregate_" + hashlib.sha256(json.dumps(shape).encode()).hexdigest()[:24]


def aggregate_helpers(fields):
    name = aggregate_type(fields)
    lines = [f"struct {name}", "{"]
    lines.extend(f"    {field.value_type} field{index};" for index, field in enumerate(fields))
    lines.extend(("};", f"{name} {name}_Make(" + ", ".join(
        f"{field.value_type} fieldValue{index}" for index, field in enumerate(fields)) + ")", "{", f"    {name} value;"))
    lines.extend(f"    value.field{index} = fieldValue{index};" for index in range(len(fields)))
    lines.extend(("    return value;", "}"))
    for index, field in enumerate(fields):
        lines.extend((f"{name} {name}_Replace{index}({name} value, {field.value_type} field)",
                      "{", f"    value.field{index} = field;", "    return value;", "}"))
    return "\n".join(lines)


def aggregate_pins(node, _contract):
    fields = aggregate_fields(node.properties)
    value_type = aggregate_type(fields)
    index = int(node.properties.get("field_index", 0))
    if not 0 <= index < len(fields):
        raise ShaderGraphError("Aggregate field selection is outside its declared layout")
    field = fields[index]
    if node.kind == "aggregate_construct":
        return tuple(ShaderPin(f"field{i}", f.path or "Value", f.value_type, "input") for i, f in enumerate(fields)) + (
            ShaderPin("value", "Aggregate", value_type, "output"),)
    return (ShaderPin("aggregate", "Aggregate", value_type, "input"),) + (
        (ShaderPin("value", field.path or "Value", field.value_type, "output"),) if node.kind == "aggregate_extract" else
        (ShaderPin("field", field.path or "Value", field.value_type, "input"), ShaderPin("value", "Aggregate", value_type, "output")))


def aggregate_expression(node, pin, incoming):
    if pin != "value":
        raise ShaderGraphError("Aggregate nodes have only a value output")
    fields = aggregate_fields(node.properties)
    value_type = aggregate_type(fields)
    index = int(node.properties.get("field_index", 0))
    aggregate_pins(node, None)  # Validate the selected field before indexing it.
    if node.kind == "aggregate_construct":
        values = [incoming(f"field{i}", field.value_type) for i, field in enumerate(fields)]
    else:
        base = incoming("aggregate", value_type)
        if node.kind == "aggregate_extract":
            return f"({base}).field{index}"
        return f"{value_type}_Replace{index}({base}, {incoming('field', fields[index].value_type)})"
    return f"{value_type}_Make({', '.join(values)})"


def aggregate_projection(graph, node, pin, index, links):
    visited = set()
    while node.kind in {"aggregate_construct", "aggregate_replace"}:
        key = node.node_id, pin, index
        if key in visited:
            raise ShaderGraphError("Aggregate graph contains a cycle")
        visited.add(key)
        fields = aggregate_fields(node.properties)
        if pin != "value" or not 0 <= index < len(fields):
            raise ShaderGraphError("Aggregate projection has an invalid field")
        if node.kind == "aggregate_construct":
            socket, result_index = f"field{index}", None
        elif index == int(node.properties.get("field_index", 0)):
            socket, result_index = "field", None
        else:
            socket, result_index = "aggregate", index
        link = links.get((node.node_id, socket))
        if link is None or (source := graph.node(link.source_node)) is None:
            raise ShaderGraphError(f"Aggregate {socket} is unconnected")
        node, pin, index = source, link.source_pin, result_index
        if index is None:
            break
    return node, pin, index


for _kind, _title in (("aggregate_construct", "Construct Aggregate"), ("aggregate_extract", "Extract Aggregate Field"),
                       ("aggregate_replace", "Replace Aggregate Field")):
    register_shader_graph_node_extension(ShaderGraphNodeExtension(
        kind=_kind, title=_title, pins_provider=aggregate_pins, expression=aggregate_expression,
        hlsl_helpers_provider=lambda node, _contract: aggregate_helpers(aggregate_fields(node.properties)),
        description=QT_TRANSLATE_NOOP("ShaderGraphNodes", "Edits logical fields of a typed renderer aggregate without treating padding as data."),
    ))
