lazy from .profiles.re_engine_spirv_authoring import _UniformLayout, _UniformOffset
from .vanilla_function import _ResourceHandle, VanillaFunctionError


class SpirvBufferMemory:
    def __init__(self, module, builder, resources, value):
        self.module, self.builder, self.resources, self.value = module, builder, resources, value

    def add(self, left, right):
        if isinstance(left, int) and isinstance(right, int):
            return left + right
        if left == 0:
            return right
        if right == 0:
            return left
        return self.builder.operation('add', a=self.builder.integer(left) if isinstance(left, int) else left,
                                      b=self.builder.integer(right) if isinstance(right, int) else right)

    def path(self, pointer):
        row = self.module.definitions[pointer]
        if row.opcode == 83:
            return self.path(row.values[2])
        if row.opcode in {65, 66}:
            root, indices = self.path(row.values[2])
            return root, (*indices, *row.values[3:])
        if row.opcode == 59:
            return pointer, ()
        raise VanillaFunctionError('This Vulkan pointer needs its own address provenance.')

    def address(self, pointer):
        module, b = self.module, self.builder
        root, indices = self.path(pointer)
        resource = self.resources.get(root)
        if resource is None or resource.input_type not in {'structured-buffer', 'rw-structured-buffer'}:
            return None
        declarations = module.decorations.get(root, ())
        if (21,) in declarations:
            raise VanillaFunctionError('Volatile Vulkan buffer accesses need an explicit memory-policy adapter.')
        resource = _ResourceHandle(resource, globally_coherent=(23,) in declarations)
        type_id, index, offset, layout = module.pointee(root), None, 0, _UniformLayout()
        for identity in indices:
            declaration = module.definitions[type_id]
            if declaration.opcode == 29:
                stride = next((d[1] for d in module.decorations.get(type_id, ()) if d[0] == 6), None)
                if index is not None or offset != 0 or stride != resource.sample_count:
                    raise VanillaFunctionError('A Vulkan runtime buffer array disagrees with its declared record stride.')
                index = b.convert(self.value(identity), 'uint', bits=True)
                type_id = declaration.values[1]
                continue
            constant = module.constant(identity)
            child, base, stride, next_layout = module.uniform_element(type_id, constant, layout)
            offset = self.add(offset, base)
            if stride:
                if constant is not None:
                    if not 0 <= constant < module.composite_count(type_id):
                        raise VanillaFunctionError('A Vulkan buffer index exceeds its declared member axis.')
                    increment = constant * stride
                else:
                    increment = b.operation('multiply', a=b.convert(self.value(identity), 'uint', bits=True), b=b.integer(stride))
                offset = self.add(offset, increment)
            type_id, layout = child, next_layout
        if index is None or type_id != module.pointee(pointer):
            raise VanillaFunctionError('A Vulkan buffer pointer has no complete typed record address.')
        return resource, index, offset, layout

    def leaves(self, pointer, type_id):
        address = self.address(pointer)
        if address is None:
            return None
        resource, index, offset, layout = address
        return [(path, scalar, resource, index, self.add(offset, part.constant))
                for path, scalar, part in self.module.uniform_leaves(type_id, _UniformOffset(), layout)]

    def atomic(self, row):
        v, b = row.values, self.builder
        operations = {'OpAtomicIAdd': 'add', 'OpAtomicAnd': 'and', 'OpAtomicOr': 'or', 'OpAtomicXor': 'xor',
            'OpAtomicExchange': 'exchange', 'OpAtomicSMin': 'minimum', 'OpAtomicUMin': 'minimum',
            'OpAtomicSMax': 'maximum', 'OpAtomicUMax': 'maximum'}
        if row.name not in operations:
            raise VanillaFunctionError(f'{row.name} needs an explicit atomic ordering adapter.')
        if len(v) != 6 or self.module.constant(v[3]) != 1 or self.module.constant(v[4]) != 0:
            raise VanillaFunctionError('This Vulkan atomic scope/ordering needs its own memory-policy adapter.')
        address = self.address(v[2])
        if address is None:
            raise VanillaFunctionError('A Vulkan atomic needs a declared writable buffer address.')
        resource, index, offset, _layout = address
        operand = self.value(v[5])
        domain = 'int' if row.name in {'OpAtomicSMin', 'OpAtomicSMax'} else 'uint'
        original = b.memory('memory_atomic', resource, index, record_offset=offset,
            operation=operations[row.name], value=b.convert(operand, domain, bits=True))['original']
        return original
