from dataclasses import dataclass
from functools import lru_cache

from file_handlers.sdf.spirv import reflect_spirv
from file_handlers.sdf.spirv_contract import _DOMAIN_LOCATION_BUILTIN
from .profiles.re_engine_spirv_authoring import _Module
from .vanilla_function import _Builder, _Value, VanillaFunctionError
from PySide6.QtCore import QT_TRANSLATE_NOOP


@dataclass(frozen=True)
class _Pointer:
    root: int
    path: tuple = ()


@dataclass(frozen=True)
class _Index:
    identifier: int


@dataclass(frozen=True)
class _UndefinedOpaque:
    type_id: int


@dataclass(frozen=True)
class _Composite:
    elements: tuple


_INVOCATIONS = {'dispatch-thread-id': 'dispatch_thread_id', 'group-id': 'group_id',
                'group-thread-id': 'group_thread_id', 'group-index': 'group_index',
                'geometry-instance-id': 'geometry_instance_id',
                'output-control-point-id': 'output_control_point_id', 'primitive-id': 'primitive_id'}


def _typed(scalar, width):
    return f"{scalar}{width}" if width > 1 else scalar


def recover_spirv_function(ir, contract, *, name='Vanilla Behavior', _hull_phase=None):
    module = _Module(ir)
    native = reflect_spirv(ir.write())
    if native.stage == 'hull' and _hull_phase is None:
        from .hull_behavior import combine_hull_behaviors
        return combine_hull_behaviors(
            recover_spirv_function(ir, contract, name=name + ' Control Points', _hull_phase='control_point'),
            recover_spirv_function(ir, contract, name=name + ' Patch Constants', _hull_phase='patch'),
            contract, name)
    if native.stage not in {'vertex', 'pixel', 'compute', 'geometry', 'hull', 'domain'}:
        raise VanillaFunctionError('This Vulkan stage needs an indexed/control-flow behavior lifter.')
    entries = [r for r in ir.instructions if r.opcode == 15 and r.values[2] == native.entry_point]
    if len(entries) != 1:
        raise VanillaFunctionError('Vulkan behavior needs one identified entry point.')
    builder = _Builder(contract, ordered=True)
    definitions = module.definitions
    resources = {d.variable_id: next((r for r in contract.resources if r.name == d.name), None)
                 for d in native.descriptors}
    buffers = {d.variable_id: contract.buffer(d.name) for d in native.descriptors if d.descriptor_kind == 'constant-buffer'}
    from file_handlers.sdf.spirv_contract import patch_interface_ids, spirv_interface_rows
    patch_ids = patch_interface_ids(native, module.decorations)
    input_rows, output_rows, _undeclared_ids = spirv_interface_rows(native, contract, patch_ids)
    interface_rows = {**input_rows, **output_rows}
    interfaces = {identifier: rows[0] for identifier, rows in interface_rows.items()}
    output_ids = {item.variable_id for item in native.outputs if item.variable_id not in patch_ids}
    patch_output_ids = {item.variable_id for item in native.outputs if item.variable_id in patch_ids}
    control_point_axes = {item.variable_id for item in native.outputs
                          if native.stage == 'hull' and item.variable_id not in patch_ids
                          and definitions[module.pointee(item.variable_id)].opcode == 28}
    primitive_input_ids = {item.variable_id for item in native.inputs
                           if contract.stage in {'geometry', 'hull', 'domain'} and item.variable_id not in patch_ids
                           and definitions[module.pointee(item.variable_id)].opcode == 28}
    domain_location_ids = {}
    if any(item.built_in == _DOMAIN_LOCATION_BUILTIN for item in native.inputs):
        from file_handlers.sdf.shader_graph_tessellation import domain_coordinate_type
        domain_location_ids = {item.variable_id: int(domain_coordinate_type(contract)[-1])
                               for item in native.inputs if item.built_in == _DOMAIN_LOCATION_BUILTIN}
    values = {}
    outputs, field_lanes = {}, {}
    for identifier, rows in interface_rows.items():
        if identifier not in output_ids and identifier not in patch_output_ids:
            continue
        lane = 0
        for row in rows:
            span = tuple(range(lane, lane + row.mask.bit_count()))
            field_lanes[row] = (identifier, span)
            for component in span:
                outputs[identifier, component] = builder.unassigned(row.value_type.rstrip('234'))
            lane += row.mask.bit_count()
    invocation_ids = set()

    @lru_cache(None)
    def shape(type_id):
        row = definitions[type_id]
        if row.opcode == 20:
            return 'bool', 1
        if row.opcode == 21 and row.values[1] == 32:
            return 'int' if row.values[2] else 'uint', 1
        if row.opcode == 22 and row.values[1] == 32:
            return 'float', 1
        if row.opcode == 23 and 2 <= row.values[2] <= 4:
            domain, width = shape(row.values[1])
            if width == 1:
                return domain, row.values[2]
        raise VanillaFunctionError(f'{row.name} needs an explicit Vulkan value-shape adapter.')

    def parts(value):
        if not isinstance(value, _Value):
            raise VanillaFunctionError('A Vulkan numeric operation received an object or pointer.')
        return [builder.scalar(value, i) for i in range(int(value.value_type[-1]) if value.value_type[-1:] in '234' else 1)]

    def children(type_id):
        row = definitions[type_id]
        return row.values[1:] if row.opcode == 30 else (row.values[1],) * module.composite_count(type_id)

    def assemble(type_id, leaves, path=()):
        row = definitions[type_id]
        if row.opcode in {20, 21, 22}:
            return leaves[path]
        elements = [assemble(child, leaves, (*path, i)) for i, child in enumerate(children(type_id))]
        return builder.vector(elements) if row.opcode == 23 else _Composite(tuple(elements))

    def extract(composite, indices):
        for index in indices:
            composite = composite.elements[index] if isinstance(composite, _Composite) else builder.scalar(composite, index)
        return composite

    def construct(type_id, elements):
        if definitions[type_id].opcode == 23:
            return builder.vector([part for element in elements for part in parts(element)])
        if len(elements) != len(children(type_id)):
            raise VanillaFunctionError('A Vulkan composite does not match its declared members.')
        return _Composite(tuple(elements))

    def dot(left, right):
        products = [builder.operation('multiply', a=a, b=b) for a, b in zip(left, right, strict=True)]
        result = products[0]
        for product in products[1:]:
            result = builder.operation('add', a=result, b=product)
        return result

    def matrix_vector(matrix, vector, *, transpose=False):
        if not isinstance(matrix, _Composite):
            raise VanillaFunctionError('A Vulkan matrix product needs its declared column vectors.')
        columns = [parts(column) for column in matrix.elements]
        rows = columns if transpose else zip(*columns, strict=True)
        return builder.vector([dot(row, parts(vector)) for row in rows])

    def value(identifier):
        if identifier not in values:
            row = definitions[identifier]
            if row.opcode in {41, 42}:
                result = builder.boolean(row.opcode == 41)
            elif row.opcode == 43:
                domain, width = shape(row.type_id)
                if width != 1 or len(row.values) != 3:
                    raise VanillaFunctionError('A Vulkan literal does not match its scalar width.')
                result = builder.convert(builder.integer(row.values[2]), domain, bits=True)
            elif row.opcode == 44:
                result = construct(row.type_id, [value(item) for item in row.values[2:]])
            elif row.opcode in {1, 46}:
                result = (_UndefinedOpaque(row.type_id) if row.opcode == 1 and definitions[row.type_id].opcode in {25, 26}
                          else initial_value(row.type_id, unassigned=row.opcode == 1))
            elif row.opcode == 59:
                if row.values[2] not in {6, 7} and len(row.values) != 3:
                    raise VanillaFunctionError('This Vulkan storage initializer needs explicit execution semantics.')
                result = _Pointer(identifier)
            else:
                raise VanillaFunctionError(f'{row.name} has no value on this Vulkan predecessor edge.')
            values[identifier] = result
        return values[identifier]

    from .spirv_memory import SpirvBufferMemory
    memory = SpirvBufferMemory(module, builder, resources, value)

    def local_key(identifier):
        return ('local', identifier)

    def image_sample(type_id, texture, sampler, **arguments):
        if isinstance(texture, _UndefinedOpaque) or isinstance(sampler, _UndefinedOpaque):
            result = initial_value(type_id)
            node = builder.graph.node(result.node)
            node.properties.update(display_name='Unspecified Image Read',
                description=QT_TRANSLATE_NOOP("ShaderGraphNodes", 'The native shader samples an explicitly undefined image or sampler; no texture binding is specified.'))
            return result
        return builder.texture_operation('sample_texture', texture, sampler=sampler, **arguments)

    def local_load(item, path):
        if not path:
            return item
        index, *tail = path
        elements = item.elements if isinstance(item, _Composite) else parts(item)
        if isinstance(index, _Index):
            choices = [local_load(child, tail) for child in elements]
            leaves = [flattened(child) for child in choices]
            selected = [builder.array_element(builder.convert(value(index.identifier), 'uint', bits=True), list(row))
                        for row in zip(*leaves, strict=True)]
            return restored(choices[0], iter(selected))
        if not 0 <= index < len(elements):
            raise VanillaFunctionError('A Vulkan local pointer exceeds its declared composite extent.')
        return local_load(elements[index], tail)

    def local_store(item, path, stored):
        if not path:
            return stored
        index, *tail = path
        elements = list(item.elements if isinstance(item, _Composite) else parts(item))
        if isinstance(index, _Index):
            selector = builder.convert(value(index.identifier), 'uint', bits=True)
            for i, child in enumerate(elements):
                updated = local_store(child, tail, stored)
                condition = builder.compare('equal', selector, builder.integer(i))
                leaves = [builder.select(condition, yes, no)
                          for yes, no in zip(flattened(updated), flattened(child), strict=True)]
                elements[i] = restored(child, iter(leaves))
        else:
            if not 0 <= index < len(elements):
                raise VanillaFunctionError('A Vulkan local pointer exceeds its declared composite extent.')
            elements[index] = local_store(elements[index], tail, stored)
        return _Composite(tuple(elements)) if isinstance(item, _Composite) else builder.vector(elements)

    def load(pointer_id, type_id):
        uniform = module.uniform_pointer(pointer_id)
        if uniform is not None:
            leaves = list(module.uniform_leaves(type_id, uniform.address, uniform.layout))
            elements = {}
            for _path, _scalar, address in leaves:
                if len(address.terms) == 1 and address.terms[0][1] == 16:
                    elements.setdefault((address.terms[0][0], address.constant // 16), set()).add(address.constant % 16)
            def read(domain, offset, terms):
                if not terms:
                    return builder.convert(builder.constant_word(buffers.get(uniform.root), offset), domain, bits=True)
                index_id, stride, extent = terms[0]
                index = builder.convert(value(index_id), 'uint', bits=True)
                if len(terms) == 1 and stride == 16:
                    element = builder.constant_array_word(buffers.get(uniform.root), index, offset // 16,
                                                          (offset % 16) // 4, count=extent,
                                                          whole=len(elements.get((index_id, offset // 16), ())) > 1)
                    if element is not None:
                        return builder.convert(element, domain, bits=True)
                return builder.array_element(index, [read(domain, offset + i * stride, terms[1:]) for i in range(extent)])
            return assemble(type_id, {path: read(shape(scalar)[0], address.constant, address.terms) for path, scalar, address in leaves})
        buffer_leaves = memory.leaves(pointer_id, type_id)
        if buffer_leaves is not None:
            slots = {}
            for _path, _scalar, resource, index, offset in buffer_leaves:
                if isinstance(offset, int):
                    slots.setdefault((id(resource), index, offset // 16), []).append(offset)
            def word(resource, index, offset):
                if isinstance(offset, int) and len(slots.get((id(resource), index, offset // 16), ())) > 1:
                    vector = builder.record_slot(resource, index, offset // 16)
                    if vector is not None:
                        return builder.scalar(vector, (offset % 16) // 4)
                return builder.buffer_word(resource, index, record_offset=offset)
            return assemble(type_id, {path: builder.convert(word(resource, index, offset), shape(scalar)[0], bits=True)
                                      for path, scalar, resource, index, offset in buffer_leaves})
        pointer = value(pointer_id)
        if not isinstance(pointer, _Pointer):
            raise VanillaFunctionError('A Vulkan load has no declared pointer.')
        if local_key(pointer.root) in values:
            return local_load(values[local_key(pointer.root)], pointer.path)
        if pointer.root in resources and not pointer.path:
            resource = resources[pointer.root]
            if resource is None:
                raise VanillaFunctionError('A Vulkan descriptor has no renderer binding.')
            return resource
        if pointer.root in domain_location_ids:
            path = list(pointer.path)
            if len(path) > 1 or any(isinstance(index, _Index) or not 0 <= index < domain_location_ids[pointer.root]
                                    for index in path):
                raise VanillaFunctionError('A Vulkan domain coordinate read exceeds its declared dimension.')
            return builder.vector([builder.domain_coordinate(index) for index in path]
                                  or [builder.domain_coordinate(component)
                                      for component in range(domain_location_ids[pointer.root])])
        if pointer.root in patch_ids:
            rows = interface_rows[pointer.root]
            path = list(pointer.path)
            if len(path) > 1 or path and (isinstance(path[0], _Index) or not 0 <= path[0] < len(rows)):
                raise VanillaFunctionError('A Vulkan patch constant read exceeds its declared elements.')
            if not path:
                return builder.vector([builder.patch_constant(row, 0) for row in rows])
            return builder.patch_constant(rows[path[0]], 0)
        if pointer.root in interfaces:
            field = interfaces[pointer.root]
            if pointer.root in primitive_input_ids:
                path=list(pointer.path)
                def vertex_read(index, lanes):
                    vertex=builder.convert(value(index.identifier),'uint',bits=True) if isinstance(index,_Index) else index
                    return builder.vector([builder.stage_input(field,lane,vertex=vertex) for lane in lanes])
                if not path:
                    return _Composite(tuple(vertex_read(i,range(field.mask.bit_count())) for i in range(contract.primitive_execution.primitive_vertices)))
                index=path.pop(0)
                if len(path)>1 or path and (not isinstance(path[0],int) or not 0<=path[0]<field.mask.bit_count()):
                    raise VanillaFunctionError('A Vulkan primitive input exceeds its declared components.')
                return vertex_read(index,path or range(field.mask.bit_count()))
            lanes = tuple(range(field.mask.bit_count())) if not pointer.path else pointer.path
            if len(pointer.path) > 1 or any(not 0 <= lane < field.mask.bit_count() for lane in lanes):
                raise VanillaFunctionError('A Vulkan interface load exceeds its declared components.')
            if pointer.root in output_ids:
                return builder.vector([outputs[pointer.root, lane] for lane in lanes])
            invocation = _INVOCATIONS.get(field.system_value)
            if invocation is not None:
                return builder.vector([builder.invocation_input(invocation, lane) for lane in lanes])
            # FrontFacing is a uint in the renderer ABI but OpLoad yields a bool.
            domain, _width = shape(type_id)
            return builder.vector([builder.convert(builder.stage_input(field, lane), domain) for lane in lanes])
        raise VanillaFunctionError('A Vulkan load needs an explicit storage-class adapter.')

    binary = {'OpIAdd': 'add', 'OpFAdd': 'add', 'OpISub': 'subtract', 'OpFSub': 'subtract',
        'OpIMul': 'multiply', 'OpFMul': 'multiply', 'OpUDiv': 'divide', 'OpSDiv': 'divide', 'OpFDiv': 'divide',
        'OpUMod': 'integer_remainder', 'OpSRem': 'integer_remainder', 'OpFRem': 'float_remainder',
        'OpBitwiseAnd': 'bit_and', 'OpBitwiseOr': 'bit_or', 'OpBitwiseXor': 'bit_xor',
        'OpShiftLeftLogical': 'shift_left', 'OpShiftRightLogical': 'shift_right', 'OpShiftRightArithmetic': 'shift_right'}
    comparisons = {'Equal': 'equal', 'NotEqual': 'not_equal', 'GreaterThan': 'greater',
                   'GreaterThanEqual': 'greater_equal', 'LessThan': 'less', 'LessThanEqual': 'less_equal'}
    unary_ext = {1: 'round_even', 2: 'round_even', 3: 'truncate', 4: 'absolute', 5: 'absolute',
        8: 'floor', 9: 'ceiling', 10: 'fraction', 13: 'sine', 14: 'cosine', 15: 'tangent',
        16: 'arc_sine', 17: 'arc_cosine', 29: 'exponent2', 27: 'exponent', 28: 'logarithm',
        30: 'logarithm2', 31: 'square_root', 32: 'reciprocal_square_root', 69: 'normalize'}

    def instruction(row):
        v, op = row.values, row.name
        builder.precise_math = (42,) in module.decorations.get(row.result, ())  # NoContraction
        result = None
        if op in {'OpNop', 'OpSelectionMerge', 'OpLoopMerge', 'OpLine', 'OpNoLine'}:
            return
        if op == 'OpUndef':
            result = value(row.result)
        elif op == 'OpVariable':
            if v[2] != 7 or len(v) not in {3, 4}:
                raise VanillaFunctionError('A Vulkan function variable requires its declared local storage class.')
            values[local_key(row.result)] = value(v[3]) if len(v) == 4 else initial_value(definitions[row.type_id].values[2])
            result = _Pointer(row.result)
        elif op in {'OpAccessChain', 'OpInBoundsAccessChain'}:
            pointer = value(v[2])
            if not isinstance(pointer, _Pointer):
                raise VanillaFunctionError('A Vulkan access chain has no pointer base.')
            path = tuple(module.constant(index) if module.constant(index) is not None else _Index(index) for index in v[3:])
            if (any(isinstance(index, _Index) for index in path) and local_key(pointer.root) not in values
                    and pointer.root not in primitive_input_ids and pointer.root not in control_point_axes
                    and module.uniform_pointer(row.result) is None and memory.address(row.result) is None):
                raise VanillaFunctionError('A dynamic Vulkan pointer needs its declared array-axis adapter.')
            result = _Pointer(pointer.root, (*pointer.path, *path))
        elif op == 'OpLoad':
            if len(v) != 3:
                raise VanillaFunctionError('Vulkan memory operands need explicit volatility/alignment semantics.')
            result = load(v[2], row.type_id)
            pointer = value(v[2])
            if (isinstance(pointer, _Pointer) and interfaces.get(pointer.root) is not None
                    and interfaces[pointer.root].system_value == 'output-control-point-id'):
                invocation_ids.add(row.result)
        elif op == 'OpStore':
            pointer = value(v[0])
            if isinstance(pointer, _Pointer) and local_key(pointer.root) in values:
                if len(v) != 2:
                    raise VanillaFunctionError('Vulkan local store memory operands need explicit memory-policy semantics.')
                key = local_key(pointer.root)
                values[key] = local_store(values[key], pointer.path, value(v[1]))
                return
            buffer_leaves = memory.leaves(v[0], definitions[v[1]].type_id)
            if buffer_leaves is not None:
                if len(v) != 2:
                    raise VanillaFunctionError('Vulkan store memory operands need explicit memory-policy semantics.')
                for path, _scalar, resource, index, offset in buffer_leaves:
                    builder.memory('memory_store', resource, index, record_offset=offset,
                        value=builder.convert(extract(value(v[1]), path), 'uint', bits=True))
                return
            if len(v) != 2:
                raise VanillaFunctionError('Vulkan store memory operands need explicit memory-policy semantics.')
            if not isinstance(pointer, _Pointer):
                raise VanillaFunctionError('A Vulkan store needs an explicit writable-storage adapter.')
            path = list(pointer.path)
            if pointer.root in control_point_axes:
                if not path:
                    raise VanillaFunctionError('A control-point output store needs its control-point index.')
                index, path = path[0], path[1:]
                if isinstance(index, _Index):
                    if index.identifier not in invocation_ids:
                        raise VanillaFunctionError('A control-point output store must write its own invocation.')
                elif index != 0 or contract.primitive_execution.output_control_points != 1:
                    raise VanillaFunctionError('A control-point output store must write its own invocation.')
            if pointer.root in patch_output_ids:
                if _hull_phase != 'patch':
                    raise VanillaFunctionError('Tessellation factors are written by the patch-constant phase.')
                rows = interface_rows[pointer.root]
                offset, expected = 0, sum(row.mask.bit_count() for row in rows)
                if path:
                    if len(path) != 1 or not isinstance(path[0], int) or not 0 <= path[0] < len(rows):
                        raise VanillaFunctionError('A Vulkan patch output store exceeds its declared elements.')
                    offset = sum(row.mask.bit_count() for row in rows[:path[0]])
                    expected = rows[path[0]].mask.bit_count()
                stored = flattened(value(v[1]))
                if len(stored) != expected:
                    raise VanillaFunctionError('A Vulkan patch output store disagrees with its declared value.')
                outputs.update({(pointer.root, offset + position): item for position, item in enumerate(stored)})
                return
            if pointer.root not in output_ids:
                raise VanillaFunctionError('A Vulkan store needs an explicit writable-storage adapter.')
            stored = parts(value(v[1]))
            field = interfaces[pointer.root]
            lanes = list(range(field.mask.bit_count())) if not path else path
            if len(lanes) != len(stored) or len(path) > 1 or any((pointer.root, lane) not in outputs for lane in lanes):
                raise VanillaFunctionError('A Vulkan output store exceeds its declared value.')
            outputs.update({(pointer.root, lane): item for lane, item in zip(lanes, stored)})
            return
        elif op == 'OpCopyObject':
            result = value(v[2])
        elif op == 'OpCompositeConstruct':
            result = construct(row.type_id, [value(identifier) for identifier in v[2:]])
        elif op == 'OpTranspose':
            columns = value(v[2]).elements
            result = _Composite(tuple(builder.vector(list(row)) for row in zip(*(parts(column) for column in columns), strict=True)))
        elif op == 'OpMatrixTimesMatrix':
            result = _Composite(tuple(matrix_vector(value(v[2]), column) for column in value(v[3]).elements))
        elif op == 'OpMatrixTimesScalar':
            matrix, scalar = value(v[2]), value(v[3])
            if not isinstance(matrix, _Composite):
                raise VanillaFunctionError('A Vulkan matrix scale needs its declared column vectors.')
            result = _Composite(tuple(builder.vector([builder.operation('multiply', a=part, b=scalar)
                                                      for part in parts(column)]) for column in matrix.elements))
        elif op == 'OpCompositeExtract':
            result = extract(value(v[2]), v[3:])
        elif op == 'OpCompositeInsert':
            result = local_store(value(v[3]), v[4:], value(v[2]))
        elif op == 'OpVectorShuffle':
            components = [*parts(value(v[2])), *parts(value(v[3]))]
            domain, width = shape(row.type_id)
            if len(v[4:]) != width or any(index != 0xffffffff and not 0 <= index < len(components) for index in v[4:]):
                raise VanillaFunctionError('A Vulkan shuffle has invalid components.')
            result = builder.vector([builder.unassigned(domain) if index == 0xffffffff else components[index] for index in v[4:]])
        elif op in {'OpConvertFToU', 'OpConvertFToS', 'OpConvertSToF', 'OpConvertUToF', 'OpBitcast'}:
            domain, width = shape(row.type_id)
            result = builder.convert(value(v[2]), _typed(domain, width), bits=op == 'OpBitcast')
        elif op in binary:
            left, right = value(v[2]), value(v[3])
            domain, width = shape(row.type_id)
            if op in {'OpUDiv', 'OpSDiv', 'OpUMod', 'OpSRem'}:
                integer_type = _typed('uint' if op in {'OpUDiv', 'OpUMod'} else 'int', width)
                left, right = builder.convert(left, integer_type, bits=True), builder.convert(right, integer_type, bits=True)
            if op.startswith('OpShift'):
                signed = op == 'OpShiftRightArithmetic'
                shift_type = _typed('int' if signed else 'uint', width)
                left, right = builder.convert(left, shift_type, bits=True), builder.convert(right, shift_type, bits=True)
            result = builder.operation(binary[op], a=left, b=right)
            if len(parts(result)) != width:
                raise VanillaFunctionError(f'{op} %{row.result}: recovered {left.value_type}/{right.value_type} operands disagree with declared {domain}{width}.')
            result = builder.convert(result, _typed(domain, width), bits=True)
        elif op in {'OpSNegate', 'OpFNegate', 'OpNot'}:
            result = builder.operation('bit_not' if op == 'OpNot' else 'negate', value=value(v[2]))
        elif op in {'OpIsNan', 'OpIsInf'}:
            result = builder.operation('is_nan' if op == 'OpIsNan' else 'is_infinite', value=value(v[2]))
        elif any(op == 'Op' + prefix + suffix for prefix in ('I', 'U', 'S', 'FOrd', 'FUnord', 'Logical') for suffix in comparisons):
            prefix, suffix = next((prefix, suffix) for prefix in ('I', 'U', 'S', 'FOrd', 'FUnord', 'Logical')
                                  for suffix in comparisons if op == 'Op' + prefix + suffix)
            def compare(left, right):
                if prefix in {'U', 'S'}:
                    domain = 'uint' if prefix == 'U' else 'int'
                    left, right = builder.convert(left, domain, bits=True), builder.convert(right, domain, bits=True)
                return builder.compare(comparisons[suffix], left, right, nan_result=prefix == 'FUnord')
            result = builder.vector([compare(left, right) for left, right in zip(parts(value(v[2])), parts(value(v[3])), strict=True)])
        elif op == 'OpSelect':
            condition, yes, no = value(v[2]), value(v[3]), value(v[4])
            result = builder.select(condition, yes, no) if condition.value_type == 'bool' else builder.vector([
                builder.select(c, y, n) for c, y, n in zip(parts(condition), parts(yes), parts(no), strict=True)])
        elif op in {'OpLogicalAnd', 'OpLogicalOr'}:
            result = builder.vector([builder.select(left, right, builder.boolean(False)) if op == 'OpLogicalAnd' else
                builder.select(left, builder.boolean(True), right) for left, right in zip(parts(value(v[2])), parts(value(v[3])), strict=True)])
        elif op == 'OpLogicalNot':
            result = builder.vector([builder.compare('equal', item, builder.boolean(False)) for item in parts(value(v[2]))])
        elif op in {'OpAny', 'OpAll'}:
            samples = parts(value(v[2]))
            result = samples[0]
            for sample in samples[1:]:
                result = (builder.select(result, builder.boolean(True), sample) if op == 'OpAny'
                          else builder.select(result, sample, builder.boolean(False)))
        elif op == 'OpVectorTimesScalar':
            result = builder.vector([builder.operation('multiply', a=part, b=value(v[3])) for part in parts(value(v[2]))])
        elif op == 'OpMatrixTimesVector':
            result = matrix_vector(value(v[2]), value(v[3]))
        elif op == 'OpVectorTimesMatrix':
            result = matrix_vector(value(v[3]), value(v[2]), transpose=True)
        elif op == 'OpDot':
            result = dot(parts(value(v[2])), parts(value(v[3])))
        elif op in {'OpDPdx', 'OpDPdy', 'OpDPdxCoarse', 'OpDPdyCoarse', 'OpDPdxFine', 'OpDPdyFine'}:
            result = builder.operation('derivative_' + ('x' if op.startswith('OpDPdx') else 'y') +
                ('_fine' if op.endswith('Fine') else '' if op.endswith('Coarse') else '_default'), value=value(v[2]))
        elif op == 'OpExtInst':
            if definitions[v[2]].values[1] != 'GLSL.std.450':
                raise VanillaFunctionError('This Vulkan extended instruction set needs its own semantic adapter.')
            if v[3] in unary_ext:
                result = builder.operation(unary_ext[v[3]], value=value(v[4]))
            elif v[3] == 7:  # SSign: comparisons avoid overflow at INT_MIN.
                zero,one,negative = (builder.convert(builder.constant(n),'int') for n in (0,1,-1))
                result = builder.vector([builder.select(builder.compare('less',sample,zero),negative,
                    builder.select(builder.compare('greater',sample,zero),one,zero)) for sample in parts(value(v[4]))])
            elif v[3] == 26:
                result = builder.operation('power', a=value(v[4]), b=value(v[5]))
            elif v[3] in {37, 38, 39, 40, 41, 42, 79, 80}:
                # NMin/NMax (79/80) return the non-NaN operand (HLSL min/max); FMin/FMax (37/40) leave NaN handling to the driver.
                a, b = value(v[4]), value(v[5])
                driver = v[3] in {37, 40} and a.value_type.startswith('float')
                result = builder.operation('minimum' if v[3] in {37, 38, 39, 79} else 'maximum', a=a, b=b,
                                           _properties={'nan_policy': 'driver'} if driver else None)
            elif v[3] in {43, 44, 45, 81}:
                result = builder.operation('clamp', value=value(v[4]), minimum=value(v[5]), maximum=value(v[6]))
            elif v[3] == 49:
                result = builder.operation('smoothstep', minimum=value(v[4]), maximum=value(v[5]), value=value(v[6]))
            elif v[3] == 48:
                edges, samples = parts(value(v[4])), parts(value(v[5]))
                if len(edges) == 1:
                    edges *= len(samples)
                # Kept as step: Step gives 0 for NaN, the spelled-out x < edge ? 0 : 1 gives 1.
                result = builder.vector([builder.operation('step', edge=edge, value=sample) if sample.value_type == 'float'
                    else builder.select(builder.compare('less', sample, edge), builder.constant(0), builder.constant(1))
                    for edge, sample in zip(edges, samples, strict=True)])
            elif v[3] in {66, 67}:
                components = parts(value(v[4]))
                if v[3] == 67:
                    components = [builder.operation('subtract', a=a, b=b)
                                  for a, b in zip(components, parts(value(v[5])), strict=True)]
                result = builder.operation('square_root', value=dot(components, components))
            elif v[3] == 25:  # Atan2(y, x).
                result = builder.operation('arc_tangent2', y=value(v[4]), x=value(v[5]))
            elif v[3] == 18:  # Atan(x) has the same signed-zero/quadrant as Atan2(x, 1).
                result = builder.vector([builder.operation('arc_tangent2', y=item, x=builder.constant(1))
                                         for item in parts(value(v[4]))])
            elif v[3] == 58:  # PackHalf2x16, first component in the low half.
                low, high = (builder.operation('bit_and', a=builder.operation('pack_half', value=item),
                             b=builder.integer(0xffff)) for item in parts(value(v[4])))
                result = builder.operation('bit_or', a=low,
                    b=builder.operation('shift_left', a=high, b=builder.integer(16)))
            elif v[3] == 62:  # UnpackHalf2x16, least-significant half first.
                word = builder.convert(value(v[4]), 'uint', bits=True)
                result = builder.vector([builder.operation('unpack_half', value=word), builder.operation('unpack_half',
                    value=builder.operation('shift_right', a=word, b=builder.integer(16)))])
            elif v[3] == 46:  # FMix, kept as lerp (DXC's FMix).
                # Spelled out, a zero weight on an infinite y is NaN; FMix gives x.
                a, b, weights = parts(value(v[4])), parts(value(v[5])), parts(value(v[6]))
                if len(weights) == 1:
                    weights *= len(a)
                result = builder.vector([builder.operation('lerp', a=x, b=y, factor=w) if x.value_type == 'float'
                    else builder.operation('add',
                        a=builder.operation('multiply', a=x, b=builder.operation('subtract', a=builder.constant(1), b=w)),
                        b=builder.operation('multiply', a=y, b=w)) for x, y, w in zip(a, b, weights, strict=True)])
            elif v[3] == 68:  # Cross.
                a, b = parts(value(v[4])), parts(value(v[5]))
                if len(a) != 3 or len(b) != 3:
                    raise VanillaFunctionError('A Vulkan cross product requires two three-dimensional vectors.')
                result = builder.vector([builder.operation('subtract',
                    a=builder.operation('multiply', a=a[j], b=b[k]), b=builder.operation('multiply', a=a[k], b=b[j]))
                    for j, k in ((1, 2), (2, 0), (0, 1))])
            elif v[3] == 71:  # Reflect(I, N) = I - 2 * dot(N, I) * N.
                incident, normal = parts(value(v[4])), parts(value(v[5]))
                projection = builder.operation('multiply', a=builder.constant(2), b=dot(normal, incident))
                result = builder.vector([builder.operation('subtract', a=i,
                    b=builder.operation('multiply', a=projection, b=n)) for i, n in zip(incident, normal, strict=True)])
            elif v[3] == 72:  # Refract(I, N, eta): eta*I - (eta*dot(N, I) + sqrt(k))*N, zero when k < 0.
                incident, normal = parts(value(v[4])), parts(value(v[5]))
                eta = value(v[6])
                cosine = dot(normal, incident)
                k = builder.operation('subtract', a=builder.constant(1), b=builder.operation('multiply',
                    a=builder.operation('multiply', a=eta, b=eta), b=builder.operation('subtract',
                    a=builder.constant(1), b=builder.operation('multiply', a=cosine, b=cosine))))
                scale = builder.operation('add', a=builder.operation('multiply', a=eta, b=cosine),
                                          b=builder.operation('square_root', value=k))
                refracted = builder.vector([builder.operation('subtract', a=builder.operation('multiply', a=eta, b=i),
                    b=builder.operation('multiply', a=scale, b=n)) for i, n in zip(incident, normal, strict=True)])
                result = builder.select(builder.compare('less', k, builder.constant(0)),
                    builder.vector([builder.constant(0) for _ in incident]), refracted)
            elif v[3] == 33:  # Determinant; the transpose has the same determinant.
                matrix = value(v[4])
                if not isinstance(matrix, _Composite):
                    raise VanillaFunctionError('A Vulkan determinant needs its declared column vectors.')
                columns = [parts(column) for column in matrix.elements]
                if any(len(column) != len(columns) for column in columns):
                    raise VanillaFunctionError('A Vulkan determinant requires a square matrix.')

                def determinant(rows, cols):
                    if len(cols) == 1:
                        return columns[cols[0]][rows[0]]
                    total = None
                    for position, col in enumerate(cols):
                        term = builder.operation('multiply', a=columns[col][rows[0]],
                                                 b=determinant(rows[1:], cols[:position] + cols[position + 1:]))
                        total = term if total is None else builder.operation(
                            'subtract' if position % 2 else 'add', a=total, b=term)
                    return total
                result = determinant(tuple(range(len(columns))), tuple(range(len(columns))))
            elif v[3] in {73, 74, 75}:  # FindILsb, FindSMsb, FindUMsb: bit index, -1 when absent.
                domain, width = shape(row.type_id)
                suffix = str(width) if width > 1 else ''
                operand = builder.convert(value(v[4]), ('int' if v[3] == 74 else 'uint') + suffix, bits=True)
                found = builder.operation('first_bit_low' if v[3] == 73 else 'first_bit_high', value=operand)
                result = builder.convert(found, domain + suffix, bits=True)
            else:
                raise VanillaFunctionError(f'GLSL.std.450 operation {v[3]} needs an editable calculation adapter.')
        elif op == 'OpSampledImage':
            result = value(v[2]), value(v[3])
        elif op == 'OpImage':
            result = value(v[2])[0]
        elif op == 'OpImageFetch':
            texture = value(v[2])
            mask = v[4] if len(v) > 4 else 0
            operand = 'sample_index' if 'ms' in texture.dimension else 'mip_level'
            required = 64 if operand == 'sample_index' else 2
            offset = ()
            if mask == required | 8 and len(v) == 7:  # ConstOffset, after Lod and before Sample.
                offset_id = v[5] if operand == 'sample_index' else v[6]
                declared = definitions[offset_id]
                components = declared.values[2:] if declared.opcode == 44 else (offset_id,)
                words = tuple(module.constant(item) for item in components)
                if any(word is None or shape(definitions[item].type_id) != ('int', 1)
                       for item, word in zip(components, words)):
                    raise VanillaFunctionError('Vulkan ConstOffset requires declared signed integer constants.')
                offset = tuple(word if word < 0x80000000 else word - 0x100000000 for word in words)
            elif len(v) != 6 or mask != required:
                raise VanillaFunctionError('Vulkan texel-read operands need explicit LOD/sample/offset ownership.')
            lod_or_sample = v[6] if offset and operand == 'sample_index' else v[5]
            result = builder.texture_operation('texture_load', texture,
                [builder.convert(item, 'int', bits=True) for item in parts(value(v[3]))],
                offset=offset, **{operand: builder.convert(value(lod_or_sample), 'int', bits=True)})
        elif op.startswith('OpAtomic'):
            domain, width = shape(row.type_id)
            if width != 1 or domain not in {'int', 'uint'}:
                raise VanillaFunctionError('A Vulkan buffer atomic requires a scalar 32-bit integer.')
            result = builder.convert(memory.atomic(row), domain, bits=True)
        elif op in {'OpImageSampleDrefImplicitLod', 'OpImageSampleDrefExplicitLod'}:
            texture, sampler = value(v[2])
            mask = v[5] if len(v) > 5 else 0
            mode = 'compare'
            if op == 'OpImageSampleDrefExplicitLod':
                if len(v) != 7 or mask != 2 or module.constant(v[6]) not in {0, 0x80000000}:
                    raise VanillaFunctionError('Vulkan depth comparison at a nonzero mip needs an explicit comparison-LOD operation.')
                mode = 'compare_level_zero'
            elif mask or len(v) > 6:
                raise VanillaFunctionError('Vulkan depth comparison image operands need explicit sampling ownership.')
            result = image_sample(row.type_id, texture, sampler, coordinates=parts(value(v[3])), mode=mode, comparison=value(v[4]))
        elif op in {'OpImageSampleImplicitLod', 'OpImageSampleExplicitLod'}:
            texture, sampler = value(v[2])
            mask = v[4] if len(v) > 4 else 0
            mode, arguments = 'implicit', {}
            if mask in {1, 2} and len(v) == 6:
                mode = 'bias' if mask == 1 else 'level'
                arguments['mip_bias' if mask == 1 else 'mip_level'] = value(v[5])
            elif mask == 4 and len(v) == 7:
                mode, arguments = 'gradients', {'gradient_x': value(v[5]), 'gradient_y': value(v[6])}
            elif mask or len(v) > 5 or op == 'OpImageSampleExplicitLod':
                raise VanillaFunctionError('These Vulkan image operands need a typed sampling adapter.')
            result = image_sample(row.type_id, texture, sampler, coordinates=parts(value(v[3])), mode=mode, **arguments)
        elif op in {'OpEmitVertex','OpEndPrimitive','OpEmitStreamVertex','OpEndStreamPrimitive'}:
            stream=module.constant(v[0]) if 'Stream' in op and len(v)==1 else 0 if not v else None
            if contract.stage!='geometry' or stream not in contract.primitive_execution.output_streams:
                raise VanillaFunctionError('Vulkan vertex emission requires its declared geometry stream.')
            if op.startswith('OpEmit'):
                from file_handlers.sdf.shader_graph_geometry import vertex_fields
                fields=[(item,field) for item,field in zip(native.outputs,contract.outputs,strict=True) if field.stream==stream]
                arguments={f'field{i}':builder.vector([outputs[item.variable_id,lane] for lane in range(field.mask.bit_count())])
                           for i,(item,field) in enumerate(fields)}
                builder.execute('emit_vertex',properties={'stream':stream,'fields':vertex_fields(contract,stream)},**arguments)
                # SPIR-V output values become undefined after vertex emission.
                outputs.update({(item.variable_id,lane):builder.unassigned(field.value_type.rstrip('234'))
                    for item,field in zip(native.outputs,contract.outputs,strict=True) for lane in range(field.mask.bit_count())})
            else:
                builder.execute('end_primitive',properties={'stream':stream})
            return
        elif op == 'OpKill':
            builder.discard(builder.boolean(True))
            return
        elif op == 'OpReturn':
            return
        else:
            raise VanillaFunctionError(f'{op} needs an editable Vulkan behavior adapter.')
        if row.result is None or result is None:
            raise VanillaFunctionError(f'{op} did not produce its declared result.')
        values[row.result] = result

    from .spirv_control_flow import SpirvControlFlow
    function_rows = module.functions[entries[0].values[1]][1:-1]
    if _hull_phase is not None:
        from .spirv_tessellation import hull_phase_bodies
        bodies = hull_phase_bodies(function_rows)
        body = bodies.control if _hull_phase == 'control_point' else bodies.patch
    else:
        body = function_rows
    flow = SpirvControlFlow(body)
    frames = {}

    def flattened(item):
        if isinstance(item, _Composite):
            return [leaf for child in item.elements for leaf in flattened(child)]
        if not isinstance(item, _Value):
            raise VanillaFunctionError('A Vulkan control-flow value needs a numeric aggregate or explicit object ownership.')
        return [item]

    def restored(template, leaves):
        return (_Composite(tuple(restored(child, leaves) for child in template.elements))
                if isinstance(template, _Composite) else next(leaves))

    def initial_value(type_id, *, unassigned=True):
        if definitions[type_id].opcode not in {20, 21, 22, 23}:
            return _Composite(tuple(initial_value(child, unassigned=unassigned) for child in children(type_id)))
        domain, width = shape(type_id)
        if unassigned:
            return builder.unassigned(_typed(domain, width))
        return builder.vector([builder.boolean(False) if domain == 'bool' else builder.convert(builder.integer(0), domain, bits=True)
                               for _ in range(width)])

    def mutable_keys():
        return [key for key in values if isinstance(key, tuple) and key[0] == 'local']

    def incoming(label, predecessor):
        joined = {}
        for row in flow.phis(label):
            choices = dict(zip(row.values[3::2], row.values[2::2]))
            if predecessor not in choices:
                raise VanillaFunctionError('A Vulkan phi is missing its actual predecessor.')
            joined[row.result] = value(choices[predecessor])
        values.update(joined)

    def loop(header):
        nonlocal values, outputs
        spec = flow.loops[header]
        members = spec['members']
        defined = {r.result for label in members for r in flow.blocks[label] if r.result is not None}
        used = {operand.value for label in flow.reachable - members for r in flow.blocks.get(label, ())
                for operand in r.operands if operand.kind == 'IdRef'}
        exports = [*sorted((defined & used) | {r.result for r in flow.phis(spec['merge'])}), *mutable_keys()]
        frame = dict(node_start=len(builder.graph.nodes), cache=dict(builder.values),
                     values=dict(values), outputs=dict(outputs), states=[], exports=exports)
        frames[header] = frame
        state_values = [(r.result, values[r.result]) for r in flow.phis(header)]
        state_values += [(key, values[key]) for key in mutable_keys()]
        state_values += [(('output', key), item) for key, item in outputs.items()]
        for key, initial in state_values:
            placeholders = []
            for leaf in flattened(initial):
                index = len(frame['states'])
                node = builder._node('function_input', properties=dict(pin_id=f'state_{index}',
                    display_name=f'State {index + 1}', value_type=leaf.value_type))
                state = _Value(node.node_id, 'value', leaf.value_type)
                frame['states'].append((key, leaf, state))
                placeholders.append(state)
            item = restored(initial, iter(placeholders))
            if isinstance(key, tuple) and key[0] == 'output':
                outputs[key[1]] = item
            else:
                values[key] = item
        builder.loop_execution_state(frame)
        frame['update_keys'] = [('update', header, key) for key, _ in state_values]
        frame['state_values'] = state_values
        frame['boundary_keys'] = [('continue', header), *frame['update_keys'], *exports]
        walk(header, ('boundary', header), ready=True, parent=header)
        updates = [leaf for key in frame['update_keys'] for leaf in flattened(values[key])]
        templates = {**{key: values[key] for key in exports}, **{('output', key): item for key, item in outputs.items()}}
        flat_exports = {(key, i): leaf for key, item in templates.items() for i, leaf in enumerate(flattened(item))}
        joined = builder.merge_loop(frame, values['continue', header], updates, flat_exports)
        values, outputs = frame['values'], frame['outputs']
        for key, template in templates.items():
            item = restored(template, iter(joined[key, i] for i in range(len(flattened(template)))))
            if isinstance(key, tuple) and key[0] == 'output':
                outputs[key[1]] = item
            else:
                values[key] = item
        del frames[header]

    def boundary(parent, predecessor, target):
        frame = frames[parent]
        incoming(target, predecessor)
        values['continue', parent] = builder.boolean(target == parent)
        for (key, _), update in zip(frame['state_values'], frame['update_keys'], strict=True):
            values[update] = outputs[key[1]] if isinstance(key, tuple) and key[0] == 'output' else values[key]
        for key in frame['exports']:
            if key not in values:
                values[key] = initial_value(definitions[key].type_id)

    def walk(label, stop, predecessor=None, ready=False, parent=None):
        nonlocal values, outputs
        children_loops, edges, postdominators = flow.context(parent)
        while label != stop:
            if label == flow.end:
                return True
            if isinstance(label, tuple):
                boundary(parent, label[1], label[2])
                label, ready = edges[label][0], True
                continue
            if not ready:
                incoming(label, predecessor)
            if label in children_loops:
                loop(label)
                label, ready = children_loops[label]['merge'], True
                if parent is not None and label in {parent, flow.loops[parent]['merge']}:
                    raise VanillaFunctionError('Coincident Vulkan loop boundaries need explicit edge ownership.')
                continue
            for row in flow.blocks[label]:
                if row.opcode not in {245, 249, 250, 251}:
                    instruction(row)
            targets = edges[label]
            if len(targets) == 1:
                predecessor, label, ready = label, targets[0], False
                continue
            if label in flow.switches:
                selector_id, groups = flow.switches[label]
                selector = value(selector_id)
                if selector.value_type not in {'int', 'uint'}:
                    raise VanillaFunctionError('A Vulkan switch needs a supported scalar integer selector.')
                conditions = []
                for literals in groups:
                    matches = [builder.compare('equal', selector, builder.integer(literal, selector.value_type)) for literal in literals]
                    condition = matches[0]
                    for match in matches[1:]:
                        condition = builder.select(condition, builder.boolean(True), match)
                    conditions.append(condition)
            else:
                conditions = [value(flow.conditions[label])]
            if postdominators[label] is None:
                join = flow.end  # Every path kills: the arms end with the invocation.
            else:
                candidates = postdominators[label] - {label}
                join = max(candidates, key=lambda target: len(postdominators[target]))
            original_values, original_outputs = dict(values), dict(outputs)
            cache, execution = dict(builder.values), builder.execution
            arms = []
            keys = tuple(original_outputs)
            joined_keys = (frames[parent]['boundary_keys'] if join == ('boundary', parent)
                           else [*[r.result for r in flow.phis(join)], *mutable_keys()])
            for target in targets:
                values, outputs = dict(original_values), dict(original_outputs)
                builder.values, builder.execution = dict(cache), execution
                arm_before = {node.node_id for node in builder.graph.nodes}
                killed = walk(target, join, label, parent=parent)
                results = [values[key] if key in values or not killed else initial_value(definitions[key].type_id)
                           for key in joined_keys] + [outputs[key] for key in keys]
                arms.append(({n.node_id for n in builder.graph.nodes} - arm_before, results, builder.execution))
            values, outputs = original_values, original_outputs
            builder.values = cache
            merged_nodes, merged, merged_execution = arms[-1]
            steps = tuple(zip(conditions, arms[:-1], strict=True))
            for index in reversed(range(len(steps))):
                condition, (arm_nodes, arm_values, arm_execution) = steps[index]
                before = {node.node_id for node in builder.graph.nodes} if index else set()
                merged_leaves = iter(builder.merge_branch(condition, arm_nodes, merged_nodes,
                    [leaf for item in arm_values for leaf in flattened(item)],
                    [leaf for item in merged for leaf in flattened(item)], executions=(arm_execution, merged_execution)))
                merged = [restored(template, merged_leaves) for template in arm_values]
                if index:
                    current = {node.node_id for node in builder.graph.nodes}
                    merged_nodes = ((merged_nodes | arm_nodes) & current) | (current - before)
                merged_execution = builder.execution
            phi_count = len(joined_keys)
            values.update(zip(joined_keys, merged[:phi_count]))
            outputs.update(zip(keys, merged[phi_count:]))
            label, ready = join, True
        if not ready and label != flow.end:
            incoming(label, predecessor)
        return False

    for row in ir.instructions:
        if row.opcode == 59 and row.values[2] == 6:
            values[local_key(row.result)] = value(row.values[3]) if len(row.values) == 4 else initial_value(definitions[row.type_id].values[2])
    walk(flow.start, flow.end)
    if contract.stage == 'geometry':
        return builder.finish(name, [])
    declared = []
    for field in (contract.patch_constants if _hull_phase == 'patch' else contract.outputs):
        entry = field_lanes.get(field)
        if entry is None:
            raise VanillaFunctionError('A recovered Vulkan output has no declared interface row.')
        declared.append((field, [outputs[entry[0], lane] for lane in entry[1]]))
    return builder.finish(name, declared)
