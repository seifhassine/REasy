"""See docs/internals/surface-splicing.md, Readers."""
from __future__ import annotations

from dataclasses import dataclass, field
import re

lazy from file_handlers.sdf.shader_graph import ShaderGraphError

lazy from .legacy_register_flow import destination_count
lazy from .native_dxbc_precision import operand_precision
lazy from .profiles.dxbc_recovery import DxbcSemanticIr, _output_register
lazy from .surface_flow import (
    TEXEL_TYPES, FlowStore, FlowValue, NativeFlow, buffer_component_type, buffer_element, constant_field,
    declared_component_types, structured_element,
)


_FLOAT = frozenset(("add mul mad div min max dp2 dp3 dp4 frc sqrt rsq rcp exp log round_ne round_ni round_pi "
                    "round_z deriv_rtx deriv_rty deriv_rtx_coarse deriv_rty_coarse deriv_rtx_fine deriv_rty_fine "
                    "sincos itof utof f16tof32").split())
_BOOL = frozenset("lt ge eq ne ieq ine ilt ige ult uge".split())
_INT = frozenset("iadd imul imad ishl ishr ineg imin imax ftoi ibfe firstbit_shi".split())
_UINT = frozenset("umul udiv umin umax ushr ubfe bfi and or xor not countbits firstbit_lo firstbit_hi bfrev ftou "
                  "f32tof16 uaddc usubb".split())
_COPY = frozenset("mov movc swapc".split())
_TEXTURE_READS = frozenset("sample sample_b sample_l sample_d sample_c sample_c_lz gather4 gather4_c gather4_po "
                           "gather4_po_c ld ld_ms".split())
_DOT = frozenset("dp2 dp3 dp4".split())
_BUFFER_STORES = frozenset(("store_raw", "store_structured", "store_uav_typed"))
_PURE = _FLOAT | _BOOL | _INT | _UINT | _COPY
_NON_WRITERS = re.compile(r"^(?:if_n?z|else|endif|loop|endloop|break(?:c_n?z)?|continue(?:c_n?z)?|ret(?:c_n?z)?|"
                          r"switch|case|default|endswitch|discard_n?z|store_\w+|atomic_\w+|sync\w*|emit\w*|cut\w*|"
                          r"nop|label|callc?\w*|hs_\w+)$")
_DESTINATION = re.compile(r"^(?:[ro]\d+|x\d+\[[^\]]+\]|odepth(?:ge|le)?|omask|ostencilref|null)(?:\.[xyzw]{1,4})?$")
_SWIZZLED = re.compile(r"^(?P<base>.+?)(?:\.(?P<mask>[xyzw]{1,4}))?$")
_TEMP = re.compile(r"^r(\d+)$")
_INDEXABLE = re.compile(r"^x(\d+)\[(?P<index>.+)\]$")
_CONSTANT = re.compile(r"^cb(\d+)\[(?P<index>.+)\]$")
_TABLE = re.compile(r"^icb\[(?P<index>.+)\]$")
_RESOURCE = re.compile(r"^[tu]\d+$")
_SAMPLER = re.compile(r"^s\d+$")
_DEAD = None


@dataclass
class _Frame:
    kind: str
    entry: dict | None
    condition: list = field(default_factory=list)
    then: dict | None = None
    alternate: bool = False
    exits: list = field(default_factory=list)
    back_edges: list = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    controls: list = field(default_factory=list)


def _base_opcode(opcode: str) -> str:
    return opcode.removesuffix("_sat").replace("_aoffimmi", "").replace("_indexable", "")


def _writes(instruction) -> int:
    count = destination_count(instruction)
    if count:
        return count
    opcode = _base_opcode(instruction.opcode)
    if _NON_WRITERS.match(opcode) or not instruction.operands:
        return 0
    return 1 if _DESTINATION.match(operand_precision(instruction.operands[0])[0].casefold()) else 0


def _lanes(mask: str | None, base: str) -> str:
    return mask or ("x" if not base[-1:].isdigit() else "xyzw")


def _literal_lane(text: str, lane: int) -> str:
    parts = [part.strip() for part in text[2:-1].split(",")]
    return parts[lane if len(parts) > 1 else 0]


def dxbc_flow(ir: DxbcSemanticIr, contract) -> NativeFlow:
    """Each hull phase starts afresh, its fork and join phases writing patch constants."""
    phases = {phase.start: phase for phase in ir.hull_phases}
    if any(phase.instance_count > 1 for phase in ir.hull_phases):
        raise ShaderGraphError("A hull phase runs as several instances, so its outputs cannot be told apart.")
    scope = ["output"]
    values: list[list] = []
    memo: dict[tuple, int] = {}
    memory: dict[str, list[int]] = {}
    deferred: list[tuple[int, str]] = []
    stores, discards, exits, texture_reads, read_types = [], [], [], [], {}
    textures = {f"t{item.bind_point}": item for item in contract.resources
                if item.input_type == "texture" and item.register_space == 0}
    writable = {f"u{item.bind_point}": item for item in contract.resources
                if item.input_type.startswith("rw") and item.register_space == 0}
    buffer_scalars: dict[tuple, list[str]] = {}
    typed: dict[str, str] = {}
    instructions = ir.instructions
    reads: dict[tuple[int, str, int], object] = {}
    current = [0]

    def add(kind, operands, signature, scalar="", origin=None, controls=()):
        values.append([kind, [item for item in operands if isinstance(item, int)],
                       [item for item in controls if isinstance(item, int)], signature, scalar, origin])
        return len(values) - 1

    def source(key, scalar="", operands=()):
        if key not in memo:
            memo[key] = add("source", operands, (re.match(r"[a-z]*", str(key[0]), re.IGNORECASE)[0],), scalar, key)
        return memo[key]

    def scalar_of(item):
        return values[item][4] if isinstance(item, int) else ""

    def read(state, text, lane):
        clean, _stored, _used = operand_precision(text.strip())
        clean = clean.lstrip("-")
        if clean.startswith("|") and clean.endswith("|"):
            clean = clean[1:-1]
        if clean.startswith("l("):
            return ("constant", _literal_lane(clean, lane))
        match = _SWIZZLED.match(clean)
        base, mask = match["base"], _lanes(match["mask"], match["base"])
        component = "xyzw".index(mask[lane if len(mask) > 1 else 0])
        temp = _TEMP.match(base)
        if temp:
            value = state.get(("r", int(temp[1]), component), ("constant", "undefined"))
            reads[(current[0], base, component)] = value
            return value
        indices = [value for index in re.findall(r"\[([^\]]+)\]", base)
                   for name in re.findall(r"\b(?:r\d+\.[xyzw]|vOutputControlPointID(?:\.x)?)", index)
                   for value in (read(state, name, 0),)]
        if _INDEXABLE.match(base) is not None:
            value = add("source", indices, ("indexable", base.split("[", 1)[0]), "", ("indexable", base, component))
            deferred.append((value, base.split("[", 1)[0]))
            return value
        if (constant := _CONSTANT.match(base)) is not None:
            if constant["index"].strip().isdigit():
                return source(("cb", int(constant[1]), int(constant["index"]), component))
            return add("source", indices, ("cb",), "", (base, component))
        if (table := _TABLE.match(base)) is not None:
            if table["index"].strip().isdigit():
                return ("constant", f"icb[{table['index'].strip()}].{'xyzw'[component]}")
            return add("pure", indices, ("icb", component), "", (base, component))
        if indices or base.startswith("v"):
            return source((re.sub(r"\[[^\]]*\]", "[]", base), tuple(indices), component)) if indices else \
                source((base, component))
        raise ShaderGraphError(f"DXBC operand {text} has no dataflow meaning.")

    def destinations(instruction, count):
        result = []
        for text in instruction.operands[:count]:
            clean = operand_precision(text)[0].casefold()
            if clean == "null":
                result.append(())
                continue
            match = _SWIZZLED.match(clean)
            base, mask = match["base"], _lanes(match["mask"], match["base"])
            result.append(tuple((base, "xyzw".index(axis)) for axis in mask))
        return result

    def key_of(base, lane):
        temp = _TEMP.match(base)
        return ("r", int(temp[1]), lane) if temp else ("o", base, lane)

    def merge(states, controls, position):
        alive = [item for item in states if item is not _DEAD]
        if not alive:
            return _DEAD
        result = {}
        for key in sorted(set().union(*alive), key=repr):
            seen = [item.get(key, ("constant", "undefined")) for item in alive]
            if all(item == seen[0] for item in seen):
                result[key] = seen[0]
            else:
                scalars = {scalar_of(item) for item in seen} - {""}
                result[key] = add("join", seen, ("join",), scalars.pop() if len(scalars) == 1 else "",
                                  ("join", position, key), controls)
        return result

    def body_writes(start):
        depth, written = 0, set()
        for position in range(start + 1, len(instructions)):
            instruction = instructions[position]
            opcode = _base_opcode(instruction.opcode)
            if opcode == "loop":
                depth += 1
            elif opcode == "endloop":
                if depth == 0:
                    return written
                depth -= 1
            for slots in destinations(instruction, _writes(instruction)):
                written.update(key_of(base, lane) for base, lane in slots)
        raise ShaderGraphError("DXBC loop has no end.")

    def output_stores(state, position, kind, stream=None):
        if state is _DEAD:
            return
        for output in (contract.patch_constants if scope[0] == "patch" else contract.outputs):
            if stream is not None and output.stream != stream:
                continue
            register = _output_register(output)
            for lane in range(4):
                if not output.mask & (1 << lane):
                    continue
                value = state.get(("o", register, lane))
                if value is None:
                    continue
                stores.append(FlowStore(output.semantic_name.casefold(), output.semantic_index, lane,
                                        value if isinstance(value, int) else None,
                                        "" if isinstance(value, int) else value[1], (position, kind), scope[0]))

    def operand_value(position, text, lane):
        clean = operand_precision(text.strip())[0]
        if clean.startswith(("-", "|")):
            return None
        if clean.startswith("l("):
            return ("constant", _literal_lane(clean, lane))
        match = _SWIZZLED.match(clean)
        mask = _lanes(match["mask"], match["base"])
        component = "xyzw".index(mask[lane if len(mask) > 1 else 0])
        if _TEMP.match(match["base"]):
            return reads.get((position, match["base"], component))
        constant = _CONSTANT.match(match["base"])
        if constant is not None and constant["index"].strip().isdigit():
            return memo.get(("cb", int(constant[1]), int(constant["index"]), component))
        return None

    def address_terms(item) -> tuple[list[str], int]:
        if item is None:
            return [], 0
        if not isinstance(item, int):
            try:
                word = int(item[1], 0) & 0xFFFFFFFF
            except ValueError as exc:
                raise ShaderGraphError(f"A store address adds {item[1]}, which is not an integer.") from exc
            return [], word - (word >> 31 << 32)
        origin = values[item][5]
        if isinstance(origin, tuple) and len(origin) == 4 and origin[0] == "cb":
            return [constant_field(contract, origin[1], 0, origin[2] * 16 + origin[3] * 4)], 0
        if not (isinstance(origin, tuple) and len(origin) == 3 and isinstance(origin[0], int)):
            return [], 0
        defined = instructions[origin[0]]
        addends = {"iadd": defined.operands[1:3], "imad": defined.operands[3:4]}.get(defined.opcode, ())
        fields, total = [], 0
        for text in addends:
            more, constant = address_terms(operand_value(origin[0], text, origin[2]))
            fields.extend(more)
            total += constant
        return fields, total

    def buffer_stores(position, instruction, state):
        target = _SWIZZLED.match(operand_precision(instruction.operands[0])[0].casefold())
        if not target["base"].startswith("u"):
            return  # Thread-group memory is the program's own scratch space.
        resource = writable.get(target["base"])
        if resource is None:
            raise ShaderGraphError(f"The program stores into {target['base']}, which it does not declare.")
        opcode = _base_opcode(instruction.opcode)
        if opcode == "store_uav_typed":
            semantic, first = resource.name, 0
        else:
            fields, offset = address_terms(read(state, instruction.operands[1], 0))
            if opcode == "store_structured":
                semantic, first = structured_element(resource.name, resource.sample_count, fields, offset,
                                                     *address_terms(read(state, instruction.operands[2], 0)))
            else:
                semantic, first = buffer_element(resource.name, fields, offset)
        scope = "typed" if opcode == "store_uav_typed" else "buffer"
        for axis in target["mask"] or "xyzw":
            component = "xyzw".index(axis)
            word = first + component
            value = read(state, instruction.operands[-1], component)
            stores.append(FlowStore(semantic, word // 4, word % 4, value if isinstance(value, int) else None,
                                    "" if isinstance(value, int) else value[1], (position, "store", first), scope))
            buffer_scalars.setdefault((scope, semantic, word // 4), []).append(scalar_of(value))
        if scope == "typed":
            typed[semantic] = TEXEL_TYPES.get(resource.return_type, "float32")

    state: dict | None = {}
    frames: list[_Frame] = []
    for position, instruction in enumerate(instructions):
        current[0] = position
        if position in phases:
            state, frames = {}, []
            scope[0] = "output" if phases[position].kind == "control_point" else "patch"
        opcode = _base_opcode(instruction.opcode)
        operands = instruction.operands
        if instruction.operands and re.match(r"o\[", operand_precision(instruction.operands[0])[0]):
            raise ShaderGraphError("The program writes outputs through an index, so its channels cannot be read.")
        alive = state is not _DEAD
        if opcode in {"if_nz", "if_z"}:
            frames.append(_Frame("if", dict(state) if alive else _DEAD, [read(state, operands[0], 0)] if alive else []))
        elif opcode == "else":
            frame = frames[-1]
            frame.then, frame.alternate = state, True
            state = dict(frame.entry) if frame.entry is not _DEAD else _DEAD
        elif opcode == "endif":
            frame = frames.pop()
            state = merge([frame.then if frame.alternate else frame.entry, state], frame.condition, position)
        elif opcode == "loop":
            frame = _Frame("loop", dict(state) if alive else _DEAD)
            if alive:
                for key in sorted(body_writes(position), key=repr):
                    frame.headers[key] = add("join", [state.get(key, ("constant", "undefined"))], ("join",),
                                             scalar_of(state.get(key)), ("loop", position, key))
                    state[key] = frame.headers[key]
            frames.append(frame)
        elif opcode == "endloop":
            frame = frames.pop()
            for key, header in frame.headers.items():
                for edge in [*frame.back_edges, state]:
                    item = edge.get(key) if edge is not _DEAD else None
                    if isinstance(item, int) and item != header and item not in values[header][1]:
                        values[header][1].append(item)
                values[header][2].extend(item for item in frame.controls if isinstance(item, int))
            state = merge(frame.exits, frame.controls, position)
        elif opcode == "switch":
            frames.append(_Frame("switch", dict(state) if alive else _DEAD,
                                 [read(state, operands[0], 0)] if alive else []))
            state = _DEAD
        elif opcode in {"case", "default"}:
            frame = frames[-1]
            frame.alternate |= opcode == "default"
            state = merge([frame.entry, state], frame.condition, position)
        elif opcode == "endswitch":
            frame = frames.pop()
            state = merge([*frame.exits, state, *(() if frame.alternate else (frame.entry,))], frame.condition, position)
        elif not alive:
            continue
        elif opcode.startswith(("break", "continue")):
            kinds = {"loop", "switch"} if opcode.startswith("break") else {"loop"}
            frame = next(item for item in reversed(frames) if item.kind in kinds)
            edges = frame.back_edges if opcode.startswith("continue") else frame.exits
            if opcode.endswith(("_nz", "_z")):
                frame.controls.append(read(state, operands[0], 0))
                edges.append(dict(state))
            else:
                edges.append(state)
                state = _DEAD
        elif opcode in {"ret", "retc_nz", "retc_z"}:
            exits.append((position, opcode))
            if ir.stage != "gs":
                output_stores(state, position, opcode)
            if opcode == "ret":
                state = _DEAD
        elif opcode in {"discard_nz", "discard_z"}:
            controls = [*frame.condition for frame in frames]
            condition = read(state, operands[0], 0)
            discards.append(FlowStore("discard", 0, 0, add("join", [condition], ("discard", opcode), "bool",
                                                           ("discard", position), controls), "", (position, opcode),
                                      "discard"))
        elif opcode in _BUFFER_STORES:
            buffer_stores(position, instruction, state)
        elif opcode.startswith("emit"):
            stream = int(operands[0][1:]) if operands else 0
            output_stores(state, position, opcode, stream)
            state = {key: value for key, value in state.items() if key[0] != "o"}
        else:
            count = _writes(instruction)
            if not count:
                continue
            slots = destinations(instruction, count)
            sources = operands[count:]
            updates = {}
            if opcode in _PURE:
                for slot_index, slot in enumerate(slots):
                    for base, lane in slot:
                        lanes = range(int(opcode[-1])) if opcode in _DOT else (lane,)
                        picked = [read(state, text, item) for text in sources for item in lanes]
                        modifiers = tuple(re.sub(r"[^-|]", "", operand_precision(text)[0][:2]) for text in sources)
                        scalar = ("float" if opcode in _FLOAT else "bool" if opcode in _BOOL else
                                  "int" if opcode in _INT else "uint" if opcode in _UINT else
                                  scalar_of(picked[-1]) or scalar_of(picked[0]))
                        if instruction.opcode == "mov" and modifiers == ("",) and all(
                                operand_precision(text)[1:] == (None, None) for text in operands[:2]):
                            updates[key_of(base, lane)] = picked[0]
                            continue
                        signature = (instruction.opcode, slot_index, modifiers,
                                     tuple(item[1] for item in picked if not isinstance(item, int)))
                        kind = "pure" if any(isinstance(item, int) for item in picked) else "source"
                        updates[key_of(base, lane)] = add(kind, picked, signature, scalar, (position, base, lane))
            else:
                resource = next((text for text in sources if _RESOURCE.match(_SWIZZLED.match(
                    operand_precision(text)[0])["base"])), None)
                inputs = [text for text in sources if text is not resource and not _SAMPLER.match(
                    _SWIZZLED.match(operand_precision(text)[0])["base"])]
                every = [read(state, text, item) for text in inputs for item in range(4)
                         if not operand_precision(text)[0].startswith("l(") or item == 0]
                for slot in slots:
                    for base, lane in slot:
                        updates[key_of(base, lane)] = add("source", every, (opcode,), "", (position, base, lane))
                texture = (textures.get(_SWIZZLED.match(operand_precision(resource)[0])["base"].casefold())
                           if opcode in _TEXTURE_READS and resource is not None and slots else None)
                if texture is not None:
                    swizzle = _lanes(_SWIZZLED.match(operand_precision(resource)[0])["mask"], "t0")
                    texture_reads.extend(
                        FlowStore(texture.name, 0, "xyzw".index(swizzle[lane]), updates[key_of(base, lane)], "",
                                  (position, "read"), "read") for base, lane in slots[0])
                    read_types[texture.name] = "float32" if opcode.endswith(("_c", "_c_lz")) else TEXEL_TYPES.get(
                        texture.return_type, "float32")
            for key, value in updates.items():
                if key[0] == "o" and key[1].startswith("x"):
                    memory.setdefault(key[1].split("[", 1)[0], []).append(value)
                state[key] = value
    if state is not _DEAD and (not instructions or _base_opcode(instructions[-1].opcode) != "ret"):
        exits.append((len(instructions), "end"))
        output_stores(state, len(instructions), "end")
    for value, root in deferred:
        values[value][1].extend(item for item in memory.get(root, ())
                                if isinstance(item, int) and item not in values[value][1])
    component_types = declared_component_types(contract)
    stage = {"ps": "pixel", "vs": "vertex", "gs": "geometry", "ds": "domain", "cs": "compute", "hs": "hull"}[ir.stage]
    flow_values = tuple(FlowValue(kind, tuple(dict.fromkeys(operands)), tuple(dict.fromkeys(controls)),
                                  signature, scalar, origin)
                        for kind, operands, controls, signature, scalar, origin in values)
    component_types.update({("read", name, 0): kind for name, kind in read_types.items()})
    component_types.update({key: typed[key[1]] if key[0] == "typed" else buffer_component_type(scalars)
                            for key, scalars in buffer_scalars.items()})
    return NativeFlow(stage, flow_values, (*stores, *texture_reads), tuple(discards), component_types, reads,
                      tuple(exits))


__all__ = ["dxbc_flow"]
