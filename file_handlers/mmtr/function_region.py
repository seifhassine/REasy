from contextvars import ContextVar

from file_handlers.sdf.shader_graph import generate_graph_function, GeneratedGraphFunction, ShaderNodeEvaluation
from file_handlers.sdf.shader_graph_math import NUMERIC_VALUE_TYPES
from file_handlers.sdf.shader_graph_resource import object_declaration
from .material_function import _expand_material_functions_for_compilation, _has_function_calls


_helper_results = ContextVar('material_region_helpers', default=None)


def compiled_region_helpers(node, contract):
    from file_handlers.sdf.shader_graph import shader_graph_node_extension
    from .region_codegen import region_assets, region_codegen_key

    def key(item):
        return region_codegen_key(item), contract.fingerprint

    cache = _helper_results.get()
    token = _helper_results.set({}) if cache is None else None
    cache = _helper_results.get()
    root = key(node)
    pending = [(node, root, False)]
    try:
        while pending:
            current, identity, ready = pending.pop()
            if identity in cache:
                continue
            if ready:
                provider = shader_graph_node_extension(current.kind).hlsl_helpers_provider
                cache[identity] = provider(current, contract)
            else:
                pending.append((current, identity, True))
                assets = region_assets(current)
                for asset in assets:
                    for child in _compiler_body(asset).nodes:
                        if child.kind in {'branch', 'repeat'}:
                            pending.append((child, key(child), False))
        return '\n'.join(dict.fromkeys(cache.values())) if token is not None else ''
    finally:
        if token is not None:
            _helper_results.reset(token)


def region_parameters(asset):
    names = {pin.pin_id: f'argument{index}' for index, pin in enumerate(asset.inputs)}
    declaration = ', '.join(f'{pin.value_type} {names[pin.pin_id]}' if pin.value_type in NUMERIC_VALUE_TYPES
        else object_declaration(pin.value_type, names[pin.pin_id]) for pin in asset.inputs)
    return names, declaration


def _compiler_body(asset):
    from .material_region_read import region_derived_cache
    cache = region_derived_cache('compiler_region_bodies')
    key = id(asset.graph)
    if cache is not None and key in cache:
        return cache[key][1]
    graph = (_expand_material_functions_for_compilation(asset.graph, live_only=True)
             if _has_function_calls(asset.graph, (), None) else asset.graph)
    if any(node.kind in {'branch', 'repeat'} for node in graph.nodes):
        from .region_dependencies import specialize_regions
        graph = specialize_regions(graph)
    if cache is not None:
        cache[key] = asset.graph, graph
    return graph


def region_result(asset, name, *, outputs=None):
    from file_handlers.sdf.shader_graph_execution import EXECUTION_DECLARATION
    outputs = asset.outputs if outputs is None else outputs
    prefix = EXECUTION_DECLARATION + '\n' if any(p.value_type == 'execution' for p in (*asset.inputs, *outputs)) else ''
    return prefix + f'struct {name} {{ ' + ' '.join(f'{pin.value_type} field{index};' for index, pin in enumerate(outputs)) + ' };'


def _assigned_outputs(graph):
    nodes = {node.node_id: node for node in graph.nodes}
    missing = {link.target_node for link in graph.links
               if link.target_pin == 'value' and nodes[link.source_node].kind == 'unassigned_value'}
    return {node.properties['pin_id']: node for node in graph.nodes
            if node.kind == 'function_output' and node.node_id not in missing}


def region_helpers(asset, name, result_type, contract):
    names, parameters = region_parameters(asset)
    graph = _compiler_body(asset)
    outputs = _assigned_outputs(graph)
    def build(_expression, incoming):
        assignments = [f"    result.field{index} = {incoming(outputs[pin.pin_id], 'value', pin.value_type)};"
                       for index, pin in enumerate(asset.outputs) if pin.pin_id in outputs]
        # Unassigned arm outputs start defined; FXC rejects reading them (X8000).
        return [f'    {result_type} result = ({result_type})0;', *assignments, '    return result;']
    generated = generate_graph_function(graph, contract, build, memoize_values=True,
        value_resolver=lambda node, _pin, _incoming: names[node.properties['pin_id']] if node.kind == 'function_input' else None)
    from file_handlers.sdf.shader_graph_resource import resource_type_declarations
    declarations = list(dict.fromkeys(declaration for pin in asset.inputs for declaration in resource_type_declarations(pin.value_type)))
    declarations.extend(line for line in generated.declarations if line and line not in declarations)
    return [*declarations, '\n'.join((f'{result_type} {name}({parameters})', '{', *generated.body, '}'))]


def inline_region(node, contract, incoming, prefix, *, unassigned_inputs=()):
    if node.kind == 'branch':
        from .material_branch import branch_arms
        assets = branch_arms(node)
        continuation = None
    else:
        from .material_loop import loop_body
        asset, _, continuation = loop_body(node)
        assets = (asset,)
    result_type, result = prefix + 'Result', prefix + 'Value'
    arguments = {pin.pin_id: incoming(pin.pin_id, pin.value_type) for pin in assets[0].inputs}
    from .region_dependencies import _Region
    dependencies = _Region(node)
    states = dependencies.states
    aliases = {pin.pin_id: arguments[source] for pin in assets[0].outputs
               if pin.pin_id != continuation and (source := dependencies.alias_input(pin.pin_id)) is not None}
    selected = tuple(pin for pin in assets[0].outputs if pin.pin_id not in aliases)
    fields = {pin.pin_id: f'field{i}' for i, pin in enumerate(selected)}
    if not selected:
        return GeneratedGraphFunction((), ()), ShaderNodeEvaluation('', 'void', ()), aliases
    declarations = [region_result(assets[0], result_type, outputs=selected)]
    lines = [f'{result_type} {result} = ({result_type})0;']
    if node.kind == 'branch':
        lines.append(f"if ({incoming('condition', 'bool')})")
    else:
        lines.append('{')
        domains = {pin.pin_id: pin.value_type for pin in assets[0].inputs}
        for i, (source, _) in enumerate(states):
            name = prefix + f'State{i}'
            initial = '' if source in unassigned_inputs else ' = '+arguments[source]
            lines.append(f'    {domains[source]} {name}{initial};')
            arguments[source] = name
        lines.append('    [loop] while (true)')
    for index, asset in enumerate(assets):
        if index:
            lines.append('else')
        graph = _compiler_body(asset)
        outputs = _assigned_outputs(graph)

        def build(_expression, read):
            return [f"{result}.{fields[pin.pin_id]} = {read(outputs[pin.pin_id], 'value', pin.value_type)};"
                    for pin in asset.outputs if pin.pin_id in fields and pin.pin_id in outputs]

        generated = generate_graph_function(graph, contract, build, memoize_values=True, inline_regions=True,
            variable_prefix=prefix + f'Arm{index}',
            value_resolver=lambda item, _pin, _read: arguments[item.properties['pin_id']] if item.kind == 'function_input' else None)
        declarations.extend(generated.declarations)
        lines.append('{')
        lines.extend('    ' + line for line in generated.body)
        if continuation is not None:
            # FXC reports X4555 for loop-carried assignments after a conditional break, so carry state before the exit test.
            for source, target in states:
                value = aliases[target] if target in aliases else f'{result}.{fields[target]}'
                lines.append(f'    {arguments[source]} = {value};')
            lines.append(f'    if (!{result}.{fields[continuation]}) break;')
        lines.append('}')
    if continuation is not None:
        lines.append('}')
    evaluation = ShaderNodeEvaluation(result, result_type, tuple((pin, field) for pin, field in fields.items() if pin != continuation))
    return GeneratedGraphFunction(tuple(declarations), tuple(lines)), evaluation, aliases
