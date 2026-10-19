from .shader_graph import (ShaderGraphError, ShaderGraphNodeExtension, ShaderPin, register_shader_graph_node_extension,
                           _identifier, signature_declaration, signature_member)
from PySide6.QtCore import QT_TRANSLATE_NOOP


MESH_EFFECTS = frozenset({'mesh_output_counts', 'mesh_indices', 'mesh_vertex', 'mesh_primitive'})


def mesh_shape(node):
    inputs = [('exec', 'execution')]
    if node.kind == 'mesh_output_counts':
        inputs += [('vertices', 'uint'), ('primitives', 'uint')]
    elif node.kind == 'mesh_indices':
        topology = node.properties.get('topology')
        if topology not in {'line', 'triangle'}:
            raise ShaderGraphError('Mesh indices require line or triangle topology.')
        inputs += [('index', 'uint'), ('vertices', 'uint2' if topology == 'line' else 'uint3')]
    elif node.kind in {'mesh_vertex', 'mesh_primitive'}:
        semantic, index = node.properties.get('semantic'), node.properties.get('semantic_index')
        domain, component = node.properties.get('value_type'), node.properties.get('component')
        domains = {'bool'} | {base + width for base in ('float', 'uint', 'int') for width in ('', '2', '3', '4')}
        width = int(domain[-1]) if domain in domains and domain[-1:] in '234' else 1
        if (not isinstance(semantic, str) or not semantic or len(semantic) > 256
                or type(index) is not int or not 0 <= index <= 255
                or domain not in domains
                or component is not None and (type(component) is not int or not 0 <= component < width)):
            raise ShaderGraphError('Invalid mesh output attribute or component.')
        inputs += [('index', 'uint'), ('value', domain.rstrip('234') if component is not None else domain)]
    else:
        raise ShaderGraphError('Unknown mesh output operation.')
    return tuple(inputs), (('next', 'execution'),)


def mesh_pins(node, contract):
    inputs, outputs = mesh_shape(node)
    if contract is not None and contract.stage != 'material_function':
        if contract.stage != 'mesh' or contract.mesh_execution is None:
            raise ShaderGraphError('Mesh output operations require a mesh-stage renderer.')
        if node.kind == 'mesh_indices' and node.properties['topology'] != contract.mesh_execution.output_topology:
            raise ShaderGraphError('Mesh index topology differs from the renderer contract.')
        if node.kind in {'mesh_vertex', 'mesh_primitive'}:
            fields = contract.outputs if node.kind == 'mesh_vertex' else contract.patch_constants
            field = next((f for f in fields if f.key == (node.properties['semantic'].casefold(), node.properties['semantic_index'])), None)
            if field is None or field.value_type != node.properties['value_type']:
                raise ShaderGraphError('Mesh output attribute differs from its renderer declaration.')
    return tuple(ShaderPin(pin, pin.title(), domain, direction, direction == 'input')
                 for direction, ports in (('input', inputs), ('output', outputs)) for pin, domain in ports)


def mesh_statements(node, contract, incoming, _variable):
    mesh_pins(node, contract)
    inputs, _ = mesh_shape(node)
    args = {pin: incoming(pin, domain) for pin, domain in inputs}
    if node.kind == 'mesh_output_counts':
        statement = f"SetMeshOutputCounts({args['vertices']}, {args['primitives']});"
    elif node.kind == 'mesh_indices':
        statement = f"reasyMeshIndices[{args['index']}] = {args['vertices']};"
    else:
        array = 'reasyMeshVertices' if node.kind == 'mesh_vertex' else 'reasyMeshPrimitives'
        member = _identifier(f"out_{node.properties['semantic']}_{node.properties['semantic_index']}")
        component = node.properties.get('component')
        suffix = '.' + 'xyzw'[component] if component is not None and node.properties['value_type'][-1:] in '234' else ''
        statement = f"{array}[{args['index']}].{member}{suffix} = {args['value']};"
    return [statement], args['exec']


def mesh_templates(contract):
    if contract is None or contract.stage != 'mesh' or contract.mesh_execution is None:
        return ()
    result = [('mesh_output_counts', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Set Mesh Output Counts'), {}),
              ('mesh_indices', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Write Primitive Indices'), {'topology': contract.mesh_execution.output_topology})]
    for kind, label, fields in (('mesh_vertex', 'Write Vertex', contract.outputs), ('mesh_primitive', 'Write Primitive', contract.patch_constants)):
        result.extend((kind, f'{label} {field.display_name}', attribute_properties(field)) for field in fields)
    return tuple(result)


def attribute_properties(field, component=None):
    return dict(semantic=field.semantic_name, semantic_index=field.semantic_index, value_type=field.value_type,
                **({'component': component} if component is not None else {}))


def mesh_declarations(contract):
    execution = contract.mesh_execution
    if execution is None:
        raise ShaderGraphError('Mesh behavior needs its native output declarations.')
    execution.validate_stage('mesh')
    if execution.payload_bytes and execution.payload_layout is None:
        raise ShaderGraphError('This mesh entry requires a typed amplification-payload adapter.')
    if not execution.max_output_vertices or not execution.max_output_primitives:
        raise ShaderGraphError('Zero-sized mesh output arrays need an explicit compiler lowering.')
    if contract.inputs or any(f.stream for f in (*contract.outputs, *contract.patch_constants)):
        raise ShaderGraphError('Mesh entries use invocation inputs and per-vertex/per-primitive outputs, not geometry streams.')
    declarations, parameters = [], []
    for label, modifier, name, count, fields in (
        ('Vertex', 'vertices', 'reasyMeshVertices', execution.max_output_vertices, contract.outputs),
        ('Primitive', 'primitives', 'reasyMeshPrimitives', execution.max_output_primitives, contract.patch_constants)):
        if not fields and label == 'Primitive':
            continue
        declarations += [f'struct REasyMesh{label}', '{']
        for field in fields:
            declarations.append(signature_declaration(field, signature_member('out', field)))
        declarations += ['};', '']
        parameters.append(f'out {modifier} REasyMesh{label} {name}[{count}]')
    index_type = 'uint2' if execution.output_topology == 'line' else 'uint3'
    parameters.append(f'out indices {index_type} reasyMeshIndices[{execution.max_output_primitives}]')
    return declarations, parameters, [f'[outputtopology("{execution.output_topology}")]']


def _effect_expression(_node, _pin, _incoming):
    raise ShaderGraphError('Mesh output writes require invocation-owned statement generation.')


for _kind, _title in (('mesh_output_counts', 'Set Mesh Output Counts'), ('mesh_indices', 'Write Primitive Indices'),
                      ('mesh_vertex', 'Write Vertex Attribute'), ('mesh_primitive', 'Write Primitive Attribute')):
    register_shader_graph_node_extension(ShaderGraphNodeExtension(kind=_kind, title=_title, 
        pins_provider=mesh_pins, expression=_effect_expression,
        description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'An ordered write to the current mesh group output. The index selects a vertex or primitive, not a thread.')))
