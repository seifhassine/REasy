from .shader_graph_math import NUMERIC_VALUE_TYPES
from .shader_graph import ShaderGraphError, ShaderGraphNodeExtension, ShaderNodeEvaluation, ShaderPin, register_shader_graph_node_extension
from PySide6.QtCore import QT_TRANSLATE_NOOP


EXECUTION_TYPE = 'execution'
EXECUTION_DECLARATION = '\n'.join(('#ifndef REasyExecution_DEFINED', '#define REasyExecution_DEFINED',
    'struct execution { uint sequence; };', 'execution REasyExecutionBegin() { execution e; e.sequence = 0u; return e; }', '#endif'))
EXECUTION_KINDS = frozenset({'execution_begin', 'capture_value', 'discard_pixel', 'memory_load', 'memory_store', 'memory_atomic', 'memory_barrier', 'image_load', 'image_store', 'image_atomic'})
ATOMIC_OPERATIONS = {'add': 'InterlockedAdd', 'and': 'InterlockedAnd', 'or': 'InterlockedOr', 'xor': 'InterlockedXor',
                     'minimum': 'InterlockedMin', 'maximum': 'InterlockedMax', 'exchange': 'InterlockedExchange',
                     'compare_exchange': 'InterlockedCompareExchange'}
EXECUTION_TEMPLATES = (
    ('execution_begin', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Begin Execution'), {}), ('capture_value', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Evaluate Value'), {'value_type': 'float'}),
    ('discard_pixel', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Discard Pixel'), {}), ('memory_load', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Read Buffer Word'), {}), ('memory_store', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Write Buffer Word'), {}),
    ('memory_atomic', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Atomic Buffer Update'), {'operation': 'add', 'value_type': 'uint'}),
    ('memory_barrier', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Memory Barrier'), {'scope': 'device', 'synchronize': False}),
    ('image_load', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Read Writable Texture'), {'dimension':'texture2d', 'element_type':'float4'}),
    ('image_store', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Write Texture'), {'dimension':'texture2d', 'element_type':'float4'}),
    ('image_atomic', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Atomic Texel Update'), {'dimension':'texture2d', 'element_type':'uint', 'operation':'add'}),
)


def execution_shape(node):
    kind, properties = node.kind, node.properties
    result = []
    if kind != 'execution_begin':
        result.append(('exec', EXECUTION_TYPE))
    domain = properties.get('value_type', 'float' if kind == 'capture_value' else 'uint')
    outputs = [('next', EXECUTION_TYPE)]
    if kind == 'capture_value':
        if domain not in NUMERIC_VALUE_TYPES:
            raise ShaderGraphError('Evaluate Value requires an explicit numeric domain.')
        result.append(('value', domain))
        outputs.append(('value', domain))
    elif kind == 'discard_pixel':
        result.append(('condition', 'bool'))
    elif kind in {'memory_load', 'memory_store', 'memory_atomic'}:
        from .shader_graph_resource import resource_coherence
        from .shader_graph_buffer import structured_buffer_type
        stride = properties.get('stride', 0)
        words = properties.get('words', 1)
        if type(words) is not int or not 1 <= words <= 4 or words > 1 and (stride or kind == 'memory_atomic'):
            raise ShaderGraphError('A multi-word buffer access moves two to four words of a byte-address buffer.')
        word_type = 'uint' if words == 1 else f'uint{words}'
        word_bits = 64 if kind == 'memory_atomic' and domain.endswith('64_t') else 32
        resource_type = structured_buffer_type(stride, writable=True, word_bits=word_bits) if stride else 'rw_byte_address_buffer'
        result.append(('buffer', resource_coherence(resource_type, properties.get('globally_coherent', False))))
        if stride:
            result.append(('index', 'uint'))
        result.append(('byte_offset', 'uint'))
        if kind == 'memory_atomic':
            operation = properties.get('operation', 'add')
            domains = {'int', 'uint', 'int64_t', 'uint64_t'}
            if operation not in ATOMIC_OPERATIONS or domain not in domains:
                raise ShaderGraphError('Atomic update requires a supported integer operation.')
            if operation == 'compare_exchange':
                result.append(('compare', domain))
            result.append(('value', domain))
            outputs.append(('original', domain))
        elif kind == 'memory_load':
            outputs.append(('value', word_type))
        else:
            result.append(('value', word_type))
    elif kind in {'image_load', 'image_store', 'image_atomic'}:
        from .shader_graph_texture import texture_object_type, WRITABLE_TEXTURE_DIMENSIONS, TEXTURE_LOAD_COORDINATES
        dimension, element = properties.get('dimension', 'texture2d'), properties.get('element_type', 'uint' if kind == 'image_atomic' else 'float4')
        if dimension not in WRITABLE_TEXTURE_DIMENSIONS:
            raise ShaderGraphError('This image dimension is not writable.')
        from .shader_graph_resource import resource_coherence
        resource_type = resource_coherence('rw_' + texture_object_type(dimension, element), properties.get('globally_coherent', False))
        result.extend((('texture', resource_type), ('coordinates', TEXTURE_LOAD_COORDINATES[dimension].replace('int', 'uint'))))
        if kind == 'image_load':
            outputs.append(('value', element))
        elif kind == 'image_store':
            result.append(('value', element))
        else:
            operation = properties.get('operation', 'add')
            domain = properties.get('value_type', element)
            family = {'int64_t','uint64_t'} if element.endswith('64_t') else {'int','uint'}
            if element not in family or domain not in family or operation not in ATOMIC_OPERATIONS:
                raise ShaderGraphError('Texel atomics require a scalar integer image and a supported operation.')
            if operation == 'compare_exchange':
                result.append(('compare', domain))
            result.append(('value', domain))
            outputs.append(('original', domain))
    elif kind == 'memory_barrier':
        if properties.get('scope', 'device') not in {'device', 'group', 'all'} or type(properties.get('synchronize', False)) is not bool:
            raise ShaderGraphError('Memory barriers need a declared scope and synchronization policy.')
    elif kind != 'execution_begin':
        raise ShaderGraphError('Unknown execution operation.')
    return tuple(result), tuple(outputs)


def execution_pins(node, _contract):
    inputs, outputs = execution_shape(node)
    return tuple(ShaderPin(name, name.replace('_', ' ').title(), domain, direction, direction == 'input')
                 for direction, ports in (('input', inputs), ('output', outputs)) for name, domain in ports)


def _name(node):
    import hashlib
    inputs, outputs = execution_shape(node)
    shape = (node.kind, inputs, outputs, node.properties.get('operation'), node.properties.get('scope'), node.properties.get('synchronize'))
    return 'REasyExecution_' + hashlib.sha256(repr(shape).encode()).hexdigest()[:24]


def execution_evaluation(node, incoming):
    inputs, outputs = execution_shape(node)
    arguments = [incoming(pin, domain) for pin, domain in inputs]
    by_pin = {pin: argument for (pin, _), argument in zip(inputs, arguments, strict=True)}
    if node.kind == 'capture_value':
        return ShaderNodeEvaluation(by_pin['value'], dict(inputs)['value'], (), direct=('value',),
                                    forwarded=(('next', by_pin['exec']),))
    name = _name(node)
    return ShaderNodeEvaluation(f"{name}({', '.join(arguments)})", name + '_Result', tuple((pin, pin) for pin, _ in outputs))


def _atomic_update(target, carrier, domain, operation):
    def convert(text, source, destination):
        if source == destination:
            return text
        return f'{destination}({text})' if destination.endswith('64_t') else f'as{destination}({text})'
    if carrier != domain and operation in {'minimum','maximum'}:
        compare = 'min' if operation == 'minimum' else 'max'
        desired = convert(f'{compare}({convert("expected",carrier,domain)}, value)', domain, carrier)
        return [f'{carrier} expected; InterlockedOr({target}, {carrier}(0), expected);',
            f'{carrier} observed;', '[allow_uav_condition] while (true) {',
            f'    {carrier} desired = {desired};',
            f'    InterlockedCompareExchange({target}, expected, desired, observed);',
            '    if (observed == expected) break;', '    expected = observed;', '}',
            f'result.original = {convert("observed",carrier,domain)};']
    arguments = target + ', ' + (convert('compare',domain,carrier)+', ' if operation == 'compare_exchange' else '') + convert('value',domain,carrier) + ', previous'
    return [f'{carrier} previous;', f'{ATOMIC_OPERATIONS[operation]}({arguments});',
            f'result.original = {convert("previous",carrier,domain)};']


def execution_helpers(node, contract):
    inputs, outputs = execution_shape(node)
    kind, name = node.kind, _name(node)
    if kind == 'capture_value':
        return ''
    if (kind in {'image_atomic','memory_atomic'} and node.properties.get('value_type',node.properties.get('element_type','uint')).endswith('64_t')
            and (contract.model_major, contract.model_minor) < (6, 6)):
        raise ShaderGraphError('64-bit resource atomics require Shader Model 6.6 or later.')
    from .shader_graph_resource import object_declaration
    if kind == 'discard_pixel' and contract.stage != 'pixel':
        raise ShaderGraphError('Discard Pixel requires a pixel implementation.')
    if kind == 'memory_barrier' and (node.properties.get('scope', 'device') != 'device' or node.properties.get('synchronize', False)) and contract.stage != 'compute':
        raise ShaderGraphError('Group memory and group synchronization require a compute implementation.')
    parameters = ', '.join(f'{domain} {pin}' if domain in NUMERIC_VALUE_TYPES or domain == EXECUTION_TYPE
                           else object_declaration(domain, 'image' if pin == 'texture' else pin) for pin, domain in inputs)
    from .shader_graph_buffer import structured_dynamic_word, structured_storage, structured_word_member
    stride = node.properties.get('stride', 0) if kind in {'memory_load', 'memory_store', 'memory_atomic'} else 0
    word_bits = 64 if kind == 'memory_atomic' and node.properties.get('value_type','uint').endswith('64_t') else 32
    word_bytes = word_bits // 8
    declarations = (structured_storage(stride, word_bits=word_bits)[1],) if stride else ()
    lines = [f'#ifndef {name}_DEFINED', f'#define {name}_DEFINED', EXECUTION_DECLARATION, *declarations,
             f'struct {name}_Result {{ ' + ' '.join(f'{domain} {pin};' for pin, domain in outputs) + ' };',
             f'{name}_Result {name}({parameters}) {{', f'    {name}_Result result;',
             '    result.next = ' + ('REasyExecutionBegin();' if kind == 'execution_begin' else 'exec;')]
    if kind == 'discard_pixel':
        # clip preserves gradient control flow; discard makes later gradients fail with X3662.
        lines.append('    clip(condition ? -1.0 : 1.0);')
    elif stride and kind in {'memory_load', 'memory_store'} and structured_dynamic_word(stride, 'word') is not None:
        # Slots are an array, so a dynamic word index needs no switch.
        target = 'buffer[index].' + structured_dynamic_word(stride, 'word')
        lines.append('    uint word = byte_offset >> 2;')
        lines.append(f'    result.value = {target};' if kind == 'memory_load' else f'    {target} = value;')
    elif stride:
        if kind in {'memory_load', 'memory_atomic'}:
            lines.append('    result.' + ('value' if kind == 'memory_load' else 'original') + ' = 0u;')
        lines.append('    switch (byte_offset) {')
        for offset in range(0, stride, word_bytes):
            target = 'buffer[index].' + structured_word_member(stride, offset // word_bytes, word_bits=word_bits)
            lines.append(f'    case {offset}u: {{')
            if kind == 'memory_load':
                lines.append(f'        result.value = {target};')
            elif kind == 'memory_store':
                lines.append(f'        {target} = value;')
            else:
                operation = node.properties.get('operation', 'add')
                lines.extend('        '+line for line in _atomic_update(target,'uint64_t' if word_bits == 64 else 'uint',node.properties.get('value_type','uint'),operation))
            lines.extend(('        break;', '    }'))
        lines.append('    }')
    elif kind in {'memory_load', 'memory_store'}:
        words = node.properties.get('words', 1)
        suffix = str(words) if words > 1 else ''
        lines.append(f'    result.value = buffer.Load{suffix}(byte_offset);' if kind == 'memory_load'
                     else f'    buffer.Store{suffix}(byte_offset, value);')
    elif kind == 'memory_atomic':
        arguments = 'byte_offset, ' + ('compare, ' if node.properties.get('operation', 'add') == 'compare_exchange' else '') + 'value, result.original'
        suffix = '64' if node.properties.get('value_type','uint').endswith('64_t') else ''
        lines.append(f"    buffer.{ATOMIC_OPERATIONS[node.properties.get('operation', 'add')]}{suffix}({arguments});")
    elif kind == 'memory_barrier':
        function = {'device': 'DeviceMemoryBarrier', 'group': 'GroupMemoryBarrier', 'all': 'AllMemoryBarrier'}[node.properties.get('scope', 'device')]
        lines.append(f"    {function + ('WithGroupSync' if node.properties.get('synchronize', False) else '')}();")
    elif kind == 'image_load':
        lines.append('    result.value = image[coordinates];')
    elif kind == 'image_store':
        lines.append('    image[coordinates] = value;')
    elif kind == 'image_atomic':
        element = node.properties.get('element_type','uint')
        lines.extend('    '+line for line in _atomic_update('image[coordinates]',element,node.properties.get('value_type',element),node.properties.get('operation','add')))
    lines.extend(('    return result;', '}', '#endif'))
    return '\n'.join(lines)


def execution_diagnostics(graph, pins):
    from .shader_graph import ShaderGraphDiagnostic
    if not any(node.kind in EXECUTION_KINDS or node.properties.get('value_type') == EXECUTION_TYPE for node in graph.nodes) and not any(link.target_pin == 'execution' for link in graph.links):
        return ()
    nodes = {node.node_id: node for node in graph.nodes}
    live = graph.upstream_nodes(node.node_id for node in graph.nodes if node.kind in {'output', 'function_output', 'material_surface'})
    successors = {}
    for link in graph.links:
        if link.source_node not in live or link.target_node not in live:
            continue
        node = nodes[link.source_node]
        if any(pin.name == link.source_pin and pin.direction == 'output' and pin.value_type == EXECUTION_TYPE for pin in pins(node)):
            successors.setdefault(node.node_id, []).append(link)
    def continuations(links):
        terminals = [nodes[link.target_node].kind in {'output', 'function_output', 'material_surface'} for link in links]
        return sum(not terminal for terminal in terminals) + any(terminals)
    return tuple(ShaderGraphDiagnostic('error', 'execution-fork', 'Connect execution through one ordered continuation; this live operation has multiple successors.', identity)
                 for identity, links in successors.items() if continuations(links) > 1)


for _kind, _title, _properties in EXECUTION_TEMPLATES:
    register_shader_graph_node_extension(ShaderGraphNodeExtension(
        kind=_kind, title=_title, pins_provider=execution_pins,
        evaluation=execution_evaluation, hlsl_helpers_provider=execution_helpers,
        description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Connect the execution sockets to specify when this operation runs. Numeric results are evaluated once.'),
    ))
