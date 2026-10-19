"""See docs/internals/surface-splicing.md."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
import re

from file_handlers.sdf.shader_graph import ShaderGraphError

from .native_dxbc_precision import operand_precision
from .profiles.dxbc_recovery import DxbcHookPlan, DxbcHookSite, DxbcInstruction, DxbcSemanticIr, _output_register
from .surface_flow import NativeFlow
from .surface_flow_dxbc import _DOT, _RESOURCE, _SWIZZLED, _base_opcode, _literal_lane, _writes


def _bits(expression: str, scalar: str) -> str:
    if scalar == "bool":
        return f"(({expression}) ? 0xffffffffu : 0u)"
    return expression if scalar == "uint" else f"asuint({expression})"


def _typed(expression: str, scalar: str) -> str:
    return {"float": f"asfloat({expression})", "int": f"asint({expression})", "uint": expression,
            "bool": f"({expression} != 0u)"}[scalar]


_ZERO = {"float": "0.0", "int": "0", "uint": "0u", "bool": "false"}


def _scalar(value_type: str) -> str:
    return value_type.rstrip("234")


def splice_dxbc_surface(ir: DxbcSemanticIr, flow: NativeFlow, contract, edits) -> tuple[DxbcSemanticIr, DxbcHookPlan]:
    # Hull phases each declare their own registers; fresh ones follow the largest.
    temps = max((ir.temp_count, *(phase.temp_count for phase in ir.hull_phases)))
    starts = sorted(phase.start for phase in ir.hull_phases)

    def phase_of(position) -> int:
        return sum(start <= position for start in starts)

    def fresh() -> str:
        nonlocal temps
        temps += 1
        return f"r{temps - 1}"

    after: dict[int, list[DxbcInstruction]] = defaultdict(list)
    sites: dict[tuple[int, str], dict[str, list]] = {}
    captured: dict[int, str] = {}
    discarded: dict[int, list[DxbcInstruction]] = {}
    prologue: list[DxbcInstruction] = []
    flag = ""

    def capture(value: int, site) -> str:
        origin = flow.values[value].origin
        if phase_of(origin[1] if origin[0] in {"join", "loop"} else origin[0]) != phase_of(site[0]):
            raise ShaderGraphError("An edit reads a value another hull phase computes.")
        if value not in captured:
            if origin[0] in {"join", "loop"}:
                position, (_kind, register, lane) = origin[1], origin[2]
                source = f"r{register}.{'xyzw'[lane]}"
            else:
                position, base, lane = origin
                source = f"{base}.{'xyzw'[lane]}"
            captured[value] = fresh()
            after[position].append(DxbcInstruction("mov", (f"{captured[value]}.x", source)))
        return captured[value]

    def output_register(store) -> str:
        fields = contract.patch_constants if store.scope == "patch" else contract.outputs
        field = next((item for item in fields if item.semantic_name.casefold() == store.semantic
                      and item.semantic_index == store.semantic_index), None)
        if field is None:
            raise ShaderGraphError(f"{store.semantic}{store.semantic_index} is not a declared output.")
        return _output_register(field)

    def output_variable(store) -> str:
        register = output_register(store)
        return f"patchRegisters[{register[1:]}]" if store.scope == "patch" else register

    copies: dict[tuple, str] = {}
    rewritten: dict[int, DxbcInstruction] = {}

    def entry_at(site, taps=()):
        entry = sites.setdefault(site, {"statements": [], "instructions": []})
        for tap in taps:
            declaration = f"{tap.value_type} {tap.name} = {tap_expression(tap, site)};"
            if declaration not in entry["statements"]:
                entry["statements"].append(declaration)
        return entry

    def store_copy(site) -> str:
        if site not in copies:
            copies[site] = fresh()
            instruction = ir.instructions[site[0]]
            entry = entry_at(site)
            entry.setdefault("before", []).append(
                DxbcInstruction("mov", (f"{copies[site]}.xyzw", instruction.operands[-1])))
            rewritten[site[0]] = replace(instruction, operands=(*instruction.operands[:-1], f"{copies[site]}.xyzw"))
        return copies[site]

    def stored_lane(store, statement: bool) -> str:
        if store.scope in {"buffer", "typed"}:
            return f"{store_copy(store.site)}.{'xyzw'[store.semantic_index * 4 + store.lane - store.site[2]]}"
        return f"{output_variable(store) if statement else output_register(store)}.{'xyzw'[store.lane]}"

    def read_lanes(position) -> dict[int, list[str]]:
        instruction = ir.instructions[position]
        count = _writes(instruction)
        target = _SWIZZLED.match(operand_precision(instruction.operands[0])[0])
        resource = next(text for text in instruction.operands[count:]
                        if _RESOURCE.match(_SWIZZLED.match(operand_precision(text)[0])["base"]))
        swizzle = _SWIZZLED.match(operand_precision(resource)[0])["mask"] or "xyzw"
        lanes: dict[int, list[str]] = {}
        for lane in target["mask"] or "xyzw":
            lanes.setdefault("xyzw".index(swizzle["xyzw".index(lane)]), []).append(f"{target['base']}.{lane}")
        return lanes

    def destination_lanes(instruction) -> set[tuple[str, int]]:
        lanes = set()
        for text in instruction.operands[:_writes(instruction)]:
            match = _SWIZZLED.match(operand_precision(text)[0])
            lanes.update((match["base"], "xyzw".index(axis)) for axis in match["mask"] or "xyzw")
        return lanes

    plans: dict[str, tuple] = {}

    def position_plan(split):
        """(completing site, original component registers, function giving the code that makes every later read see new
        components)."""
        if split.channel.key in plans:
            return plans[split.channel.key]
        values = [store.value for store in split.stores]
        origins = [flow.values[value].origin for value in values]
        if all(isinstance(origin[0], str) and len(origin) == 2 for origin in origins):
            # Input registers copied as is: replaced before the program starts.
            registers = [f"{base}.{'xyzw'[lane]}" for base, lane in origins]
            plans[split.channel.key] = ((0, "position"), registers, lambda components: (
                [f"{register} = {component};" for register, component in zip(registers, components)], []))
            return plans[split.channel.key]
        # A value joined from several branches lives in its register from the join on.
        origins = [(origin[1], f"r{origin[2][1]}", origin[2][2]) if origin[0] == "join" and origin[2][0] == "r"
                   else origin for origin in origins]
        if not all(isinstance(origin[0], int) and len(origin) == 3 for origin in origins):
            raise ShaderGraphError("This pass computes its position in a way it cannot be changed in one place.")
        defining = {origin[0] for origin in origins}
        first, last = min(defining), max(defining)
        changed, rerun = set(values), []
        for position in range(first + 1, last + 1):
            instruction = ir.instructions[position]
            if not _writes(instruction) or any(text.lstrip("-|").startswith("x") for text in instruction.operands):
                raise ShaderGraphError("This pass branches or uses memory while it computes its position, so the "
                                       "position cannot be changed in one place.")
            if not any(value in changed for key, value in flow.reads.items() if key[0] == position):
                continue
            if position in defining:
                raise ShaderGraphError("This pass computes part of its position from another part.")
            produced = sorted((index for index, item in enumerate(flow.values) if isinstance(item.origin, tuple)
                               and len(item.origin) == 3 and item.origin[0] == position),
                              key=lambda index: flow.values[index].origin[2])
            rerun.extend(produced)
            changed.update(produced)
        site = (last + 1, "position")
        originals = [f"{capture(value, site)}.x" for value in values]

        def write(components):
            homes, statements = {}, []
            for value, component in zip(values, components):
                homes[value] = fresh()
                statements.append(f"{homes[value]}.x = {component};")
            instructions = _rerun_values(ir, flow, rerun, homes, fresh, lambda value: capture(value, site))
            held = {}
            for position in range(first, last + 1):
                instruction = ir.instructions[position]
                for base, lane in destination_lanes(instruction):
                    value = next((index for index in changed if flow.values[index].origin == (position, base, lane)),
                                 None)
                    if value is None and instruction.opcode == "mov" and position not in defining:
                        source = _SWIZZLED.match(operand_precision(instruction.operands[1])[0])
                        target = _SWIZZLED.match(operand_precision(instruction.operands[0])[0])
                        mask, written = source["mask"] or "xyzw", target["mask"] or "xyzw"
                        axis = mask[written.index("xyzw"[lane])] if len(mask) > 1 else mask
                        value = flow.reads.get((position, source["base"], "xyzw".index(axis)))
                    held[base, lane] = value
                held.update(((base, lane), value) for value, (defined, base, lane) in zip(values, origins)
                            if defined == position)
            instructions.extend(DxbcInstruction("mov", (f"{base}.{'xyzw'[lane]}", f"{homes[value]}.x"))
                                for (base, lane), value in held.items() if value in homes)
            return statements, instructions

        plans[split.channel.key] = (site, originals, write)
        return plans[split.channel.key]

    def tap_expression(tap, site) -> str:
        scalar = _scalar(tap.value_type)
        if tap.split.channel.scope == "position":
            where, registers, _write = position_plan(tap.split)
            if where != site:
                raise ShaderGraphError("The projected position can only be read where it is changed.")
            parts = [_typed(register, scalar) for register in registers]
            return parts[0] if len(parts) == 1 else f"{tap.value_type}({', '.join(parts)})"
        if tap.split.channel.scope == "discard":
            return _typed(f"{flag}.x", "bool") if flag else "false"
        if tap.split.channel.scope == "read":
            if site[1] != "read" or not any(store.site == (site[0] - 1, "read") for store in tap.split.stores):
                raise ShaderGraphError("A texture as read can only be changed where the texture is read.")
            lanes = read_lanes(site[0] - 1)
            parts = [_typed(lanes[component][0], scalar) if component in lanes else _ZERO[scalar]
                     for component in range(4)]
            return f"{tap.value_type}({', '.join(parts)})"
        if tap.form == "value":
            parts = [_typed(f"{capture(item, site)}.x", scalar) for item in tap.split.frontier]
        else:
            stores = [store for store in tap.split.stores if store.site == site]
            if len(stores) != len(tap.split.channel.lanes):
                raise ShaderGraphError("An edit reads a channel that is not stored where the edited channel is.")
            parts = [_typed(stored_lane(store, True), scalar) for store in stores]
        return parts[0] if len(parts) == 1 else f"{tap.value_type}({', '.join(parts)})"

    for index, edit in enumerate(edits):
        result = f"REasySurfaceValue_{index}"
        if edit.split.channel.scope == "discard":
            if any(tap.split.channel.scope == "discard" for tap in edit.taps) and edit.split.stores:
                flag = fresh()
                prologue.append(DxbcInstruction("mov", (f"{flag}.x", "l(0x00000000)")))
            for store in edit.split.stores:
                position, opcode = store.site
                condition = ir.instructions[position].operands[0]
                clean, stored, used = operand_precision(condition)
                if stored or used or clean.startswith(("-", "|")):
                    raise ShaderGraphError("A discard tests a modified value; it cannot be replaced.")
                discarded[position] = [] if not flag else [
                    DxbcInstruction("ine" if opcode == "discard_nz" else "ieq", (f"{flag}.y", clean, "l(0x00000000)")),
                    DxbcInstruction("or", (f"{flag}.x", f"{flag}.x", f"{flag}.y"))]
            for site in flow.exits:
                entry = entry_at(site, edit.taps)
                entry["statements"].append(f"bool {result} = {edit.expression};")
                test = "" if site[1] in {"ret", "end"} else _typed(ir.instructions[site[0]].operands[0], "bool")
                if site[1] == "retc_z":
                    test = f"!{test}"
                entry["statements"].append(f"if ({f'{test} && ' if test else ''}{result}) discard;")
            continue
        if edit.split.channel.scope == "position":
            site, _registers, write = position_plan(edit.split)
            entry = entry_at(site, edit.taps)
            entry["statements"].append(f"{edit.value_type} {result} = {edit.expression};")
            statements, instructions = write([_bits(f"{result}.{'xyzw'[lane]}", "float")
                                              for lane in range(len(edit.split.stores))])
            entry["statements"].extend(statements)
            entry["instructions"].extend(instructions)
            continue
        if edit.split.channel.scope == "added":
            # A value added for the pixel shader has an output register of its own, so it is stored at the start.
            entry = entry_at((0, "added"))
            channel = edit.split.channel
            field = next((item for item in contract.outputs if item.semantic_name.casefold() == channel.semantic
                          and item.semantic_index == channel.semantic_index), None)
            if field is None:
                raise ShaderGraphError("A value added for the pixel shader is not a declared output.")
            entry["statements"].extend((f"{edit.value_type} {result} = {edit.expression};",
                                        f"{_output_register(field)} = {_bits(result, _scalar(edit.value_type))};"))
            continue
        if edit.split.channel.scope == "read":
            scalar = _scalar(edit.value_type)
            for position in sorted({store.site[0] for store in edit.split.stores}):
                site = (position + 1, "read")
                entry = entry_at(site, edit.taps)
                entry["statements"].append(f"{edit.value_type} {result} = {edit.expression};")
                entry["statements"].extend(f"{lane} = {_bits(f'{result}.{"xyzw"[component]}', scalar)};"
                                           for component, lanes in read_lanes(position).items() for lane in lanes)
            continue
        stored_sites = sorted({store.site for store in edit.split.stores})
        if edit.form == "value" and len(stored_sites) != 1:
            raise ShaderGraphError("A packed channel must be stored at one site to change it before packing.")
        for site in stored_sites:
            entry = entry_at(site, edit.taps)
            entry["statements"].append(f"{edit.value_type} {result} = {edit.expression};")
            scalar = _scalar(edit.value_type)
            components = [result] if edit.value_type == scalar else [f"{result}.{'xyzw'[item]}"
                                                                     for item in range(int(edit.value_type[-1]))]
            if edit.form == "stored":
                for component, store in zip(components, sorted(
                        (item for item in edit.split.stores if item.site == site), key=lambda item: item.lane)):
                    entry["statements"].append(f"{stored_lane(store, True)} = {_bits(component, scalar)};")
                continue
            preload = {}
            for member, component in zip(edit.split.frontier, components):
                preload[member] = fresh()
                entry["statements"].append(f"{preload[member]}.x = {_bits(component, scalar)};")
            entry["instructions"].extend(_rerun_packing(ir, flow, edit.split, preload, fresh,
                                                        lambda store: stored_lane(store, False)))

    instructions, hooks = list(prologue), []
    moved: dict[int, int] = {}
    for position, instruction in enumerate((*ir.instructions, None)):
        moved[position] = len(instructions)
        for key in sorted((site for site in sites if site[0] == position), key=repr):
            # Statements attach to a fresh anchor copy, a site every hook accepts; each site has its own scope.
            anchor = fresh()
            instructions.extend(sites[key].get("before", ()))
            hooks.append(DxbcHookSite(len(instructions), "before", ("{", *sites[key]["statements"], "}")))
            instructions.append(DxbcInstruction("mov", (f"{anchor}.x", "l(0x00000000)")))
            instructions.extend(sites[key]["instructions"])
        if position in discarded:
            instructions.extend(discarded[position])
        elif instruction is not None:
            instructions.append(rewritten.get(position, instruction))
            instructions.extend(after.get(position, ()))
    unplaced = {site for site in sites if site[0] > len(ir.instructions)}
    if unplaced:
        raise ShaderGraphError("A channel is stored at a site outside the recovered program.")
    if temps > 4096:
        raise ShaderGraphError("Surface edits need more registers than a DXBC program allows.")
    phases = tuple(replace(phase, start=moved[phase.start], temp_count=temps) for phase in ir.hull_phases)
    return (replace(ir, temp_count=temps, instructions=tuple(instructions), hull_phases=phases),
            DxbcHookPlan(tuple(hooks)))


def _rerun_packing(ir, flow, split, preload, fresh, stored_lane) -> list[DxbcInstruction]:
    homes: dict[int, str] = dict(preload)
    result = _rerun_values(ir, flow, sorted(split.tail), homes, fresh, None)
    for store in split.stores:
        result.append(DxbcInstruction("mov", (stored_lane(store), f"{homes[store.value]}.x")))
    return result


def _rerun_values(ir, flow, values, homes: dict, fresh, outside) -> list[DxbcInstruction]:
    result: list[DxbcInstruction] = []
    for value in values:
        position, base, lane = flow.values[value].origin
        instruction = ir.instructions[position]
        count = _writes(instruction)
        opcode = _base_opcode(instruction.opcode)
        lanes = range(int(opcode[-1])) if opcode in _DOT else (lane,)
        sources = []
        for text in instruction.operands[count:]:
            clean, stored, used = operand_precision(text)
            if stored or used:
                raise ShaderGraphError("A packing instruction converts precision; it cannot be re-run lane by lane.")
            negate = clean.startswith("-")
            inner = clean[1:] if negate else clean
            absolute = inner.startswith("|") and inner.endswith("|")
            inner = inner[1:-1] if absolute else inner
            if inner.startswith("l("):
                values = [_literal_lane(inner, item) for item in lanes]
                operand = f"l({', '.join(values if len(values) == 1 else (values + ['0x00000000'] * 4)[:4])})"
            else:
                match = _SWIZZLED.match(inner)
                mask = match["mask"] or "xyzw"
                picked = []
                for item in lanes:
                    component = "xyzw".index(mask[item if len(mask) > 1 else 0])
                    read = flow.reads.get((position, match["base"], component))
                    if isinstance(read, int) and read in homes:
                        picked.append(f"{homes[read]}.x")
                    elif read is not None and not isinstance(read, int):
                        picked.append(f"l({read[1]})")
                    elif isinstance(read, int) and outside is not None:
                        picked.append(f"{outside(read)}.x")
                    elif read is None and outside is not None and not re.match(r"[rx]\d", match["base"]):
                        picked.append(f"{match['base']}.{'xyzw'[component]}")
                    else:
                        raise ShaderGraphError("A channel's packing reads a value from outside the channel.")
                if len(picked) == 1:
                    operand = picked[0]
                else:
                    gathered = fresh()
                    for item, source in enumerate(picked):
                        result.append(DxbcInstruction("mov", (f"{gathered}.{'xyzw'[item]}", source)))
                    operand = f"{gathered}.{('xyzw'[:len(picked)] + 'xxxx')[:4]}"
            if absolute:
                operand = f"|{operand}|"
            sources.append(f"-{operand}" if negate else operand)
        homes[value] = fresh()
        destinations = []
        for text in instruction.operands[:count]:
            target = _SWIZZLED.match(operand_precision(text)[0].casefold())["base"]
            destinations.append(f"{homes[value]}.x" if target == base.casefold() else "null")
        precise = "x" if "xyzw"[lane] in instruction.precise_mask else ""
        result.append(DxbcInstruction(instruction.opcode, (*destinations, *sources), precise))
    return result


__all__ = ["splice_dxbc_surface"]
