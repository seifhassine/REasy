"""See docs/internals/surface-splicing.md."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from functools import cached_property
import re

lazy from file_handlers.sdf.shader_graph import ShaderGraphError

lazy from .native_dxbc_precision import operand_precision
lazy from .profiles.dxbc_recovery import DxbcHookPlan, DxbcHookSite, DxbcInstruction, DxbcSemanticIr, _output_register
lazy from .surface_flow import NativeFlow
lazy from .surface_flow_dxbc import _DOT, _RESOURCE, _SWIZZLED, _base_opcode, _literal_lane, _writes


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


# A parameter read from the copy per drawn instance at t<binding> names the element it reads by this placeholder.
_ELEMENT = re.compile(r"REasyStructuredElementIndex_(\d+)")


def splice_dxbc_surface(ir: DxbcSemanticIr, flow: NativeFlow, contract, edits) -> tuple[DxbcSemanticIr, DxbcHookPlan]:
    return _DxbcSplicer(ir, flow, contract).splice(edits)


class _DxbcSplicer:
    def __init__(self, ir: DxbcSemanticIr, flow: NativeFlow, contract):
        self.ir, self.flow, self.contract = ir, flow, contract
        # Hull phases each declare their own registers; fresh ones follow the largest.
        self.temps = max((ir.temp_count, *(phase.temp_count for phase in ir.hull_phases)))
        self.starts = sorted(phase.start for phase in ir.hull_phases)
        self.after: dict[int, list[DxbcInstruction]] = defaultdict(list)
        self.sites: dict[tuple[int, str], dict[str, list]] = {}
        self.captured: dict[int, str] = {}
        self.discarded: dict[int, list[DxbcInstruction]] = {}
        self.prologue: list[DxbcInstruction] = []
        self.flag = ""
        self.copies: dict[tuple, str] = {}
        self.rewritten: dict[int, DxbcInstruction] = {}
        self.plans: dict[str, tuple] = {}

    def splice(self, edits) -> tuple[DxbcSemanticIr, DxbcHookPlan]:
        for index, edit in enumerate(edits):
            scope, result = edit.split.channel.scope, f"REasySurfaceValue_{index}"
            if scope == "discard":
                self.splice_discard(edit, result)
            elif scope == "position":
                self.splice_position(edit, result)
            elif scope == "added":
                self.splice_added(edit, result)
            elif scope == "read":
                self.splice_read(edit, result)
            else:
                self.splice_stores(edit, result)
        return self.assembled()

    def value(self, edit, result: str, site, declared: str = "") -> list[str]:
        return [*(self.located(statement, site) for statement in edit.statements),
                f"{declared or edit.value_type} {result} = {self.located(edit.expression, site)};"]

    def splice_discard(self, edit, result: str):
        if any(tap.split.channel.scope == "discard" for tap in edit.taps) and edit.split.stores:
            self.flag = self.fresh()
            self.prologue.append(DxbcInstruction("mov", (f"{self.flag}.x", "l(0x00000000)")))
        for store in edit.split.stores:
            position, opcode = store.site
            condition = self.ir.instructions[position].operands[0]
            clean, stored, used = operand_precision(condition)
            if stored or used or clean.startswith(("-", "|")):
                raise ShaderGraphError("A discard tests a modified value; it cannot be replaced.")
            self.discarded[position] = [] if not self.flag else [
                DxbcInstruction("ine" if opcode == "discard_nz" else "ieq", (f"{self.flag}.y", clean, "l(0x00000000)")),
                DxbcInstruction("or", (f"{self.flag}.x", f"{self.flag}.x", f"{self.flag}.y"))]
        for site in self.flow.exits:
            entry = self.entry_at(site, edit.taps)
            entry["statements"].extend(self.value(edit, result, site, "bool"))
            test = "" if site[1] in {"ret", "end"} else _typed(self.ir.instructions[site[0]].operands[0], "bool")
            if site[1] == "retc_z":
                test = f"!{test}"
            entry["statements"].append(f"if ({f'{test} && ' if test else ''}{result}) discard;")

    def splice_position(self, edit, result: str):
        site, _registers, write = self.position_plan(edit.split)
        entry = self.entry_at(site, edit.taps)
        entry["statements"].extend(self.value(edit, result, site))
        statements, instructions = write([_bits(f"{result}.{'xyzw'[lane]}", "float")
                                          for lane in range(len(edit.split.stores))])
        entry["statements"].extend(statements)
        entry["instructions"].extend(instructions)

    def splice_added(self, edit, result: str):
        # A value added for the pixel shader has an output register of its own, so it is stored at the start.
        entry = self.entry_at((0, "added"))
        channel = edit.split.channel
        field = next((item for item in self.contract.outputs if item.semantic_name.casefold() == channel.semantic
                      and item.semantic_index == channel.semantic_index), None)
        if field is None:
            raise ShaderGraphError("A value added for the pixel shader is not a declared output.")
        entry["statements"].extend((*self.value(edit, result, (0, "added")),
                                    f"{_output_register(field)} = {_bits(result, _scalar(edit.value_type))};"))

    def splice_read(self, edit, result: str):
        scalar = _scalar(edit.value_type)
        for position in sorted({store.site[0] for store in edit.split.stores}):
            site = (position + 1, "read")
            entry = self.entry_at(site, edit.taps)
            entry["statements"].extend(self.value(edit, result, site))
            entry["statements"].extend(f"{lane} = {_bits(f'{result}.{"xyzw"[component]}', scalar)};"
                                       for component, lanes in self.read_lanes(position).items() for lane in lanes)

    def splice_stores(self, edit, result: str):
        stored_sites = sorted({store.site for store in edit.split.stores})
        if edit.form == "value" and len(stored_sites) != 1:
            raise ShaderGraphError("A packed channel must be stored at one site to change it before packing.")
        for site in stored_sites:
            entry = self.entry_at(site, edit.taps)
            entry["statements"].extend(self.value(edit, result, site))
            scalar = _scalar(edit.value_type)
            components = [result] if edit.value_type == scalar else [f"{result}.{'xyzw'[item]}"
                                                                     for item in range(int(edit.value_type[-1]))]
            if edit.form == "stored":
                for component, store in zip(components, sorted(
                        (item for item in edit.split.stores if item.site == site), key=lambda item: item.lane)):
                    entry["statements"].append(f"{self.stored_lane(store, True)} = {_bits(component, scalar)};")
                continue
            preload = {}
            for member, component in zip(edit.split.frontier, components):
                preload[member] = self.fresh()
                entry["statements"].append(f"{preload[member]}.x = {_bits(component, scalar)};")
            entry["instructions"].extend(_rerun_packing(self.ir, self.flow, edit.split, preload, self.fresh,
                                                        lambda store: self.stored_lane(store, False)))

    def assembled(self) -> tuple[DxbcSemanticIr, DxbcHookPlan]:
        instructions, hooks = list(self.prologue), []
        moved: dict[int, int] = {}
        for position, instruction in enumerate((*self.ir.instructions, None)):
            moved[position] = len(instructions)
            for key in sorted((site for site in self.sites if site[0] == position), key=repr):
                # Statements attach to a fresh anchor copy, a site every hook accepts; each site has its own scope.
                anchor = self.fresh()
                instructions.extend(self.sites[key].get("before", ()))
                hooks.append(DxbcHookSite(len(instructions), "before", ("{", *self.sites[key]["statements"], "}")))
                instructions.append(DxbcInstruction("mov", (f"{anchor}.x", "l(0x00000000)")))
                instructions.extend(self.sites[key]["instructions"])
            if position in self.discarded:
                instructions.extend(self.discarded[position])
            elif instruction is not None:
                instructions.append(self.rewritten.get(position, instruction))
                instructions.extend(self.after.get(position, ()))
        if any(site[0] > len(self.ir.instructions) for site in self.sites):
            raise ShaderGraphError("A channel is stored at a site outside the recovered program.")
        if self.temps > 4096:
            raise ShaderGraphError("Surface edits need more registers than a DXBC program allows.")
        phases = tuple(replace(phase, start=moved[phase.start], temp_count=self.temps) for phase in self.ir.hull_phases)
        return (replace(self.ir, temp_count=self.temps, instructions=tuple(instructions), hull_phases=phases),
                DxbcHookPlan(tuple(hooks)))

    def phase_of(self, position) -> int:
        return sum(start <= position for start in self.starts)

    def fresh(self) -> str:
        self.temps += 1
        return f"r{self.temps - 1}"

    def capture(self, value: int, site) -> str:
        origin = self.flow.values[value].origin
        if self.phase_of(origin[1] if origin[0] in {"join", "loop"} else origin[0]) != self.phase_of(site[0]):
            raise ShaderGraphError("An edit reads a value another hull phase computes.")
        if value not in self.captured:
            if origin[0] in {"join", "loop"}:
                position, (_kind, register, lane) = origin[1], origin[2]
                source = f"r{register}.{'xyzw'[lane]}"
            else:
                position, base, lane = origin
                source = f"{base}.{'xyzw'[lane]}"
            self.captured[value] = self.fresh()
            self.after[position].append(DxbcInstruction("mov", (f"{self.captured[value]}.x", source)))
        return self.captured[value]

    @cached_property
    def depths(self) -> list[int]:
        depths, depth = [], 0
        for instruction in self.ir.instructions:
            opcode = _base_opcode(instruction.opcode)
            if opcode in {"endif", "endloop", "endswitch"}:
                depth -= 1
            depths.append(depth)
            if opcode.startswith("if") or opcode in {"loop", "switch"}:
                depth += 1
        return depths

    def element_index(self, binding: int, site) -> str:
        found = set()
        for position, instruction in enumerate(self.ir.instructions):
            if (not _base_opcode(instruction.opcode).startswith("ld_structured")
                    or _SWIZZLED.match(operand_precision(instruction.operands[3])[0])["base"] != f"t{binding}"):
                continue
            text = operand_precision(instruction.operands[1])[0]
            match = None if text.startswith("l(") else _SWIZZLED.match(text)
            lane = "xyzw".index((match["mask"] or "x")[0]) if match else 0
            value = (("constant", _literal_lane(text, 0)) if match is None
                     else self.flow.reads.get((position, match["base"], lane)) if match["base"].startswith("r")
                     else None)
            # Edit code reads inputs as floats (generate_recovered_dxbc_shader), so an index taken from one is its bits.
            if match is not None and match["base"].startswith("v"):
                found.add(f"asuint({match['base']}.{'xyzw'[lane]})")
            elif isinstance(value, int):
                origin = self.flow.values[value].origin
                found.add(f"asuint({origin[0]}.{'xyzw'[origin[1]]})"
                          if len(origin) == 2 and str(origin[0]).startswith("v") else value)
            elif value is not None and re.fullmatch(r"0x[0-9a-f]+|\d+", value[1], re.IGNORECASE):
                found.add(f"{int(value[1], 0)}u")
            else:
                raise ShaderGraphError("This pass reads its material's copy per drawn instance at an element it does "
                                       "not keep, so an edit cannot read the material there.")
        if len(found) != 1:
            raise ShaderGraphError("This pass reads several material records where the channel is stored." if found
                                   else "This pass does not read the drawn material's values where the channel is "
                                   "stored, so the edit cannot use them here. Use Drawing For to leave this pass out "
                                   "of the edit.")
        element = found.pop()
        if isinstance(element, str):
            return element
        origin = self.flow.values[element].origin
        defined = origin[1] if origin[0] == "join" else origin[0]
        if not isinstance(defined, int) or defined >= site[0] or self.depths[defined]:
            raise ShaderGraphError("This pass does not read the drawn material's values where the channel is stored, "
                                   "so the edit cannot use them here. Use Drawing For to leave this pass out of the "
                                   "edit.")
        return f"{self.capture(element, site)}.x"

    def located(self, expression: str, site) -> str:
        return _ELEMENT.sub(lambda match: f"({self.element_index(int(match[1]), site)})", expression)

    def output_register(self, store) -> str:
        fields = self.contract.patch_constants if store.scope == "patch" else self.contract.outputs
        field = next((item for item in fields if item.semantic_name.casefold() == store.semantic
                      and item.semantic_index == store.semantic_index), None)
        if field is None:
            raise ShaderGraphError(f"{store.semantic}{store.semantic_index} is not a declared output.")
        return _output_register(field)

    def output_variable(self, store) -> str:
        register = self.output_register(store)
        return f"patchRegisters[{register[1:]}]" if store.scope == "patch" else register

    def entry_at(self, site, taps=()):
        entry = self.sites.setdefault(site, {"statements": [], "instructions": []})
        for tap in taps:
            declaration = f"{tap.value_type} {tap.name} = {self.tap_expression(tap, site)};"
            if declaration not in entry["statements"]:
                entry["statements"].append(declaration)
        return entry

    def store_copy(self, site) -> str:
        if site not in self.copies:
            self.copies[site] = self.fresh()
            instruction = self.ir.instructions[site[0]]
            entry = self.entry_at(site)
            entry.setdefault("before", []).append(
                DxbcInstruction("mov", (f"{self.copies[site]}.xyzw", instruction.operands[-1])))
            self.rewritten[site[0]] = replace(instruction,
                                              operands=(*instruction.operands[:-1], f"{self.copies[site]}.xyzw"))
        return self.copies[site]

    def stored_lane(self, store, statement: bool) -> str:
        if store.scope in {"buffer", "typed"}:
            return f"{self.store_copy(store.site)}.{'xyzw'[store.semantic_index * 4 + store.lane - store.site[2]]}"
        return f"{self.output_variable(store) if statement else self.output_register(store)}.{'xyzw'[store.lane]}"

    def read_lanes(self, position) -> dict[int, list[str]]:
        instruction = self.ir.instructions[position]
        count = _writes(instruction)
        target = _SWIZZLED.match(operand_precision(instruction.operands[0])[0])
        resource = next(text for text in instruction.operands[count:]
                        if _RESOURCE.match(_SWIZZLED.match(operand_precision(text)[0])["base"]))
        swizzle = _SWIZZLED.match(operand_precision(resource)[0])["mask"] or "xyzw"
        lanes: dict[int, list[str]] = {}
        for lane in target["mask"] or "xyzw":
            lanes.setdefault("xyzw".index(swizzle["xyzw".index(lane)]), []).append(f"{target['base']}.{lane}")
        return lanes

    def position_plan(self, split):
        """(completing site, original component registers, function giving the code that makes every later read see new
        components)."""
        if split.channel.key not in self.plans:
            values = [store.value for store in split.stores]
            origins = [self.flow.values[value].origin for value in values]
            if all(isinstance(origin[0], str) and len(origin) == 2 for origin in origins):
                # Input registers copied as is: replaced before the program starts.
                registers = [f"{base}.{'xyzw'[lane]}" for base, lane in origins]
                self.plans[split.channel.key] = ((0, "position"), registers, lambda components: (
                    [f"{register} = {component};" for register, component in zip(registers, components)], []))
            else:
                self.plans[split.channel.key] = self.computed_position(values, origins)
        return self.plans[split.channel.key]

    def computed_position(self, values, origins):
        # A value joined from several branches lives in its register from the join on.
        origins = [(origin[1], f"r{origin[2][1]}", origin[2][2]) if origin[0] == "join" and origin[2][0] == "r"
                   else origin for origin in origins]
        if not all(isinstance(origin[0], int) and len(origin) == 3 for origin in origins):
            raise ShaderGraphError("This pass computes its position in a way it cannot be changed in one place.")
        defining = {origin[0] for origin in origins}
        first, last = min(defining), max(defining)
        changed, rerun = set(values), []
        for position in range(first + 1, last + 1):
            instruction = self.ir.instructions[position]
            if not _writes(instruction) or any(text.lstrip("-|").startswith("x") for text in instruction.operands):
                raise ShaderGraphError("This pass branches or uses memory while it computes its position, so the "
                                       "position cannot be changed in one place.")
            if not any(value in changed for key, value in self.flow.reads.items() if key[0] == position):
                continue
            if position in defining:
                raise ShaderGraphError("This pass computes part of its position from another part.")
            produced = sorted((index for index, item in enumerate(self.flow.values) if isinstance(item.origin, tuple)
                               and len(item.origin) == 3 and item.origin[0] == position),
                              key=lambda index: self.flow.values[index].origin[2])
            rerun.extend(produced)
            changed.update(produced)
        site = (last + 1, "position")
        originals = [f"{self.capture(value, site)}.x" for value in values]

        def write(components):
            homes, statements = {}, []
            for value, component in zip(values, components):
                homes[value] = self.fresh()
                statements.append(f"{homes[value]}.x = {component};")
            instructions = _rerun_values(self.ir, self.flow, rerun, homes, self.fresh,
                                         lambda value: self.capture(value, site))
            held = {}
            for position in range(first, last + 1):
                instruction = self.ir.instructions[position]
                for base, lane in _destination_lanes(instruction):
                    value = next((index for index in changed
                                  if self.flow.values[index].origin == (position, base, lane)), None)
                    if value is None and instruction.opcode == "mov" and position not in defining:
                        source = _SWIZZLED.match(operand_precision(instruction.operands[1])[0])
                        target = _SWIZZLED.match(operand_precision(instruction.operands[0])[0])
                        mask, written = source["mask"] or "xyzw", target["mask"] or "xyzw"
                        axis = mask[written.index("xyzw"[lane])] if len(mask) > 1 else mask
                        value = self.flow.reads.get((position, source["base"], "xyzw".index(axis)))
                    held[base, lane] = value
                held.update(((base, lane), value) for value, (defined, base, lane) in zip(values, origins)
                            if defined == position)
            instructions.extend(DxbcInstruction("mov", (f"{base}.{'xyzw'[lane]}", f"{homes[value]}.x"))
                                for (base, lane), value in held.items() if value in homes)
            return statements, instructions

        return site, originals, write

    def tap_expression(self, tap, site) -> str:
        scalar = _scalar(tap.value_type)
        if tap.split.channel.scope == "position":
            where, registers, _write = self.position_plan(tap.split)
            if where != site:
                raise ShaderGraphError("The projected position can only be read where it is changed.")
            parts = [_typed(register, scalar) for register in registers]
            return parts[0] if len(parts) == 1 else f"{tap.value_type}({', '.join(parts)})"
        if tap.split.channel.scope == "discard":
            return _typed(f"{self.flag}.x", "bool") if self.flag else "false"
        if tap.split.channel.scope == "read":
            if site[1] != "read" or not any(store.site == (site[0] - 1, "read") for store in tap.split.stores):
                raise ShaderGraphError("A texture as read can only be changed where the texture is read.")
            lanes = self.read_lanes(site[0] - 1)
            parts = [_typed(lanes[component][0], scalar) if component in lanes else _ZERO[scalar]
                     for component in range(4)]
            return f"{tap.value_type}({', '.join(parts)})"
        if tap.form == "value":
            parts = [_typed(f"{self.capture(item, site)}.x", scalar) for item in tap.split.frontier]
        else:
            stores = [store for store in tap.split.stores if store.site == site]
            if len(stores) != len(tap.split.channel.lanes):
                raise ShaderGraphError("An edit reads a channel that is not stored where the edited channel is.")
            parts = [_typed(self.stored_lane(store, True), scalar) for store in stores]
        return parts[0] if len(parts) == 1 else f"{tap.value_type}({', '.join(parts)})"


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


def _destination_lanes(instruction) -> set[tuple[str, int]]:
    lanes = set()
    for text in instruction.operands[:_writes(instruction)]:
        match = _SWIZZLED.match(operand_precision(text)[0])
        lanes.update((match["base"], "xyzw".index(axis)) for axis in match["mask"] or "xyzw")
    return lanes


__all__ = ["splice_dxbc_surface"]
