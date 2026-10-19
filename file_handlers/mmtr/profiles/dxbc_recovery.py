from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import re
import struct
from typing import Iterable, Mapping

from file_handlers.sdf.dxbc import DxbcDisassemblyError, disassemble_dxbc
from file_handlers.sdf.hlsl_contract import resource_declaration, resource_identifier
from file_handlers.sdf.shader_graph import (
    GeneratedShaderSource,
    ShaderContract,
    ShaderContractBuffer,
    ShaderContractResource,
    ShaderGraphError,
    _constant_buffer_identifiers,
    _identifier,
    signature_declaration,
    signature_member,
    mask_components,
    texture_element_type,
    texture_hlsl_type,
)
from file_handlers.sdf.shader_graph_texture import texture_coordinate_width
from file_handlers.sdf.shader_stage_execution import (
    ShaderPrimitiveExecution as DxbcStageExecution, patch_field_members, patch_struct,
)
from file_handlers.sdf.storage_codec import pack_ir_storage, unpack_ir_storage
from .dxbc_stages import stage_declaration, pixel_signature_interpolation, signature_packing_modes


IR_SCHEMA = "reasy.dmc5.dxbc-ir"
IR_VERSION = 1
MAX_IR_JSON_BYTES = 4 * 1024 * 1024

_LINE_PREFIX = re.compile(
    r"^\s*(?:\d+\s+)?(?:0x[0-9A-Fa-f]+:\s+)?"
)
_RESOURCE = re.compile(
    r"^(?P<class>[tsu])(?P<index>\d+)"
    r"(?:\.(?P<swizzle>[xyzw]{1,4}))?$",
    re.IGNORECASE,
)
_CBUFFER = re.compile(
    r"^cb(?P<index>\d+)\[(?P<register>[^\]]+)\]"
    r"(?:\.(?P<swizzle>[xyzw]{1,4}))?$",
    re.IGNORECASE,
)
_TEMP = re.compile(
    r"^(?P<class>[rvo])(?P<index>\d+)"
    r"(?:\.(?P<swizzle>[xyzw]{1,4}))?$",
    re.IGNORECASE,
)
_INDEXABLE_TEMP = re.compile(
    r"^x(?P<array>\d+)\[(?P<index>[^\]]+)\]"
    r"(?:\.(?P<swizzle>[xyzw]{1,4}))?$",
    re.IGNORECASE,
)
_IMMEDIATE_BUFFER = re.compile(
    r"^icb\[(?P<index>[^\]]+)\]"
    r"(?:\.(?P<swizzle>[xyzw]{1,4}))?$",
    re.IGNORECASE,
)
_DECL_TEMPS = re.compile(r"^dcl_temps\s+(?P<count>\d+)$", re.IGNORECASE)
_DECL_INDEXABLE_TEMP = re.compile(
    r"^dcl_indexableTemp\s+x(?P<array>\d+)\[(?P<count>\d+)\],\s*(?P<components>[1-4])$",
    re.IGNORECASE,
)
_STAGE = re.compile(r"^(?:ps|vs|gs|hs|ds|cs)_\d+_\d+$", re.IGNORECASE)
_COMPUTE_INPUTS = {
    "vthreadid": ("uint3", "SV_DispatchThreadID"),
    "vthreadgroupid": ("uint3", "SV_GroupID"),
    "vthreadidingroup": ("uint3", "SV_GroupThreadID"),
    "vthreadidingroupflattened": ("uint", "SV_GroupIndex"),
}
_COMPUTE_INPUT = re.compile(r"^(vThreadID|vThreadGroupID|vThreadIDInGroup|vThreadIDInGroupFlattened)(?:\.([xyz]{1,4}))?$", re.IGNORECASE)
_PRIMITIVE_SYSTEM_INPUTS = {"vprim": "SV_PrimitiveID", "vgsinstanceid": "SV_GSInstanceID",
                          "voutputcontrolpointid": "SV_OutputControlPointID", "vforkinstanceid": "", "vjoininstanceid": ""}
_PIXEL_SYSTEM_INPUTS = {"vcoverage": "SV_Coverage"}
_PIXEL_OUTPUTS = {"odepth": "sv_depth", "odepthge": "sv_depthgreaterequal",
                  "odepthle": "sv_depthlessequal", "omask": "sv_coverage"}


def _stage_output_type(stream):
    return "REasyStageOutput" + (str(stream) if stream else "")


def _geometry_stream_name(stream):
    return "stream" + (str(stream) if stream else "")


_OMITTING_STAGES = ("vs", "ps", "ds")


def _never_written(field):
    return getattr(field, "read_write_mask", 0) & field.mask


def _unwritten_outputs(ir, contract):
    if ir.stage not in _OMITTING_STAGES:
        return ()
    return tuple((field.semantic_name, field.semantic_index, _never_written(field))
                 for field in contract.outputs if _never_written(field))


def _output_register(field):
    # Depth and coverage both use signature register UINT_MAX but are separate operand types.
    if field.register == 0xffffffff:
        name = next((name for name, semantic in _PIXEL_OUTPUTS.items()
                     if semantic == field.semantic_name.casefold()), None)
        if name is None:
            raise ShaderGraphError(f"Unknown special pixel output {field.semantic_name}")
        return name
    return f"o{field.register}"


@dataclass(frozen=True, slots=True)
class DxbcInstruction:
    opcode: str
    operands: tuple[str, ...] = ()
    precise_mask: str = ""
    texel_offset: tuple[int, ...] = ()

    def __post_init__(self):
        if not isinstance(self.precise_mask, str) or self.precise_mask != "".join(
            lane for lane in "xyzw" if lane in self.precise_mask
        ):
            raise ShaderGraphError("Recovered DXBC instruction has an invalid precise mask.")
        if self.texel_offset and (len(self.texel_offset) != 3 or any(type(value) is not int or not -8 <= value <= 7 for value in self.texel_offset)):
            raise ShaderGraphError('Recovered DXBC instruction has an invalid immediate texel offset.')
        if bool(self.texel_offset) != ('_aoffimmi' in self.opcode):
            raise ShaderGraphError('The native opcode and its immediate texel offset must agree.')

    def to_dict(self) -> dict[str, object]:
        return {"op": self.opcode, "args": list(self.operands), **({"precise": self.precise_mask} if self.precise_mask else {}),
                **({'texel_offset': list(self.texel_offset)} if self.texel_offset else {})}

    @classmethod
    def from_dict(cls, value: object) -> "DxbcInstruction":
        if not isinstance(value, dict):
            raise ShaderGraphError("recovered DXBC instruction is invalid")
        opcode = str(value.get("op", "")).strip().casefold()
        operands = value.get("args", [])
        if (
            not opcode
            or len(opcode) > 96
            or not isinstance(operands, list)
            or len(operands) > 8
            or any(not isinstance(item, str) or len(item) > 512 for item in operands)
        ):
            raise ShaderGraphError("recovered DXBC instruction is invalid")
        return cls(opcode, tuple(operands), value.get("precise", ""), tuple(value.get('texel_offset', ())))


@dataclass(frozen=True, slots=True)
class DxbcHullPhase:
    kind: str
    start: int
    instance_count: int = 1
    temp_count: int = 0
    indexable_temps: tuple[tuple[int, int, int], ...] = ()

    def __post_init__(self):
        if (self.kind not in {"control_point", "fork", "join"} or self.start < 0
                or not 1 <= self.instance_count <= 32 or not 0 <= self.temp_count <= 4096
                or (self.kind == "control_point" and self.instance_count != 1)):
            raise ShaderGraphError("Invalid native hull phase")


@dataclass(frozen=True, slots=True)
class DxbcSemanticIr:
    stage: str
    temp_count: int
    indexable_temps: tuple[tuple[int, int, int], ...]
    immediate_constants: tuple[tuple[str, str, str, str], ...]
    instructions: tuple[DxbcInstruction, ...]
    thread_group_size: tuple[int, int, int] | None = None
    compute_inputs: tuple[str, ...] = ()
    global_flags: tuple[str, ...] = ("refactoringAllowed",)
    input_interpolation: tuple[tuple[int, str, str], ...] = ()
    shared_memory: tuple[tuple[int, int, int], ...] = ()  # register, stride (0 = raw), element/byte count
    globally_coherent_uavs: tuple[int, ...] = ()
    execution: DxbcStageExecution = DxbcStageExecution()
    primitive_system_inputs: tuple[str, ...] = ()
    hull_phases: tuple[DxbcHullPhase, ...] = ()
    pixel_system_inputs: tuple[str, ...] = ()

    def __post_init__(self):
        self.execution.validate_stage(self.stage)
        if set(self.primitive_system_inputs) - _PRIMITIVE_SYSTEM_INPUTS.keys():
            raise ShaderGraphError("Unsupported primitive system input")
        if (set(self.pixel_system_inputs) - _PIXEL_SYSTEM_INPUTS.keys()
                or (self.pixel_system_inputs and self.stage != "ps")
                or len(set(self.pixel_system_inputs)) != len(self.pixel_system_inputs)):
            raise ShaderGraphError("Invalid pixel system inputs")
        if self.hull_phases:
            starts = [phase.start for phase in self.hull_phases]
            if (self.stage != "hs" or starts[0] != 0 or sorted(set(starts)) != starts
                    or starts[-1] >= len(self.instructions)
                    or any(p.kind == "control_point" for p in self.hull_phases[1:])):
                raise ShaderGraphError("Invalid hull phase instruction ownership")
        shared_ids = set()
        for register, stride, count in self.shared_memory:
            if self.stage != "cs" or register < 0 or register in shared_ids or stride < 0 or stride % 4 or count <= 0 or (stride == 0 and count % 4):
                raise ShaderGraphError("Recovered DXBC group-shared declaration is invalid.")
            shared_ids.add(register)
        if sum(stride * count if stride else count for _, stride, count in self.shared_memory) > 32768:
            raise ShaderGraphError("Recovered SM5 group-shared allocation exceeds 32 KiB.")
        if any(i < 0 for i in self.globally_coherent_uavs) or len(set(self.globally_coherent_uavs)) != len(self.globally_coherent_uavs):
            raise ShaderGraphError("Recovered DXBC UAV coherency declarations are invalid.")
        known_flags = {"refactoringAllowed", "enableDoublePrecisionFloatOps", "forceEarlyDepthStencil",
                       "enableRawAndStructuredBuffers", "skipOptimization", "enableMinimumPrecision",
                       "enableDoubleExtensions", "enableShaderExtensions", "allResourcesBound"}
        if len(set(self.global_flags)) != len(self.global_flags) or set(self.global_flags) - known_flags:
            raise ShaderGraphError("Recovered DXBC contains unsupported global execution flags.")
        if self.stage != "ps" and (self.input_interpolation or "forceEarlyDepthStencil" in self.global_flags):
            raise ShaderGraphError("Pixel execution declarations occur in a non-pixel program.")
        occupied = set()
        for register, mask, mode in self.input_interpolation:
            if register < 0 or not mask or not set(mask) <= set("xyzw") or mode not in {
                "constant", "linear", "linear centroid", "linear noperspective", "linear noperspective centroid",
                "linear sample", "linear noperspective sample",
            }:
                raise ShaderGraphError("Recovered DXBC input interpolation is invalid.")
            for lane in mask:
                if (register, lane) in occupied:
                    raise ShaderGraphError("Recovered DXBC interpolation declarations overlap.")
                occupied.add((register, lane))
        if self.stage == "cs":
            if self.thread_group_size is None or len(self.thread_group_size) != 3 or any(
                not isinstance(n, int) or n <= 0 for n in self.thread_group_size
            ):
                raise ShaderGraphError("Recovered compute shader needs an explicit thread-group size.")
            if any(name not in _COMPUTE_INPUTS for name in self.compute_inputs) or len(set(self.compute_inputs)) != len(self.compute_inputs):
                raise ShaderGraphError("Recovered compute shader has invalid system inputs.")
        elif self.thread_group_size is not None or self.compute_inputs:
            raise ShaderGraphError("A non-compute program contains compute declarations.")

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": IR_SCHEMA,
                "version": IR_VERSION,
                "stage": self.stage,
                "temps": self.temp_count,
                "indexable_temps": [list(item) for item in self.indexable_temps],
                "immediate_constants": [
                    list(item) for item in self.immediate_constants
                ],
                "instructions": [item.to_dict() for item in self.instructions],
                "global_flags": list(self.global_flags),
                "input_interpolation": [list(row) for row in self.input_interpolation],
                "shared_memory": [list(row) for row in self.shared_memory],
                "globally_coherent_uavs": list(self.globally_coherent_uavs),
                "execution": asdict(self.execution),
                "primitive_system_inputs": list(self.primitive_system_inputs),
                "hull_phases": [asdict(p) for p in self.hull_phases],
                "pixel_system_inputs": list(self.pixel_system_inputs),
                **({"thread_group_size": list(self.thread_group_size), "compute_inputs": list(self.compute_inputs)}
                   if self.stage == "cs" else {}),
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def to_storage(self) -> str:
        raw = self.to_json().encode("utf-8")
        if len(raw) > MAX_IR_JSON_BYTES:
            raise ShaderGraphError("recovered DXBC material semantics are too large")
        return pack_ir_storage(raw, level=9)

    @classmethod
    def from_json(cls, text: str) -> "DxbcSemanticIr":
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_IR_JSON_BYTES:
            raise ShaderGraphError("recovered DXBC material semantics are too large")
        try:
            value = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ShaderGraphError("recovered DXBC material semantics are invalid") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != IR_SCHEMA
            or int(value.get("version", -1)) != IR_VERSION
        ):
            raise ShaderGraphError("recovered DXBC material semantics are invalid")
        instructions = value.get("instructions", [])
        if not isinstance(instructions, list) or len(instructions) > 20000:
            raise ShaderGraphError("recovered DXBC material semantics are too large")
        temp_count = int(value.get("temps", 0))
        if not 0 <= temp_count <= 4096:
            raise ShaderGraphError("recovered DXBC temporary-register count is invalid")
        indexable_value = value.get("indexable_temps", [])
        if not isinstance(indexable_value, list) or len(indexable_value) > 64:
            raise ShaderGraphError("recovered DXBC indexable temporaries are invalid")
        indexable_temps: list[tuple[int, int, int]] = []
        for item in indexable_value:
            if not isinstance(item, list) or len(item) != 3:
                raise ShaderGraphError("recovered DXBC indexable temporary is invalid")
            array, count, components = (int(part) for part in item)
            if not 0 <= array < 64 or not 1 <= count <= 4096 or not 1 <= components <= 4:
                raise ShaderGraphError("recovered DXBC indexable temporary is invalid")
            indexable_temps.append((array, count, components))
        immediate_value = value.get("immediate_constants", [])
        if not isinstance(immediate_value, list) or len(immediate_value) > 4096:
            raise ShaderGraphError("recovered DXBC immediate constants are invalid")
        immediate_constants: list[tuple[str, str, str, str]] = []
        for item in immediate_value:
            if (
                not isinstance(item, list)
                or len(item) != 4
                or any(not isinstance(part, str) or len(part) > 64 for part in item)
            ):
                raise ShaderGraphError("recovered DXBC immediate constant is invalid")
            immediate_constants.append(tuple(item))
        return cls(
            str(value.get("stage", "")).casefold(),
            temp_count,
            tuple(indexable_temps),
            tuple(immediate_constants),
            tuple(DxbcInstruction.from_dict(item) for item in instructions),
            tuple(value["thread_group_size"]) if isinstance(value.get("thread_group_size"), list) else None,
            tuple(value.get("compute_inputs", ())),
            tuple(value.get("global_flags", ("refactoringAllowed",))),
            tuple((int(r), str(mask), str(mode)) for r, mask, mode in value.get("input_interpolation", ())),
            tuple(tuple(int(v) for v in row) for row in value.get("shared_memory", ())),
            tuple(int(v) for v in value.get("globally_coherent_uavs", ())),
            DxbcStageExecution(**value.get("execution", {})),
            tuple(value.get("primitive_system_inputs", ())),
            tuple(DxbcHullPhase(**{**p, "indexable_temps": tuple(tuple(row) for row in p.get("indexable_temps", ()))})
                  for p in value.get("hull_phases", ())),
            tuple(value.get("pixel_system_inputs", ())),
        )

    @classmethod
    def from_storage(cls, text: str) -> "DxbcSemanticIr":
        return cls.from_json(unpack_ir_storage(text, MAX_IR_JSON_BYTES, "Recovered DXBC material semantics"))


@dataclass(frozen=True, slots=True)
class DxbcHookSite:
    instruction_index: int
    position: str
    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.position not in {"before", "after"}:
            raise ShaderGraphError("recovered DXBC hook position is invalid")
        if int(self.instruction_index) < 0:
            raise ShaderGraphError("recovered DXBC hook index is invalid")
        if not self.statements or any(
            not isinstance(line, str) or "\n" in line or "\r" in line
            for line in self.statements
        ):
            raise ShaderGraphError("recovered DXBC hook statements are invalid")


@dataclass(frozen=True, slots=True)
class DxbcHookPlan:
    sites: tuple[DxbcHookSite, ...] = ()


def _validate_recovered_control_flow(ir: DxbcSemanticIr) -> None:
    loops: list[list[int | bool]] = []
    for index, instruction in enumerate(ir.instructions):
        opcode = instruction.opcode.removesuffix("_sat")
        if opcode == "loop":
            loops.append([index, False])
            continue
        if opcode == "endloop":
            if not loops:
                raise ShaderGraphError("recovered DXBC shader has an unmatched endloop")
            start, has_exit = loops.pop()
            if not has_exit:
                raise ShaderGraphError(
                    "recovered DXBC shader loop at instruction "
                    f"{int(start)} has no explicit exit"
                )
            continue
        if opcode in {"break", "breakc_nz", "breakc_z"}:
            if not loops:
                raise ShaderGraphError(
                    "recovered DXBC shader has a break outside a loop"
                )
            loops[-1][1] = True
        elif opcode in {"ret", "discard_nz", "discard_z"}:
            for loop in loops:
                loop[1] = True
        elif opcode == "continue" and not loops:
            raise ShaderGraphError(
                "recovered DXBC shader has a continue outside a loop"
            )
    if loops:
        raise ShaderGraphError("recovered DXBC shader has an unterminated loop")


def validate_recovered_dxbc_shader_bytecode(
    bytecode: bytes | bytearray | memoryview,
) -> None:
    try:
        assembly = disassemble_dxbc(bytecode)
    except DxbcDisassemblyError as exc:
        raise ShaderGraphError(
            "compiled DXBC shader could not be checked for safe loop bounds"
        ) from exc

    instructions = []
    for line in assembly.splitlines():
        text = _LINE_PREFIX.sub("", line).strip().casefold()
        if text and not text.startswith("//"):
            instructions.append(text)

    loop_depth = 0
    for instruction in instructions:
        opcode = instruction.split(None, 1)[0]
        if opcode == "loop":
            loop_depth += 1
        elif opcode == "endloop":
            loop_depth = max(0, loop_depth - 1)
        elif (
            loop_depth
            and opcode in {"uge", "ult"}
            and "l(0x00100000)" in instruction
        ):
            raise ShaderGraphError(
                "compiled DXBC shader contains a synthetic million-iteration loop"
            )


def _split_operands(text: str) -> tuple[str, ...]:
    values: list[str] = []
    start = 0
    parentheses = 0
    brackets = 0
    for index, character in enumerate(text):
        if character == "(":
            parentheses += 1
        elif character == ")":
            parentheses -= 1
        elif character == "[":
            brackets += 1
        elif character == "]":
            brackets -= 1
        elif character == "," and parentheses == 0 and brackets == 0:
            values.append(text[start:index].strip())
            start = index + 1
        if parentheses < 0 or brackets < 0:
            raise ShaderGraphError("malformed DXBC operand list")
    if parentheses or brackets:
        raise ShaderGraphError("malformed DXBC operand list")
    tail = text[start:].strip()
    if tail:
        values.append(tail)
    return tuple(values)


def _split_opcode_line(line: str) -> tuple[str, str]:
    parentheses = 0
    for index, character in enumerate(line):
        if character == "(":
            parentheses += 1
        elif character == ")":
            parentheses -= 1
        elif character.isspace() and parentheses == 0:
            return line[:index], line[index:].strip()
    return line, ""


def parse_dxbc_disassembly(assembly: str) -> DxbcSemanticIr:
    stage = ""
    temp_count = 0
    indexable_temps: list[tuple[int, int, int]] = []
    immediate_constants: list[tuple[str, str, str, str]] = []
    thread_group_size = None
    compute_inputs = set()
    global_flags = ()
    interpolation = []
    shared_memory, coherent_uavs = [], []
    execution = {}
    primitive_system_inputs = set()
    pixel_system_inputs = set()
    hull_phases = []
    immediate_match = re.search(
        r"dcl_immediateConstantBuffer\s*\{(?P<body>.*?)\}\s*"
        r"(?=\r?\n\s*(?:0x[0-9A-Fa-f]+:|\d+\s+0x[0-9A-Fa-f]+:))",
        str(assembly),
        re.IGNORECASE | re.DOTALL,
    )
    if immediate_match is not None:
        for row in re.findall(r"\{([^{}]+)\}", immediate_match.group("body")):
            values = _split_operands(row)
            if len(values) != 4:
                raise ShaderGraphError("DXBC immediate constant buffer row is invalid")
            immediate_constants.append(tuple(values))
    instructions: list[DxbcInstruction] = []
    for source_line in str(assembly).splitlines():
        line = _LINE_PREFIX.sub("", source_line).strip()
        if not line or line.startswith("//"):
            continue
        if _STAGE.fullmatch(line):
            stage = line.split("_", 1)[0].casefold()
            continue
        if line == "hs_decls":
            continue
        phase = re.fullmatch(r"hs_(control_point|fork|join)_phase", line)
        if phase:
            hull_phases.append(DxbcHullPhase(phase[1], len(instructions)))
            continue
        phase_count = re.fullmatch(r"dcl_hs_(fork|join)_phase_instance_count (\d+)", line)
        if phase_count:
            if not hull_phases or hull_phases[-1].kind != phase_count[1]:
                raise ShaderGraphError("Hull instance declaration has no owning phase")
            hull_phases[-1] = replace(hull_phases[-1], instance_count=int(phase_count[2]))
            continue
        execution_declaration = stage_declaration(line)
        stream_declaration = re.fullmatch(r"dcl_stream m([0-3])", line)
        if stream_declaration:
            execution["output_streams"] = (*execution.get("output_streams", ()), int(stream_declaration[1]))
            continue
        if execution_declaration:
            key, value = execution_declaration
            if key in execution and execution[key] != value:
                raise ShaderGraphError(f"Conflicting stage declaration: {line}")
            execution[key] = value
            continue
        declaration = _DECL_TEMPS.fullmatch(line)
        if declaration is not None:
            if hull_phases:
                hull_phases[-1] = replace(hull_phases[-1], temp_count=int(declaration.group("count")))
            else:
                temp_count = max(temp_count, int(declaration.group("count")))
            continue
        indexable_declaration = _DECL_INDEXABLE_TEMP.fullmatch(line)
        if indexable_declaration is not None:
            target_temps = list(hull_phases[-1].indexable_temps) if hull_phases else indexable_temps
            target_temps.append(
                (
                    int(indexable_declaration.group("array")),
                    int(indexable_declaration.group("count")),
                    int(indexable_declaration.group("components")),
                )
            )
            if hull_phases:
                hull_phases[-1] = replace(hull_phases[-1], indexable_temps=tuple(target_temps))
            continue
        if line.casefold().startswith("dcl_thread_group "):
            dimensions = _split_operands(line.split(" ", 1)[1])
            if thread_group_size is not None or len(dimensions) != 3 or any(not n.isdigit() for n in dimensions):
                raise ShaderGraphError("DXBC compute thread-group declaration is invalid.")
            thread_group_size = tuple(map(int, dimensions))
            continue
        if line.casefold().startswith("dcl_input "):
            builtin = _COMPUTE_INPUT.fullmatch(line.split(" ", 1)[1])
            if builtin:
                compute_inputs.add(builtin[1].casefold())
            system = line.split(" ", 1)[1].split(".", 1)[0].casefold()
            if system in _PRIMITIVE_SYSTEM_INPUTS:
                primitive_system_inputs.add(system)
            if system in _PIXEL_SYSTEM_INPUTS:
                pixel_system_inputs.add(system)
        if line.casefold().startswith("dcl_tgsm"):
            declaration = re.fullmatch(r"dcl_tgsm_(raw|structured) g(\d+), (\d+)(?:, (\d+))?", line)
            if declaration is None or (declaration[1] == "raw") != (declaration[4] is None):
                raise ShaderGraphError(f"Invalid group-shared declaration: {line}")
            shared_memory.append((int(declaration[2]), int(declaration[3]) if declaration[1] == "structured" else 0,
                                  int(declaration[4] or declaration[3])))
            continue
        if line.casefold().startswith("dcl_uav") and "_glc" in line.split()[0]:
            declaration = re.search(r"\bu(\d+)\b", line)
            if declaration is None:
                raise ShaderGraphError("Globally coherent UAV has no register.")
            coherent_uavs.append(int(declaration[1]))
        if line.casefold().startswith("dcl_globalflags "):
            global_flags = tuple(sorted(flag.strip() for flag in line.split(" ", 1)[1].split("|") if flag.strip()))
            continue
        if line.casefold().startswith("dcl_input_ps"):
            declaration = re.fullmatch(r"dcl_input_ps(?:_sgv|_siv)? (.+?) v(\d+)\.([xyzw]+)(?:, \w+)?", line)
            if declaration is None:
                raise ShaderGraphError(f"Unsupported DXBC pixel input declaration: {line}")
            interpolation.append((int(declaration[2]), declaration[3], declaration[1]))
            continue
        if line.casefold().startswith("dcl_"):
            continue
        opcode_token, operand_text = _split_opcode_line(line)
        opcode = re.sub(r"\([^()]*\)", "", opcode_token).casefold()
        texel_offset = ()
        if '_aoffimmi' in opcode:
            offset = re.search(r'\((-?\d+),\s*(-?\d+),\s*(-?\d+)\)', opcode_token)
            if offset is None:
                raise ShaderGraphError('A native immediate texel offset is missing its coordinates.')
            texel_offset = tuple(map(int, offset.groups()))
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,95}", opcode):
            continue
        precise_mask = ""
        precision = re.match(r"\[precise(?:\(([xyzw]+)\))?\]\s+", operand_text)
        if precision is not None:
            precise_mask = precision[1] or "xyzw"
            operand_text = operand_text[precision.end():]
        try:
            operands = _split_operands(operand_text) if operand_text else ()
        except ShaderGraphError as exc:
            raise ShaderGraphError(f"{exc}: {line}") from exc
        instructions.append(DxbcInstruction(opcode, operands, precise_mask, texel_offset))
    if stage not in {"ps", "vs", "cs", "ds", "gs", "hs"} or not instructions:
        raise ShaderGraphError("DXBC does not contain a recoverable shader program")
    return DxbcSemanticIr(
        stage,
        temp_count,
        tuple(sorted(set(indexable_temps))),
        tuple(immediate_constants),
        tuple(instructions),
        thread_group_size,
        tuple(sorted(compute_inputs)),
        global_flags,
        tuple(interpolation),
        tuple(shared_memory),
        tuple(coherent_uavs),
        DxbcStageExecution(**{**({'max_tessellation_factor': None} if stage == 'hs' else {}), **execution}),
        tuple(sorted(primitive_system_inputs)),
        tuple(hull_phases),
        tuple(sorted(pixel_system_inputs)),
    )


def recover_dxbc_ir(bytecode: bytes) -> DxbcSemanticIr:
    try:
        assembly = disassemble_dxbc(bytecode, hex_literals=True)
    except DxbcDisassemblyError as exc:
        raise ShaderGraphError("The DXBC shader could not be disassembled.") from exc
    return parse_dxbc_disassembly(assembly)


def relocated_dxbc_inputs(ir: DxbcSemanticIr, source: ShaderContract, contract: ShaderContract) -> DxbcSemanticIr:
    placed = {field.key: field.register for field in contract.inputs}
    moves = {field.register: placed[field.key] for field in source.inputs
             if field.key in placed and placed[field.key] != field.register}
    if not moves:
        return ir
    def operand(text):
        return re.sub(r"(?<![\w.])v(\d+)\b", lambda match: f"v{moves.get(int(match[1]), int(match[1]))}", text)
    return replace(ir, instructions=tuple(replace(item, operands=tuple(map(operand, item.operands)))
                                          for item in ir.instructions),
                   input_interpolation=tuple((moves.get(register, register), mask, mode)
                                             for register, mask, mode in ir.input_interpolation))


def _width_type(base: str, width: int) -> str:
    return base if int(width) == 1 else f"{base}{int(width)}"


def _splat(base: str, width: int, value: str) -> str:
    if int(width) == 1:
        return str(value)
    return f"{_width_type(base, width)}({', '.join(str(value) for _ in range(int(width)))})"


def _recovered_buffer_aliases(
    contract: ShaderContract,
    semantic_buffer_bindings: frozenset[int] = frozenset(),
) -> list[str]:
    lines: list[str] = []
    identifiers = _constant_buffer_identifiers(contract.constant_buffers, reserved=(item.name for item in contract.resources))
    for buffer in contract.constant_buffers:
        for variable in buffer.variables:
            declaration = str(variable.hlsl_declaration or "").strip()
            if (
                not declaration
                or variable.byte_offset < 0
                or variable.byte_offset % 4
                or variable.byte_size <= 0
            ):
                continue
            name = identifiers[buffer.name, variable.name]
            register, byte_in_register = divmod(int(variable.byte_offset), 16)
            lane = byte_in_register // 4
            scalar_match = re.fullmatch(
                r"(?P<base>float|int|uint|bool)(?P<width>[1-4]?)",
                declaration,
                re.IGNORECASE,
            )
            matrix_match = re.fullmatch(
                r"(?:row_major\s+)?float(?P<rows>[2-4])x(?P<columns>[2-4])",
                declaration,
                re.IGNORECASE,
            )
            expression = ""
            if int(buffer.bind_point) in semantic_buffer_bindings:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable.name):
                    raise ShaderGraphError(
                        f"semantic buffer field {buffer.name}.{variable.name} is not a valid HLSL identifier"
                    )
                expression = variable.name
            elif scalar_match is not None and int(variable.array_length) == 1:
                base = scalar_match.group("base").casefold()
                width = int(scalar_match.group("width") or "1")
                if lane + width > 4:
                    continue
                swizzle = "xyzw"[lane : lane + width]
                expression = f"REasyCB{buffer.bind_point}[{register}u].{swizzle}"
                if base == "float":
                    expression = f"asfloat({expression})"
                elif base == "int":
                    expression = f"asint({expression})"
                elif base in {"uint", "bool"}:
                    expression = f"asuint({expression})"
            elif (
                matrix_match is not None
                and int(variable.array_length) == 1
                and lane == 0
            ):
                rows = int(matrix_match.group("rows"))
                columns = int(matrix_match.group("columns"))
                if columns != 4 or variable.byte_size < rows * 16:
                    continue
                row_values = ", ".join(
                    f"asfloat(REasyCB{buffer.bind_point}[{register + row}u])"
                    for row in range(rows)
                )
                expression = f"float{rows}x{columns}({row_values})"
            if expression:
                lines.append(f"#define {name} ({expression})")
    if lines:
        lines.append("")
    return lines


def _min_precision(token: str) -> bool:
    annotation = re.search(r"\{([^{}]+)\}\s*$", token.strip())
    return annotation is not None and re.fullmatch(r"(?:\w+ as )?min16f", annotation[1].strip()) is not None


def _strip_modifier(token: str) -> tuple[str, bool, bool]:
    value = re.sub(r"\s+\{[^{}]+\}\s*$", "", token.strip())
    negative = False
    absolute = False
    if value.startswith("-"):
        negative = True
        value = value[1:].strip()
    if len(value) >= 2 and value.startswith("|") and value.endswith("|"):
        absolute = True
        value = value[1:-1].strip()
    return value, absolute, negative


def _literal_values(token: str) -> tuple[str, ...] | None:
    value = token.strip()
    if not value.casefold().startswith("l(") or not value.endswith(")"):
        return None
    return _split_operands(value[2:-1])


def literal_integer_values(token: str) -> tuple[int, ...] | None:
    values = _literal_values(token)
    if values is None:
        return None
    try:
        return tuple(int(value, 0) & 0xffffffff for value in values)
    except ValueError:
        return None


def _select(values: Iterable[str], width: int) -> tuple[str, ...]:
    items = tuple(values)
    if not items:
        items = ("0",)
    if len(items) == 1 and width > 1:
        return items * width
    if len(items) < width:
        return items + (items[-1],) * (width - len(items))
    return items[:width]


def _select_lanes(values: Iterable[str], selector: int | str) -> tuple[str, ...]:
    items = tuple(values)
    if isinstance(selector, int):
        return _select(items, selector)
    lanes = str(selector)
    if not items:
        items = ("0",)
    if len(items) == 1:
        return items * len(lanes)
    padded = items + (items[-1],) * (4 - len(items))
    return tuple(padded["xyzw".index(lane)] for lane in lanes)


def _raw_literal(value: str) -> str:
    text = value.strip().casefold()
    if "inf" in text:
        return "asfloat(0xff800000u)" if text.startswith("-") else "asfloat(0x7f800000u)"
    if "nan" in text:
        return "asfloat(0x7fc00000u)"
    if text.startswith("0x"):
        return f"asfloat({text}u)"
    if re.fullmatch(r"[-+]?\d+", text):
        number = int(text)
        if number == 0:
            return "0.0"
        return f"asfloat(uint({number}))"
    return text if any(character in text for character in ".e") else text + ".0"


def _uint_literal(value: str) -> str:
    text = value.strip().casefold()
    if text.startswith("0x"):
        return text + "u"
    if re.fullmatch(r"[-+]?\d+", text):
        return f"uint({int(text)})"
    return f"asuint({_raw_literal(text)})"


def _sint_literal(value: str) -> str:
    text = value.strip().casefold()
    if text.startswith("0x"):
        return f"asint({text}u)"
    if re.fullmatch(r"[-+]?\d+", text):
        return str(int(text))
    return f"asint({_raw_literal(text)})"


def _outer_call_argument(expression: str, function: str) -> str | None:
    text = str(expression).strip()
    prefix = f"{function}("
    if not text.startswith(prefix) or not text.endswith(")"):
        return None
    depth = 0
    for index, character in enumerate(text[len(function) :], len(function)):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return None
    return text[len(prefix) : -1] if depth == 0 else None


def _override_bits(expression: str) -> str:
    text = expression.strip()
    argument = _outer_call_argument(text, "asfloat")
    if argument is not None:
        argument = argument.strip()
        if re.fullmatch(r"(?:0x[0-9a-f]+|\d+)u", argument, re.IGNORECASE) or _outer_call_argument(argument, "uint") is not None:
            return argument
    if re.fullmatch(r"[-+]?(?:(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?|\d+[eE][-+]?\d+)[fF]?", text):
        bits = struct.unpack("<I", struct.pack("<f", float(text.removesuffix("f").removesuffix("F"))))[0]
        return f"0x{bits:08x}u"
    return f"asuint({expression})"


def _operand_key(token: str) -> str:
    return _strip_modifier(token)[0].casefold()


def _scalar_temp(token: str) -> str | None:
    value, absolute, negative = _strip_modifier(token)
    match = _TEMP.fullmatch(value)
    if (
        absolute
        or negative
        or match is None
        or match.group("class").casefold() != "r"
        or len(match.group("swizzle") or "") != 1
    ):
        return None
    return (
        f"r{int(match.group('index'))}."
        f"{match.group('swizzle').casefold()}"
    )


@dataclass(frozen=True, slots=True)
class _StructuredLoop:
    number: int
    start: int
    end: int
    body_start: int
    state: str
    state_type: str
    predicate: str
    bound: str = ""
    other: str = ""

    @property
    def variable(self) -> str:
        return f"reasyLoopState{self.number}"


def _loop_ranges(
    instructions: tuple[DxbcInstruction, ...],
) -> tuple[tuple[int, int], ...]:
    stack: list[int] = []
    ranges: list[tuple[int, int]] = []
    for index, instruction in enumerate(instructions):
        opcode = instruction.opcode.removesuffix("_sat")
        if opcode == "loop":
            stack.append(index)
        elif opcode == "endloop":
            if not stack:
                raise ShaderGraphError("recovered DXBC shader has an unmatched endloop")
            ranges.append((stack.pop(), index))
    if stack:
        raise ShaderGraphError("recovered DXBC shader has an unterminated loop")
    return tuple(sorted(ranges))


def _writes_state(
    instructions: tuple[DxbcInstruction, ...],
    start: int,
    end: int,
    state: str,
    opcodes: frozenset[str],
) -> bool:
    nested = 0
    for instruction in instructions[start:end]:
        opcode = instruction.opcode.removesuffix("_sat")
        if opcode == "loop":
            nested += 1
            continue
        if opcode == "endloop":
            nested -= 1
            continue
        if (
            nested == 0
            and opcode in opcodes
            and instruction.operands
            and _scalar_temp(instruction.operands[0]) == state
        ):
            return True
    return False


def _recover_counted_loop(
    instructions: tuple[DxbcInstruction, ...],
    number: int,
    start: int,
    end: int,
) -> _StructuredLoop | None:
    if start + 2 >= end:
        return None
    comparison, branch = instructions[start + 1 : start + 3]
    pattern = {
        ("uge", "breakc_nz"): ("uint", "less", 1, 2),
        ("ult", "breakc_z"): ("uint", "less", 1, 2),
        ("ige", "breakc_nz"): ("int", "less", 1, 2),
        ("ilt", "breakc_z"): ("int", "less", 1, 2),
        # DXBC may express `state <= bound` by breaking when `bound < state`.
        ("ult", "breakc_nz"): ("uint", "less_equal", 2, 1),
        ("uge", "breakc_z"): ("uint", "less_equal", 2, 1),
        ("ilt", "breakc_nz"): ("int", "less_equal", 2, 1),
        ("ige", "breakc_z"): ("int", "less_equal", 2, 1),
    }.get((comparison.opcode.removesuffix("_sat"), branch.opcode))
    if (
        pattern is None
        or len(comparison.operands) != 3
        or len(branch.operands) != 1
        or _operand_key(comparison.operands[0])
        != _operand_key(branch.operands[0])
    ):
        return None
    state_type, predicate, state_index, bound_index = pattern
    state = _scalar_temp(comparison.operands[state_index])
    if not state or not _writes_state(
        instructions,
        start + 3,
        end,
        state,
        frozenset(("iadd", "mov", "movc", "ftou")),
    ):
        return None
    return _StructuredLoop(
        number,
        start,
        end,
        start + 3,
        state,
        state_type,
        predicate,
        comparison.operands[bound_index],
    )


def _recover_bitset_loop(
    instructions: tuple[DxbcInstruction, ...],
    number: int,
    start: int,
    end: int,
) -> _StructuredLoop | None:
    if start + 2 >= end:
        return None
    branch, first_bit = instructions[start + 1 : start + 3]
    state = (
        _scalar_temp(branch.operands[0])
        if branch.opcode == "breakc_z" and len(branch.operands) == 1
        else None
    )
    if (
        not state
        or first_bit.opcode.removesuffix("_sat") != "firstbit_lo"
        or len(first_bit.operands) != 2
        or _scalar_temp(first_bit.operands[1]) != state
        or not _writes_state(
            instructions,
            start + 2,
            end,
            state,
            frozenset(("and", "mov", "movc")),
        )
    ):
        return None
    return _StructuredLoop(
        number,
        start,
        end,
        start + 2,
        state,
        "uint",
        "nonzero",
    )


def _recover_bounded_traversal_loop(
    instructions: tuple[DxbcInstruction, ...],
    number: int,
    start: int,
    end: int,
) -> _StructuredLoop | None:
    if start + 5 >= end:
        return None
    index_test, increment, limit_test, conjunction, branch = (
        instructions[start + 1 : start + 6]
    )
    if (
        index_test.opcode.removesuffix("_sat") != "ine"
        or len(index_test.operands) != 3
        or literal_integer_values(index_test.operands[2]) != (0xffffffff,)
        or increment.opcode.removesuffix("_sat") != "iadd"
        or len(increment.operands) != 3
        or limit_test.opcode.removesuffix("_sat") != "ilt"
        or len(limit_test.operands) != 3
        or conjunction.opcode.removesuffix("_sat") != "and"
        or len(conjunction.operands) != 3
        or branch.opcode != "breakc_z"
        or len(branch.operands) != 1
    ):
        return None
    counter = _scalar_temp(increment.operands[0])
    limit = _literal_values(limit_test.operands[2])
    predicates = {
        _operand_key(conjunction.operands[1]),
        _operand_key(conjunction.operands[2]),
    }
    if (
        not counter
        or _scalar_temp(increment.operands[1]) != counter
        or literal_integer_values(increment.operands[2]) != (1,)
        or _scalar_temp(limit_test.operands[1]) != counter
        or not limit
        or len(limit) != 1
        or predicates
        != {
            _operand_key(index_test.operands[0]),
            _operand_key(limit_test.operands[0]),
        }
        or _operand_key(branch.operands[0])
        != _operand_key(conjunction.operands[0])
    ):
        return None
    try:
        limit_value = int(limit[0], 0)
    except ValueError:
        return None
    if not 1 <= limit_value <= 65536:
        return None
    return _StructuredLoop(
        number,
        start,
        end,
        start + 6,
        counter,
        "int",
        "preincrement_less_and_not_equal",
        str(limit_value),
        index_test.operands[1],
    )


def _recover_structured_loops(ir: DxbcSemanticIr) -> tuple[_StructuredLoop, ...]:
    loops: list[_StructuredLoop] = []
    recognizers = (
        _recover_counted_loop,
        _recover_bitset_loop,
        _recover_bounded_traversal_loop,
    )
    for number, (start, end) in enumerate(_loop_ranges(ir.instructions)):
        loop = None
        for recognize in recognizers:
            loop = recognize(ir.instructions, number, start, end)
            if loop is not None and _can_recover_loop_header(ir.instructions, loop):
                break
            loop = None
        if loop is None:
            loop = _StructuredLoop(
                number,
                start,
                end,
                start + 1,
                "",
                "",
                "unconditional",
            )
        loops.append(loop)
    return tuple(loops)


def _can_recover_loop_header(instructions, loop):
    from ..legacy_register_flow import destination_count, destination_register, source_references

    state_register, state_axis = loop.state.split(".")
    state = (state_register, "xyzw".index(state_axis))
    discarded = set()
    header = instructions[loop.start + 1:loop.body_start]
    for item in header:
        for operand in item.operands[:destination_count(item)]:
            destination = destination_register(operand)
            if destination is None or not destination[1].startswith("r"):
                return False
            lanes = {(destination[1], "xyzw".index(axis)) for axis in destination[2] or "xyzw"}
            if loop.predicate == "preincrement_less_and_not_equal" and item.opcode == "iadd" and lanes == {state}:
                continue
            discarded.update(lanes)
    if state in discarded:
        return False

    written = set()
    for item in header:
        count = destination_count(item)
        if any(source_references(item, index) & (discarded - written)
               for index in range(count, len(item.operands))):
            return False
        for operand in item.operands[:count]:
            destination = destination_register(operand)
            written.update((destination[1], "xyzw".index(axis)) for axis in destination[2] or "xyzw")
    for index, item in enumerate(instructions):
        count = destination_count(item)
        if not loop.start < index < loop.body_start and any(
            source_references(item, source) & discarded for source in range(count, len(item.operands))
        ):
            return False
        if loop.body_start <= index < loop.end:
            for operand in item.operands[:count]:
                destination = destination_register(operand)
                if (destination is not None and destination[1] == state_register
                        and state_axis in (destination[2] or "xyzw") and destination[2] != state_axis):
                    return False
    if loop.other and state in source_references(DxbcInstruction("if_nz", (loop.other,)), 0):
        return False
    return True


class _HlslEmitter:
    def __init__(
        self,
        ir: DxbcSemanticIr,
        contract: ShaderContract,
        *,
        constant_overrides: Mapping[tuple[int, int], str] | None = None,
        structured_overrides: Mapping[tuple[int, int], str] | None = None,
        texture_binding_overrides: Mapping[int, int] | None = None,
        instruction_hooks: DxbcHookPlan | None = None,
        semantic_buffer_bindings: frozenset[int] = frozenset(),
        output_array: str = "",
    ) -> None:
        self.ir = ir
        self.contract = contract
        self.resources = {
            (item.register_class, item.bind_point): item
            for item in contract.resources
        }
        redirects = {}
        for source, target in (texture_binding_overrides or {}).items():
            resource = self.resources.get(("t", int(target)))
            if resource is None or not resource.sampleable:
                raise ShaderGraphError("Texture redirect has no destination binding")
            redirects[("t", int(source))] = resource
        self.resources.update(redirects)
        self.indent = 1
        self.lines: list[str] = []
        loops = _recover_structured_loops(ir)
        self.loops_by_start = {item.start: item for item in loops}
        self.loops_by_end = {item.end: item for item in loops}
        self.loop_stack: list[_StructuredLoop] = []
        self._aliases: dict[str, list[tuple[str, str]]] = {}
        self.constant_overrides = dict(constant_overrides or {})
        self.structured_overrides = dict(structured_overrides or {})
        self.semantic_buffer_bindings = frozenset(
            int(value) for value in semantic_buffer_bindings
        )
        self.output_array = output_array
        self._hooks: dict[tuple[int, str], list[str]] = {}
        self._emitted_hooks: set[tuple[int, str]] = set()
        self._atomic_index = 0
        self._precise_mask = ""
        self._precise_index = 0
        hook_bytes = 0
        for site in (instruction_hooks or DxbcHookPlan()).sites:
            index = int(site.instruction_index)
            if index >= len(ir.instructions):
                raise ShaderGraphError("recovered DXBC hook index is out of range")
            opcode = ir.instructions[index].opcode.removesuffix("_sat")
            if opcode in {
                "loop",
                "endloop",
                "if_nz",
                "if_z",
                "else",
                "endif",
                "break",
                "breakc_nz",
                "breakc_z",
                "continue",
                "discard_nz",
                "discard_z",
            } or (opcode == "ret" and site.position != "before"):
                raise ShaderGraphError(
                    "recovered DXBC hook targets an unsafe control-flow site"
                )
            hook_bytes += sum(len(line.encode("utf-8")) for line in site.statements)
            self._hooks.setdefault((index, site.position), []).extend(
                site.statements
            )
        if hook_bytes > 1024 * 1024:
            raise ShaderGraphError("recovered DXBC hooks exceed the safety limit")

    def _emit_hook(self, index: int, position: str) -> None:
        key = (int(index), str(position))
        statements = self._hooks.get(key)
        if not statements:
            return
        self.lines.extend("    " * self.indent + line for line in statements)
        self._emitted_hooks.add(key)

    def _emit(self, text: str) -> None:
        self.lines.append("    " * self.indent + text)

    def _uav(self, token: str):
        match = _RESOURCE.fullmatch(token)
        return match, None if match is None else self.resources.get(("u", int(match.group("index"))))

    def _dest(self, token: str) -> tuple[str, str] | None:
        value = re.sub(r"\s+\{[^{}]+\}\s*$", "", token.strip())
        if value.casefold() == "null":
            return None
        if value.casefold() in _PIXEL_OUTPUTS:
            if not any(_output_register(field) == value.casefold() for field in self.contract.outputs):
                raise ShaderGraphError(f"Undeclared special pixel output {value}")
            return value.casefold(), "x"
        dynamic_output = re.fullmatch(r"o\[([^\]]+)\](?:\.([xyzw]+))?", value, re.I)
        if dynamic_output:
            if not self.output_array:
                raise ShaderGraphError("Dynamic patch output has no owning hull phase")
            return f"{self.output_array}[{self._index_expression(dynamic_output[1])}]", dynamic_output[2] or "xyzw"
        match = _TEMP.fullmatch(value)
        indexable = _INDEXABLE_TEMP.fullmatch(value)
        if indexable is not None:
            name = (
                f"x{indexable.group('array')}["
                f"{self._index_expression(indexable.group('index'))}]"
            )
            return name, (indexable.group("swizzle") or "xyzw").casefold()
        if match is None or match.group("class").casefold() not in {"r", "o"}:
            raise ShaderGraphError(f"unsupported DXBC destination {token!r}")
        name = match.group("class").casefold() + match.group("index")
        if self.output_array and match.group("class").casefold() == "o":
            name = f"{self.output_array}[{match.group('index')}]"
        return name, (match.group("swizzle") or "xyzw").casefold()

    def _index_expression(self, value: str) -> str:
        text = value.strip()
        if re.fullmatch(r"\d+", text):
            return text + "u"
        match = re.fullmatch(
            r"(?P<register>r\d+\.[xyzw])(?:\s*(?P<operator>[+-])\s*(?P<offset>\d+))?",
            text,
            re.IGNORECASE,
        )
        if match is None:
            raise ShaderGraphError(f"unsupported DXBC dynamic index {value!r}")
        register = match.group("register").casefold()
        alias = self._active_alias(register)
        expression = (
            alias[0]
            if alias is not None and alias[1] == "uint"
            else f"asuint({alias[0]})"
            if alias is not None
            else register
        )
        if match.group("operator"):
            expression += f" {match.group('operator')} {int(match.group('offset'))}u"
        return f"({expression})"

    def _active_alias(self, state: str) -> tuple[str, str] | None:
        aliases = self._aliases.get(str(state).casefold())
        return aliases[-1] if aliases else None

    def _push_alias(self, loop: _StructuredLoop) -> None:
        self._aliases.setdefault(loop.state, []).append(
            (loop.variable, loop.state_type)
        )

    def _pop_alias(self, loop: _StructuredLoop) -> None:
        aliases = self._aliases.get(loop.state)
        if not aliases or aliases[-1][0] != loop.variable:
            raise ShaderGraphError("recovered DXBC loop state is unbalanced")
        aliases.pop()
        if not aliases:
            del self._aliases[loop.state]

    @staticmethod
    def _format_vector(parts: Iterable[str], base: str = "float") -> str:
        values = tuple(parts)
        if len(values) == 1:
            return values[0]
        return f"{_width_type(base, len(values))}({', '.join(values)})"

    def _compute_input_parts(self, value: str, selector: int | str) -> tuple[str, ...] | None:
        builtin = _COMPUTE_INPUT.fullmatch(value)
        if builtin is None:
            return None
        name = builtin[1].casefold()
        if name not in self.ir.compute_inputs:
            raise ShaderGraphError(f"DXBC uses undeclared compute input {name}.")
        scalar = _COMPUTE_INPUTS[name][0] == "uint"
        lanes = _select_lanes(builtin[2] or ("x" if scalar else "xyz"), selector)
        if not set(lanes) <= set("x" if scalar else "xyz"):
            raise ShaderGraphError(f"Invalid compute input lanes for {name}.")
        return tuple(name if scalar else f"{name}.{lane}" for lane in lanes)

    def _primitive_input_parts(self, value: str, selector: int | str) -> tuple[str, ...] | None:
        pixel = re.fullmatch(r"(vCoverage)(?:\.(x+))?", value, re.I)
        if pixel:
            if pixel[1].casefold() not in self.ir.pixel_system_inputs:
                raise ShaderGraphError("Undeclared pixel system input")
            return tuple(pixel[1].casefold() for _ in _select_lanes(pixel[2] or "x", selector))
        system = re.fullmatch(r"(vPrim|vGSInstanceID|vOutputControlPointID|vForkInstanceID|vJoinInstanceID)(?:\.(x+))?", value, re.I)
        if system:
            if system[1].casefold() not in self.ir.primitive_system_inputs:
                raise ShaderGraphError("Undeclared primitive system input")
            return tuple(system[1].casefold() for _ in _select_lanes(system[2] or "x", selector))
        match = re.fullmatch(r"(vicp|vocp|v)\[([^\]]+)\]\[(\d+)\](?:\.([xyzw]+))?", value, re.I)
        if match:
            is_geometry = self.ir.stage == "gs" and match[1].casefold() == "v"
            output = match[1].casefold() == "vocp"
            if self.ir.stage not in ({"gs"} if is_geometry else {"hs", "ds"}):
                raise ShaderGraphError("Control-point input occurs outside tessellation")
            index, register = match[2], int(match[3])
            count = (self.ir.execution.primitive_vertices if is_geometry else self.ir.execution.output_control_points if output
                     else self.ir.execution.input_control_points)
            if index.isdecimal() and int(index) >= count:
                raise ShaderGraphError("Control-point input index is out of range")
            if register not in {field.register for field in (self.contract.outputs if output else self.contract.inputs)}:
                raise ShaderGraphError("Undeclared control-point register")
            name = f"{'vocp' if output else 'vicp'}{register}[{self._index_expression(index)}]"
            return tuple(f"{name}.{lane}" for lane in _select_lanes(match[4] or "xyzw", selector))
        domain = re.fullmatch(r"vDomain(?:\.([xyz]+))?", value, re.I)
        if domain:
            if self.ir.stage != "ds":
                raise ShaderGraphError("Domain location occurs outside a domain shader")
            lanes = _select_lanes(domain[1] or ("xyz" if self.ir.execution.domain == "tri" else "xy"), selector)
            return tuple(f"asuint(vDomain.{lane})" for lane in lanes)
        patch = re.fullmatch(r"vpc(?:\[(\d+)\]|(\d+))(?:\.([xyzw]+))?", value, re.I)
        if patch:
            register = int(patch[1] or patch[2])
            if register not in {field.register for field in self.contract.patch_constants}:
                raise ShaderGraphError("Undeclared patch-constant register")
            return tuple(f"vpc{register}.{lane}" for lane in _select_lanes(patch[3] or "xyzw", selector))
        return None

    def _register_parts(self, token: str, selector: int | str) -> tuple[str, ...]:
        value, absolute, negative = _strip_modifier(token)
        literals = _literal_values(value)
        if literals is not None:
            parts = tuple(
                _raw_literal(item) for item in _select_lanes(literals, selector)
            )
        else:
            cb = _CBUFFER.fullmatch(value)
            temp = _TEMP.fullmatch(value)
            indexable = _INDEXABLE_TEMP.fullmatch(value)
            immediate = _IMMEDIATE_BUFFER.fullmatch(value)
            builtin = self._primitive_input_parts(value, selector) or self._compute_input_parts(value, selector)
            if builtin is not None:
                parts = tuple(f"asfloat({part})" for part in builtin)
            elif cb is not None:
                parts = tuple(f"asfloat({part})" for part in self._constant_parts(cb, selector))
            elif indexable is not None:
                name = (
                    f"x{indexable.group('array')}["
                    f"{self._index_expression(indexable.group('index'))}]"
                )
                swizzle = indexable.group("swizzle") or "xyzw"
                swizzle = "".join(_select_lanes(swizzle, selector))
                parts = tuple(
                    f"asfloat({name}.{component})" for component in swizzle
                )
            elif immediate is not None:
                if not self.ir.immediate_constants:
                    raise ShaderGraphError("DXBC uses an absent immediate constant buffer")
                name = f"REasyICB[{self._index_expression(immediate.group('index'))}]"
                swizzle = immediate.group("swizzle") or "xyzw"
                swizzle = "".join(_select_lanes(swizzle, selector))
                parts = tuple(f"asfloat({name}.{component})" for component in swizzle)
            elif temp is not None:
                name = temp.group("class").casefold() + temp.group("index")
                if self.output_array and temp.group("class").casefold() == "o":
                    name = f"{self.output_array}[{temp.group('index')}]"
                swizzle = temp.group("swizzle") or "xyzw"
                swizzle = "".join(_select_lanes(swizzle, selector))
                parts = tuple(
                    f"asfloat({alias[0]})"
                    if (alias := self._active_alias(f"{name}.{component}"))
                    is not None
                    else f"asfloat({name}.{component})"
                    for component in swizzle
                )
            else:
                raise ShaderGraphError(f"unsupported DXBC source operand {token!r}")
        if absolute:
            parts = tuple(f"abs({part})" for part in parts)
        if negative:
            parts = tuple(f"-({part})" for part in parts)
        return parts

    def _constant_parts(self, cb, selector: int | str) -> tuple[str, ...]:
        bind_point = int(cb.group("index"))
        register_text = cb.group("register").strip()
        register = self._index_expression(register_text)
        value = (f"REasyLoadCB{bind_point}({register})" if bind_point in self.semantic_buffer_bindings
                 else f"REasyCB{bind_point}[{register}]")
        parts = []
        for lane in _select_lanes(cb.group("swizzle") or "xyzw", selector):
            lane_offset = "xyzw".index(lane) * 4
            part = f"{value}.{lane}"
            if register_text.isdecimal():
                override = self.constant_overrides.get((bind_point, int(register_text) * 16 + lane_offset))
                if override is not None:
                    part = _override_bits(override)
            else:
                for (binding, offset), override in sorted(self.constant_overrides.items()):
                    if binding == bind_point and offset % 16 == lane_offset:
                        part = f"(({register}) == {offset // 16}u ? {_override_bits(override)} : {part})"
            parts.append(part)
        return tuple(parts)

    def raw(self, token: str, selector: int | str) -> str:
        parts = self._register_parts(token, selector)
        expression = self._format_vector(parts)
        if _min_precision(token):
            # {min16f} sources make the instruction run at half precision.
            expression = f"(({_width_type('min16float', len(parts))})({expression}))"
        return expression

    def uint(self, token: str, selector: int | str) -> str:
        # Typeless instructions (movc, mov) apply -/|..| as float modifiers. FXC never modifies a literal.
        literals = _literal_values(_strip_modifier(token)[0])
        if literals is not None:
            selected = _select_lanes(literals, selector)
            return self._format_vector(
                (_uint_literal(item) for item in selected),
                "uint",
            )
        typed = self._typed_alias_parts(token, selector, "uint")
        if typed is not None:
            return self._format_vector(typed, "uint")
        return f"asuint({self.raw(token, selector)})"

    def sint(self, token: str, selector: int | str) -> str:
        value, absolute, negative = _strip_modifier(token)
        if absolute or negative:
            expression = self.sint(value, selector)
            if absolute:
                expression = f"abs({expression})"
            return f"(-{expression})" if negative else expression
        literals = _literal_values(value)
        if literals is not None:
            selected = _select_lanes(literals, selector)
            return self._format_vector(
                (_sint_literal(item) for item in selected),
                "int",
            )
        typed = self._typed_alias_parts(token, selector, "int")
        if typed is not None:
            return self._format_vector(typed, "int")
        return f"asint({self.raw(token, selector)})"

    def _typed_alias_parts(
        self,
        token: str,
        selector: int | str,
        value_type: str,
    ) -> tuple[str, ...] | None:
        value, absolute, negative = _strip_modifier(token)
        if absolute or negative:
            return None
        cb = _CBUFFER.fullmatch(value)
        immediate = _IMMEDIATE_BUFFER.fullmatch(value)
        if cb is not None:
            parts = self._constant_parts(cb, selector)
            return parts if value_type == "uint" else tuple(f"asint({part})" for part in parts)
        if immediate is not None:
            if not self.ir.immediate_constants:
                raise ShaderGraphError("DXBC uses an absent immediate constant buffer")
            name = f"REasyICB[{self._index_expression(immediate.group('index'))}]"
            parts = tuple(f"{name}.{lane}" for lane in _select_lanes(immediate.group("swizzle") or "xyzw", selector))
            return parts if value_type == "uint" else tuple(f"asint({part})" for part in parts)
        builtin = self._primitive_input_parts(value, selector) or self._compute_input_parts(value, selector)
        if builtin is not None:
            return tuple(part if value_type == "uint" else f"asint({part})" for part in builtin)
        indexable = _INDEXABLE_TEMP.fullmatch(value)
        if indexable is not None:
            name = f"x{indexable.group('array')}[{self._index_expression(indexable.group('index'))}]"
            lanes = _select_lanes(indexable.group("swizzle") or "xyzw", selector)
            return tuple(f"{name}.{lane}" if value_type == "uint" else f"asint({name}.{lane})" for lane in lanes)
        match = _TEMP.fullmatch(value)
        if match is None:
            return None
        name = match.group("class").casefold() + match.group("index")
        if self.output_array and match.group("class").casefold() == "o":
            name = f"{self.output_array}[{match.group('index')}]"
        swizzle = match.group("swizzle") or "xyzw"
        swizzle = "".join(_select_lanes(swizzle, selector))
        parts: list[str] = []
        for component in swizzle:
            alias = self._active_alias(f"{name}.{component}")
            if alias is None:
                parts.append(f"{name}.{component}" if value_type == "uint" else f"asint({name}.{component})")
            elif alias[1] == value_type:
                parts.append(alias[0])
            else:
                parts.append(f"as{value_type}({alias[0]})")
        return tuple(parts)

    def condition(self, token: str, *, zero: bool = False) -> str:
        operator = "==" if zero else "!="
        return f"({self.uint(token, 1)} {operator} 0u)"

    def _loop_condition(self, loop: _StructuredLoop) -> str:
        if loop.predicate == "unconditional":
            return "true"
        if loop.predicate in {"less", "less_equal"}:
            render = self.uint if loop.state_type == "uint" else self.sint
            operator = "<=" if loop.predicate == "less_equal" else "<"
            return f"{loop.variable} {operator} {render(loop.bound, 1)}"
        if loop.predicate == "nonzero":
            return f"{loop.variable} != 0u"
        if loop.predicate == "preincrement_less_and_not_equal":
            return (
                f"(++{loop.variable} < {int(loop.bound)}) && "
                f"({self.sint(loop.other, 1)} != -1)"
            )
        raise ShaderGraphError("recovered DXBC loop predicate is invalid")

    def _destination_alias(self, destination: str) -> tuple[str, str] | None:
        resolved = self._dest(destination)
        if resolved is None or len(resolved[1]) != 1:
            return None
        return self._active_alias(f"{resolved[0]}.{resolved[1]}")

    def write(self, destination: str, expression: str, *, saturate: bool = False) -> None:
        resolved = self._dest(destination)
        if resolved is None:
            return
        name, mask = resolved
        if saturate:
            expression = f"saturate({expression})"
        if _min_precision(destination):  # Stored widened: its readers see {min16f as def32}.
            expression = f"(({_width_type('float', len(mask))})({expression}))"
        raw_bits = _outer_call_argument(expression, "asfloat")
        precise_lanes = [i for i, lane in enumerate(mask) if lane in self._precise_mask]
        if precise_lanes and raw_bits is None:
            value = f"reasyPreciseValue{self._precise_index}"
            self._precise_index += 1
            prefix = "    " * self.indent
            self.lines.append(f"{prefix}{_width_type('float', len(mask))} {value} = {expression};")
            components = []
            for i in range(len(mask)):
                component = f"{value}.{'xyzw'[i]}" if len(mask) > 1 else value
                if i in precise_lanes:
                    precise = f"{value}_{mask[i]}"
                    self.lines.append(f"{prefix}precise float {precise} = {component};")
                    component = precise
                components.append(component)
            expression = components[0] if len(mask) == 1 else f"float{len(mask)}({', '.join(components)})"
        alias = self._active_alias(f"{name}.{mask}") if len(mask) == 1 else None
        if alias is not None:
            self._emit(f"{alias[0]} = as{alias[1]}({raw_bits or expression});")
            return
        expression = raw_bits or f"asuint({expression})"
        self._emit(f"{name}.{mask} = {expression};")

    def _binary(self, item: DxbcInstruction, operator: str, *, mode: str = "raw") -> None:
        destination, left, right = item.operands
        resolved = self._dest(destination)
        if resolved is None:
            return
        lanes = resolved[1]
        width = len(lanes)
        render = {"raw": self.raw, "uint": self.uint, "sint": self.sint}[mode]
        result = f"({render(left, lanes)} {operator} {render(right, lanes)})"
        alias = self._destination_alias(destination)
        if (
            alias is not None
            and width == 1
            and mode == {"uint": "uint", "int": "sint"}[alias[1]]
        ):
            self._emit(f"{alias[0]} = {result};")
            return
        if mode != "raw":
            result = f"asfloat({result})"
        self.write(destination, result, saturate=item.opcode.endswith("_sat"))

    def _comparison(self, item: DxbcInstruction, operator: str, *, mode: str = "raw") -> None:
        destination, left, right = item.operands
        resolved = self._dest(destination)
        if resolved is None:
            return
        lanes = resolved[1]
        width = len(lanes)
        render = {"raw": self.raw, "uint": self.uint, "sint": self.sint}[mode]
        value_type = _width_type("uint", width)
        result = (
            f"asfloat({value_type}({render(left, lanes)} {operator} "
            f"{render(right, lanes)}) * {_splat('uint', width, '0xffffffffu')})"
        )
        self.write(destination, result)

    def _sample(self, item: DxbcInstruction) -> None:
        destination = item.operands[0]
        resolved = self._dest(destination)
        if resolved is None:
            return
        coordinates, texture_token, sampler_token = item.operands[1:4]
        texture_match = _RESOURCE.fullmatch(texture_token)
        sampler_match = _RESOURCE.fullmatch(sampler_token)
        if texture_match is None or sampler_match is None:
            raise ShaderGraphError("unsupported DXBC texture sample binding")
        source_bind_point = int(texture_match.group("index"))
        resource = self.resources.get(("t", source_bind_point))
        sampler = self.resources.get(("s", int(sampler_match.group("index"))))
        if resource is None or sampler is None:
            raise ShaderGraphError("DXBC texture sample references an absent binding")
        texture = resource_identifier(resource)
        sampler_name = resource_identifier(sampler)
        coordinate_width = texture_coordinate_width(resource.dimension) or 2
        coordinate = self.raw(coordinates, coordinate_width)
        opcode = item.opcode.removesuffix("_sat")
        if opcode == "sample_indexable":
            result = f"{texture}.Sample({sampler_name}, {coordinate})"
        elif opcode == "sample_b_indexable":
            result = f"{texture}.SampleBias({sampler_name}, {coordinate}, {self.raw(item.operands[4], 1)})"
        elif opcode == "sample_l_indexable":
            result = (
                f"{texture}.SampleLevel({sampler_name}, {coordinate}, "
                f"{self.raw(item.operands[4], 1)})"
            )
        elif opcode == "sample_d_indexable":
            derivative_width = max(1, coordinate_width - (1 if "array" in resource.dimension else 0))
            result = (
                f"{texture}.SampleGrad({sampler_name}, {coordinate}, "
                f"{self.raw(item.operands[4], derivative_width)}, "
                f"{self.raw(item.operands[5], derivative_width)})"
            )
        elif opcode == "sample_c_lz_indexable":
            result = (
                f"{texture}.SampleCmpLevelZero({sampler_name}, {coordinate}, "
                f"{self.raw(item.operands[4], 1)})"
            )
        else:
            raise ShaderGraphError(f"unsupported DXBC sample opcode {item.opcode}")
        return_type = resource.return_type
        if return_type in {"uint", "sint"}:
            result = f"asfloat({result})"
        swizzle = texture_match.group("swizzle") or "xyzw"
        swizzle = "".join(_select_lanes(swizzle, resolved[1]))
        self.write(destination, f"({result}).{swizzle}", saturate=item.opcode.endswith("_sat"))

    def _resource_info(self, item):
        # Native result modes/layout: https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/resinfo--sm4---asm-
        destination, mip, token = item.operands
        match = _RESOURCE.fullmatch(token)
        resource = self.resources.get((match["class"], int(match["index"]))) if match else None
        lanes = {"texture1d": "x", "texture1d-array": "xy", "texture2d": "xy",
                 "texture2d-array": "xyz", "texture3d": "xyz", "texture-cube": "xy"}
        if resource is None or resource.input_type != "texture" or resource.dimension not in lanes:
            raise ShaderGraphError("resinfo needs a supported native mipmapped texture")
        mode = item.opcode.removeprefix("resinfo_indexable")
        if mode not in {"", "_uint", "_rcpfloat"}:
            raise ShaderGraphError("resinfo has an unsupported result modifier")
        resolved = self._dest(destination)
        if resolved is None:
            return
        prefix = "    " * self.indent
        scalar = "uint" if mode == "_uint" else "float"
        self.lines.extend((prefix + "{", prefix + f"    {scalar}4 reasyDimensions = 0;"))
        arguments = [self.uint(mip, 1)] + [f"reasyDimensions.{c}" for c in lanes[resource.dimension] + "w"]
        self.lines.append(prefix + f"    {resource_identifier(resource)}.GetDimensions({', '.join(arguments)});")
        if mode == "_rcpfloat":
            # Array size and mip count are not reciprocal dimensions.
            dimensions = {"texture1d-array": "x", "texture2d-array": "xy"}.get(resource.dimension, lanes[resource.dimension])
            self.lines.append(prefix + f"    reasyDimensions.{dimensions} = 1.0 / reasyDimensions.{dimensions};")
        selected = "".join(_select_lanes(match["swizzle"] or "xyzw", resolved[1]))
        value = f"reasyDimensions.{selected}"
        self.indent += 1
        self.write(destination, f"asfloat({value})" if mode == "_uint" else value)
        self.indent -= 1
        self.lines.append(prefix + "}")

    def _load(self, item: DxbcInstruction) -> None:
        destination = item.operands[0]
        resolved = self._dest(destination)
        if resolved is None:
            return
        opcode = item.opcode
        if opcode in {"ld_raw", "ld_raw_indexable"}:
            address, resource_token = item.operands[1:3]
            match = _RESOURCE.fullmatch(resource_token)
            if match is None:
                raise ShaderGraphError("unsupported raw-buffer binding")
            resource = self.resources.get((match.group("class"), int(match.group("index"))))
            if resource is None:
                raise ShaderGraphError("raw-buffer load references an absent binding")
            parts = [f"{resource_identifier(resource)}.Load({self.uint(address, 1)} + {'xyzw'.index(lane) * 4}u)"
                     for lane in _select_lanes(match.group("swizzle") or "xyzw", resolved[1])]
            result = self._format_vector(parts, "uint")
            self.write(destination, f"asfloat({result})")
            return
        if opcode in {"ld_structured", "ld_structured_indexable"}:
            index, byte_offset, resource_token = item.operands[1:4]
            match = _RESOURCE.fullmatch(resource_token)
            if match is None:
                raise ShaderGraphError("unsupported structured-buffer binding")
            resource = self.resources.get((match.group("class"), int(match.group("index"))))
            if resource is None:
                raise ShaderGraphError("structured-buffer load references an absent binding")
            helper = f"REasyLoad{'RW' if resource.register_class == 'u' else ''}Structured_{resource.bind_point}"
            literal_offset = literal_integer_values(byte_offset)
            base_offset = literal_offset[0] if literal_offset and len(literal_offset) == 1 else None
            parts = []
            for lane in _select_lanes(match.group("swizzle") or "xyzw", resolved[1]):
                component = "xyzw".index(lane)
                override = (
                    self.structured_overrides.get(
                        (resource.bind_point, base_offset + component * 4)
                    )
                    if base_offset is not None and resource.register_class == "t"
                    else None
                )
                if override is not None:
                    override = override.replace(
                        f"REasyStructuredElementIndex_{resource.bind_point}",
                        f"({self.uint(index, 1)})",
                    )
                    parts.append(_override_bits(override))
                else:
                    address = f"{self.uint(byte_offset, 1)} + {component * 4}u"
                    word = (
                        f"{helper}({self.uint(index, 1)}, "
                        f"{address})"
                    )
                    if base_offset is None and resource.register_class == "t":
                        for (binding, offset), expression in sorted(self.structured_overrides.items()):
                            if binding != resource.bind_point:
                                continue
                            expression = expression.replace(
                                f"REasyStructuredElementIndex_{binding}", f"({self.uint(index, 1)})",
                            )
                            word = f"(({address}) == {offset}u ? {_override_bits(expression)} : {word})"
                    parts.append(word)
            result = self._format_vector(parts, "uint")
            self.write(destination, f"asfloat({result})")
            return
        if opcode == "ld_indexable":
            coordinates, resource_token = item.operands[1:3]
            match = _RESOURCE.fullmatch(resource_token)
            if match is None:
                raise ShaderGraphError("unsupported texture-load binding")
            resource = self.resources.get(("t", int(match.group("index"))))
            if resource is None:
                raise ShaderGraphError("texture load references an absent binding")
            coordinate_width = (texture_coordinate_width(resource.dimension, load=True) or 2) + 1
            result = f"{resource_identifier(resource)}.Load({self.sint(coordinates, coordinate_width)})"
            if resource.return_type in {"uint", "sint"}:
                result = f"asfloat({result})"
            swizzle = match.group("swizzle") or "xyzw"
            swizzle = "".join(_select_lanes(swizzle, resolved[1]))
            self.write(destination, f"({result}).{swizzle}")
            return
        raise ShaderGraphError(f"unsupported DXBC load opcode {opcode}")

    def _shared_instruction(self, item: DxbcInstruction) -> bool:
        shared = [(i, re.fullmatch(r"g(\d+)(?:\.([xyzw]+))?", operand)) for i, operand in enumerate(item.operands)]
        shared = [(i, match) for i, match in shared if match is not None]
        if not shared:
            return False
        if len(shared) != 1:
            raise ShaderGraphError("Group-shared instruction needs one g-register.")
        position, match = shared[0]
        register, mask = int(match[1]), match[2] or "xyzw"
        allocation = next(((stride, count) for r, stride, count in self.ir.shared_memory if r == register), None)
        if allocation is None:
            raise ShaderGraphError("Group-shared instruction references an undeclared allocation.")
        stride, _count = allocation
        def address(index, offset, lane=0):
            base = f"REasyShared{register}"
            if stride:
                base += f"[{self.uint(index, 1)}].words"
            return f"{base}[({self.uint(offset, 1)}) / 4u + {lane}u]"
        op = item.opcode
        if op in {"ld_raw", "ld_raw_indexable", "ld_structured", "ld_structured_indexable"}:
            structured = op.startswith("ld_structured")
            if bool(stride) != structured or position != (3 if structured else 2):
                raise ShaderGraphError("Group-shared load disagrees with its declaration.")
            destination = self._dest(item.operands[0])
            if destination is not None:
                index, offset = item.operands[1:3] if structured else ("l(0)", item.operands[1])
                values = [address(index, offset, "xyzw".index(lane)) for lane in _select_lanes(mask, destination[1])]
                self.write(item.operands[0], f"asfloat({self._format_vector(values, 'uint')})")
            return True
        if op in {"store_raw", "store_structured"}:
            structured = op == "store_structured"
            if bool(stride) != structured or position != 0:
                raise ShaderGraphError("Group-shared store disagrees with its declaration.")
            index, offset, value = item.operands[1:] if structured else ("l(0)", *item.operands[1:])
            mask = match[2] or "x"
            for lane in mask:
                self._emit(f"{address(index, offset, 'xyzw'.index(lane))} = {self.uint(value, lane)};")
            return True
        immediate = op.startswith("imm_atomic_")
        operation = op.removeprefix("imm_").removeprefix("atomic_")
        methods = {"iadd": "Add", "and": "And", "or": "Or", "xor": "Xor", "umax": "Max", "umin": "Min", "exch": "Exchange"}
        if not (immediate or op.startswith("atomic_")) or position != int(immediate):
            raise ShaderGraphError(f"Unsupported group-shared operation {op}.")
        operands = item.operands[position + 1:]
        encoded_address = operands[0]
        index = self.uint(encoded_address, "x")
        offset = self.uint(encoded_address, "y" if stride else "x")
        location = (f"REasyShared{register}[{index}].words[{offset} / 4u]" if stride
                    else f"REasyShared{register}[{offset} / 4u]")
        temporary = f"reasySharedAtomic{self._atomic_index}"
        self._atomic_index += 1
        self._emit(f"uint {temporary};")
        if operation in methods and len(operands) == 2:
            self._emit(f"Interlocked{methods[operation]}({location}, {self.uint(operands[1], 1)}, {temporary});")
        elif operation in {"cmp_exch", "cmp_store"} and len(operands) == 3:
            self._emit(f"InterlockedCompareExchange({location}, {self.uint(operands[1], 1)}, {self.uint(operands[2], 1)}, {temporary});")
        elif operation in {"imax", "imin"} and len(operands) == 2:
            self._emit(f"InterlockedOr({location}, 0u, {temporary});")
            self._emit("[loop] while (true) {")
            self._emit(f"    uint desired = asuint({'max' if operation == 'imax' else 'min'}(asint({temporary}), {self.sint(operands[1], 1)}));")
            self._emit("    uint observed;")
            self._emit(f"    InterlockedCompareExchange({location}, {temporary}, desired, observed);")
            self._emit(f"    if (observed == {temporary}) break;")
            self._emit(f"    {temporary} = observed;")
            self._emit("}")
        else:
            raise ShaderGraphError(f"Unsupported group-shared atomic operands for {op}.")
        if immediate:
            self.write(item.operands[0], f"asfloat({temporary})")
        return True

    def emit_instruction(self, item: DxbcInstruction) -> None:
        if self._shared_instruction(item):
            return
        barriers = {"sync_g": "GroupMemoryBarrier", "sync_g_t": "GroupMemoryBarrierWithGroupSync",
                    "sync_uglobal": "DeviceMemoryBarrier", "sync_uglobal_t": "DeviceMemoryBarrierWithGroupSync",
                    "sync_uglobal_g": "AllMemoryBarrier", "sync_uglobal_g_t": "AllMemoryBarrierWithGroupSync"}
        if item.opcode in barriers and not item.operands:
            self._emit(barriers[item.opcode] + "();")
            return
        opcode = item.opcode
        base = opcode.removesuffix("_sat")
        saturate = opcode.endswith("_sat")
        if base in {"emit", "cut", "emitthencut", "emit_stream", "cut_stream", "emitthencut_stream"}:
            stream_match = re.fullmatch(r"m([0-3])", item.operands[0]) if len(item.operands) == 1 else None
            if (self.ir.stage != "gs" or (base.endswith("_stream") and stream_match is None)
                    or (not base.endswith("_stream") and item.operands)):
                raise ShaderGraphError("Geometry emission has an invalid stream operand")
            stream = int(stream_match[1]) if stream_match else 0
            if stream not in (self.ir.execution.output_streams or (0,)):
                raise ShaderGraphError("Geometry emission references an undeclared stream")
            if base.startswith("emit"):
                self._emit("{")
                self.indent += 1
                self.lines.extend(self._return_lines(emit=True, stream=stream))
                self.indent -= 1
                self._emit("}")
            if "cut" in base:
                self._emit(f"{_geometry_stream_name(stream)}.RestartStrip();")
            return
        if base == "mov":
            destination, source = item.operands
            resolved = self._dest(destination)
            if resolved is not None:
                # MOV copies bits; FXC may flush small integer bit patterns passed through float constructors.
                expression = self.raw(source, resolved[1]) if saturate else f"asfloat({self.uint(source, resolved[1])})"
                self.write(destination, expression, saturate=saturate)
            return
        if base in {"add", "mul", "div", "min", "max"}:
            operator = {"add": "+", "mul": "*", "div": "/"}.get(base)
            if operator is not None:
                self._binary(item, operator)
            else:
                destination, left, right = item.operands
                resolved = self._dest(destination)
                if resolved is not None:
                    lanes = resolved[1]
                    self.write(
                        destination,
                        f"{base}({self.raw(left, lanes)}, {self.raw(right, lanes)})",
                        saturate=saturate,
                    )
            return
        if base == "mad":
            destination, left, right, addend = item.operands
            resolved = self._dest(destination)
            if resolved is not None:
                lanes = resolved[1]
                self.write(
                    destination,
                    f"({self.raw(left, lanes)} * {self.raw(right, lanes)} + {self.raw(addend, lanes)})",
                    saturate=saturate,
                )
            return
        if base.startswith("dp") and base[2:] in {"2", "3", "4"}:
            destination, left, right = item.operands
            resolved = self._dest(destination)
            if resolved is not None:
                dot_width = int(base[2:])
                result = f"dot({self.raw(left, dot_width)}, {self.raw(right, dot_width)})"
                if len(resolved[1]) > 1:
                    result = _splat("float", len(resolved[1]), result)
                self.write(destination, result, saturate=saturate)
            return
        unary = {
            "rcp": lambda value: f"(1.0 / ({value}))",
            "rsq": lambda value: f"rsqrt({value})",
            "sqrt": lambda value: f"sqrt({value})",
            "exp": lambda value: f"exp2({value})",
            "log": lambda value: f"log2({value})",
            "frc": lambda value: f"frac({value})",
            "round_ne": lambda value: f"round({value})",
            "round_ni": lambda value: f"floor({value})",
            "round_pi": lambda value: f"ceil({value})",
            "round_z": lambda value: f"trunc({value})",
            "deriv_rtx_coarse": lambda value: f"ddx_coarse({value})",
            "deriv_rty_coarse": lambda value: f"ddy_coarse({value})",
        }
        if base in unary:
            destination, source = item.operands
            resolved = self._dest(destination)
            if resolved is not None:
                self.write(
                    destination,
                    unary[base](self.raw(source, resolved[1])),
                    saturate=saturate,
                )
            return
        if base in {"lt", "ge", "eq", "ne"}:
            self._comparison(item, {"lt": "<", "ge": ">=", "eq": "==", "ne": "!="}[base])
            return
        if base in {"ult", "uge", "ieq", "ine", "ilt", "ige"}:
            mode = "uint" if base[0] == "u" else "sint"
            compare = base[1:]
            self._comparison(
                item,
                {"lt": "<", "ge": ">=", "eq": "==", "ne": "!="}[compare],
                mode=mode,
            )
            return
        if base == "swapc":
            destination0, destination1, condition, left, right = item.operands
            # SM5 swapc reads both sources before writing either destination (including aliases).
            self._emit("{")
            self.indent += 1
            pending = []
            for index, (destination, when_true, when_false) in enumerate(
                ((destination0, right, left), (destination1, left, right))
            ):
                resolved = self._dest(destination)
                if resolved is None:
                    continue
                lanes = resolved[1]
                width = len(lanes)
                test = f"({self.uint(condition, lanes)} != {_splat('uint', width, '0u')})"
                yes, no = self.uint(when_true, lanes), self.uint(when_false, lanes)
                expression = f"({test} ? {yes} : {no})"
                name = f"reasySwap{index}"
                self._emit(f"{_width_type('uint', width)} {name} = {expression};")
                pending.append((destination, name))
            for destination, name in pending:
                self.write(destination, f"asfloat({name})", saturate=saturate)
            self.indent -= 1
            self._emit("}")
            return
        if base == "movc":
            destination, condition, when_true, when_false = item.operands
            resolved = self._dest(destination)
            if resolved is not None:
                lanes = resolved[1]
                width = len(lanes)
                test = f"({self.uint(condition, lanes)} != {_splat('uint', width, '0u')})"
                when_true_value = self.uint(when_true, lanes)
                when_false_value = self.uint(when_false, lanes)
                expression = f"({test} ? {when_true_value} : {when_false_value})"
                self.write(destination, f"asfloat({expression})", saturate=saturate)
            return
        if base in {"and", "or", "xor"}:
            self._binary(item, {"and": "&", "or": "|", "xor": "^"}[base], mode="uint")
            return
        if base in {"imin", "imax", "umin", "umax"}:
            destination, left, right = item.operands
            resolved = self._dest(destination)
            if resolved is not None:
                lanes = resolved[1]
                render = self.sint if base.startswith("i") else self.uint
                self.write(destination, f"asfloat({base[1:]}({render(left, lanes)}, {render(right, lanes)}))")
            return
        if base == "not":
            destination, source = item.operands
            resolved = self._dest(destination)
            if resolved is not None:
                self.write(destination, f"asfloat(~{self.uint(source, resolved[1])})")
            return
        if base == "udiv":
            quotient, remainder, numerator, denominator = item.operands
            # Both results read the sources before either destination is written; DXBC defines division by zero as UINT_MAX.
            self._emit("{")
            self.indent += 1
            statements = [f"uint4 reasyDivA = {self.uint(numerator, 'xyzw')};",
                          f"uint4 reasyDivB = {self.uint(denominator, 'xyzw')};"]
            for destination, name, operator in ((quotient, "reasyDivQ", "/"), (remainder, "reasyDivR", "%")):
                if self._dest(destination) is not None:
                    # Scalar selects compile identically in FXC and DXC's HLSL 2021 mode, which forbids a vector ?:
                    # condition.
                    lanes = [f"(reasyDivB.{lane} == 0u ? 0xffffffffu : reasyDivA.{lane} {operator} reasyDivB.{lane})" for lane in "xyzw"]
                    statements.append(f"uint4 {name} = uint4({', '.join(lanes)});")
            self.lines.extend("    " * self.indent + statement for statement in statements)
            for destination, name in ((quotient, "reasyDivQ"), (remainder, "reasyDivR")):
                resolved = self._dest(destination)
                if resolved is not None:
                    self.write(destination, f"asfloat({name}.{resolved[1]})")
            self.indent -= 1
            self._emit("}")
            return
        if base in {"imul", "umul"}:
            high, low, left, right = item.operands
            # Four 16-bit partial products give the full 64-bit result on SM5.
            self._emit("{")
            self.indent += 1
            render = (lambda value: f"asuint({self.sint(value, 'xyzw')})") if base == "imul" else (lambda value: self.uint(value, "xyzw"))
            statements = [f"uint4 reasyMulA = {render(left)};", f"uint4 reasyMulB = {render(right)};",
                          "uint4 reasyMulLo = reasyMulA * reasyMulB;"]
            if self._dest(high) is not None:
                from file_handlers.sdf.shader_graph_math import multiply_high_statements
                statements += multiply_high_statements('uint4','reasyMulA','reasyMulB','reasyMulHi',signed=base=='imul')
            self.lines.extend("    " * self.indent + statement for statement in statements)
            for destination, value in ((high, "reasyMulHi"), (low, "reasyMulLo")):
                resolved = self._dest(destination)
                if resolved is not None:
                    self.write(destination, f"asfloat({value}.{resolved[1]})")
            self.indent -= 1
            self._emit("}")
            return
        if base in {"iadd", "imad"}:
            operands = item.operands
            destination = operands[0]
            resolved = self._dest(destination)
            if resolved is not None:
                lanes = resolved[1]
                width = len(lanes)
                sources = operands[1:]
                alias = self._destination_alias(destination)
                if base == "iadd" and alias is not None and width == 1:
                    render = self.uint if alias[1] == "uint" else self.sint
                    self._emit(
                        f"{alias[0]} = ({render(sources[0], lanes)} + "
                        f"{render(sources[1], lanes)});"
                    )
                    return
                values = [self.sint(source, lanes) for source in sources]
                expression = (
                    f"({values[0]} + {values[1]})"
                    if base == "iadd"
                    else f"({values[0]} * {values[1]} + {values[2]})"
                )
                self.write(destination, f"asfloat({expression})")
            return
        if base == "ineg":
            destination, source = item.operands
            resolved = self._dest(destination)
            if resolved is not None:
                self.write(destination, f"asfloat(-{self.sint(source, resolved[1])})")
            return
        if base in {"ishl", "ishr", "ushr"}:
            destination, value, shift = item.operands
            resolved = self._dest(destination)
            if resolved is not None:
                lanes = resolved[1]
                left = self.uint(value, lanes) if base != "ishr" else self.sint(value, lanes)
                amount = self.uint(shift, lanes)
                self.write(destination, f"asfloat({left} {'<<' if base == 'ishl' else '>>'} ({amount} & 31u))")
            return
        if base in {"ftoi", "ftou", "itof", "utof", "f16tof32", "f32tof16", "firstbit_lo", "countbits", "bfrev"}:
            destination, source = item.operands
            resolved = self._dest(destination)
            if resolved is not None:
                lanes = resolved[1]
                width = len(lanes)
                value_type = _width_type("int", width)
                uint_type = _width_type("uint", width)
                if base == "ftoi":
                    expression = f"asfloat({value_type}({self.raw(source, lanes)}))"
                elif base == "ftou":
                    expression = f"asfloat({uint_type}({self.raw(source, lanes)}))"
                elif base == "itof":
                    expression = f"float{'' if width == 1 else width}({self.sint(source, lanes)})"
                elif base == "utof":
                    expression = f"float{'' if width == 1 else width}({self.uint(source, lanes)})"
                elif base == "f16tof32":
                    expression = f"f16tof32({self.uint(source, lanes)})"
                elif base == "f32tof16":
                    expression = f"asfloat(f32tof16({self.raw(source, lanes)}))"
                elif base == "countbits":
                    expression = f"asfloat(countbits({self.uint(source, lanes)}))"
                elif base == "bfrev":
                    expression = f"asfloat(reversebits({self.uint(source, lanes)}))"
                else:
                    expression = f"asfloat(firstbitlow({self.uint(source, lanes)}))"
                self.write(destination, expression)
            return
        if base == "ubfe":
            destination, width_operand, offset, value = item.operands
            resolved = self._dest(destination)
            if resolved is not None:
                lanes = resolved[1]
                self.write(
                    destination,
                    f"asfloat(REasyUbfe({self.uint(width_operand, lanes)}, {self.uint(offset, lanes)}, {self.uint(value, lanes)}))",
                )
            return
        if base == "bfi":
            destination, bit_width, offset, insert, base_value = item.operands
            resolved = self._dest(destination)
            if resolved is not None:
                lanes = resolved[1]
                self.write(
                    destination,
                    f"asfloat(REasyBfi({self.uint(bit_width, lanes)}, {self.uint(offset, lanes)}, {self.uint(insert, lanes)}, {self.uint(base_value, lanes)}))",
                )
            return
        if base == "sincos":
            sine_destination, cosine_destination, source = item.operands
            self._emit("{")
            self.indent += 1
            pending = []
            for index, (destination, function) in enumerate(((sine_destination, "sin"), (cosine_destination, "cos"))):
                resolved = self._dest(destination)
                if resolved is not None:
                    name = f"reasySincos{index}"
                    value_type = _width_type("float", len(resolved[1]))
                    self._emit(f"{value_type} {name} = {function}({self.raw(source, resolved[1])});")
                    pending.append((destination, name))
            for destination, name in pending:
                self.write(destination, name, saturate=saturate)
            self.indent -= 1
            self._emit("}")
            return
        if base.startswith("sample"):
            self._sample(item)
            return
        if base.startswith("resinfo"):
            self._resource_info(item)
            return
        if base.startswith("ld_"):
            self._load(item)
            return
        atomic_methods = {
            "imm_atomic_iadd": "InterlockedAdd",
            "imm_atomic_and": "InterlockedAnd",
            "imm_atomic_or": "InterlockedOr",
            "imm_atomic_xor": "InterlockedXor",
            "imm_atomic_imax": "InterlockedMax",
            "imm_atomic_imin": "InterlockedMin",
            "imm_atomic_umax": "InterlockedMax",
            "imm_atomic_umin": "InterlockedMin",
            "imm_atomic_exch": "InterlockedExchange",
        }
        if base in atomic_methods:
            destination, resource_token, address, value = item.operands
            match, resource = self._uav(resource_token)
            resolved = self._dest(destination)
            if resolved is None or len(resolved[1]) != 1 or resource is None:
                raise ShaderGraphError("unsupported recovered atomic-add operands")
            temporary = f"reasyAtomicOriginal{self._atomic_index}"
            self._atomic_index += 1
            signed_atomic = base in {"imm_atomic_imax", "imm_atomic_imin"}
            scalar_type = "int" if signed_atomic else "uint"
            render = self.sint if signed_atomic else self.uint
            self._emit(f"{scalar_type} {temporary};")
            self._emit(
                f"{resource_identifier(resource)}.{atomic_methods[base]}("
                f"{self.uint(address, 1)}, {render(value, 1)}, {temporary});"
            )
            self.write(destination, f"asfloat({temporary})")
            return
        if base in {
            "atomic_iadd",
            "atomic_and",
            "atomic_or",
            "atomic_xor",
            "atomic_imax",
            "atomic_imin",
            "atomic_umax",
            "atomic_umin",
            "atomic_exch",
        }:
            resource_token, address, value = item.operands
            match, resource = self._uav(resource_token)
            if resource is None or resource.input_type != "rw-byte-address-buffer":
                raise ShaderGraphError("unsupported recovered non-returning atomic operands")
            immediate_opcode = "imm_" + base
            method = atomic_methods[immediate_opcode]
            signed_atomic = base in {"atomic_imax", "atomic_imin"}
            scalar_type = "int" if signed_atomic else "uint"
            render = self.sint if signed_atomic else self.uint
            temporary = f"reasyAtomicDiscarded{self._atomic_index}"
            self._atomic_index += 1
            self._emit(f"{scalar_type} {temporary};")
            self._emit(
                f"{resource_identifier(resource)}.{method}("
                f"{self.uint(address, 1)}, {render(value, 1)}, {temporary});"
            )
            return
        if base == "store_raw":
            resource_token, address, value = item.operands
            match, resource = self._uav(resource_token)
            if resource is None:
                raise ShaderGraphError("raw-buffer store references an absent binding")
            components = match.group("swizzle") or "x"
            width = len(components)
            if not 1 <= width <= 4:
                raise ShaderGraphError("raw-buffer store has an invalid vector width")
            method = "Store" if width == 1 else f"Store{width}"
            self._emit(
                f"{resource_identifier(resource)}.{method}("
                f"{self.uint(address, 1)}, {self.uint(value, components)});"
            )
            return
        if base == "store_structured":
            resource_token, index, byte_offset, value = item.operands
            match, resource = self._uav(resource_token)
            if resource is None or resource.input_type != "rw-structured-buffer":
                raise ShaderGraphError("structured UAV store references an absent binding")
            components = match.group("swizzle") or "x"
            if not 1 <= len(components) <= 4 or len(set(components)) != len(components):
                raise ShaderGraphError("structured UAV store has an invalid component mask")
            rendered = self.uint(value, components)
            for source_component, target_component in enumerate(components):
                scalar = (
                    rendered
                    if len(components) == 1
                    else f"({rendered}).{'xyzw'[source_component]}"
                )
                self._emit(
                    f"REasyStoreRWStructuredWord_{resource.bind_point}("
                    f"{self.uint(index, 1)}, {self.uint(byte_offset, 1)} / 4u + "
                    f"{'xyzw'.index(target_component)}u, {scalar});"
                )
            return
        if base == "store_uav_typed":
            resource_token, coordinates, value = item.operands
            match, resource = self._uav(resource_token)
            if resource is None or resource.input_type != "rw-typed":
                raise ShaderGraphError("typed UAV store references an absent binding")
            coordinate_width = texture_coordinate_width(resource.dimension, load=True)
            if coordinate_width is None:
                raise ShaderGraphError(
                    f"typed UAV store has unsupported dimension {resource.dimension}"
                )
            components = match.group("swizzle") or "xyzw"
            rendered = {
                "uint": self.uint,
                "sint": self.sint,
            }.get(resource.return_type, self.raw)(value, components)
            suffix = "" if components == "xyzw" else "." + components
            self._emit(
                f"{resource_identifier(resource)}["
                f"{self.sint(coordinates, coordinate_width)}]{suffix} = {rendered};"
            )
            return
        if base == "if_nz" or base == "if_z":
            self._emit(f"if {self.condition(item.operands[0], zero=base == 'if_z')}")
            self._emit("{")
            self.indent += 1
            return
        if base == "else":
            self.indent -= 1
            self._emit("}")
            self._emit("else")
            self._emit("{")
            self.indent += 1
            return
        if base == "endif":
            self.indent -= 1
            self._emit("}")
            return
        if base in {"breakc_nz", "breakc_z"}:
            self._emit(f"if {self.condition(item.operands[0], zero=base.endswith('_z'))} break;")
            return
        if base in {"break", "continue"}:
            self._emit(base + ";")
            return
        if base in {"discard_nz", "discard_z"}:
            # Use clip() for the kill mask: a discard statement makes FXC taint later derivatives after UAV atomic branches.
            condition = self.condition(item.operands[0], zero=base == "discard_z")
            self._emit(f"clip({condition} ? -1.0 : 1.0);")
            return
        if base == "ret":
            self.lines.extend(self._return_lines())
            return
        raise ShaderGraphError(f"unsupported DXBC opcode {item.opcode!r}")

    def _return_lines(self, *, emit=False, stream=0) -> list[str]:
        lines: list[str] = []
        if (self.ir.stage == "gs" and not emit) or self.output_array:
            return ["    " * self.indent + "return;"]
        if self.contract.outputs:
            output_type = _stage_output_type(stream)
            fields = [field for field in self.contract.outputs if field.stream == stream]
            omitted = [_never_written(field) if self.ir.stage in _OMITTING_STAGES else 0 for field in fields]
            initializer = "" if any(omitted) else f" = ({output_type})0"
            lines.append("    " * self.indent + f"{output_type} result{initializer};")
            for field, never in zip(fields, omitted):
                lanes = [lane for lane in range(4) if field.mask & (1 << lane)]
                kept = [index for index, lane in enumerate(lanes) if not never & (1 << lane)]
                if not kept:
                    continue
                source = f"{_output_register(field)}.{''.join('xyzw'[lanes[index]] for index in kept)}"
                identifier = _identifier(
                    f"out_{field.semantic_name}_{field.semantic_index}"
                )
                if len(kept) < len(lanes):
                    identifier += "." + "".join("xyzw"[index] for index in kept)
                if field.component_type == "sint32":
                    source = f"asint({source})"
                elif field.component_type != "uint32":
                    source = f"asfloat({source})"
                lines.append("    " * self.indent + f"result.{identifier} = {source};")
            lines.append("    " * self.indent + (f"{_geometry_stream_name(stream)}.Append(result);" if emit else "return result;"))
        else:
            lines.append("    " * self.indent + "return;")
        return lines

    def body(self) -> list[str]:
        for index, item in enumerate(self.ir.instructions):
            self._precise_mask = item.precise_mask
            if self.loop_stack and index < self.loop_stack[-1].body_start:
                continue
            loop = self.loops_by_start.get(index)
            if loop is not None:
                if loop.predicate == "unconditional":
                    self._emit("[loop] while (true)")
                    self._emit("{")
                    self.indent += 1
                    self.loop_stack.append(loop)
                    continue
                render = self.uint if loop.state_type == "uint" else self.sint
                initial = render(loop.state, 1)
                self._emit(f"{loop.state_type} {loop.variable} = {initial};")
                self._push_alias(loop)
                self._emit(f"[loop] while ({self._loop_condition(loop)})")
                self._emit("{")
                self.indent += 1
                self.loop_stack.append(loop)
            elif index in self.loops_by_end:
                loop = self.loops_by_end[index]
                if not self.loop_stack or self.loop_stack[-1] != loop:
                    raise ShaderGraphError("unbalanced structured DXBC loop")
                self.indent -= 1
                self._emit("}")
                self.loop_stack.pop()
                if loop.predicate != "unconditional":
                    self._pop_alias(loop)
                    self.write(loop.state, f"asfloat({loop.variable})")
            else:
                self._emit_hook(index, "before")
                self.emit_instruction(item)
                self._emit_hook(index, "after")
        if self.indent != 1 or self.loop_stack or self._aliases:
            raise ShaderGraphError("unbalanced control flow in recovered DXBC shader")
        missing_hooks = set(self._hooks) - self._emitted_hooks
        if missing_hooks:
            raise ShaderGraphError(
                "recovered DXBC hook targets an instruction removed by "
                "structured control-flow recovery"
            )
        if not self.ir.instructions or self.ir.instructions[-1].opcode != "ret":
            self.lines.extend(self._return_lines())
        return self.lines


def _texture_type(resource: ShaderContractResource) -> str:
    if texture_coordinate_width(resource.dimension) is None:
        raise ShaderGraphError(f"unsupported recovered texture dimension {resource.dimension}")
    return texture_hlsl_type(resource.dimension, resource.return_type)


def _structured_declarations(resource: ShaderContractResource) -> list[str]:
    writable = resource.input_type == "rw-structured-buffer"
    prefix = "RW" if writable else ""
    stride = int(resource.sample_count)
    if stride <= 0 or stride % 4:
        raise ShaderGraphError(f"structured buffer {resource.name} has invalid stride {stride}")
    identifier = resource_identifier(resource)
    element = f"REasy{prefix}StructuredElement_{resource.bind_point}"
    words = stride // 4
    lines = [f"struct {element}", "{"]
    for slot in range((words + 3) // 4):
        width = min(4, words - slot * 4)
        lines.append(f"    {_width_type('uint', width)} slot{slot};")
    lines.extend(
        (
            "};",
            f"{prefix}StructuredBuffer<{element}> {identifier} : register({resource.register_class}{resource.bind_point});",
            f"uint REasyLoad{prefix}Structured_{resource.bind_point}(uint index, uint byteOffset)",
            "{",
            "    uint word = byteOffset >> 2;",
            "    uint component = word & 3u;",
            "    switch (word >> 2)",
            "    {",
        )
    )
    for slot in range((words + 3) // 4):
        width = min(4, words - slot * 4)
        if width == 1:
            value = f"{identifier}[index].slot{slot}"
        else:
            value = f"{identifier}[index].slot{slot}[min(component, {width - 1}u)]"
        lines.append(f"        case {slot}u: return {value};")
    lines.extend(("        default: return 0u;", "    }", "}", ""))
    if not writable:
        return lines
    lines.extend(
        (
            f"void REasyStoreRWStructuredWord_{resource.bind_point}(uint index, uint word, uint value)",
            "{",
            "    switch (word)",
            "    {",
        )
    )
    for word in range(words):
        slot, component = divmod(word, 4)
        width = min(4, words - slot * 4)
        target = f"{identifier}[index].slot{slot}"
        if width > 1:
            target += f".{'xyzw'[component]}"
        lines.append(f"        case {word}u: {target} = value; return;")
    lines.extend(("        default: return;", "    }", "}", ""))
    return lines


def _input_register_initializers(
    contract: ShaderContract,
    *,
    control_points: int = 0,
    source_name: str = "input",
    field_prefix: str = "in",
    register_prefix: str = "",
) -> list[str]:
    fields = tuple(f for f in contract.inputs if not control_points or f.register != 0xFFFFFFFF)
    registers = sorted({item.register for item in fields})
    prefix = register_prefix or ("vicp" if control_points else "v")
    lines = [f"    uint4 {prefix}{register}{f'[{control_points}]' if control_points else ''};" for register in registers]
    for register in registers:
        for index in range(control_points or 1):
            destination = f"{prefix}{register}[{index}]" if control_points else f"{prefix}{register}"
            lines.append(f"    {destination} = 0;")
    for field in fields:
        components = mask_components(field.mask)
        identifier = signature_member(field_prefix, field)
        for index in range(control_points or 1):
            source = f"{source_name}{f'[{index}]' if control_points else ''}.{identifier}"
            if field.component_type != "uint32":
                source = f"asuint({source})"
            destination = f"{prefix}{field.register}[{index}]" if control_points else f"{prefix}{field.register}"
            lines.append(f"    {destination}.{components} = {source};")
    return lines


def _temporary_registers(ir):
    return ([f"    uint4 r{register} = 0;" for register in range(ir.temp_count)]
            + [f"    uint4 x{array}[{count}];" for array, count, _ in ir.indexable_temps])


def _hull_entry_points(ir, contract, emitter_options):
    if not ir.hull_phases or not contract.patch_constants:
        raise ShaderGraphError("Hull shader lacks native phase/patch-constant semantics")
    execution = ir.execution
    input_patch = f"InputPatch<REasyStageInput, {execution.input_control_points}> input"
    output_patch = f"OutputPatch<REasyStageOutput, {execution.output_control_points}> output"
    patch_registers = max(field.register for field in contract.patch_constants) + 1
    if patch_registers > 128:
        raise ShaderGraphError("Patch-constant register extent is invalid")
    lines, patch_calls, control_phase = [], [], None
    for index, phase in enumerate(ir.hull_phases):
        stop = ir.hull_phases[index + 1].start if index + 1 < len(ir.hull_phases) else len(ir.instructions)
        body_ir = replace(ir, instructions=ir.instructions[phase.start:stop], hull_phases=(),
                          temp_count=phase.temp_count, indexable_temps=phase.indexable_temps)
        hooks = emitter_options["instruction_hooks"]
        phase_options = {**emitter_options, "instruction_hooks": hooks and DxbcHookPlan(tuple(
            replace(site, instruction_index=site.instruction_index - phase.start) for site in hooks.sites
            if phase.start <= site.instruction_index < stop))}
        if phase.kind == "control_point":
            control_phase, control_options = body_ir, phase_options
            continue
        name = f"REasyHullPhase{index}"
        lines.extend((f"void {name}({input_patch}, {output_patch}, uint vprim, uint v{phase.kind}instanceid, "
                      f"inout uint4 patchRegisters[{patch_registers}])", "{"))
        lines.extend(_input_register_initializers(contract, control_points=execution.input_control_points))
        lines.extend(_input_register_initializers(replace(contract, inputs=contract.outputs),
                     control_points=execution.output_control_points, source_name="output", field_prefix="out", register_prefix="vocp"))
        lines.extend(_temporary_registers(body_ir))
        lines.extend(_HlslEmitter(body_ir, contract, output_array="patchRegisters", **phase_options).body())
        lines.append("}")
        patch_calls.append(f"    [unroll] for (uint instance{index}=0; instance{index}<{phase.instance_count}; ++instance{index}) "
                           f"{name}(input, output, vprim, instance{index}, patchRegisters);")
    lines.extend((f"REasyPatchConstants REasyHullPatch({input_patch}, {output_patch}, uint vprim : SV_PrimitiveID)", "{",
                  f"    uint4 patchRegisters[{patch_registers}];"))
    lines.extend(f"    patchRegisters[{index}] = 0;" for index in range(patch_registers))
    lines.extend(patch_calls)
    lines.append("    REasyPatchConstants result = (REasyPatchConstants)0;")
    for field, _, _, member in patch_field_members(contract.patch_constants):
        source = f"patchRegisters[{field.register}].{mask_components(field.mask)}"
        if field.component_type != "uint32":
            source = f"as{'int' if field.component_type == 'sint32' else 'float'}({source})"
        lines.append(f"    result.{member} = {source};")
    lines.extend(("    return result;", "}",
                  f'[domain("{execution.domain}")][partitioning("{execution.partitioning}")]',
                  f'[outputtopology("{execution.tessellation_primitive}")][outputcontrolpoints({execution.output_control_points})]',
                  (f'[maxtessfactor({execution.max_tessellation_factor})]' if execution.max_tessellation_factor is not None else '')
                  + '[patchconstantfunc("REasyHullPatch")]',
                  f"REasyStageOutput main({input_patch}, uint voutputcontrolpointid : SV_OutputControlPointID, uint vprim : SV_PrimitiveID)", "{"))
    if control_phase is None:
        if execution.input_control_points != execution.output_control_points:
            raise ShaderGraphError("A passthrough hull changes its control-point count")
        lines.append("    REasyStageOutput result = (REasyStageOutput)0;")
        for field in contract.outputs:
            source = contract.input(*field.key)
            if source is None or source.value_type != field.value_type:
                raise ShaderGraphError("Passthrough hull input/output semantics differ")
            lines.append(f"    result.{signature_member('out', field)} = "
                         f"input[voutputcontrolpointid].{signature_member('in', source)};")
        lines.append("    return result;")
    else:
        lines.extend(_input_register_initializers(contract, control_points=execution.input_control_points))
        lines.extend(_temporary_registers(control_phase))
        lines.extend(f"    uint4 o{register} = 0;" for register in sorted({f.register for f in contract.outputs}))
        lines.extend(_HlslEmitter(control_phase, contract, **control_options).body())
    return lines + ["}", ""]


def _semantic_cbuffer_declaration(buffer: ShaderContractBuffer) -> list[str]:
    if int(buffer.byte_size) <= 0 or int(buffer.byte_size) % 4:
        raise ShaderGraphError(
            f"semantic constant buffer {buffer.name} has an invalid byte size"
        )
    name = _identifier(buffer.name, prefix=f"REasyBuffer{buffer.bind_point}")
    lines = [
        f"cbuffer {name} : register(b{int(buffer.bind_point)})",
        "{",
    ]
    words: dict[int, str] = {}
    maximum_end = 0
    identifiers: set[str] = set()
    for variable in sorted(buffer.variables, key=lambda item: item.byte_offset):
        declaration = str(variable.hlsl_declaration or "").strip()
        identifier = str(variable.name)
        match = re.fullmatch(
            r"(?:(?P<major>row_major|column_major)\s+)?"
            r"(?P<base>float|int|uint)(?P<rows>[1-4]?)"
            r"(?:x(?P<columns>[1-4]))?",
            declaration,
            re.IGNORECASE,
        )
        if (
            match is None
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier)
            or identifier in identifiers
            or int(variable.byte_offset) < 0
            or int(variable.byte_offset) % 4
            or int(variable.array_length) <= 0
            or bool(match.group("columns")) != bool(variable.matrix)
        ):
            raise ShaderGraphError(
                f"semantic material field {buffer.name}.{variable.name} has an unsupported declaration"
            )
        register, byte_in_register = divmod(int(variable.byte_offset), 16)
        array_length = int(variable.array_length)
        suffix = f"[{array_length}]" if array_length > 1 else ""
        aggregate = bool(variable.matrix) or array_length > 1
        if aggregate and byte_in_register:
            raise ShaderGraphError(
                f"semantic material field {buffer.name}.{variable.name} is not register-aligned"
            )
        lines.append(
            f"    {declaration} {identifier}{suffix} : "
            f"packoffset(c{register}{'' if aggregate else '.' + 'xyzw'[byte_in_register // 4]});"
        )
        base = match.group("base").casefold()
        rows = int(match.group("rows") or "1")
        columns = int(match.group("columns") or "0")

        def store(word_offset: int, value: str) -> None:
            if base != "uint":
                value = f"asuint({value})"
            words[int(variable.byte_offset) // 4 + word_offset] = value

        if columns:
            row_major = (match.group("major") or "column_major").casefold() == "row_major"
            major_count = rows if row_major else columns
            minor_count = columns if row_major else rows
            expected_size = array_length * major_count * 16
            if int(variable.byte_size) != expected_size:
                raise ShaderGraphError(
                    f"semantic material matrix {buffer.name}.{variable.name} has an inconsistent size"
                )
            for array_index in range(array_length):
                prefix = f"{identifier}[{array_index}]" if array_length > 1 else identifier
                for major in range(major_count):
                    for minor in range(minor_count):
                        row, column = (major, minor) if row_major else (minor, major)
                        store(
                            array_index * major_count * 4 + major * 4 + minor,
                            f"{prefix}[{row}][{column}]",
                        )
        elif array_length > 1:
            minimum_size = (array_length - 1) * 16 + rows * 4
            maximum_size = array_length * 16
            if not minimum_size <= int(variable.byte_size) <= maximum_size:
                raise ShaderGraphError(
                    f"semantic material array {buffer.name}.{variable.name} has an inconsistent size"
                )
            for array_index in range(array_length):
                prefix = f"{identifier}[{array_index}]"
                for component in range(rows):
                    value = prefix if rows == 1 else f"{prefix}.{'xyzw'[component]}"
                    store(array_index * 4 + component, value)
        else:
            if int(variable.byte_size) != rows * 4:
                raise ShaderGraphError(
                    f"semantic material field {buffer.name}.{variable.name} has an inconsistent size"
                )
            for component in range(rows):
                value = identifier if rows == 1 else f"{identifier}.{'xyzw'[component]}"
                store(component, value)
        identifiers.add(identifier)
        maximum_end = max(
            maximum_end,
            int(variable.byte_offset) + int(variable.byte_size),
        )
    if maximum_end < int(buffer.byte_size):
        offset = int(buffer.byte_size) - 4
        register, byte_in_register = divmod(offset, 16)
        lines.append(
            f"    uint REasyReserved_{int(buffer.bind_point)}_{offset} : "
            f"packoffset(c{register}.{'xyzw'[byte_in_register // 4]});"
        )
    lines.extend(("};", ""))
    lines.extend(
        (
            f"uint4 REasyLoadCB{int(buffer.bind_point)}(uint registerIndex)",
            "{",
            "    switch (registerIndex)",
            "    {",
        )
    )
    for register in range((int(buffer.byte_size) + 15) // 16):
        values = [words.get(register * 4 + component, "0u") for component in range(4)]
        lines.append(
            f"        case {register}u: return uint4({', '.join(values)});"
        )
    lines.extend(("        default: return 0u;", "    }", "}", ""))
    return lines


def _graph_inputs(expression, ir, contract):
    def read(match):
        register, element = int(match[1]), match[2]
        if register not in {field.register for field in contract.inputs}:
            raise ShaderGraphError("Graph references an absent input register")
        count = ir.execution.primitive_vertices if ir.stage == "gs" else ir.execution.input_control_points if ir.stage in {"hs", "ds"} else 0
        if count:
            if element is None or not element.isdecimal() or not 0 <= int(element) < count:
                raise ShaderGraphError("Graph input needs an explicit control-point/primitive index within the native extent")
            return f"asfloat(vicp{register}[{element}])"
        if element is not None:
            raise ShaderGraphError("Scalar stage input has no primitive axis")
        return f"asfloat(v{register})"
    # An assignment writes the register itself (a surface edit replacing an input as the program reads it).
    return re.sub(r"\bv(\d+)\b(?:\[([^\]]+)\])?(?!\.[xyzw]{1,4}\s*=(?!=))", read, expression)


def _generated_source(lines, ir, contract, unwritten):
    source = "\n".join(lines)
    if len(source.encode("utf-8")) > 4 * 1024 * 1024:
        raise ShaderGraphError("recovered DXBC shader source exceeds the safety limit")
    return GeneratedShaderSource(source, "main", contract.target, ieee_strict="refactoringAllowed" not in ir.global_flags,
                                 unwritten_outputs=unwritten)


def generate_recovered_dxbc_shader(
    ir: DxbcSemanticIr,
    contract: ShaderContract,
    *,
    constant_overrides: Mapping[tuple[int, int], str] | None = None,
    structured_overrides: Mapping[tuple[int, int], str] | None = None,
    texture_binding_overrides: Mapping[int, int] | None = None,
    additional_helpers: str = "",
    instruction_hooks: DxbcHookPlan | None = None,
    semantic_buffer_names: frozenset[str] = frozenset(),
) -> GeneratedShaderSource:
    expected_stage = {"pixel": "ps", "vertex": "vs", "compute": "cs", "domain": "ds", "geometry": "gs", "hull": "hs"}.get(contract.stage)
    if expected_stage is None or ir.stage != expected_stage:
        raise ShaderGraphError("recovered shader stage does not match its reflected contract")
    constant_overrides = {key: _graph_inputs(value, ir, contract) for key, value in (constant_overrides or {}).items()}
    structured_overrides = {key: _graph_inputs(value, ir, contract) for key, value in (structured_overrides or {}).items()}
    if instruction_hooks is not None:
        instruction_hooks = DxbcHookPlan(tuple(
            replace(site, statements=tuple(_graph_inputs(line, ir, contract) for line in site.statements))
            for site in instruction_hooks.sites))
    _validate_recovered_control_flow(ir)
    unwritten = _unwritten_outputs(ir, contract)
    lines = [
        "// Generated from REasy's semantic DXBC model.",
        "// This is regenerated behavior, not preserved source bytecode.",
        "#pragma warning(disable: 4008)",
        "#pragma warning(disable: 3557)",
        # Lanes the native program never writes stay unwritten, so its signature keeps them unused.
        *(("#pragma warning(disable: 3578)",) if unwritten else ()),
        "",
        "uint REasyUbfeScalar(uint width, uint offset, uint value)",
        "{",
        "    width &= 31u;",
        "    if (width == 0u) return 0u;",
        "    uint mask = (1u << width) - 1u;",
        "    return (value >> (offset & 31u)) & mask;",
        "}",
        "uint REasyBfiScalar(uint width, uint offset, uint insertValue, uint baseValue)",
        "{",
        "    width &= 31u;",
        "    if (width == 0u) return baseValue;",
        "    uint shift = offset & 31u;",
        "    uint sourceMask = (1u << width) - 1u;",
        "    uint mask = sourceMask << shift;",
        "    return (baseValue & ~mask) | ((insertValue << shift) & mask);",
        "}",
    ]
    for width in range(1, 5):
        suffix = "" if width == 1 else str(width)
        uint_type = f"uint{suffix}"
        if width == 1:
            lines.extend(
                (
                    "uint REasyUbfe(uint width, uint offset, uint value) { return REasyUbfeScalar(width, offset, value); }",
                    "uint REasyBfi(uint width, uint offset, uint insertValue, uint baseValue) { return REasyBfiScalar(width, offset, insertValue, baseValue); }",
                )
            )
        else:
            components = "xyzw"[:width]
            ubfe = ", ".join(
                f"REasyUbfeScalar(width.{component}, offset.{component}, value.{component})"
                for component in components
            )
            bfi = ", ".join(
                f"REasyBfiScalar(width.{component}, offset.{component}, insertValue.{component}, baseValue.{component})"
                for component in components
            )
            lines.extend(
                (
                    f"{uint_type} REasyUbfe({uint_type} width, {uint_type} offset, {uint_type} value) {{ return {uint_type}({ubfe}); }}",
                    f"{uint_type} REasyBfi({uint_type} width, {uint_type} offset, {uint_type} insertValue, {uint_type} baseValue) {{ return {uint_type}({bfi}); }}",
                )
            )
    lines.append("")

    if ir.immediate_constants:
        lines.append(
            f"static const uint4 REasyICB[{len(ir.immediate_constants)}] ="
        )
        lines.append("{")
        for row in ir.immediate_constants:
            lines.append(
                "    uint4(" + ", ".join(_uint_literal(value) for value in row) + "),"
            )
        lines.extend(("};", ""))

    semantic_names = {str(value).casefold() for value in semantic_buffer_names}
    semantic_bindings = frozenset(
        int(buffer.bind_point)
        for buffer in contract.constant_buffers
        if buffer.name.casefold() in semantic_names
    )
    for buffer in contract.constant_buffers:
        if int(buffer.bind_point) in semantic_bindings:
            lines.extend(_semantic_cbuffer_declaration(buffer))
        else:
            registers = max(1, (int(buffer.byte_size) + 15) // 16)
            name = _identifier(buffer.name, prefix=f"REasyBuffer{buffer.bind_point}")
            lines.extend(
                (
                    f"cbuffer {name} : register(b{buffer.bind_point})",
                    "{",
                    f"    uint4 REasyCB{buffer.bind_point}[{registers}];",
                    "};",
                    "",
                )
            )
    for resource in contract.resources:
        declaration_start = len(lines)
        identifier = resource_identifier(resource)
        suffix = f"[{resource.bind_count}]" if resource.bind_count > 1 else ""
        if resource.input_type in {"constant-buffer"}:
            continue
        if resource.input_type == "texture":
            lines.append(
                f"{_texture_type(resource)} {identifier}{suffix} : register(t{resource.bind_point});"
            )
        elif resource.input_type in {"sampler", "byte-address-buffer", "rw-byte-address-buffer"}:
            lines.append(resource_declaration(resource))
        elif resource.input_type == "rw-typed":
            element = texture_element_type(resource.return_type)
            object_type = {
                "texture1d": "RWTexture1D",
                "texture1d-array": "RWTexture1DArray",
                "texture2d": "RWTexture2D",
                "texture2d-array": "RWTexture2DArray",
                "texture3d": "RWTexture3D",
            }.get(resource.dimension)
            if object_type is None:
                raise ShaderGraphError(
                    f"unsupported recovered RW texture dimension {resource.dimension}"
                )
            lines.append(
                f"{object_type}<{element}> {identifier}{suffix} : register(u{resource.bind_point});"
            )
        elif resource.input_type in {"structured-buffer", "rw-structured-buffer"}:
            lines.extend(_structured_declarations(resource))
        else:
            raise ShaderGraphError(
                f"unsupported recovered resource kind {resource.input_type}"
            )
        if resource.register_class == "u" and resource.bind_point in ir.globally_coherent_uavs:
            for index in range(declaration_start, len(lines)):
                if lines[index].startswith("RW") and ": register(" in lines[index]:
                    lines[index] = "globallycoherent " + lines[index]
    for register, stride, count in ir.shared_memory:
        if stride:
            lines.append(f"struct REasySharedElement{register} {{ uint words[{stride // 4}]; }};")
            lines.append(f"groupshared REasySharedElement{register} REasyShared{register}[{count}];")
        else:
            lines.append(f"groupshared uint REasyShared{register}[{count // 4}];")
    lines.append("")

    lines.extend(_recovered_buffer_aliases(contract, semantic_bindings))

    if contract.inputs:
        lines.extend(("struct REasyStageInput", "{"))
        input_modes = (pixel_signature_interpolation(contract.inputs, ir.input_interpolation) if ir.stage == "ps"
                       else signature_packing_modes(contract.inputs) if ir.stage in {"gs", "hs", "ds"} else {})
        for field in contract.inputs:
            if ir.stage in {"gs", "hs", "ds"} and field.register == 0xFFFFFFFF:
                continue  # Primitive ID is a separate scalar, not a vertex attribute.
            lines.append(signature_declaration(field, signature_member("in", field), interpolation=input_modes.get(field.key)))
        lines.extend(("};", ""))
    streams = sorted({field.stream for field in contract.outputs})
    if ir.stage != "gs" and any(stream != 0 for stream in streams):
        raise ShaderGraphError("Only a geometry shader can declare multiple output streams")
    for stream in streams:
        fields = tuple(field for field in contract.outputs if field.stream == stream)
        lines.extend((f"struct {_stage_output_type(stream)}", "{"))
        output_modes = signature_packing_modes(fields) if ir.stage != "ps" else {}
        for field in fields:
            lines.append(signature_declaration(field, signature_member("out", field), interpolation=output_modes.get(field.key)))
        lines.extend(("};", ""))
    if ir.stage in {"ds", "hs"}:
        lines.extend(patch_struct(contract.patch_constants))

    helpers = str(additional_helpers).strip()
    if len(helpers.encode("utf-8")) > 1024 * 1024:
        raise ShaderGraphError("recovered DXBC material helpers exceed the safety limit")
    if helpers:
        lines.extend((helpers, ""))

    emitter_options = dict(constant_overrides=constant_overrides, structured_overrides=structured_overrides,
                           texture_binding_overrides=texture_binding_overrides, instruction_hooks=instruction_hooks,
                           semantic_buffer_bindings=semantic_bindings)
    if ir.stage == "hs":
        lines.extend(_hull_entry_points(ir, contract, emitter_options))
        return _generated_source(lines, ir, contract, unwritten)

    return_type = "REasyStageOutput" if contract.outputs else "void"
    if "forceEarlyDepthStencil" in ir.global_flags:
        lines.append("[earlydepthstencil]")
    parameter = "REasyStageInput input" if contract.inputs else ""
    if ir.stage == "ps":
        parameter = ", ".join(([parameter] if parameter else []) +
                              [f"uint {name} : {_PIXEL_SYSTEM_INPUTS[name]}" for name in ir.pixel_system_inputs])
    if ir.stage == "cs":
        if contract.inputs or contract.outputs:
            raise ShaderGraphError("Compute shaders cannot have vertex/pixel signature registers.")
        parameter = ", ".join(f"{_COMPUTE_INPUTS[name][0]} {name}: {_COMPUTE_INPUTS[name][1]}" for name in ir.compute_inputs)
        lines.append(f"[numthreads({', '.join(map(str, ir.thread_group_size))})]")
    if ir.stage == "ds":
        parameter = (f"REasyPatchConstants patch, float{'3' if ir.execution.domain == 'tri' else '2'} vDomain : SV_DomainLocation, "
                     f"const OutputPatch<REasyStageInput, {ir.execution.input_control_points}> input")
        lines.append(f'[domain("{ir.execution.domain}")]')
    if ir.stage == "gs":
        stream_type = {"pointlist": "PointStream", "linestrip": "LineStream", "trianglestrip": "TriangleStream"}[ir.execution.output_topology]
        parameters = [f"{ir.execution.input_primitive} REasyStageInput input[{ir.execution.primitive_vertices}]"]
        parameters.extend(f"uint {name} : {_PRIMITIVE_SYSTEM_INPUTS[name]}" for name in ir.primitive_system_inputs)
        # HLSL stream numbers are parameter ordinals, so unused preceding streams reserve them (DXBC can declare only m2).
        parameters.extend(
            f"inout {stream_type}<{_stage_output_type(stream if stream in streams else streams[0])}> {_geometry_stream_name(stream)}"
            for stream in range(max(streams,default=-1)+1)
        )
        parameter, return_type = ", ".join(parameters), "void"
        lines.append(f"[maxvertexcount({ir.execution.max_output_vertices})]")
        if ir.execution.geometry_instances != 1:
            lines.append(f"[instance({ir.execution.geometry_instances})]")
    lines.extend((f"{return_type} main({parameter})", "{"))
    lines.extend(
        _input_register_initializers(
            contract,
            control_points=(ir.execution.input_control_points if ir.stage == "ds" else ir.execution.primitive_vertices if ir.stage == "gs" else 0),
        )
    )
    if ir.stage == "ds":
        for register in sorted({field.register for field in contract.patch_constants}):
            lines.append(f"    uint4 vpc{register} = 0;")
        for field, _, _, member in patch_field_members(contract.patch_constants):
            value = f"patch.{member}"
            if field.component_type != "uint32":
                value = f"asuint({value})"
            lines.append(f"    vpc{field.register}.{mask_components(field.mask)} = {value};")
    lines.extend(_temporary_registers(ir))
    for name in sorted({_output_register(item) for item in contract.outputs}):
        lines.append(f"    uint4 {name} = uint4(0u, 0u, 0u, 0u);")
    lines.extend(
        _HlslEmitter(
            ir,
            contract,
            **emitter_options,
        ).body()
    )
    lines.extend(("}", ""))
    return _generated_source(lines, ir, contract, unwritten)


__all__ = [
    "DxbcHookPlan",
    "DxbcHookSite",
    "DxbcInstruction",
    "DxbcSemanticIr",
    "IR_SCHEMA",
    "IR_VERSION",
    "MAX_IR_JSON_BYTES",
    "generate_recovered_dxbc_shader",
    "parse_dxbc_disassembly",
    "recover_dxbc_ir",
    "relocated_dxbc_inputs",
    "validate_recovered_dxbc_shader_bytecode",
]
