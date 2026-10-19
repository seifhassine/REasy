"""The body reads only its inputs, so it builds the same in every pass and backend."""
from __future__ import annotations

import hashlib
import json
import re

from PySide6.QtCore import QT_TRANSLATE_NOOP

from .shader_graph import ShaderGraphError, ShaderGraphNodeExtension, ShaderPin, register_shader_graph_node_extension
lazy from .shader_graph_math import NUMERIC_VALUE_TYPES

CODE_KIND = "code"
MAX_CODE_BYTES = 64 * 1024
MAX_CODE_PINS = 16
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def code_pins(properties, direction: str) -> tuple[tuple[str, str], ...]:
    rows = properties.get(direction, [])
    if not isinstance(rows, list) or len(rows) > MAX_CODE_PINS or (direction == "outputs" and not rows):
        raise ShaderGraphError(f"A Code node needs 1 to {MAX_CODE_PINS} outputs and at most {MAX_CODE_PINS} inputs.")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ShaderGraphError("A Code node pin is invalid.")
        name, value_type = str(row.get("name", "")), str(row.get("type", ""))
        if not _IDENTIFIER.fullmatch(name) or name.startswith("REasy"):
            raise ShaderGraphError(f"{name!r} is not a usable name for a Code node pin.")
        if value_type not in NUMERIC_VALUE_TYPES:
            raise ShaderGraphError(f"A Code node pin cannot have type {value_type!r}.")
        result.append((name, value_type))
    return tuple(result)


def validate_code(properties) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    inputs, outputs = code_pins(properties, "inputs"), code_pins(properties, "outputs")
    names = [name for name, _type in (*inputs, *outputs)]
    if len(set(names)) != len(names):
        raise ShaderGraphError("Every pin of a Code node needs its own name.")
    body = properties.get("body", "")
    if not isinstance(body, str) or len(body.encode("utf-8")) > MAX_CODE_BYTES:
        raise ShaderGraphError("A Code node's body is invalid or larger than 64 KiB.")
    if re.search(r"^\s*#", body, re.MULTILINE):
        raise ShaderGraphError("A Code node's body cannot use preprocessor directives.")
    return inputs, outputs


def parse_code_pins(text: str) -> list[dict[str, str]]:
    rows = []
    for part in filter(None, (item.strip() for item in str(text).split(","))):
        words = part.split()
        if len(words) != 2:
            raise ShaderGraphError("Write each pin as a type and a name, like float3 colour.")
        rows.append({"name": words[1], "type": words[0]})
    return rows


def code_pin_text(properties, direction: str) -> str:
    return ", ".join(f"{row['type']} {row['name']}" for row in properties.get(direction, ()))


def updated_code(graph, node, inputs: str, outputs: str, body: str) -> dict:
    properties = {**node.properties, "inputs": parse_code_pins(inputs), "outputs": parse_code_pins(outputs),
                  "body": body}
    validate_code(properties)
    kept = {(row["name"], row["type"]) for row in (*properties["inputs"], *properties["outputs"])}
    before = {(row["name"], row["type"]) for row in (*node.properties.get("inputs", ()),
                                                      *node.properties.get("outputs", ()))}
    gone = {name for name, _type in before - kept}
    graph.links[:] = [link for link in graph.links
                      if not (link.target_node == node.node_id and link.target_pin in gone)
                      and not (link.source_node == node.node_id and link.source_pin in gone)]
    return properties


def _helper_name(properties, output: str) -> str:
    digest = hashlib.sha256(json.dumps([properties.get("inputs"), properties.get("outputs"), properties.get("body")],
                                       sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"REasyCode_{digest}_{output}"


def code_helpers(node, _contract=None) -> str:
    inputs, outputs = validate_code(node.properties)
    parameters = ", ".join(f"{value_type} {name}" for name, value_type in inputs)
    declarations = "\n".join(f"    {value_type} {name} = ({value_type})0;" for name, value_type in outputs)
    body = "\n".join("    " + line for line in str(node.properties.get("body", "")).splitlines())
    return "\n\n".join(
        f"{value_type} {_helper_name(node.properties, name)}({parameters})\n{{\n{declarations}\n{body}\n    return {name};\n}}"
        for name, value_type in outputs)


def code_expression(node, pin: str, incoming) -> str:
    inputs = code_pins(node.properties, "inputs")
    if pin not in {name for name, _type in code_pins(node.properties, "outputs")}:
        raise ShaderGraphError(f"The Code node has no output {pin!r}.")
    return f"{_helper_name(node.properties, pin)}({', '.join(incoming(name, value_type) for name, value_type in inputs)})"


def _pins(node, _contract):
    try:
        inputs, outputs = code_pins(node.properties, "inputs"), code_pins(node.properties, "outputs")
    except ShaderGraphError:
        return ()
    return (*(ShaderPin(name, name, value_type, "input", True) for name, value_type in inputs),
            *(ShaderPin(name, name, value_type, "output") for name, value_type in outputs))


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind=CODE_KIND, title="Code", pins_provider=_pins, expression=code_expression,
    hlsl_helpers_provider=code_helpers,
    description=QT_TRANSLATE_NOOP(
        "ShaderGraphNodes",
        "A small HLSL function you write: typed inputs and outputs, and a body that assigns the outputs."),
))

__all__ = [
    "CODE_KIND", "code_expression", "code_helpers", "code_pin_text", "code_pins", "parse_code_pins", "updated_code",
    "validate_code",
]
