from __future__ import annotations

from file_handlers.sdf.spirv import SpirvError

from .surface_flow import ChannelSplit, NativeFlow


def parameter_sources(ir_kind: str, ir, contract, flow: NativeFlow, buffer_name: str, prefix: int) -> dict[int, int]:
    buffer = contract.buffer(buffer_name)
    result: dict[int, int] = {}
    if ir_kind == "dxbc-sm5":
        if buffer is None:
            return result
        for index, value in enumerate(flow.values):
            origin = value.origin
            if (value.kind == "source" and isinstance(origin, tuple) and len(origin) == 4 and origin[0] == "cb"
                    and origin[1] == buffer.bind_point):
                result[index] = origin[2] * 16 + origin[3] * 4
        return result
    origins = {value.origin: index for index, value in enumerate(flow.values)}
    if ir_kind == "dxil":
        from .profiles.constant_words import word_parts
        from .profiles.re_engine_bindless_material import material_record_accesses
        from .profiles.re_engine_dxil_authoring import _constant_extracts
        lines = ir.to_assembly().splitlines()
        if buffer is not None:
            for word, names in _constant_extracts(lines, contract).items():
                binding, offset, space = word_parts(word)
                if (binding, space) == (buffer.bind_point, buffer.register_space):
                    result.update((origins[name], offset) for name in names if name in origins)
            return result
        for access in material_record_accesses(lines, contract):
            result.update((origins[word.value], word.byte_offset - prefix) for word in access.words
                          if word.value in origins and word.byte_offset >= prefix)
        return result
    if buffer is None:
        return result
    from .profiles.re_engine_spirv_authoring import _Module
    module = _Module(ir)
    for row in ir.instructions:
        if row.opcode != 61:
            continue
        try:
            pointer = module.uniform_pointer(row.values[2])
        except SpirvError:
            continue  # An address only resolved semantically names no fixed word.
        if pointer is None or module.names.get(pointer.root) != buffer.name:
            continue
        for lane, (_path, _type, address) in enumerate(module.uniform_leaves(row.type_id, pointer.address, pointer.layout)):
            if not address.terms and (row.result, lane) in origins:
                result[origins[row.result, lane]] = address.constant
    return result


def channel_reach(flow: NativeFlow, splits: tuple[ChannelSplit, ...],
                  sources: dict[int, str]) -> dict[str, dict[str, str]]:
    names = sorted(set(sources.values()))
    bits = {name: 1 << index for index, name in enumerate(names)}
    own = [bits[sources[index]] if index in sources else 0 for index in range(len(flow.values))]
    direct, every = list(own), list(own)
    changed = True
    while changed:
        changed = False
        for index, value in enumerate(flow.values):
            reached = direct[index]
            for operand in value.operands:
                reached |= direct[operand]
            anything = every[index] | reached
            for item in (*value.operands, *value.controls):
                anything |= every[item]
            if (reached, anything) != (direct[index], every[index]):
                direct[index], every[index] = reached, anything
                changed = True
    result: dict[str, dict[str, str]] = {}
    for split in splits:
        if split.channel.scope == "read":
            continue
        values = [store.value for store in split.stores if store.value is not None]
        reached = anything = 0
        for value in values:
            reached |= direct[value]
            anything |= every[value]
        for name, bit in bits.items():
            if anything & bit:
                result.setdefault(name, {})[split.channel.key] = "value" if reached & bit else "branch"
    return result


__all__ = ["channel_reach", "parameter_sources"]
