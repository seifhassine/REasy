"""See docs/internals/surface-splicing.md, Readers."""
from __future__ import annotations

import re

lazy from file_handlers.sdf.shader_graph import ShaderGraphError

lazy from .native_loop_flow import dominator_sets, predecessor_map, reachable_blocks
lazy from .profiles.re_engine_dxil_authoring import (
    _ATTRIBUTE_DEFINITION, _CALL, _DECLARE, _SSA, _VALUE, _function_spans,
)
lazy from .profiles.re_engine_dxil_recovery import _TYPE
lazy from .profiles.re_engine_dxil_signatures import DxilEntryMetadata
lazy from .surface_flow import (
    TEXEL_TYPES, FlowStore, FlowValue, NativeFlow, buffer_component_type, buffer_element, constant_field,
    declared_component_types, structured_element,
)
lazy from .profiles.constant_words import word_parts
lazy from .profiles.re_engine_bindless_material import texture_handles


_LABEL = re.compile(r"^(?:; <label>:(?P<comment>\d+)|(?P<name>[-$._A-Za-z0-9]+):)")
_SCALAR = re.compile(r"\b(half|float|double|i1|i8|i16|i32|i64)\b")
_TARGETS = re.compile(r"label (%[-$._A-Za-z0-9]+)")
_INCOMING = re.compile(r"\[\s*([^,\]]+?)\s*,\s*(%[-$._A-Za-z0-9]+)\s*\]")
_LITERAL_ARGUMENTS = re.compile(r"\b(i1|i8|i16|i32|i64|half|float|double) (-?[0-9][0-9a-fA-FxX.+-]*|true|false|undef)")
_OUTPUT_OPCODES = {5: "output", 106: "patch"}
_DISCARD_OPCODE = 82
# The first stored value's argument of rawBufferStore, bufferStore and textureStore; textureStore's are components.
_RESOURCE_STORES = {140: 4, 69: 4, 67: 5}
_ADDITION = re.compile(r"(?:add|or)(?: nuw| nsw| disjoint)* i32 ([^,]+), (.+)")
_TEXTURE_READS = ("dx.op.sample", "dx.op.textureLoad", "dx.op.textureGather")


def _scalar(llvm_type: str) -> str:
    return "float" if llvm_type in {"half", "float", "double"} else "bool" if llvm_type == "i1" else "integer"


def _statements(lines: list[str]):
    block, pending, start, depth = "%0", [], 0, 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not pending:
            label = _LABEL.match(stripped)
            if label is not None:
                block = "%" + (label["comment"] or label["name"])
                continue
            if not stripped or stripped.startswith(";"):
                continue
            start = index
        text = stripped.split(";", 1)[0] if not stripped.startswith("[") else stripped
        pending.append(text)
        depth += text.count("[") - text.count("]")
        if depth <= 0:
            yield block, start, " ".join(pending)
            pending, depth = [], 0
    if pending:
        raise ShaderGraphError("DXIL entry function ends inside a statement.")


def _dominators(blocks: list[str], successors: dict[str, set[str]]) -> dict[str, set[str]]:
    dominators = dominator_sets(blocks, predecessor_map(blocks, successors), unreachable=True)
    return {block: set(members) for block, members in dominators.items()}


def _entry(lines: list[str]):
    metadata = DxilEntryMetadata.parse(lines)
    spans = _function_spans(lines)
    if metadata.function_name not in spans:
        raise ShaderGraphError("DXIL entry function is missing.")
    begin, end = spans[metadata.function_name]
    return metadata, begin, end, list(_statements(lines[begin + 1:end - 1]))


def _control_flow(statements):
    blocks, successors, conditions = [], {}, {}
    for block, _line, text in statements:
        if block not in successors:
            blocks.append(block)
            successors[block] = set()
        if text.startswith(("br ", "switch ")):
            successors[block].update(_TARGETS.findall(text))
            if text.startswith(("switch ", "br i1 ")):
                conditions[block] = _SSA.findall(text.split(",", 1)[0])[0]
    return blocks, successors, conditions, _dominators(blocks, successors)


def entry_dominance(assembly: str) -> tuple[dict[str, tuple[str, int]], dict[str, set[str]]]:
    _metadata, begin, _end, statements = _entry(assembly.splitlines())
    defined = {match["result"]: (block, begin + 1 + line) for block, line, text in statements
               if (match := _VALUE.match(text))}
    return defined, _control_flow(statements)[3]


# ``annotateHandle``'s resource properties: their first word marks a UAV with this bit.
_ANNOTATED = re.compile(r"@dx\.op\.annotateHandle\(i32 216, %dx\.types\.Handle [^,]+, %dx\.types\.ResourceProperties "
                        r"\{ i32 (-?\d+),")
_UAV_PROPERTY = 1 << 12
_MEMORY_INSTRUCTIONS = ("phi ", "load ", "store ", "alloca ", "atomicrmw ", "cmpxchg ", "fence ")


def value_numbers(assembly: str) -> dict[str, str]:
    lines = assembly.splitlines()
    _metadata, _begin, _end, statements = _entry(lines)
    access = call_access(lines)
    bodies = {match["result"]: text[match.end():].strip() for _block, _line, text in statements
              if (match := _VALUE.match(text))}
    read_only = {name for name, body in bodies.items()
                 if (found := _ANNOTATED.search(body)) is not None and not int(found[1]) & _UAV_PROPERTY}
    def pure(body: str) -> bool:
        if body.startswith(_MEMORY_INSTRUCTIONS):
            return False
        called = _CALL.search(body) if body.startswith(("call ", "tail call ")) else None
        if called is None:
            return not body.startswith(("call ", "tail call "))
        kind = access.get(called["name"], "effect")
        return kind == "readnone" or kind == "readonly" and all(
            item in read_only for item in _SSA.findall(body) if bodies.get(item, "").startswith(
                ("call %dx.types.Handle", "tail call %dx.types.Handle")))

    # Values are defined before their uses except through phis, which keep their own key.
    keys: dict[str, str] = {}
    numbered: dict[str, str] = {}
    for value, body in bodies.items():
        if pure(body):
            shape = _SSA.sub(lambda match: keys.get(match[0], match[0]), body)
            keys[value] = numbered.setdefault(shape, f"#{len(numbered)}")
        else:
            keys[value] = value
    return keys


def call_access(lines: list[str]) -> dict[str, str]:
    groups = {int(match[1]): match[2] for line in lines if (match := _ATTRIBUTE_DEFINITION.match(line))}
    access = {}
    for line in lines:
        declaration = _DECLARE.match(line)
        if declaration is not None:
            attribute = re.search(r"#(\d+)\s*$", line)
            text = groups.get(int(attribute[1]), "") if attribute else ""
            access[declaration["name"]] = "readnone" if "readnone" in text else "readonly" if "readonly" in text else "effect"
    return access


def dxil_flow(assembly: str, contract) -> NativeFlow:
    lines = assembly.splitlines()
    metadata, begin, _end, statements = _entry(lines)
    types = {match["name"] for line in lines if (match := _TYPE.match(line))}
    access = call_access(lines)
    outputs = {}
    for scope, rows in (("output", metadata.output_rows()), ("patch", metadata.patch_rows())):
        for (name, semantic_index), (identity, row) in rows.items():
            outputs[(scope, identity, row)] = (name, semantic_index)
    blocks, successors, conditions, dominators = _control_flow(statements)

    def branches(block):
        strict = dominators[block] - {block}
        dominator = next((item for item in blocks if item in strict and dominators[item] == strict), None)
        return sorted(reachable_blocks(dominator, successors, block), key=blocks.index) if dominator else []

    index: dict[str, int] = {}
    values: list[FlowValue] = []
    pending: list[tuple[int, str, tuple[str, ...], tuple[str, ...], str]] = []
    stores, discards, exits, resource_stores = [], [], [], []
    bodies: dict[str, str] = {}
    from .profiles.re_engine_bindless_material import material_record_accesses, texture_handles
    handles = texture_handles(lines, contract, material_record_accesses(lines, contract))
    samples: dict[str, tuple[str, tuple, str]] = {}
    texture_reads: list[tuple[str, int, str, tuple]] = []
    memory: dict[str, list[str]] = {}
    roots: dict[str, str] = {}
    addresses: dict[str, tuple[str, ...]] = {}

    def memory_root(text: str) -> str:
        found = re.search(r'@(?:"(?:[^"\\]|\\.)*"|[-$._A-Za-z0-9]+)', text)
        return found[0] if found is not None and not found[0].startswith("@dx.op.") else ""

    def operands_of(text: str) -> tuple[str, ...]:
        text = _TARGETS.sub("", text)
        call = _CALL.search(text)
        if call is not None:
            text = text[call.end():]
        return tuple(token for token in _SSA.findall(text) if token not in types)

    for block, line_index, text in statements:
        definition = _VALUE.match(text)
        result = definition["result"] if definition else None
        body = text[definition.end():].strip() if definition else text
        opcode = body.split(" ", 1)[0]
        site = (begin + 1 + line_index, block)
        if opcode == "call":
            callee = _CALL.search(body)
            if callee is None:
                raise ShaderGraphError("DXIL call has no callee.")
            name = callee["name"]
            literal = _LITERAL_ARGUMENTS.findall(body[callee.end():])
            operation = int(literal[0][1]) if name.startswith("dx.op.") and literal and literal[0][1].lstrip("-").isdigit() else None
            arguments = operands_of(body)
            if name.startswith(("dx.op.storeOutput", "dx.op.storePatchConstant")):
                scope = _OUTPUT_OPCODES.get(operation)
                tail = body[callee.end():].rsplit(")", 1)[0].split(",")
                if scope is None or len(tail) != 5:
                    raise ShaderGraphError("DXIL output store is malformed.")
                identity, row, lane = (int(item.split()[-1]) for item in tail[1:4])
                semantic = outputs.get((scope, identity, row))
                if semantic is None:
                    raise ShaderGraphError("DXIL output store has no signature row.")
                stored = tail[4].split()[-1]
                stores.append((semantic, scope, lane, stored, (*site, 4)))
                continue
            if operation in _RESOURCE_STORES:
                resource_stores.append((operation, name, [item.split()[-1] for item in
                                                          body[callee.end():].rsplit(")", 1)[0].split(",")], site))
                continue
            if operation == _DISCARD_OPCODE:
                discards.append((arguments[0] if arguments else body.rsplit(" ", 1)[-1].rstrip(")"), site))
                continue
            if result is None:
                continue
            if name.startswith(_TEXTURE_READS) and (handle := next((item for item in arguments if item in handles), None)):
                texel = re.search(r"%dx\.types\.ResRet\.([fi])\d+", body)
                samples[result] = (handles[handle], site, "float32" if texel is None or texel[1] == "f" else "uint32")
            scalar = _SCALAR.search(body[len("call"):callee.start()])
            kind = "pure" if access.get(name) == "readnone" and arguments else "source"
            # A source is identified by its kind (the DXIL operation) only, not by which input or word it reads.
            family = tuple(value for _scalar_type, value in (literal if kind == "pure" else literal[:1]))
            pending.append((len(values), kind, arguments, (), body))
            values.append(FlowValue(kind, signature=(name, family),
                                    scalar=_scalar(scalar[1]) if scalar else "", origin=result))
        elif opcode == "phi":
            incoming = _INCOMING.findall(body)
            operands = tuple(value for value, _label in incoming if value.startswith("%"))
            controls = tuple(conditions[item] for item in branches(block) if item in conditions)
            pending.append((len(values), "join", operands, controls, body))
            scalar = _SCALAR.search(body)
            values.append(FlowValue("join", scalar=_scalar(scalar[1]) if scalar else "", origin=result))
        elif result is not None:
            bodies[result] = body
            arguments = operands_of(body)
            scalar_text = body.split(" to ", 1)[1] if " to " in body else body
            if opcode == "select":
                scalar_text = body.split(",", 1)[1]
            elif opcode == "extractvalue":
                element = re.search(r"\.([fi])\d+\b", body)
                scalar_text = "" if element is None else "float" if element[1] == "f" else "i32"
            scalar = _SCALAR.search("i1" if opcode in {"fcmp", "icmp"} else scalar_text)
            if opcode in {"alloca", "getelementptr"}:
                # Addresses aren't values: a load depends on its indices and on what was stored there.
                base = next((item for item in arguments if item in roots), None)
                roots[result] = roots[base] if base is not None else memory_root(body) or result
                addresses[result] = (*addresses.get(base, ()), *(item for item in arguments if item != base))
                continue
            if opcode == "extractvalue" and arguments and arguments[0] in samples:
                lane = int(body.rsplit(",", 1)[1])
                if lane < 4:
                    texture_reads.append((*samples[arguments[0]][:1], lane, result, samples[arguments[0]][1]))
            kind = "source" if opcode in {"load", "atomicrmw", "cmpxchg"} or (
                opcode == "extractvalue" and arguments and arguments[0] in index
                and values[index[arguments[0]]].kind == "source") or not arguments else "pure"
            literal = () if kind == "source" and opcode == "extractvalue" else tuple(
                re.findall(r"(?<![%\w.])-?(?:0x[0-9A-Fa-f]+|[0-9][0-9.e+-]*)", body))
            flags = tuple(word for word in body.split()[1:4] if word.isalpha())
            pending.append((len(values), kind, arguments, (), body))
            values.append(FlowValue(kind, signature=(opcode, flags, literal),
                                    scalar=_scalar(scalar[1]) if scalar else "", origin=result))
        else:
            if opcode == "ret":
                exits.append(site)
            if opcode in {"store", "atomicrmw", "cmpxchg"}:
                parts = operands_of(body)
                pointer = next((item for item in parts if item in roots), None)
                root = roots[pointer] if pointer is not None else memory_root(body)
                memory.setdefault(root, []).extend(item for item in parts if item != pointer)
            continue
        if result in index:
            raise ShaderGraphError(f"DXIL redefines {result}.")
        index[result] = len(values) - 1

    resolved = []
    for position, kind, operands, controls, body in pending:
        value = values[position]
        if value.signature and value.signature[0] in {"load", "atomicrmw", "cmpxchg"}:
            pointer = next((item for item in operands if item in roots), None)
            root = roots[pointer] if pointer is not None else memory_root(body)
            operands = (*(item for item in operands if item != pointer), *addresses.get(pointer, ()),
                        *memory.get(root, ()))
        references = tuple(dict.fromkeys(index[item] for item in operands if item in index))
        resolved.append(FlowValue(
            kind if references or kind != "pure" else "source", references,
            tuple(dict.fromkeys(index[item] for item in controls if item in index)),
            value.signature, value.scalar, value.origin,
        ))

    component_types = declared_component_types(contract)
    flow_stores = []
    for (name, semantic_index), scope, lane, stored, site in stores:
        value = index.get(stored)
        if value is None and stored.startswith("%"):
            raise ShaderGraphError(f"DXIL stores undefined value {stored}.")
        flow_stores.append(FlowStore(name, semantic_index, lane, value, "" if value is not None else stored, site, scope))
    for texture, lane, value, site in texture_reads:
        flow_stores.append(FlowStore(texture, 0, lane, index[value], "", site, "read"))
    flow_stores.extend(_resource_stores(lines, contract, resource_stores, bodies, index, component_types))
    for texture, _site, texel in samples.values():
        resource = contract.resource(texture)
        component_types[("read", texture, 0)] = (TEXEL_TYPES.get(resource.return_type, texel)
                                                 if resource is not None and texel != "float32" else texel)
    flow_discards = []
    for condition, site in discards:
        controls = tuple(dict.fromkeys(index[conditions[item]] for item in branches(site[1])
                                       if conditions.get(item) in index))
        resolved.append(FlowValue("join", (index[condition],) if condition in index else (), controls, ("discard",),
                                  "bool", ("discard", site)))
        flow_discards.append(FlowStore("discard", 0, 0, len(resolved) - 1, "", site, "discard"))
    return NativeFlow(metadata.stage, tuple(resolved), tuple(flow_stores), tuple(flow_discards), component_types,
                      exits=tuple(exits))


def _resource_stores(lines, contract, calls, bodies, index, component_types) -> list[FlowStore]:
    if not calls:
        return []
    from .profiles.re_engine_dxil_authoring import _constant_extracts
    handles = texture_handles(lines, contract, writable=True)
    fields = {}
    for word, names in _constant_extracts(lines, contract).items():
        binding, offset, space = word_parts(word)
        fields.update((name, constant_field(contract, binding, space, offset)) for name in names)

    def address_terms(token: str) -> tuple[list[str], int]:
        if token == "undef":
            return [], 0
        if not token.startswith("%"):
            return [], int(token)
        if token in fields:
            return [fields[token]], 0
        addition = _ADDITION.fullmatch(bodies.get(token, ""))
        if addition is None:
            return [], 0
        (left, left_offset), (right, right_offset) = (address_terms(item.strip()) for item in addition.groups())
        return [*left, *right], left_offset + right_offset

    result, scalars, typed = [], {}, {}
    for operation, callee, arguments, site in calls:
        name = handles.get(arguments[1])
        resource = contract.resource(name) if name is not None else None
        if resource is None:
            raise ShaderGraphError("The program stores into a resource it does not declare by name.")
        first_value = _RESOURCE_STORES[operation]
        if operation == 67 or resource.input_type == "rw-typed":
            scope, semantic, first = "typed", name, 0
            typed[semantic] = TEXEL_TYPES.get(resource.return_type, "float32")
        else:
            scope = "buffer"
            terms, offset = address_terms(arguments[2])
            if resource.input_type == "rw-structured-buffer":
                semantic, first = structured_element(name, resource.sample_count, terms, offset,
                                                     *address_terms(arguments[3]))
            else:
                semantic, first = buffer_element(name, terms, offset)
        mask = int(arguments[first_value + 4])
        for component in range(4):
            if not mask >> component & 1:
                continue
            stored, word = arguments[first_value + component], first + component
            value = index.get(stored)
            if value is None and stored.startswith("%"):
                raise ShaderGraphError(f"DXIL stores undefined value {stored}.")
            result.append(FlowStore(semantic, word // 4, word % 4, value, "" if value is not None else stored,
                                    (*site, first_value + component), scope))
            scalars.setdefault((scope, semantic, word // 4), []).append(
                "float" if callee.rsplit(".", 1)[1].startswith("f") else "integer")
    for key, kinds in scalars.items():
        component_types[key] = typed[key[1]] if key[0] == "typed" else buffer_component_type(kinds)
    return result


__all__ = ["call_access", "dxil_flow", "entry_dominance"]
