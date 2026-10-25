from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
import operator
import re

from file_handlers.sdf.shader_graph import ShaderContract, ShaderContractResource, ShaderGraphError
lazy from ..native_resource import DXIL_RESOURCE_CLASSES, DXIL_TEXTURE_KINDS
lazy from .re_engine_dxil_signatures import DxilEntryMetadata
lazy from ..native_loop_flow import predecessor_map
lazy from ..surface_flow_dxil import _control_flow, _entry
lazy import fastdxiltext


_VALUE = r"%[-$._A-Za-z0-9]+"
_ASSIGN = re.compile(rf"^(?P<result>{_VALUE})\s*=\s*(?P<body>.+)$")
_LEGACY = re.compile(
    r"@dx\.op\.createHandle\(i32 57, i8 (\d+), i32 (\d+),"
)
_BINDING = re.compile(
    r"@dx\.op\.createHandleFromBinding\(i32 217, %dx\.types\.ResBind "
    # DXIL prints UINT_MAX (the unbounded range endpoint) as signed i32 -1.
    r"(zeroinitializer|\{ i32 (\d+), i32 (?:\d+|-1), i32 (\d+), i8 (\d+) \})"
)
_ANNOTATE = re.compile(
    rf"@dx\.op\.annotateHandle\(i32 216, %dx\.types\.Handle ({_VALUE}), "
    r"%dx\.types\.ResourceProperties \{ i32 (\d+), i32 (\d+) \}"
)
_RAW_LOAD = re.compile(
    rf"@dx\.op\.(?:rawBufferLoad|bufferLoad)\.(i32|f32)\(i32 (?:139|68), %dx\.types\.Handle ({_VALUE}), "
    r"i32 ([^,]+), i32 ([^,)]+)(?:, i8 (\d+), i32 (\d+))?\)"
)
_CB_LOAD = re.compile(
    rf"@dx\.op\.cbufferLoadLegacy\.(i32|f32)\(i32 59, %dx\.types\.Handle ({_VALUE}), i32 ([^)]+)\)"
)
_HEAP = re.compile(
    rf"@dx\.op\.createHandleFromHeap\(i32 218, i32 ({_VALUE}), i1 false, i1 (?:false|true)\)"
)
_EXTRACT = re.compile(
    rf"extractvalue %dx\.types\.(?:ResRet|CBufRet)\.(i32|f32) ({_VALUE}), ([0-3])$"
)
_ADD = re.compile(r"add(?: nuw| nsw)* i32 ([^,]+), (.+)$")
_INTEGER_CAST = re.compile(rf"bitcast float ({_VALUE}) to i32$")
_MASK = re.compile(rf"and i32 ({_VALUE}), -?\d+$")
_TEXTURE_INDEX = re.compile(rf", i32 ({_VALUE}), i1 (?:true|false)\)")
# DXIL's resource kind of a raw (byte address) buffer (``annotateHandle``'s first property word, low byte).
_RAW_BUFFER = 11
_U32 = 0xFFFFFFFF
_ENUMERATED = 1 << 12
_OPERAND = rf"{_VALUE}|-?\d+"
_BINARY = re.compile(rf"(add|sub|and|or|shl|lshr)(?: nuw| nsw| exact)* i32 ({_OPERAND}), ({_OPERAND})$")
_OPERATIONS = {"add": operator.add, "sub": operator.sub, "and": operator.and_, "or": operator.or_,
               "shl": operator.lshift, "lshr": operator.rshift}
_COMPARE = re.compile(rf"icmp (eq|ne|[us][gl][te]) i32 ({_OPERAND}), ({_OPERAND})$")
_ELEMENT = re.compile(rf"getelementptr inbounds \[\d+ x i32\], \[\d+ x i32\]\* ({_VALUE}), i32 0, i32 ({_OPERAND})$")
_LOAD = re.compile(rf"load i32, i32\* ({_VALUE})\b")
_STORE = re.compile(rf"store i32 ({_OPERAND}), i32\* ({_VALUE})\b")
_BRANCH = re.compile(rf"br i1 ({_VALUE}), label ({_VALUE}), label ({_VALUE})\b")
_INCOMING = re.compile(rf"\[ ({_OPERAND}), ({_VALUE}) \]")


def _zero_add_source(match) -> str | None:
    left, right = match.groups()
    return left if right == "0" else right if left == "0" else None


def _binding_resource(body: str, by_binding):
    if "@dx.op.createHandleFromBinding(" in body and (match := _BINDING.search(body)):
        lower, space, kind = (0, 0, 0) if match[1] == "zeroinitializer" else tuple(int(match[i]) for i in (2, 3, 4))
        return by_binding.get((DXIL_RESOURCE_CLASSES.get(kind, ""), space, lower))
    return None


def bound_resource(body: str, by_id, by_binding):
    if "@dx.op.createHandle(" in body and (match := _LEGACY.search(body)):
        return by_id.get((DXIL_RESOURCE_CLASSES.get(int(match[1]), ""), int(match[2])))
    return _binding_resource(body, by_binding)


def _signed(value: int) -> int:
    return value - (1 << 32) if value >> 31 else value


_PREDICATES = {"eq": operator.eq, "ne": operator.ne, "ugt": operator.gt, "uge": operator.ge, "ult": operator.lt,
               "ule": operator.le, "sgt": lambda a, b: _signed(a) > _signed(b),
               "sge": lambda a, b: _signed(a) >= _signed(b), "slt": lambda a, b: _signed(a) < _signed(b),
               "sle": lambda a, b: _signed(a) <= _signed(b)}
_NEGATED = {"eq": "ne", "ne": "eq", "ugt": "ule", "ule": "ugt", "uge": "ult", "ult": "uge", "sgt": "sle", "sle": "sgt",
            "sge": "slt", "slt": "sge"}
_SWAPPED = {"eq": "eq", "ne": "ne", "ugt": "ult", "ult": "ugt", "uge": "ule", "ule": "uge", "sgt": "slt", "slt": "sgt",
            "sge": "sle", "sle": "sge"}


@dataclass(frozen=True, slots=True)
class MaterialWordRead:
    byte_offset: int
    value: str
    scalar_type: str
    line_index: int
    load_line_index: int


@dataclass(frozen=True, slots=True)
class BindlessMaterialAccess:
    storage: str
    resource_name: str
    address: str
    words: tuple[MaterialWordRead, ...]
    constant_buffer_size: int = 0


def material_record_accesses(
    lines: list[str],
    contract: ShaderContract,
    *,
    entry_metadata: DxilEntryMetadata | None = None,
    _texture_candidates: list[tuple[str, str]] | None = None,
) -> tuple[BindlessMaterialAccess, ...]:
    if entry_metadata is None and _texture_candidates is None:
        return _shader_record_accesses("\n".join(lines), contract)
    return _record_accesses(lines, contract, entry_metadata=entry_metadata, _texture_candidates=_texture_candidates)


@lru_cache(maxsize=8)
def _shader_record_accesses(assembly: str, contract: ShaderContract) -> tuple[BindlessMaterialAccess, ...]:
    """A shader's readers ask in turn: its flow, then its reach."""
    return _record_accesses(assembly.split("\n"), contract)


def _record_accesses(
    lines: list[str],
    contract: ShaderContract,
    *,
    entry_metadata: DxilEntryMetadata | None = None,
    _texture_candidates: list[tuple[str, str]] | None = None,
) -> tuple[BindlessMaterialAccess, ...]:
    by_id = {(r.register_class, r.resource_id): r for r in contract.resources}
    by_binding = {
        (r.register_class, r.register_space, r.bind_point): r
        for r in contract.resources
    }
    structured = {(b.register_space, b.bind_point): b for b in contract.structured_buffers}
    metadata = entry_metadata or DxilEntryMetadata.parse(lines)
    root = contract.buffer("RootConstant")
    material_redirects = {"BindlessRedirectTbl"}
    native_entry = metadata.stage, metadata.function_name
    arena = contract.resource("BindlessBuffer")
    spline_entry = arena is not None and arena.sample_count == 256 and native_entry in {
        ("compute", "MeshletGeometryTransform"),
        ("vertex", "VS_MeshletStaticVisibilityWrite"),
        ("mesh", "MS_MeshletStaticVisibilityWrite"),
        ("vertex", "VS_MeshletStaticMesh"),
        ("mesh", "MS_MeshletStaticMesh"),
    }
    fog_entry = native_entry == ("pixel", "VolumetricFogCustomMaterialPS")
    speedtree_entry = native_entry in {
        ("vertex", "SpeedTreeV8_MeshletVisibilityWriteVS"),
        ("mesh", "SpeedTreeV8_MeshletVisibilityWriteMS"),
        ("compute", "SpeedTreeV8_MeshletSoftwareRasterizer"),
        ("compute", "SpeedTreeV8_MeshletGeometryTransform"),
    }
    if fog_entry:
        # Instanced fog passes a base list index in the root constant; a uint redirect table supplies each instance's MDF handle.
        material_redirects.add("VolumetricFogBindlessRedirectTbl")
    # The non-instanced entry receives the MDF handle directly as its root constant.
    root_material_handle = (
        fog_entry and contract.resource("VolumetricFogBindlessRedirectTbl") is None
        and root is not None and root.byte_size == 16 and root.bind_count == 1
        and any(v.name == "constant32Bits" and v.byte_offset == 0 and v.byte_size == 4
                and v.graph_type == "uint" and v.array_length == 1 for v in root.variables)
    )
    root_binding = (root.register_space, root.bind_point) if root_material_handle else None
    material_inputs = {
        location for (semantic, index), location in metadata.input_rows().items()
        if semantic in {"materialhandle", "material", "vis_param"} and index == 0
    }

    def describe(resource):
        schema = structured.get((resource.register_space, resource.bind_point))
        return _resource_facts(resource, schema, material_redirects, root_binding)

    return fastdxiltext.record_accesses(
        lines, describe, by_id, by_binding, DXIL_RESOURCE_CLASSES, material_inputs, spline_entry, speedtree_entry,
        arena is not None and arena.sample_count in (16, 256), _texture_candidates, MaterialWordRead,
        BindlessMaterialAccess,
    )


def _resource_facts(resource, schema, material_redirects, root_binding):
    """What the record reader asks of a resource it loads from, in fastdxiltext's Fact order."""
    name, structured = resource.name, resource.input_type == "structured-buffer"
    variables = () if schema is None else schema.variables
    # The current DD2 arena uses uint4[16] pages.
    paged_element = (structured and schema is not None and schema.byte_size == 256 and len(variables) == 1
                     and variables[0].name == "block" and variables[0].byte_offset == 0
                     and variables[0].byte_size == 256 and variables[0].hlsl_declaration == "uint4"
                     and variables[0].array_length == 16)
    return (
        name, name.casefold() == "bindlessconstantbufferviewtbl", structured and resource.sample_count == 4,
        name == "BindlessBuffer", resource.sample_count == 256, paged_element,
        structured and resource.sample_count == 16, name in material_redirects,
        resource.input_type == "byte-address-buffer",
        root_binding is not None and resource.register_class == "b"
        and (resource.register_space, resource.bind_point) == root_binding,
        tuple(v.byte_offset for v in variables if name == "BINDLESSINFOSRV"
              and v.name in {"materialByteOffset", "geometryByteOffset"} and v.byte_size == 4 and v.graph_type == "uint"),
        tuple((v.byte_offset, v.name == "material") for v in variables if name == "Instances"
              and v.byte_size == 4 and v.array_length == 1
              and (v.name == "material" and v.graph_type in {"int", "uint"}
                   or v.name == "extendedBindlessIndex" and v.graph_type == "uint")),
    )


@dataclass(frozen=True, slots=True)
class MaterialTextureAccess:
    """``addressed``: some read names a mip or a texel of the texture outright (loads, sizes, gathers, explicit
    levels), so the texture is read whole."""
    byte_offset: int
    dimension: str
    resource: ShaderContractResource | None = None  # None: descriptor heap.
    addressed: bool = False


# Texture operations taking a handle that name a mip or texel outright: a texture streamed from a later mip reads
# other texels through them.
_ADDRESSED = re.compile(
    r"@dx\.op\.(?:textureLoad|getDimensions|sampleLevel|sampleCmpLevelZero|sampleCmpLevel|textureGather|"
    rf"textureGatherCmp|calculateLOD)[.\w]*\(i32 \d+, %dx\.types\.Handle ({_VALUE})")


def material_texture_accesses(
    lines: list[str], contract: ShaderContract, accesses: tuple[BindlessMaterialAccess, ...],
) -> tuple[MaterialTextureAccess, ...]:
    return _material_texture_accesses(_texture_instructions(lines), contract, accesses)


def _texture_instructions(lines):
    for line in lines:
        if "add " not in line and "Handle" not in line:
            continue
        stripped = line.strip()
        if not stripped.startswith("%"):
            continue
        assignment = _ASSIGN.match(stripped)
        if assignment is not None:
            yield assignment.group("result", "body")


def _material_texture_accesses(instructions, contract, accesses):
    sources = {word.value: word.byte_offset for access in accesses for word in access.words}
    if not sources:
        return ()
    by_id = {(r.register_class, r.resource_id): r for r in contract.resources}
    by_binding = {(r.register_class, r.register_space, r.bind_point): r for r in contract.resources}
    heap_sources = {}
    # The record word's byte offset of each texture handle.
    handles: dict[str, int] = {}
    result = set()
    instructions = list(instructions)
    for value, body in instructions:
        if not body.startswith("add ") and "@dx.op." not in body:
            continue
        if body.startswith("add ") and (match := _ADD.fullmatch(body)):
            source = _zero_add_source(match)
            if source in sources:
                sources[value] = sources[source]
        elif "@dx.op.createHandleFromHeap(" in body and (match := _HEAP.search(body)):
            if match[1] in sources:
                heap_sources[value] = sources[match[1]]
        elif "@dx.op.annotateHandle(" in body and (match := _ANNOTATE.search(body)):
            if match[1] in heap_sources and int(match[2]) & 255 in DXIL_TEXTURE_KINDS:
                result.add(MaterialTextureAccess(heap_sources[match[1]], DXIL_TEXTURE_KINDS[int(match[2]) & 255]))
                handles[value] = heap_sources[match[1]]
            elif match[1] in handles:
                handles[value] = handles[match[1]]
        else:
            resource = bound_resource(body, by_id, by_binding)
            if resource is not None and resource.input_type == "texture" and resource.dimension in DXIL_TEXTURE_KINDS.values():
                index = _TEXTURE_INDEX.search(body)
                if index and index[1] in sources:
                    result.add(MaterialTextureAccess(sources[index[1]], resource.dimension, resource))
                    handles[value] = sources[index[1]]
    addressed = {handles[match[1]] for _value, body in instructions
                 if (match := _ADDRESSED.search(body)) and match[1] in handles}
    result = {MaterialTextureAccess(item.byte_offset, item.dimension, item.resource, True)
              if item.byte_offset in addressed else item for item in result}
    return tuple(sorted(result, key=lambda item: (item.byte_offset, item.dimension,
        item.resource.register_space if item.resource else -1,
        item.resource.bind_point if item.resource else -1)))


@dataclass(frozen=True, slots=True)
class EngineHeapHandle:
    dimension: str | None
    source: ShaderContractResource | None
    handle: str


def engine_heap_handles(lines: list[str], contract: ShaderContract,
                        accesses: tuple[BindlessMaterialAccess, ...]) -> tuple[EngineHeapHandle, ...]:
    words = {word.value for access in accesses for word in access.words}
    buffers = {access.address for access in accesses if access.storage == "descriptor-heap"}
    by_binding = {(r.register_class, r.register_space, r.bind_point): r for r in contract.resources}
    bound, loads, loaded, made, kinds = {}, {}, {}, {}, {}
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("%") or (assignment := _ASSIGN.match(stripped)) is None:
            continue
        value, body = assignment.group("result", "body")
        if body.startswith("add ") and (match := _ADD.fullmatch(body)):
            if _zero_add_source(match) in words:
                words.add(value)
        elif body.startswith("extractvalue ") and (match := _EXTRACT.fullmatch(body)):
            if match[2] in loads:
                loaded[value] = loads[match[2]]
        elif (resource := _binding_resource(body, by_binding)) is not None:
            bound[value] = resource
        elif "@dx.op.createHandleFromHeap(" in body:
            made[value] = match[1] if (match := _HEAP.search(body)) else None
        elif "@dx.op.annotateHandle(" in body and (match := _ANNOTATE.search(body)):
            if match[1] in bound:
                bound[value] = bound[match[1]]
            if match[1] in made:
                kinds[match[1]] = int(match[2]) & 255
        elif (match := _INTEGER_CAST.fullmatch(body) or _MASK.fullmatch(body)) and match[1] in loaded:
            loaded[value] = loaded[match[1]]
        elif "@dx.op." in body and (match := _RAW_LOAD.search(body)) and match[2] in bound:
            loads[value] = bound[match[2]]
    result = []
    for handle, index in made.items():
        # DXIL resource kind 13 is a constant buffer.
        kind = kinds.get(handle, 0)
        if index in words and (kind in DXIL_TEXTURE_KINDS or kind == _RAW_BUFFER) or index in buffers and kind == 13:
            continue
        result.append(EngineHeapHandle(DXIL_TEXTURE_KINDS.get(kind), loaded.get(index), handle))
    return tuple(result)


class HeapIndices:
    """``values`` gives a set of (index, (buffer, byte offset) it was loaded from, or None when computed), or None when
    unknown."""

    def __init__(self, lines: list[str], contract: ShaderContract):
        statements = _entry(lines)[3]
        blocks, successors, _conditions, self.dominators = _control_flow(statements)
        self.predecessors: dict[str, set[str]] = predecessor_map(blocks, successors)
        by_binding = {(r.register_class, r.register_space, r.bind_point): r for r in contract.resources}
        self.defined: dict[str, tuple[str, int, str]] = {}
        self.texts: dict[str, list[str]] = {}
        # ``branches``: block -> (condition, taken label, other label); ``compares``: condition -> (value, predicate,
        # constant).
        self.branches: dict[str, tuple[str, str, str]] = {}
        self.compares: dict[str, tuple[str, str, int]] = {}
        self.buffers: dict[str, str] = {}
        for block, _line, text in statements:
            texts = self.texts.setdefault(block, [])
            if match := _BRANCH.match(text):
                self.branches[block] = match.groups()
            elif assignment := _ASSIGN.match(text):
                value, body = assignment.group("result", "body")
                self.defined[value] = (block, len(texts), body)
                if match := _COMPARE.fullmatch(body):
                    predicate, left, right = match.groups()
                    if left.startswith("%") and not right.startswith("%"):
                        self.compares[value] = (left, predicate, int(right) & _U32)
                    elif right.startswith("%") and not left.startswith("%"):
                        self.compares[value] = (right, _SWAPPED[predicate], int(left) & _U32)
                elif (resource := _binding_resource(body, by_binding)) is not None:
                    if resource.register_class == "b":
                        self.buffers[value] = resource.name
                elif ("@dx.op.annotateHandle(" in body and (match := _ANNOTATE.search(body))
                      and match[1] in self.buffers):
                    self.buffers[value] = self.buffers[match[1]]
            texts.append(text)

    def values(self, handle: str, word: Callable[[str, int], int | None]) -> frozenset | None:
        block, _place, body = self.defined[handle]
        return self._value(_HEAP.search(body)[1], self._facts(block), word, {})

    def _facts(self, block: str) -> frozenset[tuple[str, bool]]:
        facts = set()
        for dominator in self.dominators[block]:
            sources = self.predecessors[dominator]
            if len(sources) == 1 and (branch := self.branches.get(next(iter(sources)))) and branch[1] != branch[2]:
                facts.add((branch[0], dominator == branch[1]))
        return frozenset(facts)

    def _edge(self, source: str, target: str) -> frozenset[tuple[str, bool]]:
        branch = self.branches.get(source)
        facts = self._facts(source)
        return facts | {(branch[0], target == branch[1])} if branch and branch[1] != branch[2] else facts

    def _value(self, operand: str, facts: frozenset, word, memo: dict) -> frozenset | None:
        if not operand.startswith("%"):
            return frozenset({(int(operand) & _U32, None)})
        if (operand, facts) not in memo:
            memo[operand, facts] = self._tested(operand, self._computed(operand, facts, word, memo), facts)
        return memo[operand, facts]

    def _tested(self, value: str, found: frozenset | None, facts: frozenset) -> frozenset | None:
        tests = [(predicate if decided else _NEGATED[predicate], constant) for condition, decided in facts
                 if (compare := self.compares.get(condition)) and compare[0] == value
                 for predicate, constant in (compare[1:],)]
        if found is None:
            low, high = 0, _U32
            for predicate, constant in tests:
                bounds = {"eq": (constant, constant), "ugt": (constant + 1, _U32), "uge": (constant, _U32),
                          "ult": (0, constant - 1), "ule": (0, constant)}.get(predicate, (0, _U32))
                low, high = max(low, bounds[0]), min(high, bounds[1])
            if high - low >= _ENUMERATED:
                return None
            found = frozenset((item, None) for item in range(low, high + 1))
        return frozenset(item for item in found if all(_PREDICATES[predicate](item[0], constant)
                                                       for predicate, constant in tests))

    def _computed(self, value: str, facts: frozenset, word, memo: dict) -> frozenset | None:
        block, place, body = self.defined.get(value, ("", 0, ""))
        if body.startswith("phi i32 "):
            found = set()
            for operand, source in _INCOMING.findall(body):
                incoming = None if block in self.dominators[source] else self._value(
                    operand, self._edge(source, block), word, memo)
                if incoming is None:
                    return None
                found |= incoming
            return frozenset(found)
        if (match := _EXTRACT.fullmatch(body)) and match[1] == "i32":
            load = _CB_LOAD.search(self.defined.get(match[2], ("", 0, ""))[2])
            rows = None if load is None or load[1] != "i32" or load[2] not in self.buffers else self._value(
                load[3], facts, word, memo)
            if rows is None:
                return None
            found = set()
            for row, _source in rows:
                offset = row * 16 + int(match[3]) * 4
                if (held := word(self.buffers[load[2]], offset)) is None:
                    return None
                found.add((held, (self.buffers[load[2]], offset)))
            return frozenset(found)
        if match := _BINARY.fullmatch(body):
            left, right = (self._value(operand, facts, word, memo) for operand in match.group(2, 3))
            if (left is None or right is None or len(left) * len(right) > _ENUMERATED
                    or match[1] in ("shl", "lshr") and any(amount >= 32 for amount, _source in right)):
                return None
            return frozenset((_OPERATIONS[match[1]](a, b) & _U32, None) for a, _ in left for b, _ in right)
        if match := _LOAD.match(body):
            return self._element(block, place, match[1], facts, word, memo)
        return None

    def _element(self, block: str, place: int, pointer: str, facts: frozenset, word, memo: dict) -> frozenset | None:
        element = _ELEMENT.fullmatch(self.defined.get(pointer, ("", 0, ""))[2])
        indices = None if element is None else self._value(element[2], facts, word, memo)
        if indices is None:
            return None
        stored = {}
        for text in self.texts[block][:place]:
            store = _STORE.match(text)
            target = store and _ELEMENT.fullmatch(self.defined.get(store[2], ("", 0, ""))[2])
            if target and target[1] == element[1]:
                if target[2].startswith("%"):
                    return None
                stored[int(target[2])] = store[1]
        found = set()
        for index, _source in indices:
            held = self._value(stored[index], facts, word, memo) if index in stored else None
            if held is None:
                return None
            found |= held
        return frozenset(found)


def material_buffer_reads(lines: list[str], accesses: tuple[BindlessMaterialAccess, ...]) -> tuple[int, ...]:
    """Raw buffers named by record words are the material's GPU buffers."""
    sources = {word.value: word.byte_offset for access in accesses for word in access.words}
    made, result = {}, set()
    for value, body in _texture_instructions(lines):
        if body.startswith("add ") and (match := _ADD.fullmatch(body)):
            source = _zero_add_source(match)
            if source in sources:
                sources[value] = sources[source]
        elif "@dx.op.createHandleFromHeap(" in body and (match := _HEAP.search(body)) and match[1] in sources:
            made[value] = sources[match[1]]
        elif "@dx.op.annotateHandle(" in body and (match := _ANNOTATE.search(body)):
            if match[1] in made and int(match[2]) & 255 == _RAW_BUFFER:
                result.add(made[match[1]])
    return tuple(sorted(result))


def texture_handles(lines: list[str], contract: ShaderContract,
                    accesses: tuple[BindlessMaterialAccess, ...] = (), *, writable: bool = False) -> dict[str, str]:
    sources = {word.value: word.byte_offset for access in accesses for word in access.words}
    by_id = {(r.register_class, r.resource_id): r for r in contract.resources}
    by_binding = {(r.register_class, r.register_space, r.bind_point): r for r in contract.resources}

    def describe(resource):
        wanted = resource.input_type.startswith("rw") if writable else resource.input_type == "texture"
        return wanted, resource.bind_count == 1, resource.name
    return fastdxiltext.texture_handles(lines, sources, by_id, by_binding, DXIL_RESOURCE_CLASSES, describe)


def material_input_accesses(
    lines: list[str], contract: ShaderContract,
    *, entry_metadata: DxilEntryMetadata | None = None,
) -> tuple[tuple[BindlessMaterialAccess, ...], tuple[MaterialTextureAccess, ...]]:
    instructions = []
    accesses = material_record_accesses(
        lines, contract, entry_metadata=entry_metadata,
        _texture_candidates=instructions,
    )
    return accesses, _material_texture_accesses(instructions, contract, accesses)


def relocate_material_record_words(
    lines: list[str], contract: ShaderContract, offsets: dict[int, int],
    *, entry_metadata: DxilEntryMetadata | None = None,
) -> list[str]:
    if not any(source != target for source, target in offsets.items()):
        return lines
    replacements: dict[int, list[str]] = {}
    for access_index, access in enumerate(material_record_accesses(lines, contract, entry_metadata=entry_metadata)):
        for word_index, word in enumerate(access.words):
            target = offsets.get(word.byte_offset, word.byte_offset)
            if target == word.byte_offset:
                continue
            if target < 0 or target % 4:
                raise ShaderGraphError("Relocated material word has an invalid byte offset.")
            register, lane = divmod(target // 4, 4)
            name = f"%reasy.material.relocate.{access_index}.{word_index}"
            source = lines[word.load_line_index]
            scalar = word.scalar_type
            if access.storage in {"structured-buffer", "paged-structured-buffer"}:
                load = _RAW_LOAD.search(source)
                if load is None:
                    raise ShaderGraphError("Material word lost its structured-buffer load.")
                address = access.address
                prefix = []
                if access.storage == "paged-structured-buffer":
                    byte_address = name + ".bytes"
                    address, element_offset = name + ".page", name + ".offset"
                    prefix.extend((
                        f"  {byte_address} = add i32 {access.address}, {target}",
                        f"  {address} = lshr i32 {byte_address}, 8",
                        f"  {element_offset} = and i32 {byte_address}, 255",
                    ))
                elif register:
                    address = name + ".address"
                    prefix.append(f"  {address} = add i32 {access.address}, {register}")
                if access.storage == "structured-buffer":
                    element_offset = str(lane * 4)
                operation, opcode, suffix = ("bufferLoad", 68, "") if load[5] is None else ("rawBufferLoad", 139, ", i8 1, i32 4")
                prefix.append(f"  {name} = call %dx.types.ResRet.{scalar} @dx.op.{operation}.{scalar}(i32 {opcode}, %dx.types.Handle {load[2]}, i32 {address}, i32 {element_offset}{suffix})")
                prefix.append(f"  {word.value} = extractvalue %dx.types.ResRet.{scalar} {name}, 0")
            else:
                load = _CB_LOAD.search(source)
                if load is None:
                    raise ShaderGraphError("Material word lost its constant-buffer load.")
                if access.constant_buffer_size and target + 4 > access.constant_buffer_size:
                    raise ShaderGraphError("Relocated material word exceeds the native CBV range.")
                prefix = [
                    f"  {name} = call %dx.types.CBufRet.{scalar} @dx.op.cbufferLoadLegacy.{scalar}(i32 59, %dx.types.Handle {load[2]}, i32 {register})",
                    f"  {word.value} = extractvalue %dx.types.CBufRet.{scalar} {name}, {lane}",
                ]
            replacements[word.line_index] = prefix
    return [*replacements.get(index, (line,)) for index, line in enumerate(lines)]


__all__ = ["BindlessMaterialAccess", "EngineHeapHandle", "HeapIndices", "MaterialWordRead", "MaterialTextureAccess", "bound_resource", "engine_heap_handles", "material_buffer_reads", "material_record_accesses", "material_input_accesses", "material_texture_accesses", "relocate_material_record_words", "texture_handles"]
