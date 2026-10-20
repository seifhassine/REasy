from dataclasses import replace
import hashlib
import re

lazy from .shader_graph import (ShaderContract, ShaderContractBuffer, ShaderContractResource,
                           ShaderContractVariable, ShaderSignatureField, _identifier)
lazy from .spirv import SpirvError, reflect_spirv
lazy from .spirv_execution import vulkan_primitive_execution
lazy from .spirv_ir import SpirvSemanticIr


_BUILTINS = {
    0: ("SV_Position", "position"), 7: ("SV_PrimitiveID", "primitive-id"),
    1: ("PSIZE", "point-size"), 3: ("SV_ClipDistance", "clip-distance"),
    4: ("SV_CullDistance", "cull-distance"), 9: ("SV_RenderTargetArrayIndex", "render-target-array-index"),
    10: ("SV_ViewportArrayIndex", "viewport-array-index"),
    11: ("SV_TessFactor", "tess-factor"), 12: ("SV_InsideTessFactor", "inside-tess-factor"),
    8: ("SV_OutputControlPointID", "output-control-point-id"),
    13: ("SV_DomainLocation", "domain-location"), 15: ("SV_Position", "position"),
    17: ("SV_IsFrontFace", "front-face"), 18: ("SV_SampleIndex", "sample-index"),
    20: ("SV_Coverage", "coverage"), 22: ("SV_Depth", "depth"),
    26: ("SV_GroupID", "group-id"), 27: ("SV_GroupThreadID", "group-thread-id"),
    28: ("SV_DispatchThreadID", "dispatch-thread-id"), 29: ("SV_GroupIndex", "group-index"),
    42: ("SV_VertexID", "vertex-id"), 43: ("SV_InstanceID", "instance-id"),
    4424: ("SPV_BaseVertex", "base-vertex"), 4425: ("SPV_BaseInstance", "base-instance"),
    4426: ("SPV_DrawIndex", "draw-index"),
}


_PATCH_BUILTINS = frozenset({11, 12})
_DOMAIN_LOCATION_BUILTIN = 13
_PATCH_DECORATION = 15


def patch_interface_ids(native, decorations):
    return {item.variable_id for item in (*native.inputs, *native.outputs)
            if item.built_in in _PATCH_BUILTINS
            or (item.storage_class == 'input'
                and any(row[0] == _PATCH_DECORATION for row in decorations.get(item.variable_id, ())))}


def patch_element_count(item):
    axes = [int(token[:-1]) for token in item.value_type.split('[')[1:] if token.endswith(']')]
    return axes[0] if axes else 1


def spirv_interface_rows(native, contract, patch_ids):
    input_rows, output_rows, undeclared = {}, {}, set()
    patch_index = 0
    for items, control, target in ((native.inputs, contract.inputs, input_rows),
                                   (native.outputs, contract.outputs, output_rows)):
        index = 0
        for item in items:
            if item.built_in == _DOMAIN_LOCATION_BUILTIN and native.stage == 'domain':
                undeclared.add(item.variable_id)
                continue
            if item.variable_id in patch_ids:
                count = patch_element_count(item)
                if patch_index + count > len(contract.patch_constants):
                    raise SpirvError('The Vulkan interface declares undeclared patch constants.')
                target[item.variable_id] = tuple(contract.patch_constants[patch_index:patch_index + count])
                patch_index += count
                continue
            if index >= len(control):
                raise SpirvError('The Vulkan interface declares an undeclared attribute.')
            target[item.variable_id] = (control[index],)
            index += 1
        if index != len(control):
            raise SpirvError('The declared Vulkan contract has unmatched attributes.')
    if patch_index != len(contract.patch_constants):
        raise SpirvError('The declared Vulkan contract has unmatched patch constants.')
    return input_rows, output_rows, undeclared


def _member(member, path):
    declaration = member.value_type
    structure = _identifier(path) if member.members else ""
    children = tuple(_member(child, path + "_" + child.name) for child in member.members)
    if children:
        declaration = structure
    elif member.matrix_layout:
        match = re.fullmatch(r"(float|int|uint)([1-4])x([1-4])", declaration)
        if match is None:
            raise SpirvError(f"Unsupported matrix type {declaration}")
        major = "row_major" if member.matrix_layout == "column-major" else "column_major"
        declaration = f"{major} {match[1]}{match[3]}x{match[2]}"
    graph_type = declaration if re.fullmatch(r"(?:float|int|uint)[1-4]?", declaration) else None
    return ShaderContractVariable(member.name, member.byte_offset, member.value_byte_size,
                                  declaration, graph_type, member.array_count, bool(member.matrix_layout), structure, children)


def spirv_shader_contract(module, *, entry_point=None):
    native = reflect_spirv(module, entry_point=entry_point)
    ir = SpirvSemanticIr.from_bytes(module)
    definitions = {row.result: row for row in ir.instructions if row.result is not None}
    decorations = {}
    for row in ir.instructions:
        if row.opcode == 71:
            decorations.setdefault(row.values[0], []).append(row.values[1:])
    patch_ids = patch_interface_ids(native, decorations)

    def root(value):
        row = definitions.get(value)
        if row is not None and row.opcode in {61, 65, 66, 83}:
            return root(row.values[2])
        return value

    comparison = set()
    for row in ir.instructions:
        if row.opcode in {89, 90, 93, 94, 97}:  # Dref sampling / gather.
            sampled = definitions.get(row.values[2])
            if sampled is not None and sampled.opcode == 86:
                comparison.add(root(sampled.values[3]))

    streams = {row.values[0]:row.values[2] for row in ir.instructions if row.opcode==71 and row.values[1]==29}
    primitive = vulkan_primitive_execution(native, tuple(sorted({streams.get(item.variable_id,0) for item in native.outputs})) or (0,))

    def fields(items, storage):
        result = []
        for item in items:
            base_type = item.value_type.split("[", 1)[0]
            match = re.fullmatch(r"(float|int|uint|bool)([1-4]?)", base_type)
            if match is None:
                raise SpirvError(f"SPIR-V interface {item.name} has unsupported type {item.value_type}")
            width = int(match[2] or 1)
            if item.built_in is not None:
                if item.built_in not in _BUILTINS:
                    raise SpirvError(f"SPIR-V interface built-in {item.built_in} has no graph semantic")
                semantic, system = _BUILTINS[item.built_in]
                if item.built_in==8 and native.stage=='geometry':
                    semantic, system = 'SV_GSInstanceID', 'geometry-instance-id'
                index, register = 0, 0xFFFFFFFF
            else:
                name = item.name.rsplit(".", 1)[-1]
                token = re.fullmatch(r"(.+?)(\d*)", name)
                semantic, index = token[1], int(token[2] or 0)
                system, register = "undefined", int(item.location)
            component = {"float": "float32", "int": "sint32", "uint": "uint32", "bool": "uint32"}[match[1]]
            qualifiers = {row[0] for row in decorations.get(item.variable_id, ())}
            axes = re.findall(r"\[(\d+)\]", item.value_type)
            if len(axes) > 1:
                raise SpirvError(f"SPIR-V graph interface {item.name} has multiple array axes")
            patch = item.variable_id in patch_ids
            if patch and axes:
                for element in range(int(axes[0])):
                    result.append((ShaderSignatureField(semantic, element, register, component, (1 << width) - 1,
                        (1 << width) - 1, system, stream=streams.get(item.variable_id, 0)), True))
                continue
            if storage == 'input' and axes and native.stage in {'hull', 'domain', 'geometry'}:
                if native.stage == 'geometry' and int(axes[0]) != primitive.primitive_vertices:
                    raise SpirvError('Vulkan geometry input array differs from its declared primitive extent.')
                axes = []
            if storage == 'output' and axes and native.stage == 'hull':
                if int(axes[0]) != primitive.output_control_points:
                    raise SpirvError('Vulkan hull output array differs from its declared control-point count.')
                axes = []
            mode ="constant" if 14 in qualifiers else "linear" + (" noperspective" if 13 in qualifiers else "")
            if 16 in qualifiers: mode += " centroid"
            if 17 in qualifiers: mode += " sample"
            result.append((ShaderSignatureField(semantic, index, register, component, (1 << width) - 1,
                                              (1 << width) - 1, system, stream=streams.get(item.variable_id,0), array_length=int(axes[0]) if axes else 1,
                                              interpolation=mode if item.storage_class == 'input' and native.stage == 'pixel' else ''), patch))
        return tuple(result)

    resources, buffers, structured, slots = [], [], [], {}
    for descriptor in native.descriptors:
        register = descriptor.register_class
        slot = slots.get(register, 0)
        slots[register] = slot + 1
        kind, stride = descriptor.descriptor_kind, 0
        if kind == "constant-buffer":
            buffers.append(ShaderContractBuffer(descriptor.name, descriptor.byte_size, slot, descriptor.array_count,
                           tuple(_member(v, descriptor.name + "_" + v.name) for v in descriptor.members)))
        elif kind == "storage-buffer":
            if len(descriptor.members) != 1 or descriptor.members[0].array_count != 0:
                raise SpirvError(f"Storage buffer {descriptor.name} has no runtime element array")
            element = descriptor.members[0]
            stride = element.array_stride
            kind = "structured-buffer" if register == "t" else "rw-structured-buffer"
            variables = (tuple(_member(v, descriptor.name + "_" + v.name) for v in element.members)
                         if element.members else (replace(_member(element, descriptor.name), name="$Element",
                                                          byte_offset=0, byte_size=stride, array_length=1),))
            structured.append(ShaderContractBuffer(descriptor.name, stride, slot, descriptor.array_count, variables))
        elif kind == "storage-image":
            kind = "rw-typed"
        elif kind not in {"texture", "sampler"}:
            raise SpirvError(f"Unsupported SPIR-V descriptor {descriptor.name}: {kind}")
        # OpTypeImage declares multisampling, not a sample count (UINT_MAX = unspecified).
        sample_count = 0xFFFFFFFF if kind == 'texture' else stride
        flags = (12 if kind in {'texture', 'rw-typed'} else 0) | (2 if descriptor.variable_id in comparison else 0)
        resources.append(ShaderContractResource(descriptor.name, kind, 'sint' if descriptor.return_type == 'int' else descriptor.return_type, descriptor.dimension,
                         sample_count, slot, descriptor.array_count, flags))
    inputs = tuple(row for row in fields(native.inputs, 'input') if row[0].system_value != 'domain-location')
    outputs = fields(native.outputs, 'output')
    return ShaderContract(native.stage, 6, 0, tuple(field for field, _patch in inputs if not _patch),
                          tuple(field for field, _patch in outputs if not _patch), tuple(resources),
                          tuple(buffers), hashlib.sha256(module).hexdigest(), tuple(structured),
                          tuple(field for field, _patch in (*inputs, *outputs) if _patch), primitive_execution=primitive)
