import re

from .shader_storage import ShaderStorageType
from .shader_graph import ShaderGraphError, ShaderGraphNodeExtension, ShaderPin, register_shader_graph_node_extension
from PySide6.QtCore import QT_TRANSLATE_NOOP


STORAGE_EFFECTS = frozenset({'group_read','group_write','dispatch_mesh'})


def storage_info(properties):
    storage = properties.get('storage')
    if not isinstance(storage,dict) or set(storage) != {'id','layout','scope'}:
        raise ShaderGraphError('Shared data needs its typed allocation declaration.')
    identity, scope = storage['id'], storage['scope']
    if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,95}',identity) or scope not in {'group','payload'}:
        raise ShaderGraphError('Invalid shared-data identity or ownership.')
    layout = ShaderStorageType.from_data(storage['layout'])
    if scope == 'payload' and layout.kind != 'struct':
        raise ShaderGraphError('An amplification payload must be a declared structure.')
    return identity, scope, layout


def storage_path(properties):
    identity, scope, layout = storage_info(properties)
    path = properties.get('path')
    if not isinstance(path,list) or len(path) > 32:
        raise ShaderGraphError('A shared-data access needs its declared member path.')
    suffix, indices, element = '', [], layout
    for part in path:
        if element.kind == 'struct':
            if type(part) is not int or not 0 <= part < len(element.members):
                raise ShaderGraphError('Shared-data member is outside its declared structure.')
            suffix += f'.field{part}'
            element = element.members[part]
        elif element.kind == 'array' and part is None:
            pin = f'index{len(indices)}'
            indices.append((pin,'uint'))
            suffix += '[{' + pin + '}]'
            element = element.element
        else:
            raise ShaderGraphError('Shared-data path does not follow the declared aggregate.')
    if element.kind not in {'uint','float','bool'}:
        raise ShaderGraphError('Choose a scalar member of the shared aggregate.')
    return identity, scope, layout, suffix, tuple(indices), element.kind


def storage_shape(node):
    inputs = [('exec','execution')]
    outputs = [('next','execution')]
    if node.kind == 'dispatch_mesh':
        _, scope, _ = storage_info(node.properties)
        if scope != 'group':
            raise ShaderGraphError('Amplification dispatch sends a shared payload allocation.')
        inputs += [(pin,'uint') for pin in ('x','y','z')]
    elif node.kind in {'group_read','group_write'}:
        _, scope, _, _, indices, domain = storage_path(node.properties)
        inputs.extend(indices)
        if node.kind == 'group_write':
            if scope == 'payload':
                raise ShaderGraphError('Incoming mesh payloads are read-only.')
            inputs.append(('value',domain))
        else:
            outputs.append(('value',domain))
    else:
        raise ShaderGraphError('Unknown shared-data operation.')
    return tuple(inputs), tuple(outputs)


def storage_pins(node, contract):
    inputs, outputs = storage_shape(node)
    if contract is not None and contract.stage != 'material_function':
        _, scope, layout = storage_info(node.properties)
        if contract.stage not in {'compute','mesh','amplification'}:
            raise ShaderGraphError('Shared data belongs to thread-group invocations.')
        if node.kind == 'dispatch_mesh' or scope == 'payload':
            expected_stage = 'amplification' if node.kind == 'dispatch_mesh' else 'mesh'
            if contract.stage != expected_stage or contract.mesh_execution is None or contract.mesh_execution.payload_layout != layout:
                raise ShaderGraphError('Shared payload layout differs from the stage linkage contract.')
    return tuple(ShaderPin(pin,pin.title(),domain,direction,direction=='input')
                 for direction,ports in (('input',inputs),('output',outputs)) for pin,domain in ports)


def storage_name(identity, scope):
    return 'reasyMeshPayload' if scope == 'payload' else 'reasyShared_' + identity


def storage_statements(node, contract, incoming, variable):
    storage_pins(node, contract)
    inputs, _ = storage_shape(node)
    args = {pin:incoming(pin,domain) for pin,domain in inputs}
    identity, scope, _ = storage_info(node.properties)
    allocation = storage_name(identity, scope)
    results = {'next':args['exec']}
    if node.kind == 'dispatch_mesh':
        lines = [f"DispatchMesh({args['x']}, {args['y']}, {args['z']}, {allocation});"]
    else:
        _, _, _, suffix, _, domain = storage_path(node.properties)
        address = allocation + suffix.format(**args)
        if node.kind == 'group_write':
            lines = [f"{address} = {args['value']};"]
        else:
            lines = [f'{domain} {variable} = {address};']
            results['value'] = variable
    return lines, results


def storage_allocations(graph):
    allocations = {}
    pending = [dict(kind=node.kind,properties=node.properties) for node in graph.nodes] if graph is not None else []
    while pending:
        value = pending.pop()
        if isinstance(value,dict):
            if value.get('kind') in STORAGE_EFFECTS and isinstance(value.get('properties'),dict):
                identity, scope, layout = storage_info(value['properties'])
                key = storage_name(identity,scope)
                if key in allocations and allocations[key] != (identity,scope,layout):
                    raise ShaderGraphError('Shared-data references disagree about the same allocation.')
                allocations[key] = identity, scope, layout
            pending.extend(value.values())
        elif isinstance(value,(list,tuple)):
            pending.extend(value)
    return allocations


def storage_environment(graph, contract):
    declarations = []
    for key, (_, scope, layout) in sorted(storage_allocations(graph).items()):
        declarations.extend(layout.declarations())
        if scope == 'group':
            declarations.append('groupshared ' + layout.declaration(key) + ';')
    execution = contract.mesh_execution
    parameters = []
    if contract.stage == 'mesh' and execution is not None and execution.payload_layout is not None:
        layout = execution.payload_layout
        declarations.extend(layout.declarations())
        parameters.append('in payload ' + layout.declaration('reasyMeshPayload'))
    return tuple(dict.fromkeys(declarations)), parameters


def storage_templates(contract, graph=None):
    if contract is None or contract.stage not in {'compute','mesh','amplification'}:
        return ()
    allocations = storage_allocations(graph)
    execution = contract.mesh_execution
    if execution is not None and execution.payload_layout is not None:
        layout = execution.payload_layout
        scope = 'payload' if contract.stage == 'mesh' else 'group'
        if not any((s,l)==(scope,layout) for _,s,l in allocations.values()):
            identity = 'payload'
            while storage_name(identity,scope) in allocations:
                identity += '_'
            allocations[storage_name(identity,scope)] = identity,scope,layout
    result = []
    def leaves(layout, path=(), label=''):
        if layout.kind == 'struct':
            for i, member in enumerate(layout.members):
                yield from leaves(member, (*path,i), label+f'.field{i}')
        elif layout.kind == 'array':
            yield from leaves(layout.element, (*path,None), label+'[]')
        else:
            yield path,label
    for _, (identity,scope,layout) in sorted(allocations.items()):
        storage=dict(id=identity,scope=scope,layout=layout.to_data())
        for path,label in leaves(layout):
            for kind,verb in (('group_read','Read'),('group_write','Write')):
                if kind == 'group_write' and scope == 'payload':
                    continue
                result.append((kind,f'{verb} {identity}{label}',dict(storage=storage,path=list(path))))
        if contract.stage == 'amplification' and execution is not None and execution.payload_layout == layout and scope == 'group':
            result.append(('dispatch_mesh',f'Dispatch Mesh Groups ({identity})',dict(storage=storage)))
    return tuple(result)


def _effect_expression(_node, _pin, _incoming):
    raise ShaderGraphError('Shared-data operations require invocation-owned statements.')


for _kind, _title in (('group_read','Read Shared Data'),('group_write','Write Shared Data'),('dispatch_mesh','Dispatch Mesh Groups')):
    register_shader_graph_node_extension(ShaderGraphNodeExtension(kind=_kind,title=_title,
        pins_provider=storage_pins,expression=_effect_expression,
        description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Typed shared data with explicit execution ordering. Incoming mesh payloads are read-only.')))
