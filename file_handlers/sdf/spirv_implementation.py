from dataclasses import replace

from .dxc import compile_hlsl_dxc
from .shader_graph import ShaderGraphError, _identifier
from .spirv import reflect_spirv
from .spirv_contract import (
    _BUILTINS, _PATCH_BUILTINS, _PATCH_DECORATION, patch_interface_ids, spirv_interface_rows, spirv_shader_contract,
)
from .spirv_execution import vulkan_execution
from .spirv_ir import SpirvSemanticIr, SpirvBuilder
from .spirv_tools import validate_spirv


def complete_vulkan_contract(contract, bytecode):
    ir = SpirvSemanticIr.from_bytes(bytecode)
    native = reflect_spirv(bytecode)
    if [r.values for r in ir.instructions if r.opcode == 14] != [(0, 1)]:
        raise ShaderGraphError('Vulkan behavior needs its declared addressing/memory-model adapter.')
    interfaces = {item.variable_id for item in (*native.inputs, *native.outputs)}
    if any(r.opcode == 71 and r.values[0] in interfaces and r.values[1] in {31, 32} for r in ir.instructions):
        raise ShaderGraphError('Packed/dual-source Vulkan interfaces need an explicit linkage adapter.')
    execution = vulkan_execution(native, contract)
    if contract.vulkan_execution is not None and execution != contract.vulkan_execution:
        raise ShaderGraphError('The native Vulkan linkage differs from its declared renderer ABI.')
    return replace(contract, vulkan_execution=execution, thread_group_size=native.thread_group_size)


def validate_vulkan_linkage(reference, candidate):
    if candidate is None:
        return ['The replacement has no Vulkan execution/linkage declaration.']
    errors = []
    for field in ('version', 'early_fragment_tests', 'depth_mode'):
        if getattr(reference, field) != getattr(candidate, field):
            errors.append(f'The replacement changed Vulkan {field}.')
    expected = {b.name: b for b in reference.bindings}
    for binding in candidate.bindings:
        if expected.get(binding.name) != binding:
            errors.append(f'The replacement changed Vulkan descriptor {binding.name}.')
    return errors


def _complete_declared_inputs(ir, contract, candidate):
    if contract.stage not in {'vertex', 'hull', 'domain'}:
        return ir
    declared = list(contract.inputs)
    if contract.stage == 'domain':
        declared.extend(contract.patch_constants)
    present = {field.key for field in (*candidate.inputs, *candidate.patch_constants)}
    patch_builtins = {_BUILTINS[built_in][1]: built_in for built_in in _PATCH_BUILTINS}
    missing = [field for field in declared if field.key not in present
               and (field.register != 0xffffffff or field.system_value in patch_builtins)]
    if not missing:
        return ir
    writer = SpirvBuilder(ir)
    types = {(row.opcode, row.values[1:]): row.result for row in ir.instructions if row.name.startswith('OpType')}
    declarations, names, decorations, variables = [], [], [], []
    def type_id(opcode, *values):
        key = (opcode, values)
        if key not in types:
            types[key] = writer.id()
            declarations.append(writer.instruction(opcode, types[key], *values))
        return types[key]
    def constant_id(value):
        key = ('constant', value)
        if key not in types:
            types[key] = writer.id()
            declarations.append(writer.instruction(43, type_id(21, 32, 0), types[key], value))
        return types[key]
    def scalar_id(field):
        if field.component_type not in {'float32', 'uint32', 'sint32'}:
            raise ShaderGraphError('An unused interface slot needs an explicit scalar/vector linkage type.')
        if field.component_type == 'float32':
            return type_id(22, 32)
        return type_id(21, 32, int(field.component_type == 'sint32'))

    def declare(field, value_type, name):
        pointer = type_id(32, 1, value_type)
        variable = writer.id()
        variables.append(variable)
        declarations.append(writer.instruction(59, pointer, variable, 1))
        names.append(writer.instruction(5, variable, name))
        built_in = patch_builtins.get(field.system_value)
        if built_in is not None:
            decorations.append(writer.instruction(71, variable, _PATCH_DECORATION))
            decorations.append(writer.instruction(71, variable, 11, built_in))
        else:
            decorations.append(writer.instruction(71, variable, 30, field.register))
        return variable

    grouped = {}
    for field in missing:
        if field.system_value in patch_builtins:
            grouped.setdefault(field.system_value, []).append(field)
    for rows_for_builtin in grouped.values():
        field = rows_for_builtin[0]
        elements = sum(max(1, int(row.array_length)) for row in rows_for_builtin)
        declare(field, type_id(28, scalar_id(field), constant_id(elements)),
                f'in.var.{field.semantic_name}')
    for field in missing:
        if field.system_value in patch_builtins:
            continue
        scalar = scalar_id(field)
        width = int(field.mask).bit_count()
        value_type = type_id(23, scalar, width) if width > 1 else scalar
        axis = (contract.primitive_execution.input_control_points
                if contract.stage in {'hull', 'domain'} else field.array_length)
        if axis != 1:
            value_type = type_id(28, value_type, constant_id(axis))
        declare(field, value_type, f'in.var.{field.semantic_name}{field.semantic_index}')
    rows = list(ir.instructions)
    for index, row in enumerate(rows):
        if row.opcode == 15:  # OpEntryPoint: the module's entry interface.
            rows[index] = writer.instruction(15, *row.values, *variables)
    type_start=next(i for i,row in enumerate(rows) if row.name.startswith('OpType'))
    annotation_start=next((i for i,row in enumerate(rows[:type_start]) if row.opcode in {71,72,73,74,75,332}),type_start)
    rows[type_start:type_start]=decorations
    rows[annotation_start:annotation_start]=names
    function_start=next(i for i,row in enumerate(rows) if row.opcode==54)
    rows[function_start:function_start]=declarations
    return replace(ir,instructions=tuple(rows))


def _drop_undeclared_descriptors(bytecode, declared):
    native = reflect_spirv(bytecode)
    ir = SpirvSemanticIr.from_bytes(bytecode)
    annotations = {5, 6, 71, 72}  # OpName, OpMemberName, OpDecorate, OpMemberDecorate
    candidates = {item.variable_id for item in native.descriptors if item.name not in declared}
    for row in ir.instructions:
        if row.opcode not in annotations and row.result not in candidates:
            candidates -= {operand.value for operand in row.operands if operand.kind == 'IdRef'}
    if not candidates:
        return bytecode
    rows = tuple(row for row in ir.instructions if row.result not in candidates
                 and not (row.opcode in annotations and row.values and row.values[0] in candidates))
    return replace(ir, instructions=rows).write()


def compile_vulkan_implementation(generated, contract):
    from file_handlers.mmtr.material_implementation import CompiledImplementation
    execution = contract.vulkan_execution
    if execution is None:
        raise ShaderGraphError('A Vulkan implementation requires explicit native descriptor linkage.')
    arguments = ['-fspv-target-env=' + ('vulkan1.0' if execution.version == 0x10000 else 'vulkan1.1'),
                 '-fspv-preserve-bindings']
    result = compile_hlsl_dxc(generated.source, entry_point=generated.entry_point, target=generated.target,
                             spirv=True, extra_arguments=tuple(arguments))
    compiled = _drop_undeclared_descriptors(result.bytecode, {_identifier(b.name) for b in execution.bindings})
    native = reflect_spirv(compiled)
    candidate = spirv_shader_contract(compiled)
    ir = SpirvSemanticIr.from_bytes(compiled)
    complete=_complete_declared_inputs(ir,contract,candidate)
    if complete is not ir:
        ir = complete
        module = ir.write()
        native, candidate = reflect_spirv(module), spirv_shader_contract(module)
    writer = SpirvBuilder(ir)
    locations = {}
    decorations = {}
    for row in ir.instructions:
        if row.opcode == 71:
            decorations.setdefault(row.values[0], []).append(row.values[1:])
    patch_ids = patch_interface_ids(native, decorations)
    input_rows, output_rows, _undeclared = spirv_interface_rows(native, candidate, patch_ids)
    for items, rows, originals in ((native.inputs, input_rows, contract.inputs),
                                   (native.outputs, output_rows, contract.outputs)):
        expected = {f.key: f for f in (*originals, *contract.patch_constants)}
        for item in items:
            for field in rows.get(item.variable_id, ()):
                original = expected.get(field.key)
                if original is None:
                    raise ShaderGraphError(f'Compiled Vulkan interface {field.display_name} is not declared.')
                if item.built_in is not None:
                    continue
                if original.register == 0xffffffff:
                    raise ShaderGraphError('A Vulkan built-in became an ordinary varying.')
                locations[item.variable_id] = original.register
    rows = tuple(writer.instruction(71, r.values[0], 30, locations[r.values[0]])
                 if r.opcode == 71 and r.values[1] == 30 and r.values[0] in locations else r for r in ir.instructions)
    bytecode = replace(ir, instructions=rows).write()
    validate_spirv(bytecode)
    native = reflect_spirv(bytecode)
    candidate = spirv_shader_contract(bytecode)
    expected = {_identifier(b.name): b for b in execution.bindings}
    if len(expected) != len(execution.bindings):
        raise ShaderGraphError('Vulkan resource names collide after HLSL identifier conversion.')
    def rebound(item):
        binding = expected.get(item.name)
        if binding is None:
            raise ShaderGraphError(f'Compiled Vulkan resource {item.name} is not declared.')
        return replace(item, name=binding.name, bind_point=binding.slot, register_space=binding.space)
    candidate = replace(candidate, resources=tuple(rebound(r) for r in candidate.resources),
        constant_buffers=tuple(rebound(b) for b in candidate.constant_buffers),
        structured_buffers=tuple(rebound(b) for b in candidate.structured_buffers), thread_group_size=native.thread_group_size)
    native = replace(native, descriptors=tuple(replace(d, name=expected[d.name].name) for d in native.descriptors))
    candidate = replace(candidate, vulkan_execution=vulkan_execution(native, candidate))
    issues = validate_vulkan_linkage(execution, candidate.vulkan_execution)
    if issues:
        raise ShaderGraphError('\n'.join(issues))
    return CompiledImplementation(bytecode, candidate, 'spirv-module', result.diagnostics.rstrip('\0').strip())


def relink_vertex_input_ids(bytecode, native, signature):
    declared={item.location:item for item in signature.elements}
    reflected={item.location:item for item in native.inputs if item.built_in is None}
    if declared.keys()!=reflected.keys():
        raise ShaderGraphError('Replacement vertex inputs differ from the compact SDF linkage.')
    mapping={}
    for location,element in declared.items():
        item=reflected[location]
        domain={'unsigned-integer':'uint','floating-point':'float'}[element.scalar_type]
        expected=domain+(str(element.component_count) if element.component_count>1 else '')
        if item.value_type!=expected:
            raise ShaderGraphError('Replacement vertex input type differs from the compact SDF linkage.')
        mapping[item.variable_id]=element.variable_id
    if all(source==target for source,target in mapping.items()):
        return bytecode
    ir=SpirvSemanticIr.from_bytes(bytecode)
    identities={row.result for row in ir.instructions if row.result is not None}
    next_id=max(identities | set(mapping.values()),default=0)+1
    for occupied in sorted(set(mapping.values()) & identities - mapping.keys()):
        mapping[occupied]=next_id
        next_id+=1
    result=replace(ir,instructions=tuple(row.remap(mapping) for row in ir.instructions)).write()
    validate_spirv(result)
    return result
