from file_handlers.sdf.shader_graph import ShaderContractResource, ShaderGraphError
import re
from dataclasses import replace

from .profiles.re_engine_dxil_recovery import LLVM_NAME


DXIL_RESOURCE_CLASSES = {0: 't', 1: 'u', 2: 'b', 3: 's'}
DXIL_RESOURCE_CLASS_NUMBERS = {name: number for number, name in DXIL_RESOURCE_CLASSES.items()}
# The low byte of ``annotateHandle``'s first property word.
DXIL_TEXTURE_KINDS = {1: 'texture1d', 2: 'texture2d', 3: 'texture2d-ms', 4: 'texture3d',
                      5: 'texture-cube', 6: 'texture1d-array', 7: 'texture2d-array',
                      8: 'texture2d-ms-array', 9: 'texture-cube-array'}


def complete_typed_resources(contract, metadata, lines):
    identifier = '%' + LLVM_NAME
    types = {match[1]: match[2] for line in lines
             if (match := re.fullmatch('(' + identifier + r') = type \{\s*(.*?)\s*\}', line.strip()))}
    root = metadata.operands[3]
    if root == 'null':
        roots = metadata.definitions.get('!dx.resources', ())
        root = roots[0] if len(roots) == 1 else 'null'
    groups = metadata.definitions.get(root, ())
    updates = {}
    for register_class, group in zip(('t','u'), groups[:2]):
        for identity in metadata.definitions.get(group, ()):
            row = metadata.definitions[identity]
            binding = register_class, int(row[3][4:]), int(row[4][4:])
            resource = next((r for r in contract.resources if (r.register_class,r.register_space,r.bind_point) == binding), None)
            if resource is None or resource.input_type not in {'texture','rw-typed'}:
                continue
            pointer = re.fullmatch('(' + identifier + r')\* (?:undef|null)', row[1])
            if pointer and types.get(pointer[1]) == 'i64':
                if resource.return_type not in {'uint','sint','uint64','sint64'} or resource.flags & 12:
                    raise ShaderGraphError('A 64-bit texture declaration disagrees with its reflected scalar integer shape.')
                updates[binding] = replace(resource,return_type=resource.return_type.removesuffix('64')+'64')
    return replace(contract, resources=tuple(updates.get((r.register_class,r.register_space,r.bind_point),r) for r in contract.resources)) if updates else contract


def _resource_lists(metadata):
    definitions = metadata.definitions
    root = metadata.operands[3]
    if root == 'null':
        roots = definitions.get('!dx.resources', ())
        if not roots:
            return None
        if len(roots) != 1:
            raise ShaderGraphError('DXIL resource metadata has multiple roots.')
        root = roots[0]
    groups = definitions.get(root, ())
    if len(groups) != 4:
        raise ShaderGraphError('DXIL resource metadata must have four resource-class lists.')
    return groups


def coherent_uav_bindings(metadata):
    definitions, groups = metadata.definitions, _resource_lists(metadata)
    if groups is None:
        return frozenset()
    coherent = set()
    for identity in definitions.get(groups[1], ()):
        row = definitions.get(identity, ())
        if len(row) != 11 or row[7] not in {'i1 false', 'i1 true'} or any(not re.fullmatch(r'i32 \d+', row[i]) for i in (3,4)):
            raise ShaderGraphError('DXIL UAV binding/coherence metadata is invalid.')
        if row[7] == 'i1 true':
            coherent.add((int(row[3][4:]), int(row[4][4:])))
    return frozenset(coherent)


def native_constant_buffer_extents(metadata):
    definitions, groups = metadata.definitions, _resource_lists(metadata)
    if groups is None:
        return {}
    extents = {}
    group = groups[2]
    if group != 'null' and group not in definitions:
        raise ShaderGraphError('DXIL constant-buffer metadata list is missing.')
    for identity in definitions.get(group, ()):
        row = definitions.get(identity, ())
        if len(row) != 8 or any(not re.fullmatch(r'i32 \d+', row[index]) for index in (3, 4, 6)):
            raise ShaderGraphError('DXIL constant-buffer binding/extent metadata is invalid.')
        binding = int(row[3][4:]), int(row[4][4:])
        extent = int(row[6][4:])
        if binding in extents or any(value > 0xffffffff for value in (*binding, extent)):
            raise ShaderGraphError('DXIL constant-buffer binding/extent metadata is invalid.')
        extents[binding] = extent
    return extents


def heap_resource(kind, layout, *, sampler_heap):
    resource = None
    writable = kind & 4096 and kind ^ 4096 in {1, 2, 4, 6, 7}
    texture_kind = kind ^ 4096 if writable else kind
    if texture_kind in DXIL_TEXTURE_KINDS:
        component, width, samples = layout & 255, (layout >> 8) & 255, layout >> 16
        result = {4: 'sint', 5: 'uint', 9: 'float'}.get(component)
        if result and 1 <= width <= 4 and (0 <= samples <= 32 if texture_kind in {3, 8} else samples == 0):
            resource = ShaderContractResource('Descriptor', 'rw-typed' if writable else 'texture', result, DXIL_TEXTURE_KINDS[texture_kind], samples, 0, 1, (width - 1) << 2)
    elif kind == 11 and layout == 0:
        resource = ShaderContractResource('Descriptor', 'byte-address-buffer', 'mixed', 'buffer', 0, 0, 1, 0)
    elif kind == 4107 and layout == 0:
        resource = ShaderContractResource('Descriptor', 'rw-byte-address-buffer', 'mixed', 'buffer', 0, 0, 1, 0)
    elif kind & ~0xf00 in {12, 4108}:
        # Structured annotations carry log2(alignment) apart from the stride; DXC emits 2 for 32-bit and 3 for 64-bit
        # fields.
        alignment = (kind >> 8) & 15
        if alignment in {0, 2, 3} and 4 <= layout <= 2048 and layout % max(4, 1 << alignment) == 0:
            resource = ShaderContractResource('Descriptor', 'rw-structured-buffer' if kind & 4096 else 'structured-buffer', 'mixed', 'buffer', layout, 0, 1, 0)
    elif kind in {14, 32782} and layout == 0:
        resource = ShaderContractResource('Descriptor', 'sampler', 'unknown', 'unknown', 0, 0, 1, 2 if kind & 32768 else 0)
    elif kind == 13 and 4 <= layout <= 65536 and layout % 4 == 0:
        resource = ShaderContractResource('Descriptor', 'constant-buffer', 'mixed', 'buffer', layout, 0, 1, 0)
    if resource is None:
        raise ShaderGraphError(f'Descriptor properties ({kind}, {layout}) need an additional typed resource adapter.')
    if (resource.input_type == 'sampler') != sampler_heap:
        raise ShaderGraphError('The descriptor type does not belong to the selected heap.')
    return resource


def validate_resource_properties(resource, kind, layout, contract, *, native_cbuffer_extents):
    coherent = bool(kind & 16384)
    if coherent != bool(getattr(resource, 'globally_coherent', False)):
        raise ShaderGraphError('Native handle and binding coherence declarations disagree.')
    kind &= ~16384
    if resource.input_type == 'constant-buffer':
        binding = resource.register_space, resource.bind_point
        buffer = next((buffer for buffer in contract.constant_buffers if
            (buffer.register_space, buffer.bind_point) == binding), None)
        extent = native_cbuffer_extents.get(binding)
        if (buffer is None or extent is None or (kind, layout) != (13, extent)
                or extent > buffer.byte_size
                or any(variable.byte_offset < 0 or variable.byte_size < 0
                       or variable.byte_offset + variable.byte_size > extent
                       for variable in buffer.variables)):
            raise ShaderGraphError('The native constant-buffer extent disagrees with reflection.')
        return
    try:
        decoded = heap_resource(kind, layout, sampler_heap=resource.input_type == 'sampler')
    except ShaderGraphError as exc:
        raise ShaderGraphError(f'The native handle properties disagree with the supported resource type: {exc}') from exc
    if resource.return_type in {'uint64','sint64'} and decoded.return_type == resource.return_type.removesuffix('64'):
        decoded = replace(decoded, return_type=resource.return_type)
    if decoded.input_type != resource.input_type or decoded.object_type != resource.object_type or (
            resource.input_type == 'sampler' and decoded.flags != resource.flags & 2):
        raise ShaderGraphError('The native handle properties disagree with the reflected resource type.')
