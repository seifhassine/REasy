"""See docs/internals/surface-splicing.md, Readers."""
from __future__ import annotations

from functools import lru_cache

from file_handlers.sdf.shader_graph import ShaderGraphError
from file_handlers.sdf.spirv import SpirvError, reflect_spirv
from file_handlers.sdf.spirv_contract import patch_interface_ids, spirv_interface_rows
from file_handlers.sdf.spirv_ir import SpirvSemanticIr, grammar

from .native_loop_flow import reachable_blocks
from .profiles.re_engine_spirv_authoring import _Module
from .spirv_control_flow import spirv_cfg
from .surface_flow import FlowStore, FlowValue, NativeFlow, buffer_component_type, buffer_element, constant_field
from .surface_flow_dxil import _dominators


_PURE_CLASSES = frozenset({"Arithmetic", "Relational_and_Logical", "Conversion", "Bit", "Derivative"})
_ROUTING = frozenset({"OpCompositeConstruct", "OpCompositeExtract", "OpCompositeInsert", "OpVectorShuffle",
                      "OpCopyObject", "OpCopyLogical"})
_MIXING = frozenset({"OpTranspose", "OpVectorExtractDynamic", "OpVectorTimesMatrix", "OpMatrixTimesVector",
                     "OpMatrixTimesMatrix", "OpOuterProduct", "OpDot", "OpAny", "OpAll"})
# GLSL.std.450: whole-vector functions and the interpolation functions, which read stage inputs.
_GLSL_MIXING = frozenset({33, 34, 36, 52, 66, 67, 68, 69, 70, 71, 72})
_GLSL_SOURCES = frozenset({76, 77, 78})
_DISCARDS = frozenset({"OpKill", "OpTerminateInvocation", "OpDemoteToHelperInvocation"})
_MEMORY_CLASSES = {1: "input", 3: "output", 4: "workgroup", 6: "private", 7: "function"}
_IMAGE_READS = frozenset({"OpImageSampleImplicitLod", "OpImageSampleExplicitLod", "OpImageSampleDrefImplicitLod",
                          "OpImageSampleDrefExplicitLod", "OpImageSampleProjImplicitLod", "OpImageSampleProjExplicitLod",
                          "OpImageSampleProjDrefImplicitLod", "OpImageSampleProjDrefExplicitLod", "OpImageFetch",
                          "OpImageGather", "OpImageDrefGather"})
_TEXEL_TYPES = {"float": "float32", "uint": "uint32", "int": "sint32"}
_UNIFORM, _STORAGE_BUFFER = 2, 12


def _flow_store(semantic, index, lane, value, site, scope) -> FlowStore:
    constant = not isinstance(value, int)
    return FlowStore(semantic, index, lane, None if constant else value, str(value[1]) if constant else "", site, scope)


def spirv_flow(ir: SpirvSemanticIr, contract) -> NativeFlow:
    native = reflect_spirv(ir.write())
    module = _Module(ir)
    definitions = module.definitions
    core = grammar()[0]
    names = {row["opname"]: code for code, row in core.items()}
    patch_ids = patch_interface_ids(native, module.decorations)
    _inputs, output_rows, _undeclared = spirv_interface_rows(native, contract, patch_ids)
    glsl = {row.result for row in ir.instructions if row.opcode == names["OpExtInstImport"]
            and str(row.values[1]) == "GLSL.std.450"}
    entry = next((row.values[1] for row in ir.instructions if row.opcode == names["OpEntryPoint"]
                  and row.values[2] == native.entry_point), None)
    if entry not in module.functions:
        raise ShaderGraphError("SPIR-V entry function is missing.")

    @lru_cache(None)
    def shape(type_id) -> tuple[str, ...]:
        row = definitions[type_id]
        if row.opcode == names["OpTypeBool"]:
            return ("bool",)
        if row.opcode == names["OpTypeInt"]:
            return ("int" if row.values[2] else "uint",)
        if row.opcode == names["OpTypeFloat"]:
            return ("float",)
        if row.opcode in {names["OpTypeVector"], names["OpTypeMatrix"], names["OpTypeArray"]}:
            return shape(row.values[1]) * module.composite_count(type_id)
        if row.opcode == names["OpTypeStruct"]:
            return tuple(lane for member in row.values[1:] for lane in shape(member))
        return ("handle",)

    def member_span(type_id, indices):
        offset = 0
        for index in indices:
            row = definitions[type_id]
            if row.opcode == names["OpTypeStruct"]:
                offset += sum(len(shape(member)) for member in row.values[1:1 + index])
                type_id = row.values[1 + index]
            else:
                type_id = row.values[1]
                offset += index * len(shape(type_id))
        return offset, type_id

    values: list[FlowValue] = []
    lanes: dict[int, list] = {}
    pointers: dict[int, tuple[int, tuple[int, ...]]] = {}
    memory: dict[int, list[int]] = {}
    deferred_loads: list[tuple[int, int]] = []
    stores, discards, exits = [], [], []
    texture_reads, read_types = [], {}
    resource_stores, resource_scalars = [], {}
    descriptors = {item.variable_id: item.name for item in native.descriptors}

    def texture_of(identifier):
        row = definitions.get(identifier)
        if row is not None and row.opcode == names["OpSampledImage"]:
            row = definitions.get(row.values[2])
        if row is None or row.opcode != names["OpLoad"] or row.values[2] not in pointers:
            return None
        root, path = pointers[row.values[2]]
        resource = contract.resource(descriptors.get(root, "")) if not path else None
        return resource.name if resource is not None and resource.input_type == "texture" else None

    for row in ir.instructions:
        code = row.opcode
        if code in {names["OpConstant"], names["OpSpecConstant"]}:
            lanes[row.result] = [("constant", str(row.values[2]))]
        elif code in {names["OpConstantTrue"], names["OpConstantFalse"], names["OpSpecConstantTrue"],
                      names["OpSpecConstantFalse"]}:
            lanes[row.result] = [("constant", row.name)]
        elif code in {names["OpConstantComposite"], names["OpSpecConstantComposite"]}:
            lanes[row.result] = [lane for part in row.values[2:] for lane in lanes[part]]
        elif code in {names["OpConstantNull"], names["OpUndef"]}:
            lanes[row.result] = [("constant", row.name)] * len(shape(row.type_id))
        elif code == names["OpVariable"]:
            pointers[row.result] = (row.result, ())

    def operand_lanes(identifier):
        return lanes.get(identifier, ())

    def add(kind, operands, signature, scalar, origin, controls=()):
        refs = tuple(dict.fromkeys(item for item in operands if isinstance(item, int)))
        conditions = tuple(dict.fromkeys(item for item in controls if isinstance(item, int)))
        values.append(FlowValue(kind, refs, conditions, signature, scalar, origin))
        return len(values) - 1

    def constants(items):
        return tuple(item[1] for item in items if not isinstance(item, int))

    def pointer(identifier):
        if identifier not in pointers:
            raise ShaderGraphError("SPIR-V reads memory through an untracked pointer.")
        return pointers[identifier]

    def storage_buffer(root) -> bool:
        storage = definitions[definitions[root].type_id].values[1]
        return storage == _STORAGE_BUFFER or storage == _UNIFORM and (3,) in module.decorations.get(
            module.pointee(root), ())  # BufferBlock

    def uniform_field(identifier, lane=0) -> str | None:
        row = definitions.get(identifier)
        if row is not None and row.opcode == names["OpCompositeExtract"] and len(row.values) == 4:
            return uniform_field(row.values[2], row.values[3])
        if row is None or row.opcode != names["OpLoad"]:
            return None
        try:
            found = module.uniform_pointer(row.values[2])
        except SpirvError:
            return None  # An address that only resolves per thread names no fixed word.
        if found is None:
            return None
        leaves = list(module.uniform_leaves(row.type_id, found.address, found.layout))
        buffer = contract.buffer(module.names.get(found.root, ""))
        if buffer is None or lane >= len(leaves) or leaves[lane][2].terms:
            return None
        return constant_field(contract, buffer.bind_point, buffer.register_space, leaves[lane][2].constant)

    def address_terms(identifier, scale) -> tuple[list[str], int]:
        row = definitions.get(identifier)
        if row is None:
            return [], 0
        if row.opcode == names["OpConstant"]:
            return [], int(row.values[2]) * scale
        name = core[row.opcode]["opname"]
        if name in {"OpIAdd", "OpBitwiseOr"}:
            (left, left_offset), (right, right_offset) = (address_terms(item, scale) for item in row.values[2:4])
            return [*left, *right], left_offset + right_offset
        if name in {"OpBitcast", "OpCopyObject"}:
            return address_terms(row.values[2], scale)
        shift = module.constant(row.values[3]) if name == "OpShiftRightLogical" else None
        if shift is not None and scale % (1 << shift) == 0:
            return address_terms(row.values[2], scale >> shift)
        field = uniform_field(identifier)
        return ([field if scale == 1 else f"{field}*{scale}"] if field is not None else []), 0

    def buffer_word(root, path) -> tuple[str, int, tuple[str, ...]]:
        resource = contract.resource(descriptors.get(root, ""))
        if resource is None:
            raise ShaderGraphError("The program stores into a buffer it does not declare by name.")
        type_id, fields, offset = module.pointee(root), [], 0
        for item in path:
            row = definitions[type_id]
            if row.opcode == names["OpTypeStruct"]:
                member = module.constant(item)
                offset += next(entry[2] for entry in module.member_decorations.get(type_id, ())
                               if entry[0] == member and entry[1] == 35)  # Offset
                type_id = row.values[1 + member]
                continue
            if row.opcode in {names["OpTypeArray"], names["OpTypeRuntimeArray"]}:
                stride = next(entry[1] for entry in module.decorations.get(type_id, ()) if entry[0] == 6)  # ArrayStride
            elif row.opcode == names["OpTypeVector"]:
                stride = 4
            else:
                raise ShaderGraphError("A buffer store indexes something that is not an array, vector or structure.")
            more, constant = address_terms(item, stride)
            fields.extend(more)
            offset += constant
            type_id = row.values[1]
        leaf = definitions[type_id]
        scalar = definitions[leaf.values[1]] if leaf.opcode == names["OpTypeVector"] else leaf
        if scalar.opcode not in {names["OpTypeInt"], names["OpTypeFloat"]} or scalar.values[1] != 32:
            raise ShaderGraphError("A buffer store writes something other than 32-bit words.")
        semantic, first = buffer_element(resource.name, fields, offset)
        return semantic, first, shape(type_id)

    function = module.functions[entry]
    blocks, successors, conditions, _merges = spirv_cfg(function)
    dominators = _dominators(blocks, successors)

    def path_controls(block):
        strict = dominators[block] - {block}
        dominator = next((item for item in blocks if item in strict and dominators[item] == strict), None)
        between = reachable_blocks(dominator, successors, block) if dominator is not None else set()
        return [conditions[item] for item in sorted(between, key=blocks.index) if item in conditions]

    start, current = ir.instructions.index(function[0]), None
    for step, row in enumerate(function):
        name = core[row.opcode]["opname"]
        result = row.result
        if name == "OpLabel":
            current = result
            continue
        if result is not None and row.type_id is not None and definitions[row.type_id].opcode == names["OpTypeVoid"]:
            continue
        references = [operand.value for operand in row.operands if operand.kind == "IdRef"]
        if name == "OpPhi":
            controls = [lane for condition in path_controls(current) for lane in operand_lanes(condition)]
            incoming = references[0::2]
            lanes[result] = [add("join", [operand_lanes(item)[lane] for item in incoming if operand_lanes(item)],
                                 ("phi",), scalar, (result, lane), controls)
                             for lane, scalar in enumerate(shape(row.type_id))]
        elif name in {"OpAccessChain", "OpInBoundsAccessChain", "OpPtrAccessChain"} or (
                name == "OpCopyObject" and references[0] in pointers):
            root, path = pointer(references[0])
            pointers[result] = (root, (*path, *references[1:]))
        elif name == "OpLoad":
            root, path = pointer(references[0])
            storage = _MEMORY_CLASSES.get(definitions[definitions[root].type_id].values[1])
            dynamic = [lane for item in path if module.constant(item) is None for lane in operand_lanes(item)]
            family = ("load", storage)
            lanes[result] = [add("source", dynamic, family, scalar, (result, lane))
                             for lane, scalar in enumerate(shape(row.type_id))]
            if storage in {"workgroup", "private", "function", "output"}:
                deferred_loads.extend((value, root) for value in lanes[result])
        elif name == "OpStore":
            root, path = pointer(references[0])
            stored = list(operand_lanes(references[1]))
            memory.setdefault(root, []).extend(item for item in stored if isinstance(item, int))
            if storage_buffer(root):
                semantic, first, scalars = buffer_word(root, path)
                for position, (value, scalar) in enumerate(zip(stored, scalars)):
                    element, lane = divmod(first + position, 4)
                    resource_stores.append(_flow_store(semantic, element, lane, value,
                                                       (start + step, current, position), "buffer"))
                    resource_scalars.setdefault(("buffer", semantic, element), []).append(scalar)
            elif root in output_rows:
                scope = "patch" if root in patch_ids else "output"
                constant_path = [module.constant(item) for item in path]
                fields = output_rows[root]
                variable_type = module.pointee(root)
                if None in constant_path:
                    # A dynamically indexed store may reach any lane, so those lanes keep only their stored value.
                    index_lanes = [lane for item in path for lane in operand_lanes(item)]
                    for field in fields:
                        for lane in range(field.mask.bit_count()):
                            joined = add("join", [*stored, *index_lanes], ("dynamic-store",),
                                         shape(variable_type)[0], (row.opcode, lane))
                            stores.append((field, lane, joined, (start + step, current, None), scope))
                    continue
                offset, _type = member_span(variable_type, constant_path)
                width = max(1, len(shape(variable_type)) // len(fields))
                for position, value in enumerate(stored):
                    element, lane = divmod(offset + position, width)
                    stores.append((fields[element], lane, value, (start + step, current, position), scope))
        elif name in _ROUTING:
            if name == "OpCompositeConstruct":
                lanes[result] = [lane for item in references for lane in operand_lanes(item)]
            elif name == "OpCompositeExtract":
                source_type = definitions[references[0]].type_id
                literal = [operand.value for operand in row.operands[3:]]
                offset, part = member_span(source_type, literal)
                lanes[result] = list(operand_lanes(references[0])[offset:offset + len(shape(part))])
            elif name == "OpCompositeInsert":
                literal = [operand.value for operand in row.operands[4:]]
                offset, part = member_span(row.type_id, literal)
                updated = list(operand_lanes(references[1]))
                updated[offset:offset + len(shape(part))] = operand_lanes(references[0])
                lanes[result] = updated
            elif name == "OpVectorShuffle":
                joined = [*operand_lanes(references[0]), *operand_lanes(references[1])]
                lanes[result] = [joined[operand.value] if operand.value != 0xFFFFFFFF else ("constant", "undef")
                                 for operand in row.operands[4:]]
            else:
                lanes[result] = list(operand_lanes(references[0]))
        elif name in _DISCARDS:
            controls = [lane for condition in path_controls(current) for lane in operand_lanes(condition)]
            discards.append((add("join", (), ("discard",), "bool", (row.opcode, current), controls),
                             (start + step, current, None)))
        elif name == "OpReturn":
            exits.append((start + step, current, None))
        elif name == "OpImageWrite":
            image = definitions.get(references[0])
            resource = (contract.resource(descriptors.get(pointers.get(image.values[2], (None,))[0], ""))
                        if image is not None and image.opcode == names["OpLoad"] else None)
            if resource is None:
                raise ShaderGraphError("The program writes an image it does not declare by name.")
            for component, value in enumerate(operand_lanes(references[2])):
                resource_stores.append(_flow_store(resource.name, 0, component, value,
                                                   (start + step, current, component), "typed"))
            resource_scalars[("typed", resource.name, 0)] = shape(definitions[references[2]].type_id)
        elif result is None:
            continue
        else:
            scalars = shape(row.type_id)
            operands = references
            extended = None
            if name == "OpExtInst" and references and references[0] in glsl:
                extended = row.values[3]
                operands = references[1:]
            elif name == "OpExtInst":
                continue
            classification = core[row.opcode].get("class")
            pure = (classification in _PURE_CLASSES and name not in _MIXING) or (
                extended is not None and extended not in _GLSL_SOURCES and extended not in _GLSL_MIXING)
            mixing = name in _MIXING or extended in _GLSL_MIXING
            signature = (name, extended)
            if pure:
                result_lanes = []
                for lane, scalar in enumerate(scalars):
                    picked = []
                    for item in operands:
                        source = operand_lanes(item)
                        picked.extend((source[lane],) if len(source) == len(scalars) else
                                      (source[0],) if len(source) == 1 else source)
                    result_lanes.append(add("pure" if any(isinstance(item, int) for item in picked) else "source",
                                            picked, (*signature, constants(picked)), scalar, (result, lane)))
                lanes[result] = result_lanes
            else:
                every = [lane for item in operands for lane in operand_lanes(item)]
                kind = "pure" if mixing and any(isinstance(item, int) for item in every) else "source"
                family = (*signature, constants(every)) if kind == "pure" else signature
                lanes[result] = [add(kind, every, family, scalar, (result, lane)) for lane, scalar in enumerate(scalars)]
                texture = texture_of(references[0]) if name in _IMAGE_READS else None
                if texture is not None:
                    texture_reads.extend((texture, lane, value, (start + step, current, lane))
                                         for lane, value in enumerate(lanes[result]))
                    read_types[texture] = _TEXEL_TYPES[scalars[0]]

    for value, root in deferred_loads:
        item = values[value]
        values[value] = FlowValue(item.kind, tuple(dict.fromkeys((*item.operands, *memory.get(root, ())))),
                                  item.controls, item.signature, item.scalar, item.origin)

    component_types = {}
    # A store's site is (instruction index, block, stored lane); computed indices, discards and returns have no lane.
    flow_stores = [_flow_store(field.semantic_name.casefold(), field.semantic_index, lane, value, site, scope)
                   for field, lane, value, site, scope in stores]
    for identifier, rows in output_rows.items():
        for field in rows:
            scope = "patch" if identifier in patch_ids else "output"
            component_types[(scope, field.semantic_name.casefold(), field.semantic_index)] = field.component_type
    flow_stores.extend(FlowStore(texture, 0, lane, value, "", site, "read") for texture, lane, value, site in texture_reads)
    component_types.update({("read", texture, 0): kind for texture, kind in read_types.items()})
    flow_stores.extend(resource_stores)
    component_types.update({key: _TEXEL_TYPES[kinds[0]] if key[0] == "typed" else
                            buffer_component_type(kinds)
                            for key, kinds in resource_scalars.items()})
    flow_discards = tuple(FlowStore("discard", 0, 0, value, "", site, "discard") for value, site in discards)
    return NativeFlow(native.stage, tuple(values), tuple(flow_stores), flow_discards, component_types,
                      exits=tuple(exits))


__all__ = ["spirv_flow"]
