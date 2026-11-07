"""See docs/internals/surface-splicing.md, Readers."""
from __future__ import annotations

from functools import lru_cache
import re

lazy import fastdxiltext

lazy from file_handlers.sdf.shader_graph import ShaderGraphError

lazy from .native_loop_flow import dominator_sets, predecessor_map, reachable_blocks
lazy from .profiles.re_engine_dxil_signatures import DxilEntryMetadata
lazy from .surface_flow import (
    TEXEL_TYPES, FlowStore, FlowValue, NativeFlow, buffer_element, buffer_word_types, constant_field,
    declared_component_types, structured_element,
)
lazy from .profiles.constant_words import word_parts
lazy from .profiles.re_engine_bindless_material import texture_handles


# The first stored value's argument of rawBufferStore, bufferStore and textureStore; textureStore's are components.
_RESOURCE_STORES = {140: 4, 69: 4, 67: 5}
_ADDITION = re.compile(r"(?:add|or)(?: nuw| nsw| disjoint)* i32 ([^,]+), (.+)")
_TOKEN = r"%[-$._A-Za-z0-9]+"
_LANE_FIRST = re.compile(rf"^\s*({_TOKEN}) = call i32 @dx\.op\.waveReadLaneFirst\.i32\(i32 118, i32 ({_TOKEN})\)")
_EQUAL = re.compile(rf"= icmp eq i32 ({_TOKEN}), ({_TOKEN})\s*$")


def _dominators(blocks: list[str], successors: dict[str, set[str]]) -> dict[str, set[str]]:
    dominators = dominator_sets(blocks, predecessor_map(blocks, successors), unreachable=True)
    return {block: set(members) for block, members in dominators.items()}


def _entry(lines: list[str]):
    metadata = DxilEntryMetadata.parse(lines)
    spans = fastdxiltext.function_spans(lines)
    if metadata.function_name not in spans:
        raise ShaderGraphError("DXIL entry function is missing.")
    begin, end = spans[metadata.function_name]
    return metadata, begin, end, fastdxiltext.statements(lines[begin + 1:end - 1])


@lru_cache(maxsize=2)
def _assembly_entry(assembly: str):
    """A shader's readers ask in turn: its flow, then its material records."""
    lines = assembly.splitlines()
    return (lines, *_entry(lines))


def _control_flow(statements):
    return fastdxiltext.control_flow(statements)


def material_records(assembly: str, contract, instances: str = "",
                     prefix: int = 0) -> tuple[tuple[str, str, int, str, frozenset[str], bool], ...]:
    from .profiles.re_engine_bindless_material import material_record_accesses
    lines = assembly.splitlines()
    accesses = material_record_accesses(lines, contract, instances=instances)
    if not accesses:
        return ()
    lines, _metadata, begin, _end, statements = _assembly_entry(assembly)
    # A pass making the address uniform in a loop over its lanes (waveReadLaneFirst, then the lanes whose own address
    # equals it) reads, in each lane, at the lane's own address; that address is the record's too.
    first, equal = {}, set()
    for text in lines:
        if match := _LANE_FIRST.match(text):
            first[match[1]] = match[2]
        elif match := _EQUAL.search(text):
            equal.add(frozenset(match.group(1, 2)))
    own = {access.address: first[access.address] for access in accesses
           if access.address in first and frozenset((access.address, first[access.address])) in equal}
    sites = fastdxiltext.material_sites(lines, statements, begin + 1,
                                        [*(access.address for access in accesses), *own.values()])
    records = []
    for access in accesses:
        site = sites.get(access.address)
        if site is not None:
            number, block, line, dominated = site
            header = bool(prefix and access.words and not access.storage.startswith("instance")
                          and all(word.byte_offset < prefix for word in access.words))
            records.append((number, block, line, access.address, dominated, header))
            lane = sites.get(own[access.address]) if access.address in own else None
            if lane is not None:
                records.append((number, lane[1], lane[2], own[access.address], lane[3], header))
    return tuple(records)


def call_access(lines: list[str]) -> dict[str, str]:
    return fastdxiltext.call_access(lines)


def dxil_flow(assembly: str, contract) -> NativeFlow:
    lines, metadata, begin, _end, statements = _assembly_entry(assembly)
    outputs = {}
    for scope, rows in (("output", metadata.output_rows()), ("patch", metadata.patch_rows())):
        for (name, semantic_index), (identity, row) in rows.items():
            outputs[(scope, identity, row)] = (name, semantic_index)
    blocks, successors, conditions, dominators = fastdxiltext.control_flow(statements)

    def branches(block):
        strict = dominators[block] - {block}
        dominator = next((item for item in blocks if item in strict and dominators[item] == strict), None)
        return sorted(reachable_blocks(dominator, successors, block), key=blocks.index) if dominator else []

    from .profiles.re_engine_bindless_material import material_record_accesses
    handles = texture_handles(lines, contract, material_record_accesses(lines, contract))
    (resolved, stores, texture_reads, resource_stores, samples, discards, exits, index,
     bodies) = fastdxiltext.flow(lines, begin, statements, outputs, handles, FlowValue, branches)

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

    result, words, typed = [], {}, {}
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
            if scope == "buffer":
                words.setdefault((scope, semantic, word // 4, word % 4), []).append(
                    "float" if callee.rsplit(".", 1)[1].startswith("f") else "integer")
    component_types.update({("typed", semantic, 0): kind for semantic, kind in typed.items()})
    component_types.update(buffer_word_types(words))
    return result


__all__ = ["call_access", "dxil_flow", "material_records"]
