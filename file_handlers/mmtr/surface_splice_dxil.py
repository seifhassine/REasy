"""See docs/internals/surface-splicing.md."""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cached_property

lazy from file_handlers.sdf.shader_graph import ShaderGraphError
lazy from file_handlers.sdf.shader_graph_math import value_width

lazy from .native_loop_flow import dominator_sets, predecessor_map, reachable_blocks
lazy from .profiles.re_engine_dxil_authoring import _SSA, _VALUE
lazy from .surface_flow import NativeFlow, store_visits
lazy from .material_region_read import per_build
lazy from .surface_flow_dxil import material_records, _entry
lazy from .profiles.re_engine_bindless_material import record_address_form, record_address_source
lazy import fastdxiltext


EDITED_OPTIMIZER_PASSES = ("-dce",)
# A Discard edit reading the program's own discards records them in a local.
RECORDED_DISCARD_PASSES = ("-mem2reg", "-dce")
_DISCARDED = "%reasy.discarded"
_TEXELS = tuple(f"%reasy.texel.{component}" for component in range(4))


@dataclass(frozen=True, slots=True)
class DxilSurfaceSite:
    # (store's assembly line, new value component, store argument taking it)
    stores: tuple[tuple[int, int, int], ...]
    # Native operand of every tap component, in helper parameter order.
    arguments: tuple[str, ...]
    # Before packing: replaced values, packing results in definition order, and each store's packed value with its line
    # and argument.
    frontier: tuple[str, ...] = ()
    tail: tuple[str, ...] = ()
    roots: tuple[tuple[int, str, int], ...] = ()
    material_address: str = ""
    # A Discard edit's site: a return's assembly line.
    exit: int | None = None
    # A texture-read edit's site: the read's line, result and extracted lanes. A projected position's: the completing
    # line, each component's value and the lines computing the rest of it from part of it.
    read: int | None = None
    texels: str = ""
    lanes: tuple[tuple[int, str], ...] = ()
    kept: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class DxilSurfaceEdit:
    name: str
    value_type: str
    expression: str
    taps: tuple[tuple[str, str], ...]
    sites: tuple[DxilSurfaceSite, ...]
    material: bool = False
    # A Discard edit: own discard lines, and whether the edit reads them.
    discards: tuple[int, ...] = ()
    records: bool = False
    # An added value: its output (semantic, index).
    output: tuple = ()
    address: str = ""
    statements: tuple[str, ...] = ()


def optimizer_passes(plan) -> tuple[str, ...]:
    return RECORDED_DISCARD_PASSES if any(edit.records for edit in plan) else EDITED_OPTIMIZER_PASSES


def _components(value_type: str) -> list[str]:
    return [""] if value_width(value_type) == 1 else [f".{'xyzw'[item]}" for item in range(value_width(value_type))]


def dxil_surface_plan(flow: NativeFlow, edits, assembly: str = "", contract=None, instances: str = "",
                      prefix: int = 0, record_address: str = "", tail: range = range(0)) -> tuple[DxilSurfaceEdit, ...]:
    """Edits reading the material record need ``assembly`` and ``contract``."""
    return _DxilPlanner(flow, assembly, contract, instances, prefix, record_address, tail).plan(edits)


class _DxilPlanner:
    def __init__(self, flow: NativeFlow, assembly: str, contract, instances: str, prefix: int, record_address: str,
                 tail: range):
        self.flow, self.assembly, self.contract = flow, assembly, contract
        self.instances, self.prefix, self.record_address, self.tail = instances, prefix, record_address, tail
        self.computed, self._reaching = [], {}

    def plan(self, edits) -> tuple[DxilSurfaceEdit, ...]:
        result = [self.plan_edit(index, edit) for index, edit in enumerate(edits)]
        result = [replace(item, statements=edit.statements) for item, edit in zip(result, edits)]
        if self.computed:
            self.record_address = record_address_source(self.computed[0], self.contract)
        return tuple(replace(item, material=False, address=self.record_address)
                     if self.record_address and item.material else item for item in result)

    def plan_edit(self, index: int, edit) -> DxilSurfaceEdit:
        taps = tuple((tap.name, tap.value_type) for tap in edit.taps)
        if edit.split.channel.scope == "discard":
            sites = self.exit_sites(edit)
            records = any(tap.split.channel.scope == "discard" for tap in edit.taps) and bool(edit.split.stores)
            return DxilSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps, tuple(sites),
                                   edit.material, tuple(store.site[0] for store in edit.split.stores), records)
        if edit.split.channel.scope == "added":
            channel = edit.split.channel
            sites = self.exit_sites(edit)
            return DxilSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps, tuple(sites),
                                   edit.material, output=(channel.semantic, channel.semantic_index))
        if edit.split.channel.scope == "position":
            line, block, lanes, kept = self.position_site(edit.split)
            site = DxilSurfaceSite((), self.arguments(edit, block, line), material_address=self.material_address(
                block, line + 1) if edit.material else "", read=line, lanes=lanes, kept=kept)
            return DxilSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps, (site,),
                                   edit.material)
        sites = self.read_sites(edit) if edit.split.channel.scope == "read" else self.store_sites(edit)
        return DxilSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps, tuple(sites),
                               edit.material)

    def arguments(self, edit, block, read=None, visit=(0, 1)) -> tuple[str, ...]:
        return tuple(argument for tap in edit.taps for argument in self.tap_arguments(tap, block, read, visit))

    def exit_sites(self, edit) -> list[DxilSurfaceSite]:
        sites = []
        for line, block in self.flow.exits:
            try:
                places = [(line, block, self.material_address(block, line) if edit.material else "")]
            except ShaderGraphError:
                # Onimusha's exit for another master's record joins the pass's end: the paths past the values take the
                # edit before they join.
                ends, successors = self.block_flow
                joined = [item for item in ends if block in successors.get(item, ())]
                if not any(self.unread(item, ahead=False) for item in joined):
                    raise
                places = [(ends[item], item, self.material_address(item, ends[item])) for item in joined
                          if not self.unread(item, ahead=False)]
            sites.extend(DxilSurfaceSite((), self.arguments(edit, item), exit=end, material_address=address)
                         for end, item, address in places)
        return sites

    def read_sites(self, edit) -> list[DxilSurfaceSite]:
        sites = []
        for read in sorted({store.site for store in edit.split.stores}):
            line, block = read
            lanes = tuple(sorted((store.lane, self.native(store.value)) for store in edit.split.stores
                                 if store.site == read))
            texels = self.assembly.splitlines()[line].split("=", 1)[0].strip()
            sites.append(DxilSurfaceSite((), self.arguments(edit, block, read), material_address=self.material_address(
                block, line + 1) if edit.material else "", read=line, texels=texels, lanes=lanes))
        return sites

    def store_sites(self, edit) -> list[DxilSurfaceSite]:
        blocks: dict[str, list] = {}
        for store in edit.split.stores:
            blocks.setdefault(store.site[1], []).append(store)
        sites = []
        for block, stores in blocks.items():
            if edit.form == "stored":
                if edit.material and self.unread(block):
                    continue
                # A lane stored again in its block (a geometry shader's next vertex) starts another visit with its own
                # taps; each store's lanes are computed right before it.
                visits = store_visits(stores)
                for number, visit in enumerate(visits):
                    tapped = self.arguments(edit, block, visit=(number, len(visits)))
                    address = (self.material_address(block, min(store.site[0] for store in visit))
                               if edit.material else "")
                    placed = [(store.site[0], edit.split.channel.lanes.index(store.lane), store.site[2])
                              for store in visit]
                    sites.extend(DxilSurfaceSite(tuple(item for item in placed if item[0] == line), tapped,
                                                 material_address=address)
                                 for line in sorted({item[0] for item in placed}))
                continue
            stores.sort(key=lambda item: item.lane)
            tapped = self.arguments(edit, block)
            address = self.material_address(block, min(store.site[0] for store in stores)) if edit.material else ""
            tail = edit.split.tail
            if len(blocks) > 1:
                tail, pending = set(), [store.value for store in stores]
                while pending:
                    if (value := pending.pop()) in edit.split.tail and value not in tail:
                        tail.add(value)
                        pending.extend(self.flow.values[value].operands)
            sites.append(DxilSurfaceSite(
                (), tapped, tuple(self.native(item) for item in edit.split.frontier),
                tuple(self.native(item) for item in sorted(tail)),
                tuple((store.site[0], self.native(store.value), store.site[2]) for store in stores), address))
        return sites

    def native(self, value_id) -> str:
        return str(self.flow.values[value_id].origin)

    @cached_property
    def block_flow(self):
        _metadata, begin, _end, statements = _entry(self.assembly.splitlines())
        return ({block: begin + 1 + index for block, index, _text in statements},
                fastdxiltext.control_flow(statements)[1])

    def reaching(self, block):
        if block not in self._reaching:
            self._reaching[block] = reachable_blocks(block, self.block_flow[1])
        return self._reaching[block]

    @cached_property
    def dominators(self):
        labels, successors = tuple(self.block_flow[0]), self.block_flow[1]
        return dominator_sets(labels, predecessor_map(labels, successors), unreachable=True)

    @cached_property
    def value_blocks(self) -> dict[str, str]:
        lines = self.assembly.splitlines()
        _metadata, begin, end, statements = _entry(lines)
        blocks = {begin + 1 + index: block for block, index, _text in statements}
        return {match["result"]: blocks[index] for index in range(begin, end)
                if index in blocks and (match := _VALUE.match(lines[index]))}

    @cached_property
    def reads(self):
        return per_build("material_records", (self.assembly, self.contract.fingerprint, self.instances, self.prefix,
                                              self.tail),
                         lambda: material_records(self.assembly, self.contract, self.instances, self.prefix, self.tail))

    def unread(self, block, ahead=True) -> bool:
        """The record's tag (Onimusha's prefix, MH Wilds' tail) is tested before ``block`` and no path reaching it
        (nor, ``ahead``, leaving it) reads the values: the exit for another master's record, which keeps the game's
        value."""
        values = [read[1] for read in self.reads if not read[5]]
        return (not self.record_address and bool(values) and any(block in read[4] for read in self.reads if read[5])
                and not any(block in self.reaching(item) or ahead and item in self.reaching(block) for item in values))

    def material_address(self, block, line) -> str:
        if self.record_address:
            return ""
        records: dict[str, list] = {}
        header: dict[str, bool] = {}
        for number, defined_block, defined_line, address, dominated, prefix_only in self.reads:
            if block in dominated and (defined_block != block or defined_line < line):
                records.setdefault(number, []).append((defined_line, address))
                header[number] = header.get(number, True) and prefix_only
        values = {read[0] for read in self.reads if not read[5]}
        # A read of the tag alone (Onimusha's prefix, MH Wilds' tail, tested before the values) is the values' record
        # where the values are read at its address.
        if any(header.values()) and not all(header.values()):
            records = {number: found for number, found in records.items() if not header[number]}
        elif values and not values & records.keys():
            records = {}
        if not records and len({read[0] for read in self.reads}) == 1 and (form := record_address_form(
                self.assembly.splitlines(), self.contract, self.reads[0][3])) is not None:
            self.computed.append(form)
            return ""
        if len(records) != 1:
            raise ShaderGraphError("This pass does not read the drawn material's values where the channel is "
                                   "stored, so the edit cannot use them here. Use Drawing For to leave this pass out "
                                   "of the edit." if not records else
                                   "This pass reads several material records where the channel is stored.")
        return min(next(iter(records.values())))[1]

    def position_site(self, split) -> tuple[int, str, tuple[tuple[int, str], ...], tuple[int, ...]]:
        lines = self.assembly.splitlines()
        _metadata, begin, end, statements = _entry(lines)
        blocks = {begin + 1 + index: block for block, index, _text in statements}
        names = [self.native(store.value) for store in split.stores]
        defined = {match["result"]: index for index in range(begin, end) if (match := _VALUE.match(lines[index]))}
        if any(name not in defined for name in names):
            raise ShaderGraphError("The projected position is not computed in this program.")
        last = max(defined[name] for name in names)
        # Phis open their block together, so the new value follows the last.
        while (match := _VALUE.match(lines[last + 1])) and lines[last + 1][match.end():].lstrip().startswith("phi "):
            last += 1
        if any(blocks.get(defined[name]) not in self.dominators[blocks[last]] for name in names):
            raise ShaderGraphError("This pass computes its position in several blocks, so it cannot be changed in "
                                   "one place.")

        def operands(index):
            match = _VALUE.match(lines[index])
            return match, set(_SSA.findall(lines[index][match.end():] if match else lines[index]))

        parts, derived, feeding, pending, kept = set(names), set(), set(), list(names), []
        while pending:
            found = operands(defined[pending.pop()])[1] & defined.keys()
            pending.extend(found - feeding)
            feeding |= found
        for index in range(begin, end):
            match, read = operands(index)
            if read & (derived | (parts if index <= last else set())) and (
                    index > last or match is None or match["result"] not in parts | feeding):
                raise ShaderGraphError("This pass uses its position before it is complete, so it cannot be "
                                       "changed in one place.")
            if index <= last and match is not None and read & (parts | derived) and match["result"] not in parts:
                derived.add(match["result"])
            if index <= last and read & parts:
                kept.append(index)
        return last, blocks[last], tuple(enumerate(names)), tuple(kept)

    def tap_arguments(self, tap, block, read=None, visit=(0, 1)) -> list[str]:
        if tap.split.channel.scope == "position":
            if read != self.position_site(tap.split)[0]:
                raise ShaderGraphError("The projected position can only be read where it is changed.")
            return [self.native(store.value) for store in tap.split.stores]
        if tap.split.channel.scope == "discard":
            return [_DISCARDED if tap.split.stores else "false"]
        if tap.split.channel.scope == "read":
            if read is None or not any(store.site == read for store in tap.split.stores):
                raise ShaderGraphError("A texture as read can only be changed where the texture is read.")
            return list(_TEXELS)
        if tap.form == "value":
            names = [self.native(item) for item in tap.split.frontier]
            if any(store.site[1] != block for store in tap.split.stores) and not all(
                    self.value_blocks.get(name) in self.dominators[block] for name in names):
                raise ShaderGraphError("An edit reads a channel the shader stores in another place.")
            return names
        # The edit's visit of its block (index, count) reads the tap's visit of that index, or its only one.
        found = store_visits(store for store in tap.split.stores if store.site[1] == block)
        if len(found) == visit[1] > 1:
            found = [found[visit[0]]]
        lanes = {store.lane: store for store in found[0]} if len(found) == 1 else {}
        here = {self.native(store.value) for store in lanes.values() if store.value is not None}
        for lane in (set(tap.split.channel.lanes) - lanes.keys()) if len(found) < 2 else ():
            stores = [store for store in tap.split.stores if store.lane == lane]
            values = {self.native(store.value) if store.value is not None else store.constant for store in stores}
            defined = self.value_blocks.get(next(iter(values))) if len(values) == 1 else None
            if len(values) == 1 and (stores[0].value is None or values <= here
                                     or defined not in (None, block) and defined in self.dominators[block]):
                lanes[lane] = stores[0]
        if len(lanes) != len(tap.split.channel.lanes):
            raise ShaderGraphError("An edit reads a channel that is not stored where the edited channel is.")
        return [self.native(store.value) if store.value is not None else store.constant
                for store in sorted(lanes.values(), key=lambda item: item.lane)]


def surface_helper_source(plan, parameters: list[tuple[str, str]], registers: dict) -> tuple[list[str], list]:
    lines, calls = [], []
    placeholders = {"float": "asfloat(REasyCarrierBits)", "int": "int(REasyCarrierBits)",
                    "uint": "REasyCarrierBits", "bool": "(REasyCarrierBits != 0u)"}
    for edit in plan:
        tap_parameters = [(tap_type.rstrip("234"), f"{tap}_{index}")
                          for tap, tap_type in edit.taps for index in range(value_width(tap_type))]
        address = [("uint", "REasyMaterialAddress")] if edit.material else []
        signature = ", ".join(f"{kind} {name}" for kind, name in (*parameters, *address, *tap_parameters))
        scalar = edit.value_type.rstrip("234")
        for component, suffix in enumerate(_components(edit.value_type)):
            name = f"{edit.name}_{component}"
            lines.extend((f"[noinline] {scalar} {name}({signature})", "{"))
            if edit.address:
                lines.append(f"    uint REasyMaterialAddress = {edit.address};")
            for register, values in sorted(registers.items()):
                rendered = ", ".join(values.get(item, "0.0") for item in "xyzw")
                lines.append(f"    float4 v{register} = float4({rendered});")
            for tap, tap_type in edit.taps:
                parts = ", ".join(f"{tap}_{index}" for index in range(value_width(tap_type)))
                lines.append(f"    {tap_type} {tap} = {tap_type}({parts});")
            lines.extend(f"    {statement}" for statement in edit.statements)
            lines.extend((f"    {edit.value_type} REasySurfaceValue = {edit.expression};",
                          f"    return REasySurfaceValue{suffix};", "}", ""))
            calls.append((name, scalar, ["0u"] * len(address) + [placeholders[kind] for kind, _name in tap_parameters]))
    return lines, calls


def apply_surface_plan(lines: list[str], start: int, carrier_lines: list[str], carrier_entry: list[str],
                       plan, outputs: dict) -> tuple[list[str], dict[str, list[str]]]:
    """``start`` is ``lines[0]``'s assembly line."""
    return fastdxiltext.apply_surface_plan(lines, start, carrier_lines, carrier_entry, plan, outputs, value_width)


def place_surface_code(lines: list[str], markers: dict[str, list[str]]) -> list[str]:
    return fastdxiltext.place_surface_code(lines, markers)


__all__ = [
    "DxilSurfaceEdit",
    "DxilSurfaceSite",
    "EDITED_OPTIMIZER_PASSES",
    "apply_surface_plan",
    "dxil_surface_plan",
    "optimizer_passes",
    "place_surface_code",
    "surface_helper_source",
]
