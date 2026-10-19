"""See docs/internals/surface-splicing.md."""
from __future__ import annotations

from dataclasses import dataclass
import re

lazy from file_handlers.sdf.shader_graph import ShaderGraphError
lazy from file_handlers.sdf.shader_graph_math import value_width

lazy from .profiles.re_engine_dxil_authoring import _CALL, _SSA, _VALUE, _function_spans, _symbol
lazy from .surface_flow import NativeFlow
lazy from .surface_flow_dxil import call_access, entry_dominance, value_numbers, _entry


EDITED_OPTIMIZER_PASSES = ("-dce",)
# A Discard edit reading the program's own discards records them in a local.
RECORDED_DISCARD_PASSES = ("-mem2reg", "-dce")
_DISCARDED = "%reasy.discarded"
_TEXELS = tuple(f"%reasy.texel.{component}" for component in range(4))
_MEMORY_EFFECTS = {"alloca", "store", "atomicrmw", "cmpxchg", "fence"}
_STORED_VALUE = re.compile(r"(,\s*(?:half|float|double|i1|i8|i16|i32|i64)\s+)([^,()]+?)(\)\s*(?:,\s*!.*)?)$")


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
        from .profiles.re_engine_bindless_material import material_record_accesses
        defined, dominators = entry_dominance(assembly)
        numbers = value_numbers(assembly)
        records: dict[str, list] = {}
        for access in material_record_accesses(assembly.splitlines(), contract):
            where = defined.get(access.address)
            if where is not None and where[0] in dominators[block] and (where[0] != block or where[1] < line):
                records.setdefault(numbers.get(access.address, access.address), []).append((where[1], access.address))
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


def _closure(carrier_entry: list[str], roots: list[str]) -> list[str]:
    definitions, order = {}, []
    for line in carrier_entry:
        match = _VALUE.match(line)
        if match is not None:
            definitions[match["result"]] = line
            order.append(match["result"])
    needed, pending = set(), list(roots)
    while pending:
        name = pending.pop()
        if name in needed or name not in definitions:
            continue
        needed.add(name)
        body = definitions[name][_VALUE.match(definitions[name]).end():]
        pending.extend(_SSA.findall(body))
    return [definitions[name] for name in order if name in needed]


def _call_parts(line: str):
    match = re.match(r"^\s*(%[-$._A-Za-z0-9\"\\]+)\s*=\s*call\s+(.+?)\s+(@(?:\"(?:[^\"\\]|\\.)*\"|[-$._A-Za-z0-9]+))"
                     r"\((.*)\)(.*)$", line)
    if match is None:
        raise ShaderGraphError("A DXIL surface helper call is malformed.")
    arguments, depth, current = [], 0, ""
    for character in match[4]:
        if character == "," and depth == 0:
            arguments.append(current.strip())
            current = ""
            continue
        depth += character in "([{<"
        depth -= character in ")]}>"
        current += character
    if current.strip():
        arguments.append(current.strip())
    return match[1], match[2], match[3], arguments, match[5]


def _helper_body(carrier_lines: list[str], callee: str):
    span = _function_spans(carrier_lines).get(_symbol(callee[1:]))
    if span is None:
        raise ShaderGraphError("DXIL carrier omitted a surface helper definition.")
    header, body = carrier_lines[span[0]], [line for line in carrier_lines[span[0] + 1:span[1] - 1] if line.strip()]
    parameters = re.findall(r"(%[-$._A-Za-z0-9]+)(?=\s*[,)])", header.split("(", 1)[1])
    if not body or not body[-1].strip().startswith("ret ") or any(
            re.match(r"^\s*(?:;\s*<label>|[-$._A-Za-z0-9]+:)", line) or line.strip().startswith(("br ", "switch "))
            for line in body):
        return None
    return parameters, body[:-1], body[-1].strip().split(None, 2)[2]


def _merge_repeated(code: list[str], access: dict[str, str], aliases: dict[str, str]) -> list[str]:
    seen: dict[str, str] = {}
    reads: set[str] = set()
    result = []
    for line in code:
        line = _rename(line, aliases)
        match = _VALUE.match(line)
        operation = line[match.end():].split(None, 1)[0] if match else line.split(None, 1)[0]
        callee = _CALL.search(line) if operation in {"call", "tail"} else None
        kind = (access.get(callee["name"], "effect") if callee else "effect" if operation in _MEMORY_EFFECTS
                else "readonly" if operation == "load" else "readnone")
        if kind == "effect" or match is None:
            for key in reads:
                seen.pop(key, None)
            reads.clear()
            result.append(line)
            continue
        key = line[match.end():]
        if key in seen:
            aliases[match["result"]] = seen[key]
            continue
        seen[key] = match["result"]
        if kind == "readonly":
            reads.add(key)
        result.append(line)
    return result


def apply_surface_plan(lines: list[str], start: int, carrier_lines: list[str], carrier_entry: list[str],
                       plan, outputs: dict) -> tuple[list[str], dict[str, list[str]]]:
    """``start`` is ``lines[0]``'s assembly line."""
    defining = {match["result"]: index for index, line in enumerate(lines) if (match := _VALUE.match(line))}
    carrier: dict[int, list[str]] = {}
    native: dict[int, list[str]] = {}
    prelude: dict[int, list[str]] = {}
    replaced: dict[int, list[str]] = {}
    renamed: dict[str, str] = {}
    pending = []
    # Store line -> store argument -> new value.
    stored: dict[int, dict[int, str]] = {}
    serial = 0

    def fresh() -> str:
        nonlocal serial
        serial += 1
        return f"%reasy.surface.{serial}"

    for edit in plan:
        width = value_width(edit.value_type)
        tap_count = sum(value_width(tap_type) for _tap, tap_type in edit.taps)
        for line in edit.discards:
            condition = _STORED_VALUE.search(lines[line - start])
            if condition is None:
                raise ShaderGraphError("A DXIL discard has an unexpected form.")
            recorded = fresh() if edit.records else ""
            replaced[line - start] = [] if not edit.records else [
                f"  {recorded} = load i32, i32* {_DISCARDED}, align 4",
                f"  {recorded}.bit = zext i1 {condition[2]} to i32",
                f"  {recorded}.any = or i32 {recorded}, {recorded}.bit",
                f"  store i32 {recorded}.any, i32* {_DISCARDED}, align 4"]
        for site in edit.sites:
            store_lines = [line for line, _item, _argument in (site.stores or site.roots)]
            anchor = (site.exit if site.exit is not None else site.read + 1 if site.read is not None
                      else min(store_lines)) - start
            site_arguments = list(site.arguments)
            if site.read is not None and site.texels:
                texels = re.search(r"call (%dx\.types\.ResRet\.\w+)", lines[site.read - start])
                if texels is None:
                    raise ShaderGraphError("A DXIL texture read has an unexpected form.")
                extracted = {placeholder: fresh() for placeholder in _TEXELS}
                prelude.setdefault(anchor, []).extend(f"  {name} = extractvalue {texels[1]} {site.texels}, {component}"
                                                      for component, name in enumerate(extracted.values()))
                site_arguments = [extracted.get(item, item) for item in site_arguments]
            if _DISCARDED in site_arguments:
                recorded = fresh()
                prelude.setdefault(anchor, []).extend((f"  {recorded}.word = load i32, i32* {_DISCARDED}, align 4",
                                                       f"  {recorded} = icmp ne i32 {recorded}.word, 0"))
                site_arguments = [recorded if item == _DISCARDED else item for item in site_arguments]
            code, results = [], []
            for component in range(width):
                helper = f"{edit.name}_{component}"
                call = next((line for line in carrier_entry if " call " in line
                             and re.search(rf"{re.escape(helper)}(?![0-9])", line)), None)
                if call is None:
                    raise ShaderGraphError(f"DXIL carrier omitted surface helper {helper}.")
                result, return_type, callee, arguments, tail = _call_parts(call)
                native_count = tap_count + edit.material
                if len(arguments) < native_count:
                    raise ShaderGraphError("A DXIL surface helper lost its channel parameters.")
                inputs = arguments[:len(arguments) - native_count]
                taps = [f"{argument.rsplit(' ', 1)[0]} {value}"
                        for argument, value in zip(arguments[len(arguments) - tap_count:], site_arguments)]
                address = [f"i32 {site.material_address}"] if edit.material else []
                inline = _helper_body(carrier_lines, callee)
                needed = inputs
                if inline is not None:
                    parameters, body, returned = inline
                    used = {*_SSA.findall(line) for line in body} | set(_SSA.findall(returned))
                    needed = [argument for parameter, argument in zip(parameters, inputs) if parameter in used]
                closure = _closure(carrier_entry, [*_SSA.findall(argument) for argument in needed])
                names = {match["result"]: fresh() for line in closure if (match := _VALUE.match(line))}
                code.extend(_rename(line, names) for line in closure)
                arguments = [*(_rename(argument, names) for argument in inputs), *address, *taps]
                if inline is not None:
                    names = {parameter: argument.rsplit(" ", 1)[1] for parameter, argument in zip(parameters, arguments)}
                    names.update({match["result"]: fresh() for line in body if (match := _VALUE.match(line))})
                    code.extend(_rename(line, names) for line in body)
                    results.append(_rename(returned, names))
                else:
                    value = fresh()
                    code.append(f"  {value} = call {return_type} {callee}({', '.join(arguments)}){tail}")
                    results.append(value)
            if edit.output:
                identity, _row = outputs[edit.output]
                kind, scalar = ("f32", "float") if edit.value_type.startswith("float") else ("i32", "i32")
                code.extend(f"  call void @dx.op.storeOutput.{kind}(i32 5, i32 {identity}, i32 0, i8 {component}, "
                            f"{scalar} {value})" for component, value in enumerate(results))
            elif site.exit is not None and results[0] != "false":
                code.append(f"  call void @dx.op.discard(i32 82, i1 {results[0]})")
            carrier.setdefault(anchor, []).extend(code)
            pending.append((anchor, site, results))
    access = call_access(carrier_lines)
    aliases: dict[str, str] = {}
    for anchor, code in carrier.items():
        carrier[anchor] = _merge_repeated(code, access, aliases)
    for anchor, site, results in pending:
        results = [aliases.get(value, value) for value in results]
        if site.exit is not None:
            continue
        if site.read is not None:
            renamed.update((original, results[lane]) for lane, original in site.lanes)
            continue
        if site.stores:
            for line, component, argument in site.stores:
                stored.setdefault(line - start, {})[argument] = results[component]
            continue
        mapping = dict(zip(site.frontier, results))
        for name in site.tail:
            original = lines[defining[name]]
            body = original[_VALUE.match(original).end():]
            rewritten = _rename(body, mapping)
            if rewritten == body:
                mapping[name] = name
                continue
            mapping[name] = fresh()
            native.setdefault(anchor, []).append(f"  {mapping[name]} ={rewritten}")
        for line, root, argument in site.roots:
            if mapping.get(root, root) != root:
                stored.setdefault(line - start, {})[argument] = mapping[root]
    result, markers = [], {}
    for index, line in enumerate(lines):
        if index == 1 and any(edit.records for edit in plan):
            result.extend((f"  {_DISCARDED} = alloca i32, align 4", f"  store i32 0, i32* {_DISCARDED}, align 4"))
        if index in replaced:
            result.extend(replaced[index])
            continue
        result.extend(prelude.get(index, ()))
        if carrier.get(index):
            markers[f"; reasy.surface.code {index}"] = carrier[index]
            result.append(f"; reasy.surface.code {index}")
        result.extend(native.get(index, ()))
        if index in stored:
            line = _with_arguments(line, stored[index])
        result.append(line)
    if renamed:
        result = [_rename_uses(line, renamed) for line in result]
    return result, markers


def _with_arguments(line: str, values: dict[int, str]) -> str:
    callee = _CALL.search(line)
    end = line.rfind(")")
    if callee is None or end < callee.end():
        raise ShaderGraphError("A DXIL store has an unexpected form.")
    arguments = [item.strip() for item in line[callee.end():end].split(",")]
    for argument, value in values.items():
        if argument >= len(arguments) or " " not in arguments[argument]:
            raise ShaderGraphError("A DXIL store has an unexpected form.")
        arguments[argument] = f"{arguments[argument].rsplit(' ', 1)[0]} {value}"
    return f"{line[:callee.end()]}{', '.join(arguments)}{line[end:]}"


def _rename_uses(line: str, names: dict[str, str]) -> str:
    definition = _VALUE.match(line)
    head, body = (line[:definition.end()], line[definition.end():]) if definition else ("", line)
    return head + _rename(body, names)


def _rename(text: str, names: dict[str, str]) -> str:
    return _SSA.sub(lambda token: names.get(token[0], token[0]), text)


def place_surface_code(lines: list[str], markers: dict[str, list[str]]) -> list[str]:
    result = [*markers.get(line, (line,)) for line in lines]
    if sum(line in markers for line in lines) != len(markers):
        raise ShaderGraphError("A DXIL rewrite lost the place of a surface edit.")
    return result


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
