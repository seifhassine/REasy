import re

from .dxc import assemble_dxil, disassemble_dxil, enrich_dxil_shader
lazy from file_handlers.mmtr.profiles.re_engine_dxil_authoring import _attach_reflection

lazy from .dxbc import parse_dxbc
lazy from .dxil_metadata import metadata_definitions
lazy from .shader_graph import ShaderGraphError, dxil_target


def structured_view_spaces(contract):
    if not dxil_target(contract):
        return ()
    occupied = {r.register_space for r in contract.resources} | {b.register_space for b in contract.constant_buffers}
    spaces = sorted({r.register_space for r in contract.resources
                     if r.input_type in {'structured-buffer', 'rw-structured-buffer'}})
    result, candidate = [], 0
    for original in spaces:
        while candidate in occupied:
            candidate += 1
        result.append((original, candidate))
        occupied.add(candidate)
    return tuple(result)


def coalesce_structured_views(bytecode, contract, spaces):
    if not spaces:
        return bytecode
    reverse = {private: original for original, private in spaces}
    reflected = enrich_dxil_shader(parse_dxbc(bytecode), bytecode)
    if not any(r.register_space in reverse for r in reflected.reflection.resources):
        return bytecode
    assembly = disassemble_dxil(bytecode)
    if contract.stage != 'library':
        assembly = _attach_reflection(assembly, bytecode)
    lines = assembly.splitlines()
    definitions = metadata_definitions(lines)
    roots = definitions.get('!dx.resources', ())
    if len(roots) != 1 or len(definitions.get(roots[0], ())) != 4:
        raise ShaderGraphError('Structured views require declared DXIL resource-class lists.')
    changes, dropped, remapped_ids, global_aliases = {}, set(), {}, {}
    symbol = re.compile(r'@(?:"(?:[^"\\]|\\.)*"|[-\w.$]+)')
    bindings = {(r.register_class, r.register_space, r.bind_point): r for r in contract.resources}

    def integer(value):
        if not re.fullmatch(r'i32 -?\d+', value) or not -(1 << 31) <= int(value[4:]) < (1 << 32):
            raise ShaderGraphError('Invalid structured-view metadata integer.')
        return int(value[4:]) & 0xffffffff

    def extra(row):
        operands = definitions.get(row[-1], ())
        if len(operands) % 2:
            raise ShaderGraphError('Invalid structured-view property pairs.')
        return {integer(operands[i]): operands[i + 1] for i in range(0, len(operands), 2)}

    for resource_class, group in enumerate(definitions[roots[0]][:2]):
        references = definitions.get(group, ())
        records = {ref: list(definitions[ref]) for ref in references}
        addresses = {(integer(row[3]), integer(row[4])): ref for ref, row in records.items()}
        replacements = {}
        for ref, row in records.items():
            space, slot = integer(row[3]), integer(row[4])
            if space not in reverse:
                continue
            original_space = reverse[space]
            binding = bindings.get(('tu'[resource_class], original_space, slot))
            properties = extra(row)
            if (len(row) != (9 if resource_class == 0 else 11) or row[6] != 'i32 12'
                    or binding is None or binding.input_type != ('structured-buffer' if resource_class == 0 else 'rw-structured-buffer')
                    or integer(row[5]) != (binding.bind_count or 0xffffffff)
                    or properties.get(1) != f'i32 {binding.sample_count}'):
                raise ShaderGraphError('A compiler-private structured view changed its renderer layout.')
            original = addresses.get((original_space, slot))
            if original is not None:
                other = records[original]
                if row[5:-1] != other[5:-1]:
                    raise ShaderGraphError('Structured views disagree on extent, kind, coherence, counter or ROV policy.')
                other_properties = extra(other)
                if any(properties.get(tag, value) != value for tag, value in other_properties.items()):
                    raise ShaderGraphError('Structured views disagree on their extended resource properties.')
                properties.update(other_properties)
                changes[row[-1]] = tuple(operand for tag, value in sorted(properties.items()) for operand in (f'i32 {tag}', value))
                replacements[original] = ref
                dropped.add(original)
                old_global, new_global = symbol.search(other[1]), symbol.search(row[1])
                if old_global is not None or new_global is not None:
                    if old_global is None or new_global is None:
                        raise ShaderGraphError('Structured library views need matching handle globals.')
                    global_aliases[old_global[0]] = new_global[0]
            row[3] = f'i32 {original_space}'
            row[2] = '!"' + ''.join(chr(b) if 32 <= b < 127 and b not in (34,92) else f'\\{b:02X}'
                                  for b in binding.name.encode('utf-8')) + '"'
        retained = [ref for ref in references if ref not in dropped]
        new_ids = {ref: index for index, ref in enumerate(retained)}
        for ref, row in records.items():
            remapped_ids[resource_class, integer(row[0])] = new_ids[replacements.get(ref, ref)]
            if ref not in dropped:
                row[0] = f'i32 {new_ids[ref]}'
                changes[ref] = tuple(row)
        if group != 'null':
            changes[group] = tuple(retained)

    modern = re.compile(r'(@dx\.op\.createHandleFromBinding\(i32 217, %dx\.types\.ResBind \{ i32 -?\d+, i32 -?\d+, i32 )(\d+)(, i8 [01] \})')
    legacy = re.compile(r'(@dx\.op\.createHandle\(i32 57, i8 )([01])(, i32 )(\d+)(,)')
    result = []
    for line in lines:
        if line.split(' = ', 1)[0] in global_aliases:
            continue
        identity = line.split(' = ', 1)[0] if line.startswith('!') else None
        if identity in dropped:
            continue
        if identity in changes:
            line = identity + ' = !{' + ', '.join(changes[identity]) + '}'
        else:
            if 0 in reverse:
                line = line.replace('@dx.op.createHandleFromBinding(i32 217, %dx.types.ResBind zeroinitializer',
                    '@dx.op.createHandleFromBinding(i32 217, %dx.types.ResBind '
                    + f'{{ i32 0, i32 0, i32 {reverse[0]}, i8 0 }}')
            line = modern.sub(lambda m: m[1] + str(reverse.get(int(m[2]), int(m[2]))) + m[3], line)
            line = legacy.sub(lambda m: m[1] + m[2] + m[3] + str(remapped_ids[int(m[2]), int(m[4])]) + m[5], line)
        if global_aliases:
            line = symbol.sub(lambda m: global_aliases.get(m[0], m[0]), line)
        result.append(line)
    return assemble_dxil('\n'.join(result) + '\n').bytecode
