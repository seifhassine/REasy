"""See docs/internals/surface-splicing.md."""
from __future__ import annotations

from dataclasses import dataclass

lazy from file_handlers.sdf.shader_graph import ShaderGraphError
lazy from file_handlers.sdf.shader_graph_math import value_width

lazy from .profiles.re_engine_dxil_authoring import _SSA, _VALUE
lazy from .surface_flow import NativeFlow
lazy from .material_region_read import per_build
lazy from .surface_flow_dxil import material_records, _entry
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
    # line and each component's value.
    read: int | None = None
    texels: str = ""
    lanes: tuple[tuple[int, str], ...] = ()


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


def optimizer_passes(plan) -> tuple[str, ...]:
    return RECORDED_DISCARD_PASSES if any(edit.records for edit in plan) else EDITED_OPTIMIZER_PASSES


def _components(value_type: str) -> list[str]:
    return [""] if value_width(value_type) == 1 else [f".{'xyzw'[item]}" for item in range(value_width(value_type))]


def dxil_surface_plan(flow: NativeFlow, edits, assembly: str = "", contract=None) -> tuple[DxilSurfaceEdit, ...]:
    """Edits reading the material record need ``assembly`` and ``contract``."""
    def native(value_id) -> str:
        return str(flow.values[value_id].origin)

    def material_address(block, line) -> str:
        records: dict[str, list] = {}
        for number, defined_block, defined_line, address, dominated in per_build(
                "material_records", (assembly, contract.fingerprint), lambda: material_records(assembly, contract)):
            if block in dominated and (defined_block != block or defined_line < line):
                records.setdefault(number, []).append((defined_line, address))
        if len(records) != 1:
            raise ShaderGraphError("This pass does not read the drawn material's values where the channel is stored, "
                                   "so the edit cannot use them here. Use Drawing For to leave this pass out of the edit."
                                   if not records else
                                   "This pass reads several material records where the channel is stored.")
        return min(next(iter(records.values())))[1]

    def position_site(split) -> tuple[int, str, tuple[tuple[int, str], ...]]:
        lines = assembly.splitlines()
        _metadata, begin, end, statements = _entry(lines)
        blocks = {begin + 1 + index: block for block, index, _text in statements}
        names = [native(store.value) for store in split.stores]
        defined = {match["result"]: index for index in range(begin, end) if (match := _VALUE.match(lines[index]))}
        if any(name not in defined for name in names):
            raise ShaderGraphError("The projected position is not computed in this program.")
        last = max(defined[name] for name in names)
        # Phis open their block together, so the new value follows the last.
        while (match := _VALUE.match(lines[last + 1])) and lines[last + 1][match.end():].lstrip().startswith("phi "):
            last += 1
        if len({blocks.get(defined[name]) for name in names}) != 1:
            raise ShaderGraphError("This pass computes its position in several blocks, so it cannot be changed in "
                                   "one place.")
        for index in range(begin, last + 1):
            match = _VALUE.match(lines[index])
            if set(names) & set(_SSA.findall(lines[index][match.end():] if match else lines[index])):
                raise ShaderGraphError("This pass uses its position before it is complete, so it cannot be "
                                       "changed in one place.")
        return last, blocks[last], tuple(enumerate(names))

    def tap_arguments(tap, block, read=None) -> list[str]:
        if tap.split.channel.scope == "position":
            if read != position_site(tap.split)[0]:
                raise ShaderGraphError("The projected position can only be read where it is changed.")
            return [native(store.value) for store in tap.split.stores]
        if tap.split.channel.scope == "discard":
            return [_DISCARDED if tap.split.stores else "false"]
        if tap.split.channel.scope == "read":
            if read is None or not any(store.site == read for store in tap.split.stores):
                raise ShaderGraphError("A texture as read can only be changed where the texture is read.")
            return list(_TEXELS)
        if tap.form == "value":
            if any(store.site[1] != block for store in tap.split.stores):
                raise ShaderGraphError("An edit reads a channel the shader stores in another place.")
            return [native(item) for item in tap.split.frontier]
        stores = sorted((store for store in tap.split.stores if store.site[1] == block), key=lambda item: item.lane)
        if len(stores) != len(tap.split.channel.lanes):
            raise ShaderGraphError("An edit reads a channel that is not stored where the edited channel is.")
        return [native(store.value) if store.value is not None else store.constant for store in stores]

    result = []
    for index, edit in enumerate(edits):
        taps = tuple((tap.name, tap.value_type) for tap in edit.taps)

        def arguments(block, read=None, edit=edit):
            return tuple(argument for tap in edit.taps for argument in tap_arguments(tap, block, read))

        def exit_sites(edit=edit):
            return [DxilSurfaceSite((), arguments(block), exit=line,
                                    material_address=material_address(block, line) if edit.material else "")
                    for line, block in flow.exits]
        if edit.split.channel.scope == "discard":
            sites = exit_sites()
            records = any(tap.split.channel.scope == "discard" for tap in edit.taps) and bool(edit.split.stores)
            result.append(DxilSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps,
                                          tuple(sites), edit.material,
                                          tuple(store.site[0] for store in edit.split.stores), records))
            continue
        if edit.split.channel.scope == "added":
            channel = edit.split.channel
            sites = exit_sites()
            result.append(DxilSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps,
                                          tuple(sites), edit.material, output=(channel.semantic, channel.semantic_index)))
            continue
        if edit.split.channel.scope == "position":
            line, block, lanes = position_site(edit.split)
            site = DxilSurfaceSite((), arguments(block, line),
                                   material_address=material_address(block, line + 1) if edit.material else "",
                                   read=line, lanes=lanes)
            result.append(DxilSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps, (site,),
                                          edit.material))
            continue
        if edit.split.channel.scope == "read":
            sites = []
            for read in sorted({store.site for store in edit.split.stores}):
                line, block = read
                lanes = tuple(sorted((store.lane, native(store.value)) for store in edit.split.stores if store.site == read))
                texels = assembly.splitlines()[line].split("=", 1)[0].strip()
                sites.append(DxilSurfaceSite((), arguments(block, read),
                                             material_address=material_address(block, line + 1) if edit.material else "",
                                             read=line, texels=texels, lanes=lanes))
            result.append(DxilSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps,
                                          tuple(sites), edit.material))
            continue
        blocks: dict[str, list] = {}
        for store in edit.split.stores:
            blocks.setdefault(store.site[1], []).append(store)
        if edit.form == "value" and len(blocks) != 1:
            raise ShaderGraphError("A packed channel must be stored in one place to change it before packing.")
        sites = []
        for block, stores in blocks.items():
            stores.sort(key=lambda item: item.lane)
            tapped = arguments(block)
            address = material_address(block, min(store.site[0] for store in stores)) if edit.material else ""
            if edit.form == "stored":
                sites.append(DxilSurfaceSite(tuple((store.site[0], component, store.site[2])
                                                   for component, store in enumerate(stores)),
                                             tapped, material_address=address))
            else:
                sites.append(DxilSurfaceSite(
                    (), tapped, tuple(native(item) for item in edit.split.frontier),
                    tuple(native(item) for item in sorted(edit.split.tail)),
                    tuple((store.site[0], native(store.value), store.site[2]) for store in stores), address))
        result.append(DxilSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps, tuple(sites),
                                      edit.material))
    return tuple(result)


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
            for register, values in sorted(registers.items()):
                rendered = ", ".join(values.get(item, "0.0") for item in "xyzw")
                lines.append(f"    float4 v{register} = float4({rendered});")
            for tap, tap_type in edit.taps:
                parts = ", ".join(f"{tap}_{index}" for index in range(value_width(tap_type)))
                lines.append(f"    {tap_type} {tap} = {tap_type}({parts});")
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
