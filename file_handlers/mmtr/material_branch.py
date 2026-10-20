from uuid import uuid4

from file_handlers.sdf.shader_graph import (
    ShaderGraph, ShaderGraphError, ShaderGraphNodeExtension, ShaderNodeEvaluation, ShaderPin,
    register_shader_graph_node_extension,
)
from file_handlers.sdf.shader_graph_math import NUMERIC_VALUE_TYPES
from .material_function import (
    MaterialFunctionAsset, MaterialFunctionPin,
    _compiler_function_document, _read_compiler_function,
)
from .material_region_read import read_region, publish_region_properties
from PySide6.QtCore import QT_TRANSLATE_NOOP


def _parse_branch(properties):
    try:
        arms = tuple(_read_compiler_function(properties[key]) for key in ("when_true", "when_false"))
        first, second = arms
        shape = lambda pins: tuple((pin.pin_id, pin.value_type) for pin in pins)
        if shape(first.inputs) != shape(second.inputs) or shape(first.outputs) != shape(second.outputs):
            raise ShaderGraphError("Both branch arms must declare the same input and result IDs and types.")
        if any(pin.default is not None for arm in arms for pin in arm.inputs):
            raise ShaderGraphError("Branch inputs are explicit caller values; defaults belong outside the branch.")
        if any(pin.value_type not in (*NUMERIC_VALUE_TYPES, 'execution') for pin in first.outputs):
            raise ShaderGraphError("Branches return numeric values, not resource objects.")
        if any(pin.pin_id == "condition" for pin in (*first.inputs, *first.outputs)):
            raise ShaderGraphError("The branch condition has a reserved port ID.")
        return arms
    except (KeyError, TypeError, ValueError) as exc:
        raise ShaderGraphError(f"Invalid conditional region: {exc}") from exc


def branch_arms(node):
    return read_region('branch', node.properties)


def branch_properties(when_true, when_false, *, name="Branch"):
    properties = publish_region_properties(dict(display_name=name,
        when_true=_compiler_function_document(when_true),
        when_false=_compiler_function_document(when_false)))
    read_region('branch', properties)
    return properties


def new_branch_properties():
    inputs = tuple(MaterialFunctionPin(name, label, "float") for name, label in
                   (("true_value", "True Value"), ("false_value", "False Value")))
    result = MaterialFunctionPin("result", "Result", "float", False)
    arms = []
    identity = uuid4().hex
    for index, label in enumerate(("True", "False")):
        graph = ShaderGraph("material_function", "")
        for pin in inputs:
            graph.add_node("function_input", node_id=pin.pin_id, properties=dict(
                pin_id=pin.pin_id, display_name=pin.name, value_type=pin.value_type))
        graph.add_node("function_output", node_id="output", properties=dict(
            pin_id=result.pin_id, display_name=result.name, value_type=result.value_type))
        graph.connect(inputs[index].pin_id, "value", "output", "value")
        arms.append(MaterialFunctionAsset(f"branch.{identity}.{label.casefold()}", label, "Control Flow",
            "Calculations evaluated only when this arm is selected.", inputs, (result,), graph))
    return branch_properties(*arms)


def branch_pins(node, _contract):
    arm = branch_arms(node)[0]
    return (ShaderPin("condition", "Condition", "bool", "input", True),
            *(ShaderPin(pin.pin_id, pin.name, pin.value_type, "input", True) for pin in arm.inputs),
            *(ShaderPin(pin.pin_id, pin.name, pin.value_type, "output") for pin in arm.outputs))


def _name(node):
    from .region_codegen import region_codegen_key
    return "REasyBranch_" + region_codegen_key(node)[:24]


def branch_evaluation(node, incoming):
    name = _name(node)
    arm = branch_arms(node)[0]
    arguments = [incoming("condition", "bool"), *(incoming(pin.pin_id, pin.value_type) for pin in arm.inputs)]
    return ShaderNodeEvaluation(f"{name}({', '.join(arguments)})", name + "_Result",
                                tuple((pin.pin_id, f"field{index}") for index, pin in enumerate(arm.outputs)))


def _passes_through(arm):
    graph = arm.graph
    nodes = {item.node_id: item for item in graph.nodes}
    sources = {(link.target_node, link.target_pin): link.source_node for link in graph.links}
    outputs = [item for item in graph.nodes if item.kind == 'function_output']
    return bool(outputs) and all(
        nodes.get(sources.get((output.node_id, 'value'))) is not None
        and nodes[sources[(output.node_id, 'value')]].kind == 'function_input' for output in outputs)


def branch_helpers(node, contract):
    from .function_region import region_helpers, region_parameters, region_result
    name = _name(node)
    arms = branch_arms(node)
    input_names, parameters = region_parameters(arms[0])
    output_type = name + "_Result"
    declarations = [region_result(arms[0], output_type)]
    for label, arm in zip(("True", "False"), arms):
        declarations.extend(definition for definition in region_helpers(arm, name + '_' + label, output_type, contract)
                            if definition not in declarations)
    arguments = ", ".join(input_names.values())
    passes = [_passes_through(arm) for arm in arms]
    if passes[0] != passes[1]:
        kept, taken = ("True", "False") if passes[0] else ("False", "True")
        test = "!condition" if taken == "False" else "condition"
        body = (f"    {output_type} result = {name}_{kept}({arguments});",
                f"    [branch] if ({test}) result = {name}_{taken}({arguments});")
    else:
        # One return: inliners wrap multi-return functions in an extra switch per call.
        body = (f"    {output_type} result;",
                f"    [branch] if (condition) result = {name}_True({arguments});",
                f"    else result = {name}_False({arguments});")
    declarations.extend((f"{output_type} {name}(bool condition{', ' if parameters else ''}{parameters})", "{",
                         *body, "    return result;", "}"))
    return "\n".join((f"#ifndef {name}_DEFINED", f"#define {name}_DEFINED", *declarations, "#endif"))


register_shader_graph_node_extension(ShaderGraphNodeExtension(
    kind="branch", title="Branch", pins_provider=branch_pins,
    evaluation=branch_evaluation,
    hlsl_helpers_provider=branch_helpers,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", "Evaluate only the selected function. Double-click to edit the true and false calculations."),
))
