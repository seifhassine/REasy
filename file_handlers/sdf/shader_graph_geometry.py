from .shader_graph import (ShaderGraphError, ShaderGraphNodeExtension, ShaderPin, register_shader_graph_node_extension,
    _identifier, signature_declaration, signature_member)
from PySide6.QtCore import QT_TRANSLATE_NOOP


def output_type(stream):
    return f'REasyGeometryOutput{stream}'


def stream_name(stream):
    return f'reasyGeometryStream{stream}'


def vertex_fields(contract, stream):
    return [dict(semantic=f.semantic_name, index=f.semantic_index, value_type=f.value_type)
            for f in contract.outputs if f.stream == stream]


def geometry_templates(contract):
    if contract is None or contract.stage!='geometry' or contract.primitive_execution is None:
        return ()
    return tuple((kind,f'{label} (stream {stream})',dict(stream=stream,
        **({'fields':vertex_fields(contract,stream)} if kind=='emit_vertex' else {})))
        for stream in contract.primitive_execution.output_streams
        for kind,label in (('emit_vertex','Emit Vertex'),('end_primitive','End Primitive')))


def geometry_shape(node):
    stream = node.properties.get('stream', 0)
    if type(stream) is not int or not 0 <= stream <= 3:
        raise ShaderGraphError('Choose an output stream from zero through three.')
    inputs = [('exec', 'execution')]
    if node.kind == 'emit_vertex':
        fields = node.properties.get('fields')
        if not isinstance(fields, list) or len(fields) > 64:
            raise ShaderGraphError('Emit Vertex requires its typed vertex interface.')
        seen = set()
        for i, field in enumerate(fields):
            if not isinstance(field, dict) or set(field) != {'semantic', 'index', 'value_type'}:
                raise ShaderGraphError('Invalid emitted vertex field.')
            semantic, index, domain = field['semantic'], field['index'], field['value_type']
            if (not isinstance(semantic, str) or not semantic or len(semantic) > 256
                    or type(index) is not int or not 0 <= index <= 255
                    or domain not in {base + width for base in ('float','uint','int') for width in ('','2','3','4')}):
                raise ShaderGraphError('Invalid emitted vertex field meaning or type.')
            key = semantic.casefold(), index
            if key in seen:
                raise ShaderGraphError('Emitted vertex semantics must be unique within a stream.')
            seen.add(key)
            inputs.append((f'field{i}', domain))
    elif node.kind != 'end_primitive':
        raise ShaderGraphError('Unknown geometry effect.')
    return tuple(inputs), (('next','execution'),)


def geometry_pins(node, contract):
    inputs, outputs = geometry_shape(node)
    if contract is not None and contract.stage != 'material_function':
        execution = contract.primitive_execution
        if contract.stage != 'geometry' or execution is None or node.properties.get('stream',0) not in execution.output_streams:
            raise ShaderGraphError('This effect requires a declared geometry output stream.')
        if node.kind == 'emit_vertex' and node.properties['fields'] != vertex_fields(contract,node.properties.get('stream',0)):
            raise ShaderGraphError('The emitted vertex interface differs from its renderer stream.')
    labels = {f'field{i}':f"{field['semantic']}{field['index']}" for i,field in enumerate(node.properties.get('fields',()))}
    return tuple(ShaderPin(pin,labels.get(pin,pin.title()),domain,direction,direction=='input')
                 for direction,ports in (('input',inputs),('output',outputs)) for pin,domain in ports)


def geometry_statements(node, contract, incoming, variable):
    geometry_pins(node, contract)
    inputs, _ = geometry_shape(node)
    arguments = {pin:incoming(pin,domain) for pin,domain in inputs}
    stream = node.properties.get('stream',0)
    if node.kind == 'end_primitive':
        return [f'{stream_name(stream)}.RestartStrip();'], arguments['exec']
    fields = [field for field in contract.outputs if field.stream == stream]
    lines = [f'{output_type(stream)} {variable};']
    lines.extend(f'{variable}.{signature_member('out', field)} = {arguments[f"field{i}"]};' for i,field in enumerate(fields))
    lines.append(f'{stream_name(stream)}.Append({variable});')
    return lines, arguments['exec']


def primitive_input_pins(node, contract):
    field = contract.input(node.properties.get('semantic',''),node.properties.get('semantic_index',0)) if contract else None
    if field is None or contract.stage not in {'geometry','domain','hull'} or contract.primitive_execution is None or not field.primitive_attribute:
        raise ShaderGraphError('Primitive Input requires a declared per-vertex/control-point attribute.')
    return (ShaderPin('index','Vertex Index','uint','input',True),ShaderPin('value',field.display_name,field.value_type,'output'))


def primitive_expression(node, _pin, incoming):
    member = _identifier(f"in_{node.properties['semantic']}_{node.properties.get('semantic_index',0)}")
    return f"input[{incoming('index','uint')}].{member}"


def stage_declarations(contract):
    execution = contract.primitive_execution
    if execution is None:
        raise ShaderGraphError('Geometry behavior needs its native primitive declarations.')
    execution.validate_stage('geometry')
    streams = execution.output_streams
    if not {field.stream for field in contract.outputs} <= set(streams):
        raise ShaderGraphError('Geometry vertex interfaces disagree with declared output streams.')
    declarations = []
    for stream in streams:
        declarations.extend((f'struct {output_type(stream)}','{'))
        for field in contract.outputs:
            if field.stream != stream:
                continue
            declarations.append(signature_declaration(field, signature_member('out', field)))
        declarations.extend(('};',''))
    parameters = [f'{execution.input_primitive} REasyStageInput input[{execution.primitive_vertices}]']
    stream_type = {'pointlist':'PointStream','linestrip':'LineStream','trianglestrip':'TriangleStream'}[execution.output_topology]
    # HLSL assigns stream IDs by parameter ordinal; DXIL keeps signatures of unused parameters.
    if len(streams)!=max(streams)+1:
        # FXC requires one element but prunes unused parameters; DXC keeps declared attributes.
        placeholder = 'uint unused : REASY_UNUSED;' if contract.model_major<6 else ''
        declarations.extend((f'struct REasyUnusedGeometryOutput {{{placeholder}}};',''))
    parameters.extend(f'inout {stream_type}<{output_type(stream) if stream in streams else "REasyUnusedGeometryOutput"}> {stream_name(stream)}'
                      for stream in range(max(streams)+1))
    attributes = [f'[maxvertexcount({execution.max_output_vertices})]']
    if execution.geometry_instances != 1:
        attributes.append(f'[instance({execution.geometry_instances})]')
    return declarations, parameters, attributes


def _effect_expression(_node, _pin, _incoming):
    raise ShaderGraphError('Geometry effects require the invocation-owned statement compiler.')


for _kind, _title in (('emit_vertex','Emit Vertex'),('end_primitive','End Primitive')):
    register_shader_graph_node_extension(ShaderGraphNodeExtension(kind=_kind,title=_title,pins_provider=geometry_pins,
        expression=_effect_expression,
        description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'The execution connection determines when this vertex or primitive boundary is emitted.')))
register_shader_graph_node_extension(ShaderGraphNodeExtension(kind='primitive_input',title='Primitive Input',
    pins_provider=primitive_input_pins,expression=primitive_expression,
    description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'A vertex attribute from the current input primitive, selected by vertex index.')))
