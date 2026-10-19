from dataclasses import dataclass, replace
import re

from file_handlers.sdf.dxc import compile_hlsl_dxc
from file_handlers.sdf.shader_graph import ShaderSignatureField
lazy from file_handlers.sdf.spirv import SpirvError, reflect_spirv
lazy from file_handlers.sdf.spirv_ir import Operand, SpirvBuilder, SpirvSemanticIr
lazy from file_handlers.sdf.spirv_tools import finalize_spirv
from .re_engine_dxil_authoring import _carrier_source
lazy from .material_stage_inputs import stage_input_library
lazy from .constant_words import word_parts, override_word, OVERRIDE_PATTERN, normalize_word_expressions
lazy from ..surface_splice_spirv import SpirvSurfaceSplice


@dataclass(frozen=True)
class _UniformOffset:
    constant: int = 0
    terms: tuple[tuple[int, int, int], ...] = ()  # index ID, declared stride, extent

    def advance(self, stride, index, index_id, extent):
        if index is not None:
            return replace(self, constant=self.constant + index * stride)
        return replace(self, terms=(*self.terms, (index_id, stride, extent)))


@dataclass(frozen=True)
class _UniformLayout:
    matrix_stride: int | None = None
    row_major: bool = False
    component_stride: int | None = None


@dataclass(frozen=True)
class _UniformPointer:
    root: int
    path: tuple
    address: _UniformOffset = _UniformOffset()
    layout: _UniformLayout = _UniformLayout()


class _Module:
    def __init__(self, ir):
        self.ir = ir
        self.definitions = {row.result: row for row in ir.instructions if row.result is not None}
        self.names = {row.values[0]: row.values[1] for row in ir.instructions if row.opcode == 5}
        self.members = {(row.values[0], row.values[1]): row.values[2] for row in ir.instructions if row.opcode == 6}
        self.decorations, self.member_decorations = {}, {}
        self.functions = {}
        current = None
        for row in ir.instructions:
            if row.opcode == 71:
                self.decorations.setdefault(row.values[0], []).append(row.values[1:])
            elif row.opcode == 72:
                self.member_decorations.setdefault(row.values[0], []).append(row.values[1:])
            elif row.opcode == 54:
                current = []
                self.functions[row.result] = current
            if current is not None:
                current.append(row)
            if row.opcode == 56:
                current = None

    def constant(self, value):
        row = self.definitions[value]
        return int(row.values[2]) if row.opcode == 43 else None

    def pointee(self, variable):
        pointer = self.definitions[self.definitions[variable].type_id]
        if pointer.opcode != 32:
            raise SpirvError("Material access has no pointer type")
        return pointer.values[2]

    def composite_count(self, type_id):
        declaration = self.definitions[type_id]
        if declaration.opcode == 30:
            return len(declaration.values) - 1
        if declaration.opcode in {23, 24}:
            return declaration.values[2]
        if declaration.opcode == 28:
            count = self.constant(declaration.values[2])
            if count is not None and count > 0:
                return count
        raise SpirvError(f'{declaration.name} has no fixed composite extent')

    def uniform_element(self, type_id, index, layout):
        declaration = self.definitions[type_id]
        if declaration.opcode == 30:
            if index is None or not 0 <= index < self.composite_count(type_id):
                raise SpirvError('Uniform structure index has no declared member')
            decorations = {row[1]: row[2:] for row in self.member_decorations.get(type_id, ()) if row[0] == index}
            if 35 not in decorations:
                raise SpirvError('Uniform structure member has no Offset decoration')
            matrix_stride = decorations.get(7, (None,))[0]
            if matrix_stride is not None and ((4 in decorations) == (5 in decorations) or matrix_stride <= 0):
                raise SpirvError('Uniform matrix has an invalid stride/major-order declaration')
            return declaration.values[1 + index], decorations[35][0], 0, _UniformLayout(matrix_stride, 4 in decorations)
        if declaration.opcode not in {23, 24, 28}:
            raise SpirvError(f'Uniform pointer indexes {declaration.name}, not an aggregate')
        child = declaration.values[1]
        if declaration.opcode == 28:
            stride = next((r[1] for r in self.decorations.get(type_id, ()) if r[0] == 6), None)
            if stride is None or stride <= 0:
                raise SpirvError('Uniform array has no ArrayStride decoration')
            return child, 0, stride, layout
        if declaration.opcode == 24:
            vector = self.definitions[child]
            scalar = self.definitions[vector.values[1]]
            if layout.matrix_stride is None:
                raise SpirvError('Uniform matrix has no MatrixStride decoration')
            scalar_bytes = scalar.values[1] // 8
            stride = scalar_bytes if layout.row_major else layout.matrix_stride
            components = layout.matrix_stride if layout.row_major else scalar_bytes
            return child, 0, stride, _UniformLayout(component_stride=components)
        scalar = self.definitions[child]
        return child, 0, layout.component_stride or scalar.values[1] // 8, _UniformLayout()

    def uniform_leaves(self, type_id, address, layout, path=()):
        if self.definitions[type_id].opcode in {20, 21, 22}:
            yield path, type_id, address
            return
        for index in range(self.composite_count(type_id)):
            child, base, stride, child_layout = self.uniform_element(type_id, index, layout)
            offset = replace(address, constant=address.constant + base + stride * index)
            yield from self.uniform_leaves(child, offset, child_layout, (*path, index))

    def uniform_pointer(self, value):
        row = self.definitions[value]
        if row.opcode == 83:  # OpCopyObject retains the same pointer/address.
            return self.uniform_pointer(row.values[2])
        if row.opcode == 59 and row.values[2] == 2:
            # Uniform storage also holds old BufferBlock SSBOs; only Block is a constant buffer.
            if (2,) in self.decorations.get(self.pointee(value), ()):
                return _UniformPointer(value, ())
            return None
        if row.opcode not in {65, 66}:
            pointer_type = self.definitions.get(row.type_id)
            if pointer_type and pointer_type.opcode == 32 and pointer_type.values[1] == 2:
                raise SpirvError(f'Uniform pointer {row.name} needs semantic address resolution')
            return None
        parent = self.uniform_pointer(row.values[2])
        if parent is None:
            return None
        root, path, offset, layout = parent.root, parent.path, parent.address, parent.layout
        type_id = self.pointee(row.values[2])
        for index_id in row.values[3:]:
            index = self.constant(index_id)
            declaration = self.definitions[type_id]
            child, base, stride, child_layout = self.uniform_element(type_id, index, layout)
            if declaration.opcode == 30:
                path += (("member", self.members.get((type_id, index), f"member_{index}")),)
            else:
                path += (("index", index_id),)
            offset = replace(offset, constant=offset.constant + base).advance(stride, index, index_id, self.composite_count(type_id))
            type_id, layout = child, child_layout
        return _UniformPointer(root, path, offset, layout)


def spirv_uniform_reads(ir, buffer_name):
    module = _Module(ir)
    reads = set()
    for row in ir.instructions:
        if row.opcode != 61:
            continue
        pointer = module.uniform_pointer(row.values[2])
        if pointer is None or module.names.get(pointer.root) != buffer_name:
            continue
        for _path, type_id, address in module.uniform_leaves(row.type_id, pointer.address, pointer.layout):
            scalar = module.definitions[type_id]
            if address.terms or scalar.opcode not in {21, 22} or scalar.values[1] != 32:
                return None
            reads.add(address.constant)
    return reads


def spirv_resource_reads(ir):
    module = _Module(ir)

    def root(value):
        row = module.definitions[value]
        if row.opcode == 59:
            return value
        if row.opcode in {65, 66, 67, 70, 83}:  # AccessChain variants / CopyObject
            return root(row.values[2])
        return None

    reads = set()
    for row in ir.instructions:
        if row.opcode != 61:
            continue
        pointer = module.definitions[row.values[2]]
        pointer_type = module.definitions[pointer.type_id]
        if pointer_type.opcode != 32 or pointer_type.values[1] != 0:
            continue
        variable = root(pointer.result)
        if variable is None or variable not in module.names:
            return None
        reads.add(module.names[variable])
    return reads


class _Linker:
    def __init__(self, native, carrier):
        self.native, self.carrier = _Module(native), _Module(carrier)
        self.builder = SpirvBuilder(native)
        self.ids, self.required = {}, set()
        self.globals, self.functions, self.debug, self.annotations, self.extensions = [], [], [], [], []
        self.interface = []

    def require(self, identifier):
        if identifier in self.required:
            return
        self.required.add(identifier)
        rows = self.carrier.functions.get(identifier, (self.carrier.definitions[identifier],))
        for row in rows:
            for operand in row.operands:
                if operand.is_id and operand.value != row.result:
                    self.require(operand.value)
            if row.result is not None:
                self.required.add(row.result)

    def match_type(self, source, target):
        a, b = self.carrier.definitions[source], self.native.definitions[target]
        if a.opcode != b.opcode:
            raise SpirvError(f"Linked descriptor changes {a.name} to {b.name}")
        previous = self.ids.get(source)
        if previous is not None:
            if previous != target:
                raise SpirvError("A shared Vulkan type maps to conflicting native types")
            return
        if (sorted(self.carrier.member_decorations.get(source, []))
                != sorted(self.native.member_decorations.get(target, []))):
            raise SpirvError("Linked descriptor changes structure member layout")
        if sorted(self.carrier.decorations.get(source, [])) != sorted(self.native.decorations.get(target, [])):
            raise SpirvError("Linked descriptor changes native type decorations")
        self.ids[source] = target
        aa = [v for v in a.operands if v.kind != "IdResult"]
        bb = [v for v in b.operands if v.kind != "IdResult"]
        if len(aa) != len(bb):
            raise SpirvError("Linked descriptor changes aggregate shape")
        for index, (left, right) in enumerate(zip(aa, bb)):
            if left.is_id and right.is_id:
                self.match_type(left.value, right.value)
            elif left != right:
                # Depth=2 is an unspecified image depth type, not a new format.
                if not (a.opcode == 25 and index == 2 and left.value == 2 and right.value in {0, 1}):
                    raise SpirvError(f"Linked descriptor changes {a.name} operand {index}")

    def import_helpers(self, roots, replacement_buffers):
        for identifier in roots:
            self.require(identifier)
        native_globals = {name: key for key, name in self.native.names.items()
                          if self.native.definitions.get(key) and self.native.definitions[key].opcode == 59}
        for key in sorted(self.required):
            row = self.carrier.definitions[key]
            if row.opcode != 59 or row.values[2] == 7:
                continue
            name = self.carrier.names.get(key)
            target = native_globals.get(name)
            if target is not None and name not in replacement_buffers:
                self.match_type(row.type_id, self.native.definitions[target].type_id)
                self.ids[key] = target
        reusable = {}
        # SPIR-V forbids declaring a non-aggregate type twice, so the carrier reuses each existing declaration.
        for key, row in (*self.native.definitions.items(), *((row.result, row) for row in self.globals)):
            if row.opcode in {19, 20, 21, 22, 23, 24, 25, 26, 27, 32, 33, 41, 42, 43} and not self.native.decorations.get(key):
                reusable[(row.opcode, tuple(v for v in row.operands if v.kind != "IdResult"))] = key
        for row in self.carrier.ir.instructions:
            key = row.result
            if key not in self.required or key in self.ids:
                continue
            mapped = row.remap(self.ids)
            signature = (row.opcode, tuple(v for v in mapped.operands if v.kind != "IdResult"))
            self.ids[key] = reusable.get(signature, self.builder.id())
        function_ids = {key for rows in self.carrier.functions.values() for row in rows if (key := row.result) is not None}
        for row in self.carrier.ir.instructions:
            if row.opcode in {17, 10, 11}:
                if row.result is None or row.result in self.required:
                    self.extensions.append(row.remap(self.ids))
            elif row.opcode in {5, 6, 71, 72}:
                target = row.values[0]
                if target in self.required and self.ids.get(target) not in self.native.definitions:
                    (self.debug if row.opcode in {5, 6} else self.annotations).append(row.remap(self.ids))
            elif row.result in self.required and row.result not in function_ids:
                if self.ids[row.result] not in self.native.definitions and row.opcode != 11:
                    self.globals.append(row.remap(self.ids))
        for identifier, rows in self.carrier.functions.items():
            if identifier not in self.required:
                continue
            for row in rows:
                mapped = row.remap(self.ids)
                if row.opcode == 54:
                    operands = list(mapped.operands)
                    operands[2] = replace(operands[2], value=int(operands[2].value) & ~2)  # Helpers must inline for fresh recovery.
                    mapped = replace(mapped, operands=tuple(operands))
                self.functions.append(mapped)
        for row in (*self.extensions, *self.globals, *self.functions):
            if row.result is not None:
                self.builder.decoder.definitions[row.result] = row
                if row.type_id is not None:
                    self.builder.decoder.result_types[row.result] = row.type_id

    def scalar_type(self, opcode, *operands):
        for key, row in self.builder.decoder.definitions.items():
            if row.opcode == opcode and row.values[1:] == operands:
                return key
        row = self.builder.instruction(opcode, self.builder.id(), *operands)
        self.globals.append(row)
        return row.result

    def constant(self, value):
        for row in (*self.native.ir.instructions, *self.globals):
            if row.opcode == 43 and row.values[2] == value:
                kind = self.builder.decoder.definitions[row.type_id]
                if kind.opcode == 21 and kind.values[1:] == (32, 0):
                    return row.result
        type_id = self.scalar_type(21, 32, 0)
        row = self.builder.instruction(43, type_id, self.builder.id(), value)
        self.globals.append(row)
        return row.result

    def boolean(self, value):
        return self.typed_constant(41 if value else 42, self.scalar_type(20))

    def typed_constant(self, opcode, type_id):
        """``opcode`` is that of an operand-free constant: true, false or undef."""
        for row in (*self.native.ir.instructions, *self.globals):
            if row.opcode == opcode and row.type_id == type_id:
                return row.result
        row = self.builder.instruction(opcode, type_id, self.builder.id())
        self.globals.append(row)
        return row.result

    def stage_variable(self, field, storage_class):
        scalar = {"float32": (22, 32), "uint32": (21, 32, 0), "sint32": (21, 32, 1)}.get(field.component_type)
        if scalar is None or field.system_value not in {"", "undefined"}:
            raise SpirvError(f"A pass cannot add the stage value {field.semantic_name}{field.semantic_index}")
        vector = self.scalar_type(23, self.scalar_type(*scalar), 4)
        variable = self.builder.instruction(59, self.scalar_type(32, storage_class, vector), self.builder.id(),
                                            storage_class)
        self.globals.append(variable)
        prefix = "in" if storage_class == 1 else "out"
        self.debug.append(self.builder.instruction(5, variable.result, f"{prefix}.var.{field.semantic_name}{field.semantic_index}"))
        self.annotations.append(self.builder.instruction(71, variable.result, 30, field.register))  # Location
        if field.interpolation == "constant":
            self.annotations.append(self.builder.instruction(71, variable.result, 14))  # Flat
        self.interface.append(variable.result)
        return variable.result, vector

    def finish(self, native_rows, removed, assignments):
        header, body = [], []
        in_function = False
        for row in native_rows:
            if row.opcode == 54:
                in_function = True
            if row.result in removed or row.opcode in {5, 6, 71, 72} and row.values[0] in removed:
                continue
            if row.opcode == 15:  # OpEntryPoint
                row = replace(row, operands=(*row.operands, *(Operand("IdRef", item) for item in self.interface)))
            (body if in_function else header).append(row)
        # SPIR-V logical layout places all declarations before all functions.
        groups = {key: [] for key in range(8)}
        def group(row):
            if row.opcode == 17: return 0
            if row.opcode == 10: return 1
            if row.opcode == 11: return 2
            if row.opcode == 14: return 3
            if row.opcode in {15, 16, 331}: return 4
            if row.opcode in {2, 3, 4, 5, 6, 7, 8, 317, 330}: return 5
            if row.opcode in {71, 72, 73, 74, 75, 332}: return 6
            return 7
        for row in (*header, *self.extensions, *self.debug, *self.annotations, *self.globals):
            bucket = groups[group(row)]
            if row not in bucket:
                bucket.append(row)
        names = {row.values[0]: row.values[1] for row in groups[5] if row.opcode == 5}
        annotations = []
        for row in groups[6]:
            if row.opcode == 71 and row.values[1] in {33, 34}:
                name = names.get(row.values[0], "").casefold()
                if name in assignments:
                    operands = list(row.operands)
                    operands[2] = replace(operands[2], value=assignments[name][1 if row.values[1] == 33 else 0])
                    row = replace(row, operands=tuple(operands))
            annotations.append(row)
        groups[6] = annotations
        result = SpirvSemanticIr(max(self.native.ir.version, self.carrier.ir.version),
                                tuple(row for bucket in groups.values() for row in bucket) + tuple(body) + tuple(self.functions))
        return SpirvSemanticIr.from_bytes(result.write())


def rebuild_spirv(ir, source_contract, contract, *, scalar_expressions, texture_overrides,
                  helpers, assignments, renamed_members=None, surface_plan=()):
    renamed_members = dict(renamed_members or {})
    scalar_expressions = normalize_word_expressions(scalar_expressions)
    native = _Module(ir)
    reflected = reflect_spirv(ir.write())
    replacement_buffers = {buffer.name for buffer in source_contract.constant_buffers
                           if contract.buffer(buffer.name) != buffer}
    texture_sources = { (r.register_space, r.bind_point): r for r in source_contract.resources if r.register_class == 't'}
    texture_targets = { (r.register_space, r.bind_point): r for r in contract.resources if r.register_class == 't'}
    retained = replacement_buffers | {texture_targets[value].name for value in texture_overrides.values()}
    extents = {item.location: int(match[1]) for item in reflected.inputs
               if item.location is not None and (match := re.search(r"\[(\d+)\]$", item.value_type))}
    surface_texts = [edit.expression for edit in surface_plan]
    indexed_reads, rewritten = stage_input_library(
        contract, (*scalar_expressions.values(), *surface_texts, helpers), extents)
    scalar_expressions = dict(zip(scalar_expressions, rewritten[:len(scalar_expressions)]))
    surface_plan = tuple(replace(edit, expression=text)
                         for edit, text in zip(surface_plan, rewritten[len(scalar_expressions):-1]))
    helpers = rewritten[-1]
    inputs = tuple(read.carrier for read in indexed_reads)
    carrier_contract = replace(contract, stage="pixel", inputs=inputs, patch_constants=(),
                               outputs=(ShaderSignatureField("SV_Target", 0, 0, "float32", 15, 15, "target"),))
    source = _carrier_source(carrier_contract, entry_point="REasyCarrierMain",
                             scalar_expressions=scalar_expressions, helpers=helpers, touch_contract=False,
                             retained_resources=frozenset(retained), surface_plan=surface_plan)
    # Importing a SPIR-V 1.3 carrier would upgrade a 1.0 module, so compile for the module's own version.
    environment = {0x10000: "vulkan1.0", 0x10300: "vulkan1.1"}.get(ir.version)
    if environment is None:
        raise SpirvError(f"Native SPIR-V version 0x{ir.version:05x} needs a supported compiler target environment")
    arguments = ["-Od", "-fspv-target-env=" + environment]
    for index, resource in enumerate(contract.resources):
        arguments += ["-fvk-bind-register", f"{resource.register_class}{resource.bind_point}",
                      str(resource.register_space), str(index), "0"]
    compiled = compile_hlsl_dxc(source, entry_point="REasyCarrierMain", target="ps_6_0", spirv=True,
                               extra_arguments=tuple(arguments)).bytecode
    carrier = SpirvSemanticIr.from_bytes(compiled)
    if carrier.version != ir.version:
        raise SpirvError("The Vulkan expression compiler changed the native module version")
    linker = _Linker(ir, carrier)
    added = {field.key: linker.stage_variable(field, storage_class)
             for fields, native_fields, storage_class in ((contract.inputs, source_contract.inputs, 1),
                                                          (contract.outputs, source_contract.outputs, 3))
             for field in fields if field.key not in {item.key for item in native_fields}}
    input_variables = {item.location: (item.variable_id, native.pointee(item.variable_id)) for item in reflected.inputs}
    input_variables.update((field.register, added[field.key]) for field in contract.inputs if field.key in added)
    helper_ids = {}
    for key, name in linker.carrier.names.items():
        match = re.fullmatch(OVERRIDE_PATTERN, name)
        if match and key in linker.carrier.functions:
            helper_ids[override_word(match)] = key
    if set(helper_ids) != set(scalar_expressions):
        raise SpirvError("Compiled Vulkan expression library omitted an override")
    surface = None
    if surface_plan:
        surface = SpirvSurfaceSplice(
            ir, linker, surface_plan, lambda: [input_value(read, component)[0] for read, component in input_fields],
            lambda pointer_type, *initializer: local_variable(pointer_type, *initializer), lambda row: rows.append(row),
            added)
    descriptor_ids = {d.name: d.variable_id for d in reflected.descriptors}
    carrier_globals = {name: key for key, name in linker.carrier.names.items()
                       if linker.carrier.definitions.get(key) and linker.carrier.definitions[key].opcode == 59
                       and linker.carrier.definitions[key].values[2] != 7}
    for old, new in texture_overrides.items():
        original = descriptor_ids[texture_sources[old].name]
        target = carrier_globals.get(texture_targets[new].name)
        if target is None:
            raise SpirvError(f"Texture redirect omitted {texture_targets[new].name}")
        linker.match_type(linker.carrier.definitions[target].type_id, native.definitions[original].type_id)
    linker.import_helpers((*helper_ids.values(), *(surface.helper_roots() if surface else ()),
                           *(carrier_globals[name] for name in retained if name in carrier_globals)), replacement_buffers)
    linked_globals = {linker.carrier.names[key]: linker.ids[key] for key in linker.required
                      if linker.carrier.definitions[key].opcode == 59 and linker.carrier.definitions[key].values[2] != 7}
    removed, redirects = set(), {}
    for name in replacement_buffers:
        if name in descriptor_ids:
            removed.add(descriptor_ids[name])
    for old, new in texture_overrides.items():
        source_resource, target_resource = texture_sources[old], texture_targets[new]
        original = descriptor_ids[source_resource.name]
        target = descriptor_ids.get(target_resource.name, linked_globals.get(target_resource.name))
        if target is None:
            raise SpirvError(f"Texture {target_resource.name} was omitted by the expression compiler")
        if native.definitions[original].type_id != linker.builder.decoder.definitions[target].type_id:
            raise SpirvError("Texture redirect changes its native image type")
        redirects[original] = target
        if contract.resource(source_resource.name) is None:
            removed.add(original)

    instruction = linker.builder.instruction

    def result_as(row, identifier):
        return replace(row, operands=tuple(replace(v, value=identifier) if v.kind == "IdResult" else v for v in row.operands))

    rows = []
    invalid_pointers = set()
    by_variable = {descriptor_ids[b.name]: b for b in source_contract.constant_buffers if b.name in descriptor_ids}
    function_variables, argument_variables = {}, {}
    function_id = None

    def address_match(address, wanted):
        uint_type, bool_type = linker.scalar_type(21, 32, 0), linker.scalar_type(20)
        total = linker.constant(address.constant)
        valid = None
        def operation(opcode, value_type, *operands):
            row = instruction(opcode, value_type, linker.builder.id(), *operands)
            rows.append(row)
            return row.result
        for index, stride, extent in address.terms:
            index_type = native.definitions[native.definitions[index].type_id]
            if index_type.opcode != 21 or index_type.values[1] != 32:
                raise SpirvError("Uniform array index is not a 32-bit integer")
            unsigned = index if index_type.result == uint_type else operation(124, uint_type, index)
            bound = operation(176, bool_type, unsigned, linker.constant(extent))  # OpULessThan
            valid = bound if valid is None else operation(167, bool_type, valid, bound)  # OpLogicalAnd
            scaled = operation(132, uint_type, unsigned, linker.constant(stride))  # OpIMul
            total = operation(128, uint_type, total, scaled)  # OpIAdd
        equal = operation(170, bool_type, total, linker.constant(wanted))  # OpIEqual
        return equal if valid is None else operation(167, bool_type, valid, equal)

    def input_value(read, component):
        variable, value_type = input_variables[read.native.register]
        load = instruction(61, value_type, linker.builder.id(), variable)
        rows.append(load)
        value = load.result
        type_row = linker.builder.decoder.definitions[value_type]
        if read.element is not None:
            if type_row.opcode != 28:
                raise SpirvError("Indexed input does not have a native array type")
            value_type = type_row.values[1]
            load = instruction(81, value_type, linker.builder.id(), value, read.element)
            rows.append(load)
            value, type_row = load.result, native.definitions[value_type]
        if type_row.opcode == 23:
            value_type = type_row.values[1]
            load = instruction(81, value_type, linker.builder.id(), value, component)
            rows.append(load)
            value = load.result
        return value, value_type

    def local_variable(pointer_type, *initializer):
        local = instruction(59, pointer_type, linker.builder.id(), 7, *initializer)
        function_variables.setdefault(function_id, []).append(local)
        return local.result

    input_fields = [(read, component) for read in indexed_reads for component in range(read.carrier.mask.bit_count())]

    def call_arguments(helper):
        parameters = [row for row in linker.carrier.functions[helper] if row.opcode == 55]
        if len(parameters) != len(input_fields):
            raise SpirvError("Vulkan helper argument count disagrees with its input contract")
        arguments = []
        for index, (parameter, (read, component)) in enumerate(zip(parameters, input_fields)):
            value, value_type = input_value(read, component)
            parameter_type = linker.ids[parameter.type_id]
            type_row = linker.builder.decoder.definitions[parameter_type]
            scalar_type = type_row.values[2] if type_row.opcode == 32 else parameter_type
            if value_type != scalar_type:
                cast = instruction(124, scalar_type, linker.builder.id(), value)
                rows.append(cast)
                value = cast.result
            if type_row.opcode == 32:
                if type_row.values[1] != 7:
                    raise SpirvError("Vulkan helper parameter is not a function-local value")
                key = (function_id, parameter_type, index)
                if key not in argument_variables:
                    argument_variables[key] = local_variable(parameter_type)
                local_id = argument_variables[key]
                rows.append(instruction(62, local_id, value))
                value = local_id
            arguments.append(value)
        return arguments

    for position, row in enumerate(ir.instructions):
        if row.opcode == 54:
            function_id = row.result
        if surface is not None:
            surface.before(position)
            row = surface.renamed(row)
            if row.opcode in {62, 99}:  # OpStore, OpImageWrite
                row = surface.store(position, row)
            elif row.opcode == 245:
                row = surface.phi(row)
            replacement = surface.discard(position)
            if replacement is not None:
                rows.extend(replacement)
                continue
        if row.opcode in {65, 66}:
            pointer = native.uniform_pointer(row.result)
            if pointer and pointer.root in removed and pointer.root in by_variable:
                buffer = by_variable[pointer.root]
                target = linked_globals.get(buffer.name)
                if target is None:
                    invalid_pointers.add(row.result)
                    continue
                type_id = linker.builder.decoder.definitions[linker.builder.decoder.definitions[target].type_id].values[2]
                indices = []
                for step, (kind, value) in enumerate(pointer.path):
                    declaration = linker.builder.decoder.definitions[type_id]
                    if kind == "member":
                        if step == 0:
                            value = renamed_members.get(value, value)
                        carrier_type = next((old for old, new in linker.ids.items() if new == type_id), None)
                        matches = [i for i in range(len(declaration.values) - 1) if linker.carrier.members.get((carrier_type, i), f"member_{i}") == value]
                        if len(matches) != 1:
                            invalid_pointers.add(row.result)
                            break
                        index = matches[0]
                        indices.append(linker.constant(index))
                        type_id = declaration.values[index + 1]
                    else:
                        indices.append(value)
                        type_id = declaration.values[1]
                if row.result in invalid_pointers:
                    continue
                if type_id != native.pointee(row.result):
                    raise SpirvError("Relocated uniform access changes its value type")
                rows.append(result_as(instruction(row.opcode, row.type_id, linker.builder.id(), target, *indices), row.result))
                continue
        if row.opcode == 83 and row.values[2] in invalid_pointers:
            invalid_pointers.add(row.result)
            continue
        if row.opcode != 61:
            if row.opcode not in {5, 6, 71, 72} and any(v.is_id and v.value in invalid_pointers for v in row.operands):
                raise SpirvError("Removed uniform pointer has a non-load use")
            rows.append(row.remap(redirects) if row.opcode not in {5, 6, 71, 72, 59} else row)
            continue
        pointer = native.uniform_pointer(row.values[2])
        buffer = by_variable.get(pointer.root) if pointer else None
        available = {offset: helper for key, helper in helper_ids.items()
                     for binding, offset, space in (word_parts(key),)
                     if buffer and space == buffer.register_space and binding == buffer.bind_point}
        leaves = tuple(native.uniform_leaves(row.type_id, pointer.address, pointer.layout)) if available else ()
        replacements = {path: tuple((helper, offset) for offset, helper in available.items()
                                    if address.terms or address.constant == offset)
                        for path, _scalar, address in leaves}
        replacements = {path: choices for path, choices in replacements.items() if choices}
        if not replacements:
            if row.values[2] in invalid_pointers:
                raise SpirvError("Removed uniform field is still consumed by the native program")
            rows.append(row.remap(redirects))
            continue
        original = None
        if len(replacements) != len(leaves) or any(address.terms for _path, _scalar, address in leaves):
            if row.values[2] in invalid_pointers:
                raise SpirvError("Removed uniform value has an unoverridden component")
            original = linker.builder.id()
            rows.append(result_as(row, original))
        values = {}
        for path, scalar_type, address in leaves:
            value = None
            if original is not None:
                if path:
                    extract = instruction(81, scalar_type, linker.builder.id(), original, *path)
                    rows.append(extract)
                    value = extract.result
                else:
                    value = original
            if path in replacements:
                scalar = native.definitions[scalar_type]
                if scalar.opcode not in {21, 22} or scalar.values[1] != 32:
                    raise SpirvError('A material word override requires a declared 32-bit scalar')
                for helper, offset in replacements[path]:
                    function = linker.ids[helper]
                    function_row = linker.builder.decoder.definitions[function]
                    arguments = call_arguments(helper)
                    call = instruction(57, function_row.type_id, linker.builder.id(), function, *arguments)
                    rows.append(call)
                    override = call.result
                    if call.type_id != scalar_type:
                        cast = instruction(124, scalar_type, linker.builder.id(), override)
                        rows.append(cast)
                        override = cast.result
                    if address.terms:
                        condition = address_match(address, offset)
                        select = instruction(169, scalar_type, linker.builder.id(), condition, override, value)
                        rows.append(select)
                        override = select.result
                    value = override
            values[path] = value

        def composite(type_id, path=()):
            if path in values:
                return values[path]
            components = []
            declaration = native.definitions[type_id]
            for index in range(native.composite_count(type_id)):
                child = declaration.values[1 + index] if declaration.opcode == 30 else declaration.values[1]
                components.append(composite(child, (*path, index)))
            combined = instruction(80, type_id, linker.builder.id(), *components)
            rows.append(combined)
            return combined.result

        rows.append(result_as(instruction(83, row.type_id, linker.builder.id(), composite(row.type_id)), row.result))
    removed.update(invalid_pointers)
    with_locals = []
    first_block = False
    for row in rows:
        with_locals.append(row)
        if row.opcode == 54:
            function_id, first_block = row.result, True
        elif row.opcode == 248 and first_block:
            with_locals.extend(function_variables.get(function_id, ()))
            first_block = False
    return finalize_spirv(linker.finish(with_locals, removed, assignments).write())
