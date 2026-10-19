from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import operator
import re

from file_handlers.sdf.shader_graph import ShaderContract, ShaderContractResource, ShaderGraphError
from ..native_resource import DXIL_RESOURCE_CLASSES, DXIL_TEXTURE_KINDS
from .re_engine_dxil_signatures import DxilEntryMetadata


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
_INPUT = re.compile(r"@dx\.op\.loadInput\.i32\(i32 4, i32 (\d+), i32 (\d+), i8 (\d+),")
_SHIFT = re.compile(r"(?:lshr|shl)(?: nuw| nsw)* i32 ([^,]+), (\d+)$")
_SELECT = re.compile(rf"select i1 [^,]+, i32 ({_VALUE}|-?\d+), i32 ({_VALUE}|-?\d+)$")
_INTEGER_DEPENDENCIES = re.compile(r"(?:add|sub|mul|shl|lshr|ashr|and|or|xor|select|phi)\b")
_VALUES = re.compile(_VALUE)
_INTEGER = re.compile(r"-?\d+")
_DIVIDE_REGISTER = re.compile(r"[su]div i32 ([^,]+), 16")
_WAVE_MIN = re.compile(r"@dx\.op\.waveActiveOp\.i32\(i32 119, i32 ([^,]+), i8 2, i8 1\)")
# Kunitsu-Gami/DRDR make the material handle wave-uniform with WaveReadLaneFirst instead of WaveActiveMin.
_WAVE_FIRST = re.compile(rf"@dx\.op\.waveReadLaneFirst\.i32\(i32 118, i32 ({_VALUE})\)")
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
    by_id = {(r.register_class, r.resource_id): r for r in contract.resources}
    by_binding = {
        (r.register_class, r.register_space, r.bind_point): r
        for r in contract.resources
    }
    handles = {}
    # Values read from the CBV table are descriptor indices, not material words.
    table_loads: dict[str, str] = {}
    table_indices: set[str] = set()
    integer_dependencies = {}
    descriptors: set[str] = set()
    heaps: dict[str, str] = {}
    dynamic_handles: dict[str, tuple[str, int]] = {}
    affine: dict[str, tuple[str, int]] = {}
    parameter_register_offsets: dict[str, int] = {}
    loads: dict[str, tuple[tuple[str, str, str, int], int, int]] = {}
    words: dict[tuple[str, str, str, int], list[MaterialWordRead]] = {}
    origins: dict[str, str] = {}
    named_loads = {}
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
    root_handle_loads = set()
    expressions = {}
    material_inputs = {
        location for (semantic, index), location in metadata.input_rows().items()
        if semantic in {"materialhandle", "material", "vis_param"} and index == 0
    }

    def index(value: str) -> tuple[str, int]:
        return affine.get(value, (value, 0))

    def merged_origin(values):
        kinds = {"invalid-handle" if value == "-1" else origins.get(value) for value in values}
        if kinds == {"material-handle", "invalid-handle"}:
            return "material-handle"
        if len(kinds) == 1:
            return next(iter(kinds))
        if kinds == {"renderer-record-bytes", "renderer-record-word"}:
            return "renderer-record-bytes"
        return None

    def paged_address(element, element_offset):
        page_value, page_delta = index(element)
        page = re.fullmatch(rf"lshr(?: nuw| nsw)* i32 ({_VALUE}), 8", expressions.get(page_value, ""))
        if page is None:
            raise ShaderGraphError("Paged shared-buffer load has no proven page address.")
        address = page[1]
        base, byte_offset = index(address)
        if element_offset.isdecimal():
            # Material allocations are page-aligned; renderer records are not.
            if origins.get(base) != "material-handle" or byte_offset % 256:
                raise ShaderGraphError("Constant page offset requires a page-aligned material allocation.")
            return base, byte_offset + page_delta * 256 + int(element_offset)
        masked = re.fullmatch(rf"and i32 ({_VALUE}), (255|240)", expressions.get(element_offset, ""))
        masked_base, masked_offset = index(masked[1]) if masked else (None, 0)
        if masked and masked_base == base and (masked_offset - byte_offset) % 256 == 0 and page_delta == 0:
            return base, byte_offset if masked[2] == "255" else byte_offset & -16
        aligned = re.fullmatch(rf"and i32 ({_VALUE}), -16", expressions.get(element_offset, ""))
        remainder = re.fullmatch(rf"sub i32 ({_VALUE}), ({_VALUE})", expressions.get(aligned[1], "")) if aligned else None
        page_bytes = re.fullmatch(rf"shl(?: nuw| nsw)* i32 ({_VALUE}), 8", expressions.get(remainder[2], "")) if remainder else None
        if remainder and page_bytes and remainder[1] == address and page_bytes[1] == page_value and page_delta == 0:
            return base, byte_offset & -16
        raise ShaderGraphError("Paged shared-buffer load has no proven page/remainder address.")

    for line_index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("%"):
            continue
        assignment = _ASSIGN.match(stripped)
        if assignment is None:
            continue
        result, body = assignment.group("result", "body")
        if _texture_candidates is not None and (
            body.startswith("add ") or "Handle" in body
        ):
            _texture_candidates.append((result, body))
        if _INTEGER_DEPENDENCIES.match(body):
            integer_dependencies[result] = tuple(_VALUES.findall(body))
        elif not body.startswith(("extractvalue ", "sdiv ", "udiv ")) and "@dx.op." not in body:
            continue
        if body.startswith(("lshr ", "and ", "sub ", "shl ")):
            expressions[result] = body.split(";", 1)[0].strip()
        if "@dx.op.loadInput.i32(" in body and (match := _INPUT.search(body)):
            signature, row, lane = map(int, match.groups())
            if (signature, row) in material_inputs and lane == 0:
                origins[result] = "material-handle"
        elif (resource := bound_resource(body, by_id, by_binding)) is not None:
            handles[result] = resource
        elif "@dx.op.annotateHandle(" in body and (match := _ANNOTATE.search(body)):
            base, kind, size = match.groups()
            if base in handles:
                handles[result] = handles[base]
            if base in heaps and int(kind) == 13:
                dynamic_handles[result] = (heaps[base], int(size))
        elif "@dx.op.createHandleFromHeap(" in body and (match := _HEAP.search(body)):
            if match[1] in descriptors:
                heaps[result] = match[1]
        elif body.startswith("add ") and (match := _ADD.fullmatch(body)):
            left, right = match.groups()
            if _INTEGER.fullmatch(left):
                left, right = right, left
            if _INTEGER.fullmatch(right):
                base, offset = index(left)
                affine[result] = (base, offset + int(right))
                if left in origins:
                    origins[result] = origins[left]
            else:
                for kind in ("meshlet-material-table-bytes", "meshlet-material-table-register", "renderer-record-bytes", "renderer-record-register"):
                    if kind in {origins.get(left), origins.get(right)}:
                        origins[result] = kind
                        break
        elif body.startswith(("sdiv ", "udiv ")) and (match := _DIVIDE_REGISTER.fullmatch(body)):
            if origins.get(match[1]) == "meshlet-material-table-bytes":
                origins[result] = "meshlet-material-table-register"
        elif body.startswith(("lshr ", "shl ")) and (match := _SHIFT.fullmatch(body)):
            source, amount = match[1], int(match[2])
            if body.startswith("lshr"):
                kind = origins.get(source)
                if kind == "material-handle" and amount in (4, 8):
                    origins[result] = "material-register-address" if amount == 4 else "material-cbv-index"
                elif kind == "meshlet-material-table-bytes" and amount == 4:
                    origins[result] = "meshlet-material-table-register"
                elif kind == "renderer-record-bytes" and amount == 4:
                    origins[result] = "renderer-record-register"
                elif kind == "speedtree-extension-bytes" and amount == 4:
                    origins[result] = "speedtree-extension-register"
                elif kind == "speedtree-parameters-bytes" and amount == 4:
                    origins[result] = "speedtree-parameters-register"
                    parameter_register_offsets[result] = index(source)[1]
                elif kind == "speedtree-wind-buffer-bytes" and amount == 8:
                    origins[result] = "renderer-cbv-index"
        elif body.startswith("select ") and (match := _SELECT.fullmatch(body)):
            if kind := merged_origin(match.groups()):
                origins[result] = kind
        elif "@dx.op.waveActiveOp.i32(" in body and (match := _WAVE_MIN.search(body)):
            if match[1] == "-1":
                origins[result] = "invalid-handle"
            elif match[1] in origins:
                origins[result] = origins[match[1]]
        elif "@dx.op.waveReadLaneFirst.i32(" in body and (match := _WAVE_FIRST.search(body)):
            if match[1] in origins:
                origins[result] = origins[match[1]]
        elif body.startswith("phi i32 "):
            values = [value for value, _block in _INCOMING.findall(body)]
            if values and (kind := merged_origin(values)):
                origins[result] = kind
        elif ("@dx.op.rawBufferLoad." in body or "@dx.op.bufferLoad." in body) and (match := _RAW_LOAD.search(body)):
            scalar_type, handle, element, element_offset, mask, alignment = match.groups()
            resource = handles.get(handle)
            if resource is None:
                continue
            if element_offset.isdecimal():
                named_loads[result] = (resource, int(element_offset))
            elif resource.name in material_redirects and resource.input_type == "byte-address-buffer" and element_offset == "undef" and mask in (None, "1"):
                named_loads[result] = (resource, 0)
            name = resource.name.casefold()
            if name == "bindlessconstantbufferviewtbl":
                if resource.input_type != "structured-buffer" or resource.sample_count != 4:
                    raise ShaderGraphError("Material CBV table has an unexpected element type.")
                if element_offset == "0" and mask in (None, "1"):
                    table_loads[result] = element
                    table_indices.add(element)
                else:
                    raise ShaderGraphError("Material CBV selection has an unsupported table element access.")
            elif resource.name == "BindlessBuffer":
                schema = structured.get((resource.register_space, resource.bind_point))
                if resource.sample_count == 256:
                    # The current DD2 arena uses uint4[16] pages.
                    valid_element = (resource.input_type == "structured-buffer" and schema is not None
                        and schema.byte_size == 256 and len(schema.variables) == 1
                        and schema.variables[0].name == "block" and schema.variables[0].byte_offset == 0
                        and schema.variables[0].byte_size == 256 and schema.variables[0].hlsl_declaration == "uint4"
                        and schema.variables[0].array_length == 16)
                    if not valid_element:
                        raise ShaderGraphError("Paged shared buffer has an unexpected element type.")
                    base, byte_offset = paged_address(element, element_offset)
                    key = ("paged-structured-buffer", resource.name, base, 0)
                    loads[result] = (key, byte_offset, line_index)
                    continue
                if resource.input_type != "structured-buffer" or resource.sample_count != 16:
                    raise ShaderGraphError("Material shared buffer has an unexpected element type.")
                if not re.fullmatch(r"\d+", element_offset):
                    raise ShaderGraphError("Material load has a nonconstant element offset.")
                base, register_offset = index(element)
                byte_offset = register_offset * 16 + int(element_offset)
                # WotS shifts (parameterBase + fieldOffset); other variants add the offset after shifting.
                byte_offset += parameter_register_offsets.get(base, 0)
                if byte_offset < 0 or byte_offset % 4:
                    raise ShaderGraphError("Material load has an invalid byte offset.")
                key = ("structured-buffer", resource.name, base, 0)
                loads[result] = (key, byte_offset, line_index)
        elif "@dx.op.cbufferLoadLegacy." in body and (match := _CB_LOAD.search(body)):
            scalar_type, handle, register = match.groups()
            resource = handles.get(handle)
            if (root_material_handle and scalar_type == "i32" and register == "0"
                    and resource is not None and resource.register_class == "b"
                    and (resource.register_space, resource.bind_point) == (root.register_space, root.bind_point)):
                root_handle_loads.add(result)
            dynamic = dynamic_handles.get(handle)
            if dynamic is None:
                continue
            descriptor, size = dynamic
            if origins.get(descriptor) == "renderer-cbv-descriptor":
                continue
            if not register.isdigit():
                raise ShaderGraphError("Material constant-buffer indexing is dynamic.")
            key = ("descriptor-heap", "BindlessConstantBufferViewTbl", descriptor, size)
            loads[result] = (key, int(register) * 16, line_index)
        elif body.startswith("extractvalue ") and (match := _EXTRACT.fullmatch(body)):
            scalar_type, loaded, component = match.groups()
            if loaded in root_handle_loads and scalar_type == "i32" and component == "0":
                origins[result] = "material-handle"
            if loaded in named_loads:
                resource, offset = named_loads[loaded]
                if resource.name in material_redirects and component == "0":
                    origins[result] = "material-handle"
                schema = structured.get((resource.register_space, resource.bind_point))
                if resource.name == "BINDLESSINFOSRV" and schema is not None and any(
                    v.name in {"materialByteOffset", "geometryByteOffset"} and v.byte_offset == offset + int(component) * 4
                    and v.byte_size == 4 and v.graph_type == "uint" for v in schema.variables
                ):
                    origins[result] = "renderer-record-bytes"
                if resource.name == "Instances" and schema is not None:
                    for variable in schema.variables:
                        if (variable.byte_offset != offset + int(component) * 4
                                or variable.byte_size != 4 or variable.array_length != 1):
                            continue
                        if variable.name == "material" and variable.graph_type in {"int", "uint"}:
                            origins[result] = "meshlet-material-table-bytes"
                        elif (speedtree_entry and variable.name == "extendedBindlessIndex"
                              and variable.graph_type == "uint" and scalar_type == "i32"):
                            origins[result] = "speedtree-extension-bytes"
                        elif (spline_entry
                              and variable.name == "extendedBindlessIndex"
                              and variable.graph_type == "uint" and scalar_type == "i32"):
                            origins[result] = "spline-extension-bytes"
            if loaded in table_loads and component == "0":
                descriptors.add(result)
                if origins.get(table_loads[loaded]) == "renderer-cbv-index":
                    origins[result] = "renderer-cbv-descriptor"
            load = loads.get(loaded)
            if load is None:
                continue
            key, offset, load_line_index = load
            offset += int(component) * 4
            if origins.get(key[2]) in {"renderer-record-register", "renderer-record-bytes"}:
                origins[result] = "renderer-record-word"
            if origins.get(key[2]) in {"speedtree-extension-register", "speedtree-extension-bytes"} and offset == 0:
                # Extension word zero points to per-instance SpeedTree parameters, not the meshlet MDF handle table.
                origins[result] = "speedtree-parameters-bytes"
            if (origins.get(key[2]) in {"speedtree-parameters-bytes", "speedtree-parameters-register"}
                    and offset in (16, 20) and arena is not None and arena.sample_count in (16, 256)):
                # DD2/WotS: current/previous wind-buffer handles at +16/+20, wind-state index at +24.
                origins[result] = "speedtree-wind-buffer-bytes"
            if origins.get(key[2]) == "spline-extension-bytes" and offset in (0, 4, 8):
                origins[result] = "renderer-record-bytes"
            if origins.get(key[2]) in {"meshlet-material-table-register", "meshlet-material-table-bytes"}:
                # Meshlet material entries are two uint2 handles per uint4: X/Z address the MDF allocation, Y/W are flags.
                if offset in (0, 8):
                    origins[result] = "material-handle"
            if key[3] and offset + 4 > key[3]:
                raise ShaderGraphError("Material word exceeds its declared constant buffer.")
            words.setdefault(key, []).append(
                MaterialWordRead(offset, result, scalar_type, line_index, load_line_index)
            )
    word_records = {word.value: key for key, reads in words.items() for word in reads}
    visited, pending = set(), list(table_indices)
    while pending:
        value = pending.pop()
        if value in visited:
            continue
        visited.add(value)
        key = word_records.get(value)
        if key is not None:
            if key[0] in {"structured-buffer", "paged-structured-buffer"} and origins.get(key[2]) not in {"material-register-address", "material-handle"}:
                origins[key[2]] = "material-indirection-record"
        else:
            pending.extend(integer_dependencies.get(value, ()))
    unresolved = [key[2] for key in words if key[0] in {"structured-buffer", "paged-structured-buffer"} and origins.get(key[2]) not in {
        "material-register-address", "meshlet-material-table-register", "material-indirection-record", "renderer-record-register",
        "speedtree-extension-register", "material-handle", "meshlet-material-table-bytes", "renderer-record-bytes",
        "speedtree-extension-bytes", "spline-extension-bytes",
        "speedtree-parameters-bytes", "speedtree-parameters-register",
    }]
    if unresolved:
        raise ShaderGraphError("Shared-buffer access has no proven material/renderer address origin: " + ", ".join(unresolved))
    return tuple(
        BindlessMaterialAccess(storage, name, address, tuple(reads), size)
        for (storage, name, address, size), reads in words.items()
        if storage == "descriptor-heap" or origins.get(address) == "material-register-address"
        or (storage == "paged-structured-buffer" and origins.get(address) == "material-handle")
    )


@dataclass(frozen=True, slots=True)
class MaterialTextureAccess:
    byte_offset: int
    dimension: str
    resource: ShaderContractResource | None = None  # None: descriptor heap.


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
    result = set()
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
        else:
            resource = bound_resource(body, by_id, by_binding)
            if resource is not None and resource.input_type == "texture" and resource.dimension in DXIL_TEXTURE_KINDS.values():
                index = _TEXTURE_INDEX.search(body)
                if index and index[1] in sources:
                    result.add(MaterialTextureAccess(sources[index[1]], resource.dimension, resource))
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
        from ..native_loop_flow import predecessor_map
        from ..surface_flow_dxil import _control_flow, _entry
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
    result: dict[str, str] = {}
    for value, body in _texture_instructions(lines):
        if body.startswith("add ") and (match := _ADD.fullmatch(body)):
            source = _zero_add_source(match)
            if source in sources:
                sources[value] = sources[source]
        elif "@dx.op.createHandleFromHeap(" in body and (match := _HEAP.search(body)):
            if match[1] in sources:
                result[value] = f"@{sources[match[1]]}"
        elif "@dx.op.annotateHandle(" in body and (match := _ANNOTATE.search(body)):
            if match[1] in result:
                result[value] = result[match[1]]
        else:
            resource = bound_resource(body, by_id, by_binding)
            if resource is None or (not resource.input_type.startswith("rw") if writable
                                    else resource.input_type != "texture"):
                continue
            index = _TEXTURE_INDEX.search(body)
            if index and index[1] in sources:
                result[value] = f"@{sources[index[1]]}"
            elif resource.bind_count == 1:
                result[value] = resource.name
    return result


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
    return [replacement for index, line in enumerate(lines) for replacement in replacements.get(index, (line,))]


__all__ = ["BindlessMaterialAccess", "EngineHeapHandle", "HeapIndices", "MaterialWordRead", "MaterialTextureAccess", "bound_resource", "engine_heap_handles", "material_buffer_reads", "material_record_accesses", "material_input_accesses", "material_texture_accesses", "relocate_material_record_words", "texture_handles"]
