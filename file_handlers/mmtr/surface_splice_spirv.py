"""See docs/internals/surface-splicing.md."""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache

lazy from file_handlers.sdf.shader_graph import ShaderGraphError
lazy from file_handlers.sdf.shader_graph_math import value_width
lazy from file_handlers.sdf.spirv import SpirvError
lazy from file_handlers.sdf.spirv_ir import grammar

lazy from .spirv_control_flow import spirv_cfg
lazy from .surface_flow import NativeFlow, store_visits
lazy from .surface_flow_dxil import _dominators
lazy from .surface_flow_spirv import _MIXING, _PURE_CLASSES


_OP_VARIABLE, _OP_LOAD, _OP_STORE, _OP_FUNCTION_PARAMETER, _OP_LABEL = 59, 61, 62, 55, 248
_OP_CONSTRUCT, _OP_EXTRACT, _OP_INSERT, _OP_SHUFFLE, _OP_COPY = 80, 81, 82, 79, 83
_OP_ACCESS_CHAINS = {65, 66}
_OP_RETURN_VALUE, _OP_FUNCTION, _OP_FUNCTION_END, _OP_CALL = 254, 54, 56, 57
_OP_SELECTION_MERGE, _OP_BRANCH, _OP_BRANCH_CONDITIONAL = 247, 249, 250
_OP_KILL, _OP_DEMOTE, _OP_PHI, _OP_UNDEF, _OP_CONSTANT_FALSE, _OP_CONSTANT_NULL = 252, 5380, 245, 1, 42, 46
_OP_RETURN = 253
# Operand index of the stored value, by opcode.
_STORED = {_OP_STORE: 1, 99: 2}  # 99: OpImageWrite
_ENDS = 0
_DEBUG_LINES = {8, 317}
_RECORDED, _FALSE, _ZERO = "recorded", "false", "zero"
# A helper argument the shader computes after the site; a helper using it is refused.
_LATE = "late"
_LATE_READ = "An edit reads a value the shader computes after it stores the edited channel."
_FRAME = {_OP_FUNCTION, _OP_FUNCTION_PARAMETER, _OP_FUNCTION_END}
# Results that can't move elsewhere in the native program.
_PLACED = {_OP_CALL, _OP_PHI, _OP_LABEL, _OP_VARIABLE}


@dataclass(frozen=True, slots=True)
class SpirvSurfaceSite:
    # (channel store instruction index, stored object lane, new value component)
    stores: tuple[tuple[int, int, int], ...]
    # Native (id, lane) of every tap component, in helper parameter order.
    arguments: tuple[tuple[int, int], ...]
    # Before packing: the native (id, lane) each new component replaces.
    frontier: tuple[tuple[int, int], ...] = ()
    # A Discard edit's site: a return's instruction index.
    exit: int | None = None
    # A texture-read edit's site: the read's instruction index. A projected position's: the computing instruction and
    # the lane each component replaces.
    read: int | None = None
    lanes: tuple[int, ...] = ()
    material_address: int | None = None


@dataclass(frozen=True, slots=True)
class SpirvSurfaceEdit:
    name: str
    value_type: str
    expression: str
    taps: tuple[tuple[str, str], ...]
    sites: tuple[SpirvSurfaceSite, ...]
    material: bool = False
    # A Discard edit: each own discard (instruction index, block, merge block or None for a demote, 0 at program end),
    # and whether the edit reads them.
    discards: tuple[tuple[int, int, int | None], ...] = ()
    records: bool = False
    # An added value: its output (semantic, index).
    output: tuple = ()
    address: str = ""
    statements: tuple[str, ...] = ()


def _after_phis(rows, index: int) -> int:
    while rows[index].opcode == _OP_PHI:
        index += 1
    return index


def _site_position(rows, site: SpirvSurfaceSite) -> int:
    if site.exit is not None:
        return site.exit
    if site.read is not None:
        return _after_phis(rows, site.read + 1)
    return min(index for index, _lane, _component in site.stores)


def _material_addresses(ir, elements):
    rows = ir.instructions
    positions = {row.result: index for index, row in enumerate(rows) if row.result is not None}
    functions = _functions(ir)
    first = functions[0][0][0] if functions else len(rows)
    dominance, keys = {}, {}
    variables = {row.result: row.values[2] for row in rows if row.opcode == _OP_VARIABLE}
    blocks_decorated = {row.values[0] for row in rows if row.opcode == 71 and row.values[1] == 2}  # Block

    def key(identifier):
        """``identifier``'s computation, equal for equal computations of read-only inputs."""
        if identifier not in keys:
            row = rows[positions[identifier]] if identifier in positions else None
            if row is None or positions[identifier] < first or row.opcode in _PLACED | {_OP_FUNCTION_PARAMETER}:
                keys[identifier] = ("id", identifier)
            else:
                root = row.values[2] if row.opcode == _OP_LOAD else None
                while root in positions and rows[positions[root]].opcode in _OP_ACCESS_CHAINS | {_OP_COPY}:
                    root = rows[positions[root]].values[2]
                storage = variables.get(root)
                pointee = rows[positions[rows[positions[root]].type_id]].values[2] if storage is not None else None
                # Inputs, constants and constant buffers; a pass may write other memory.
                read_only = storage in {0, 1, 9} or storage == 2 and pointee in blocks_decorated
                if row.opcode == _OP_LOAD and not read_only:
                    keys[identifier] = ("id", identifier)
                else:
                    keys[identifier] = (row.opcode, row.type_id, tuple(
                        key(item.value) if item.is_id else item.value
                        for item in row.operands if item.kind not in {"IdResult", "IdResultType"}))
        return keys[identifier]

    def address(position) -> int:
        function = next(items for items in functions if items[0][0] <= position <= items[-1][0])
        start, end = function[0][0], function[-1][0]
        if start not in dominance:
            blocks, successors, _conditions, _merges = spirv_cfg(row for _position, row in function)
            labels, current = {}, None
            for index, row in function:
                current = row.result if row.opcode == _OP_LABEL else current
                labels[index] = current
            dominance[start] = labels, _dominators(blocks, successors)
        labels, dominators = dominance[start]

        def available(identifier) -> bool:
            defined = positions[identifier]
            if defined < first or rows[defined].opcode == _OP_FUNCTION_PARAMETER and start <= defined <= end:
                return True
            if not start <= defined <= end:
                return False
            block = labels[position]
            return labels[defined] in dominators[block] and (labels[defined] != block or defined < position)
        candidates = {key(element): element for _read, element in elements if available(element)}
        if len(candidates) != 1:
            raise ShaderGraphError(
                "This pass reads several material records where the channel is stored." if candidates else
                "This pass does not read the drawn material's values where the channel is stored, so the edit "
                "cannot use them here. Use Drawing For to leave this pass out of the edit.")
        return next(iter(candidates.values()))
    return address


def _construct_exit(ir, index: int) -> int:
    function = next(rows for rows in _functions(ir) if rows[0][0] <= index <= rows[-1][0])
    blocks, successors, _conditions, merges = spirv_cfg(row for _position, row in function)
    holder = next(row.result for position, row in reversed(function) if position <= index and row.opcode == _OP_LABEL)
    dominators = _dominators(blocks, successors)
    enclosing = [header for header in dominators[holder] if header in merges and merges[header] not in dominators[holder]]
    if not enclosing:
        return _ENDS
    return merges[max(enclosing, key=lambda header: len(dominators[header]))]


def _functions(ir):
    rows, current = [], None
    for position, row in enumerate(ir.instructions):
        if row.opcode == _OP_FUNCTION:
            current = []
        if current is not None:
            current.append((position, row))
        if row.opcode == _OP_FUNCTION_END and current is not None:
            rows.append(current)
            current = None
    return rows


def spirv_surface_plan(ir, flow: NativeFlow, edits, elements=()) -> tuple[SpirvSurfaceEdit, ...]:
    """``elements``: for edits reading the material, each native read of the copy per drawn instance as (instruction
    index, element index ID)."""
    def lane(store):
        if store.site[2] is None:
            raise ShaderGraphError("A channel is stored through a computed index and cannot be edited.")
        return store.site[2]

    def position_site(split) -> tuple[int, tuple[int, ...]]:
        origins = [flow.values[store.value].origin for store in split.stores]
        if len({origin[0] for origin in origins}) != 1:
            raise ShaderGraphError("This pass computes its position piece by piece, so it cannot be changed in "
                                   "one place.")
        identifier = origins[0][0]
        return next(index for index, row in enumerate(ir.instructions) if row.result == identifier), tuple(
            lane for _identifier, lane in origins)

    def tap_arguments(tap, block, read=None, visit=(0, 1)) -> list[tuple[int, int]]:
        if tap.split.channel.scope == "position":
            if read != position_site(tap.split)[0]:
                raise ShaderGraphError("The projected position can only be read where it is changed.")
            return [flow.values[store.value].origin for store in tap.split.stores]
        if tap.split.channel.scope == "discard":
            return [(_RECORDED if tap.split.stores else _FALSE, 0)]
        if tap.split.channel.scope == "read":
            if read is None or not any(store.site[0] == read for store in tap.split.stores):
                raise ShaderGraphError("A texture as read can only be changed where the texture is read.")
            width = sum(store.site[0] == read for store in tap.split.stores)
            return [(ir.instructions[read].result, component) if component < width else (_ZERO, 0)
                    for component in range(4)]
        if tap.form == "value":
            if any(store.site[1] != block for store in tap.split.stores):
                raise ShaderGraphError("An edit reads a channel the shader stores in another place.")
            return [flow.values[item].origin for item in tap.split.frontier]
        # The edit's visit of its block (index, count) reads the tap's visit of that index, or its only one.
        found = store_visits(store for store in tap.split.stores if store.site[1] == block)
        if len(found) == visit[1] > 1:
            found = [found[visit[0]]]
        if len(found) != 1 or len(found[0]) != len(tap.split.channel.lanes):
            raise ShaderGraphError("An edit reads a channel that is not stored where the edited channel is.")
        stores = sorted(found[0], key=lambda item: item.lane)
        # A stored lane is its value as computed; the object stored may be routed from it right before its store.
        return [flow.values[store.value].origin if store.value is not None
                else (_stored_value(ir.instructions[store.site[0]]), lane(store)) for store in stores]

    result = []
    for index, edit in enumerate(edits):
        taps = tuple((tap.name, tap.value_type) for tap in edit.taps)

        def arguments(block, read=None, visit=(0, 1), edit=edit):
            return tuple(argument for tap in edit.taps for argument in tap_arguments(tap, block, read, visit))

        def exit_sites(exits):
            return tuple(SpirvSurfaceSite((), arguments(block), exit=position) for position, block, _lane in exits)
        if edit.split.channel.scope == "discard":
            discards = tuple((store.site[0], store.site[1], None if ir.instructions[store.site[0]].opcode == _OP_DEMOTE
                              else _construct_exit(ir, store.site[0])) for store in edit.split.stores)
            exits = [*flow.exits, *((position, block, None) for position, block, merge in discards if merge == _ENDS)]
            sites = exit_sites(exits)
            records = any(tap.split.channel.scope == "discard" for tap in edit.taps) and bool(discards)
            result.append(SpirvSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps, sites,
                                           edit.material, discards, records))
            continue
        if edit.split.channel.scope == "added":
            channel = edit.split.channel
            sites = exit_sites(flow.exits)
            result.append(SpirvSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps, sites,
                                           edit.material, output=(channel.semantic, channel.semantic_index)))
            continue
        if edit.split.channel.scope == "position":
            read, lanes = position_site(edit.split)
            sites = (SpirvSurfaceSite((), arguments(None, read), read=read, lanes=lanes),)
            result.append(SpirvSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps, sites,
                                           edit.material))
            continue
        if edit.split.channel.scope == "read":
            sites = tuple(SpirvSurfaceSite((), arguments(block, read), read=read)
                          for read, block in sorted({store.site[:2] for store in edit.split.stores}))
            result.append(SpirvSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps, sites,
                                           edit.material))
            continue
        blocks: dict[int, list] = {}
        for store in edit.split.stores:
            blocks.setdefault(store.site[1], []).append(store)
        if edit.form == "value" and len(blocks) != 1:
            raise ShaderGraphError("A packed channel must be stored in one place to change it before packing.")
        sites = []
        for block, stores in blocks.items():
            if edit.form == "stored":
                # A lane stored again in its block (a geometry shader's next vertex) starts another visit with its own
                # taps; each store's lanes are computed right before it.
                visits = store_visits(stores)
                for number, visit in enumerate(visits):
                    tapped = arguments(block, visit=(number, len(visits)))
                    placed = [(store.site[0], lane(store), edit.split.channel.lanes.index(store.lane))
                              for store in visit]
                    sites.extend(SpirvSurfaceSite(tuple(item for item in placed if item[0] == index), tapped)
                                 for index in sorted({item[0] for item in placed}))
                continue
            stores.sort(key=lambda item: item.lane)
            tapped = arguments(block)
            placed = [(store.site[0], lane(store), component) for component, store in enumerate(stores)]
            sites.append(SpirvSurfaceSite(tuple(placed), tapped,
                                          tuple(flow.values[item].origin for item in edit.split.frontier)))
        result.append(SpirvSurfaceEdit(f"REasySurface_{index}", edit.value_type, edit.expression, taps, tuple(sites),
                                       edit.material))
    result = [replace(item, statements=edit.statements) for item, edit in zip(result, edits)]
    if any(edit.material for edit in result):
        address = _material_addresses(ir, elements)
        result = [replace(edit, sites=tuple(
            replace(site, material_address=address(_site_position(ir.instructions, site))) for site in edit.sites))
            if edit.material else edit for edit in result]
    return tuple(result)


def _stored_value(row) -> int:
    return row.values[_STORED[row.opcode]]


@lru_cache(None)
def _core() -> dict:
    return grammar()[0]


class _NotInlined(Exception):
    pass


class _Types:
    """A value is a flat list of (id, extract path) atoms."""

    def __init__(self, definitions):
        self.definitions = definitions

    def children(self, type_id) -> list[int]:
        row = self.definitions[type_id]
        if row.opcode == 30:
            return list(row.values[1:])
        if row.opcode in {23, 24}:
            return [row.values[1]] * row.values[2]
        if row.opcode == 28:
            return [row.values[1]] * int(self.definitions[row.values[2]].values[2])
        return []

    def width(self, type_id) -> int:
        children = self.children(type_id)
        return sum(self.width(child) for child in children) if children else 1

    def paths(self, type_id, prefix=()) -> list[tuple[int, ...]]:
        children = self.children(type_id)
        if not children:
            return [prefix]
        return [*self.paths(child, (*prefix, index)) for index, child in enumerate(children)]

    def at(self, type_id, path) -> int:
        for index in path:
            type_id = self.children(type_id)[index]
        return type_id

    def span(self, type_id, path) -> tuple[int, int]:
        offset = 0
        for index in path:
            children = self.children(type_id)
            offset += sum(self.width(child) for child in children[:index])
            type_id = children[index]
        return offset, self.width(type_id)


class SpirvSurfaceSplice:
    """Used while ``rebuild_spirv`` copies the native entry function. ``emit`` appends a row, ``input_values`` emits the
    native input loads in carrier order, ``local`` declares a function-local variable, and ``outputs`` maps each added
    output to its variable and type."""

    def __init__(self, ir, linker, plan, input_values, local, emit, outputs):
        self.linker, self.input_values, self.local, self.emit = linker, input_values, local, emit
        self.outputs = outputs
        self.builder = linker.builder
        self.native = _Types(self.builder.decoder.definitions)
        self.carrier = _Types(linker.carrier.definitions)
        self.helpers = {name: key for key, name in linker.carrier.names.items() if key in linker.carrier.functions}
        self.rows = ir.instructions
        self.sites: dict[int, list[tuple[SpirvSurfaceEdit, SpirvSurfaceSite]]] = {}
        for edit in plan:
            for component in range(value_width(edit.value_type)):
                if f"{edit.name}_{component}" not in self.helpers:
                    raise SpirvError(f"Compiled Vulkan carrier omitted surface helper {edit.name}_{component}")
            for site in edit.sites:
                self.sites.setdefault(_site_position(ir.instructions, site), []).append((edit, site))
        self.positions = {row.result: index for index, row in enumerate(ir.instructions) if row.result is not None}
        self.stored: dict[int, list] = {}
        self.extracts: dict = {}
        self.kills = {index: (edit, block, merge) for edit in plan for index, block, merge in edit.discards}
        self.leaving: dict[int, list[int]] = {}
        for _edit, block, merge in self.kills.values():
            if merge is not None:
                self.leaving.setdefault(merge, []).append(block)
        self.flag = None
        self.block = None
        self.renames: dict[int, int] = {}

    def helper_roots(self) -> list[int]:
        return [key for name, key in self.helpers.items() if name.startswith("REasySurface_")]

    def type_of(self, identifier) -> int:
        return self.builder.decoder.result_types[identifier]

    def instruction(self, opcode, type_id, *operands) -> int:
        row = self.builder.instruction(opcode, type_id, self.builder.id(), *operands)
        self.emit(row)
        return row.result

    def atoms(self, identifier) -> list[tuple[int, tuple[int, ...]]]:
        return [(identifier, path) for path in self.native.paths(self.type_of(identifier))]

    def atom(self, identifier, lane) -> tuple[int, tuple[int, ...]]:
        return self.atoms(identifier)[lane]

    def materialize(self, atoms, type_id) -> int:
        owner, prefix = atoms[0][0], atoms[0][1][:len(atoms[0][1]) - len(self.native.paths(type_id)[0])]
        if atoms == [(owner, (*prefix, *path)) for path in self.native.paths(type_id)] \
                and self.native.at(self.type_of(owner), prefix) == type_id:
            if not prefix:
                return owner
            key = (owner, prefix)
            if key not in self.extracts:
                self.extracts[key] = self.instruction(_OP_EXTRACT, type_id, owner, *prefix)
            return self.extracts[key]
        parts, offset = [], 0
        for child in self.native.children(type_id):
            width = self.native.width(child)
            parts.append(self.materialize(atoms[offset:offset + width], child))
            offset += width
        if not parts:
            raise SpirvError("A surface value has no lane for a scalar component")
        return self.instruction(_OP_CONSTRUCT, type_id, *parts)

    def convert(self, atom, type_id):
        source_type = self.native.at(self.type_of(atom[0]), atom[1])
        if source_type == type_id:
            return atom
        source, target = self.native.definitions[source_type], self.native.definitions[type_id]
        value = self.materialize([atom], source_type)
        if source.opcode == target.opcode == 22:
            opcode = 115  # OpFConvert
        elif source.opcode in {21, 22} and target.opcode in {21, 22} and source.values[1] == target.values[1]:
            opcode = 124  # OpBitcast
        else:
            raise SpirvError(f"A surface value cannot become {target.name}")
        return self.instruction(opcode, type_id, value), ()

    def helper_value(self, name, inputs, taps):
        rows = self.linker.carrier.functions[self.helpers[name]]
        parameters = [row for row in rows if row.opcode == _OP_FUNCTION_PARAMETER]
        if len(parameters) != len(inputs) + len(taps):
            raise SpirvError("Vulkan surface helper parameters disagree with its inputs and channels")
        arguments = []
        for parameter, value in zip(parameters, [(item, ()) for item in inputs] + list(taps)):
            pointer = self.native.definitions[self.linker.ids[parameter.type_id]]
            arguments.append(value if value == _LATE else
                             self.convert(value, pointer.values[2] if pointer.opcode == 32 else pointer.result))
        pending, emit = [], self.emit
        self.emit = pending.append
        extracts = dict(self.extracts)
        try:
            value = self.inline(rows, parameters, arguments)
        except _NotInlined:
            self.extracts, value, pending = extracts, None, []
        finally:
            self.emit = emit
        for row in pending:
            self.emit(row)
        if value is not None:
            return value
        if _LATE in arguments:
            raise ShaderGraphError(_LATE_READ)
        values = []
        for parameter, argument in zip(parameters, arguments):
            parameter_type = self.linker.ids[parameter.type_id]
            pointer = self.native.definitions[parameter_type]
            if pointer.opcode != 32:
                values.append(self.materialize([argument], parameter_type))
                continue
            variable = self.local(parameter_type)
            self.emit(self.builder.instruction(_OP_STORE, variable, self.materialize([argument], pointer.values[2])))
            values.append(variable)
        function = self.linker.ids[self.helpers[name]]
        return self.instruction(_OP_CALL, self.builder.decoder.definitions[function].type_id, function, *values), ()

    def inline(self, rows, parameters, arguments):
        carrier = self.linker.carrier.definitions
        values: dict[int, list] = {}
        pointers: dict[int, tuple[str, int, int, int]] = {}
        contents: dict[int, list] = {}
        for index, parameter in enumerate(parameters):
            pointers[parameter.result] = ("parameter", index, 0, 1)
        labels = 0

        def operand(identifier):
            if identifier in values:
                if _LATE in values[identifier]:
                    raise ShaderGraphError(_LATE_READ)
                return self.materialize(values[identifier], self.linker.ids[carrier[identifier].type_id])
            if identifier in pointers or identifier not in self.linker.ids:
                raise _NotInlined
            return self.linker.ids[identifier]

        for row in rows:
            code = row.opcode
            if code in _FRAME or code in _DEBUG_LINES:
                continue
            if code == _OP_LABEL:
                labels += 1
                if labels > 1:
                    raise _NotInlined
            elif code == _OP_VARIABLE and row.values[2] == 7 and len(row.values) == 3:
                pointers[row.result] = ("local", row.result, 0, self.carrier.width(self.linker.carrier.pointee(row.result)))
                contents[row.result] = [None] * pointers[row.result][3]
            elif code in _OP_ACCESS_CHAINS and row.values[2] in pointers:
                kind, base, offset, _width = pointers[row.values[2]]
                indices = [self.linker.carrier.constant(item) for item in row.values[3:]]
                if kind != "local" or None in indices:
                    raise _NotInlined
                start, width = self.carrier.span(self.linker.carrier.pointee(row.values[2]), indices)
                pointers[row.result] = (kind, base, offset + start, width)
            elif code == _OP_LOAD and row.values[2] in pointers:
                kind, base, offset, width = pointers[row.values[2]]
                loaded = [arguments[base]] if kind == "parameter" else contents[base][offset:offset + width]
                if None in loaded:
                    raise _NotInlined
                values[row.result] = loaded
            elif code == _OP_STORE:
                if row.values[0] not in pointers or pointers[row.values[0]][0] != "local":
                    raise _NotInlined
                _kind, base, offset, width = pointers[row.values[0]]
                contents[base][offset:offset + width] = self.symbolic(row.values[1], values, operand)
            elif code == _OP_RETURN_VALUE:
                result = self.symbolic(row.values[0], values, operand)
                if len(result) != 1:
                    raise _NotInlined
                if result[0] == _LATE:
                    raise ShaderGraphError(_LATE_READ)
                return result[0]
            elif code == _OP_CONSTRUCT:
                values[row.result] = [*self.symbolic(item, values, operand) for item in row.values[2:]]
            elif code == _OP_EXTRACT:
                start, width = self.carrier.span(carrier[row.values[2]].type_id, row.values[3:])
                values[row.result] = self.symbolic(row.values[2], values, operand)[start:start + width]
            elif code == _OP_INSERT:
                start, width = self.carrier.span(row.type_id, row.values[4:])
                updated = list(self.symbolic(row.values[3], values, operand))
                updated[start:start + width] = self.symbolic(row.values[2], values, operand)
                values[row.result] = updated
            elif code == _OP_SHUFFLE:
                joined = [*self.symbolic(row.values[2], values, operand), *self.symbolic(row.values[3], values, operand)]
                if 0xFFFFFFFF in row.values[4:]:
                    raise _NotInlined
                values[row.result] = [joined[item] for item in row.values[4:]]
            elif code == _OP_COPY:
                values[row.result] = self.symbolic(row.values[2], values, operand)
            elif row.result is None or row.type_id is None or code in _PLACED:
                raise _NotInlined
            elif any(item.is_id and item.kind not in {"IdResult", "IdResultType"}
                     and _LATE in values.get(item.value, ()) for item in row.operands):
                values[row.result] = self.lanes_apart(row, values, operand)
            else:
                mapped = row.remap({item.value: operand(item.value) for item in row.operands
                                    if item.is_id and item.kind not in {"IdResult", "IdResultType"}})
                result = self.builder.id()
                operands = [*mapped.operands]
                for position, item in enumerate(operands):
                    if item.kind == "IdResult":
                        operands[position] = type(item)(item.kind, result, item.width)
                    elif item.kind == "IdResultType":
                        operands[position] = type(item)(item.kind, self.linker.ids[row.type_id], item.width)
                cloned = type(row)(row.opcode, tuple(operands))
                self.builder.decoder.definitions[result] = cloned
                self.builder.decoder.result_types[result] = cloned.type_id
                self.emit(cloned)
                values[row.result] = self.atoms(result)
        raise _NotInlined

    def symbolic(self, identifier, values, operand):
        if identifier in values:
            return values[identifier]
        return self.atoms(operand(identifier))

    def lanes_apart(self, row, values, operand) -> list:
        """A lane reading a late value is late too and refuses only if the edit's value uses it: the carrier computes on
        whole vectors, but a store site takes only its own lanes."""
        entry = _core().get(row.opcode, {})
        scalars = self.native.children(self.linker.ids[row.type_id])
        if entry.get("class") not in _PURE_CLASSES or entry.get("opname") in _MIXING or not scalars:
            raise ShaderGraphError(_LATE_READ)
        sources = []
        for item in row.operands:
            if item.kind in {"IdResult", "IdResultType"}:
                continue
            if not item.is_id:
                sources.append((False, item.value))
                continue
            atoms = self.symbolic(item.value, values, operand)
            if len(atoms) != len(scalars):
                raise ShaderGraphError(_LATE_READ)
            sources.append((True, atoms))
        result = []
        for lane, scalar in enumerate(scalars):
            if any(lanes and source[lane] == _LATE for lanes, source in sources):
                result.append(_LATE)
                continue
            arguments = [self.materialize([source[lane]],
                                          self.native.at(self.type_of(source[lane][0]), source[lane][1]))
                         if lanes else source for lanes, source in sources]
            result.append(self.atom(self.instruction(row.opcode, scalar, *arguments), 0))
        return result

    def before(self, index):
        if self.rows[index].opcode == _OP_LABEL:
            self.block = self.rows[index].result
        ending = self.kills.get(index)
        if ending is not None and ending[2] == _ENDS and ending[0].records:
            self.emit(self.builder.instruction(_OP_STORE, self.recorded(), self.linker.boolean(True)))
        for edit, site in self.sites.get(index, ()):
            if any(self.positions[identifier] > index for identifier, _lane in site.frontier):
                raise ShaderGraphError(_LATE_READ)
            inputs = self.input_values()
            if edit.material:
                inputs = [*inputs, site.material_address]
            taps = [_LATE if identifier not in {_RECORDED, _FALSE, _ZERO} and self.positions[identifier] > index
                    else self.argument(identifier, lane, site) for identifier, lane in site.arguments]
            # A site storing the channel computes only the components it stores.
            needed = {component for _store, _lane, component in site.stores} \
                if site.stores and not site.frontier else None
            results = [self.helper_value(f"{edit.name}_{component}", inputs, taps)
                       if needed is None or component in needed else None
                       for component in range(value_width(edit.value_type))]
            if edit.output:
                variable, vector = self.outputs[edit.output]
                scalar = self.native.children(vector)[0]
                value = self.materialize([self.convert(result, scalar) for result in results], vector)
                self.emit(self.builder.instruction(_OP_STORE, variable, value))
                continue
            if site.exit is not None:
                self.kill(results[0])
                continue
            if site.read is not None:
                texels = self.rows[site.read].result
                atoms = self.atoms(texels)
                lanes = list(atoms) if site.lanes else []
                for result, lane in zip(results, site.lanes or range(len(atoms))):
                    converted = self.convert(result, self.native.at(self.type_of(texels), atoms[lane][1]))
                    if site.lanes:
                        lanes[lane] = converted
                    else:
                        lanes.append(converted)
                if lanes != atoms:
                    self.renames[texels] = self.materialize(lanes, self.type_of(texels))
                continue
            if site.frontier:
                self.repack(index, site, results)
                continue
            for store, lane, component in site.stores:
                stored = self.stored_atoms(store)
                stored[lane] = self.convert(results[component], self.native.at(self.type_of(stored[lane][0]), stored[lane][1]))

    def argument(self, identifier, lane, site):
        if identifier == _RECORDED:
            return self.discarded()
        if identifier == _FALSE:
            return self.linker.boolean(False), ()
        if identifier == _ZERO:
            texels = self.rows[site.read].result
            scalar = self.native.at(self.type_of(texels), self.atom(texels, 0)[1])
            return self.linker.typed_constant(_OP_CONSTANT_NULL, scalar), ()
        return self.atom(identifier, lane)

    def renamed(self, row):
        return row.remap(self.renames) if self.renames else row

    def stored_atoms(self, store) -> list:
        if store not in self.stored:
            self.stored[store] = self.atoms(_stored_value(self.rows[store]))
        return self.stored[store]

    def repack(self, anchor, site, results):
        replaced = {}
        for (identifier, lane), result in zip(site.frontier, results):
            original = self.atom(identifier, lane)
            new = self.convert(result, self.native.at(self.type_of(identifier), original[1]))
            if new != original:
                replaced.setdefault(identifier, self.atoms(identifier))[lane] = new
        if not replaced:
            return
        mapping = {identifier: self.materialize(atoms, self.type_of(identifier)) for identifier, atoms in replaced.items()}
        stores = sorted({store for store, _lane, _component in site.stores})
        region, pending = set(), [_stored_value(self.rows[store]) for store in stores]
        while pending:
            identifier = pending.pop()
            position = self.positions.get(identifier)
            if identifier in region or identifier in mapping or position is None or self.rows[position].opcode in {
                    _OP_FUNCTION, _OP_FUNCTION_PARAMETER, _OP_LABEL}:
                continue
            region.add(identifier)
            row = self.rows[position]
            pending.extend(item.value for item in row.operands if item.is_id and item.kind != "IdResultType")
        for identifier in sorted(region, key=self.positions.__getitem__):
            row = self.rows[self.positions[identifier]]
            if not any(item.is_id and item.value in mapping for item in row.operands if item.kind != "IdResult"):
                continue
            if row.opcode in _PLACED or row.opcode == _OP_LOAD or any(
                    self.positions.get(item.value, -1) > anchor for item in row.operands
                    if item.is_id and item.kind != "IdResult" and item.value not in mapping):
                raise ShaderGraphError("A channel's packing cannot be re-run where the channel is stored.")
            result = self.builder.id()
            cloned = row.remap(mapping)
            cloned = type(row)(row.opcode, tuple(type(item)(item.kind, result, item.width) if item.kind == "IdResult"
                                                 else item for item in cloned.operands))
            self.builder.decoder.definitions[result] = cloned
            self.builder.decoder.result_types[result] = cloned.type_id
            self.emit(cloned)
            mapping[identifier] = result
        for store, lane, _component in site.stores:
            value = _stored_value(self.rows[store])
            if value not in mapping:
                raise ShaderGraphError("A channel's packing does not reach its stored value.")
            self.stored_atoms(store)[lane] = self.atom(mapping[value], lane)

    def recorded(self) -> int:
        if self.flag is None:
            boolean = self.linker.scalar_type(20)
            self.flag = self.local(self.linker.scalar_type(32, 7, boolean), self.linker.boolean(False))
        return self.flag

    def discarded(self):
        return self.instruction(_OP_LOAD, self.linker.scalar_type(20), self.recorded()), ()

    def kill(self, result):
        boolean = self.linker.scalar_type(20)
        condition = self.materialize([self.convert(result, boolean)], boolean)
        if self.builder.decoder.definitions[condition].opcode == _OP_CONSTANT_FALSE:
            return
        killed, resumed = self.builder.id(), self.builder.id()
        for row in (self.builder.instruction(_OP_SELECTION_MERGE, resumed, 0),
                    self.builder.instruction(_OP_BRANCH_CONDITIONAL, condition, killed, resumed),
                    self.builder.instruction(_OP_LABEL, killed), self.builder.instruction(_OP_KILL),
                    self.builder.instruction(_OP_LABEL, resumed)):
            self.emit(row)

    def discard(self, index):
        if index not in self.kills:
            return None
        edit, _block, merge = self.kills[index]
        if merge == _ENDS:
            return [self.builder.instruction(_OP_RETURN)]
        rows = [self.builder.instruction(_OP_STORE, self.recorded(), self.linker.boolean(True))] if edit.records else []
        if merge is not None:
            rows.append(self.builder.instruction(_OP_BRANCH, merge))
        return rows

    def phi(self, row):
        blocks = self.leaving.get(self.block, ())
        if not blocks:
            return row
        undefined = self.linker.typed_constant(_OP_UNDEF, row.type_id)
        reference = type(row.operands[-1])
        return type(row)(row.opcode, (*row.operands, *(reference("IdRef", item) for block in blocks
                                                       for item in (undefined, block))))

    def store(self, index, row):
        atoms = self.stored.get(index)
        if atoms is None or atoms == self.atoms(_stored_value(row)):
            return row
        value = self.materialize(atoms, self.type_of(_stored_value(row)))
        operands, position = list(row.operands), _STORED[row.opcode]
        operands[position] = type(operands[position])(operands[position].kind, value, operands[position].width)
        return type(row)(row.opcode, tuple(operands))


__all__ = ["SpirvSurfaceEdit", "SpirvSurfaceSite", "SpirvSurfaceSplice", "spirv_surface_plan"]
