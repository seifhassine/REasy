from dataclasses import replace
import itertools
import re

lazy from file_handlers.sdf.shader_graph import ShaderGraphError
lazy from .profiles.dxbc_recovery import DxbcInstruction as Instruction


_EXITS = {'break', 'breakc_nz', 'breakc_z', 'continue', 'continuec_nz', 'continuec_z'}


def joined_complementary_branches(ir):
    records, stack, regions = ir.instructions, [], {}
    for index, instruction in enumerate(records):
        if instruction.opcode in {'if_nz', 'if_z'}:
            stack.append([index, False])
        elif instruction.opcode == 'else' and stack:
            stack[-1][1] = True
        elif instruction.opcode == 'endif' and stack:
            start, has_else = stack.pop()
            regions[start] = (index, has_else)
    removed, replacements = set(), {}
    for start, (end, has_else) in sorted(regions.items()):
        if start in removed or has_else or end + 1 not in regions or regions[end + 1][1]:
            continue
        first, second = records[start], records[end + 1]
        if first.opcode == second.opcode or first.operands != second.operands or len(first.operands) != 1:
            continue
        predicate = re.fullmatch(r'(r\d+)\.([xyzw])', first.operands[0])
        if predicate is None:
            continue
        references = (match for item in records[start+1:end] for operand in item.operands
                      for match in re.finditer(r'\b(r\d+)(?:\.([xyzw]{1,4}))?\b', operand))
        if any(match[1] == predicate[1] and predicate[2] in (match[2] or 'xyzw') for match in references):
            continue
        replacements[end] = Instruction('else')
        removed.add(end+1)
    return replace(ir, instructions=tuple(replacements.get(i, item) for i,item in enumerate(records) if i not in removed)) if removed else ir


def register_extent(ir):
    named = [int(match) for instruction in ir.instructions for operand in instruction.operands
             for match in re.findall(r'\br(\d+)\b', operand)]
    return max(ir.temp_count, max(named, default=-1) + 1)


def lowered_switches(ir):
    records = ir.instructions
    if not any(instruction.opcode == 'switch' for instruction in records):
        return ir
    fresh = itertools.count(register_extent(ir))

    def temporary():
        return f'r{next(fresh)}.x'

    def switch_breaks(body):
        depth = 0
        for instruction in body:
            if instruction.opcode in {'loop', 'switch'}:
                depth += 1
            elif instruction.opcode in {'endloop', 'endswitch'}:
                depth -= 1
            elif depth == 0 and instruction.opcode in {'break', 'breakc_nz', 'breakc_z'}:
                yield instruction

    def lower(items):
        output, index = [], 0
        while index < len(items):
            instruction = items[index]
            if instruction.opcode != 'switch':
                output.append(instruction)
                index += 1
                continue
            depth, end = 0, index
            for end in range(index, len(items)):
                depth += {'switch': 1, 'endswitch': -1}.get(items[end].opcode, 0)
                if depth == 0:
                    break
            if depth or len(instruction.operands) != 1:
                raise ShaderGraphError('A legacy switch has no valid end marker.')
            clauses, labels, body, nested = [], [], [], 0
            for item in items[index + 1:end]:
                if nested == 0 and item.opcode in {'case', 'default'}:
                    if body:
                        clauses.append((labels, body))
                        labels, body = [], []
                    labels.append(item.operands[0] if item.opcode == 'case' else None)
                    continue
                if not labels:
                    raise ShaderGraphError('A legacy switch has instructions before its first label.')
                nested += {'switch': 1, 'endswitch': -1}.get(item.opcode, 0)
                body.append(item)
            if labels:
                clauses.append((labels, body))
            selector = temporary()
            output.append(Instruction('mov', (selector, instruction.operands[0])))
            conditions, branches = [], []
            for position, (clause_labels, clause_body) in enumerate(clauses):
                breaks = list(switch_breaks(clause_body))
                ends = bool(clause_body) and clause_body[-1].opcode == 'break' and breaks == [clause_body[-1]]
                if not ends and (breaks or position != len(clauses) - 1):
                    raise ShaderGraphError('A legacy switch case falls through or breaks early.')
                clause_body = lower(clause_body[:-1] if ends else clause_body)
                cases = [label for label in clause_labels if label is not None]
                condition = None
                for label in cases:
                    test = temporary()
                    output.append(Instruction('ieq', (test, selector, label)))
                    if condition is not None:
                        output.append(Instruction('or', (test, test, condition)))
                    condition = test
                if condition is not None:
                    conditions.append(condition)
                branches.append((condition, None in clause_labels, clause_body))
            for condition, has_default, clause_body in branches:
                if not clause_body:
                    continue
                if not has_default:
                    output.extend((Instruction('if_nz', (condition,)), *clause_body, Instruction('endif')))
                    continue
                others = [item for item in conditions if item != condition]
                if not others:
                    output.extend(clause_body)
                    continue
                matched = others[0]
                for item in others[1:]:
                    combined = temporary()
                    output.append(Instruction('or', (combined, matched, item)))
                    matched = combined
                output.extend((Instruction('if_z', (matched,)), *clause_body, Instruction('endif')))
            index = end + 1
        return output

    return replace(ir, instructions=tuple(lower(list(records))))


_EFFECTS = ('store', 'atomic', 'imm_atomic', 'discard', 'sync', 'emit', 'cut', 'ret')


def _continues(nodes):
    return any(node[0] == 'instruction' and node[1].opcode.startswith('continue')
               or node[0] == 'if' and (_continues(node[2]) or _continues(node[3])) for node in nodes)


def _pretested(body):
    for index, node in enumerate(body):
        if node[0] != 'instruction':
            return None
        opcode = node[1].opcode
        if opcode in {'breakc_nz', 'breakc_z'}:
            rest = body[index + 1:]
            # An always-breaking rest runs once; rotated, FXC sees a one-iteration loop (X3557).
            once = any(item[0] == 'instruction' and item[1].opcode == 'break' for item in rest)
            return None if once or _continues(rest) else (body[:index], node[1], rest)
        if opcode in _EXITS or opcode.startswith(_EFFECTS):
            return None
    return None


def normalized_loop_exits(ir, *, loop_origins=None):
    returns = [i for i, instruction in enumerate(ir.instructions) if instruction.opcode.startswith('ret')]
    early_returns = returns != [len(ir.instructions) - 1]
    if not early_returns and not any(i.opcode in _EXITS for i in ir.instructions):
        return ir
    records, cursor = ir.instructions, 0

    def block(stops=()):
        nonlocal cursor
        result = []
        while cursor < len(records):
            instruction = records[cursor]
            if instruction.opcode in stops:
                return result
            cursor += 1
            if instruction.opcode in {'if_z', 'if_nz'}:
                yes = block({'else', 'endif'})
                if cursor == len(records):
                    raise ShaderGraphError('The legacy branch has no end marker.')
                no = []
                if records[cursor].opcode == 'else':
                    cursor += 1
                    no = block({'endif'})
                if cursor == len(records) or records[cursor].operands:
                    raise ShaderGraphError('The legacy branch end is invalid.')
                cursor += 1
                result.append(('if', instruction, yes, no))
            elif instruction.opcode == 'loop':
                start = cursor - 1
                body = block({'endloop'})
                if cursor == len(records) or records[cursor].operands or instruction.operands:
                    raise ShaderGraphError('The legacy loop has no valid end marker.')
                end = cursor
                cursor += 1
                result.append(('loop', start, end, body))
            elif instruction.opcode in {'else', 'endif', 'endloop'}:
                raise ShaderGraphError('A legacy control-flow marker is outside its region.')
            else:
                result.append(('instruction', instruction))
        return result

    tree = block()
    fresh = itertools.count(register_extent(ir))

    def temporary():
        return f'r{next(fresh)}.x'

    def write(register, enabled):
        return Instruction('mov', (register, 'l(0xffffffff)' if enabled else 'l(0x00000000)'))

    function_live = temporary() if early_returns else None

    def emit(nodes, owners=()):
        owner = owners[-1] if owners else None
        output, can_exit, can_return = [], False, False
        for index, node in enumerate(nodes):
            exits = False
            if node[0] == 'if':
                _, condition, yes, no = node
                yes_code, yes_exits, yes_returns = emit(yes, owners)
                no_code, no_exits, no_returns = emit(no, owners)
                output.extend((condition, *yes_code, Instruction('else'), *no_code, Instruction('endif')))
                exits = yes_exits or no_exits
                can_return |= yes_returns or no_returns
            elif node[0] == 'loop':
                live, iteration = temporary(), temporary()
                pretested = _pretested(node[3])
                if pretested:
                    test, exit_, rest = pretested
                    body_nodes = [*rest, *(('instruction', replace(item[1])) for item in test), ('instruction', exit_)]
                    header = node[1] + len(test) + 2
                else:
                    body_nodes, header = node[3], node[1]
                body, _, body_returns = emit(body_nodes, (*owners, (live, iteration)))
                opening = Instruction('loop')
                if loop_origins is not None:
                    loop_origins[id(opening)] = (header, node[2], body_returns)
                loop = (write(live, True), write(iteration, True), opening, write(iteration, True),
                        *body, Instruction('breakc_z', (live,)), Instruction('endloop'))
                if pretested:
                    guard = Instruction('if_z' if exit_.opcode == 'breakc_nz' else 'if_nz', exit_.operands)
                    output.extend((*(item[1] for item in test), guard, *loop, Instruction('endif')))
                else:
                    output.extend(loop)
                exits = body_returns
                can_return |= body_returns
            else:
                instruction = node[1]
                if early_returns and instruction.opcode in {'ret', 'retc_nz', 'retc_z'}:
                    conditional = instruction.opcode != 'ret'
                    if len(instruction.operands) != int(conditional) or instruction.precise_mask or instruction.texel_offset:
                        raise ShaderGraphError('A return has invalid native operands or modifiers.')
                    if conditional:
                        output.append(Instruction('if_nz' if instruction.opcode.endswith('_nz') else 'if_z', instruction.operands))
                    output.append(write(function_live, False))
                    for active_loop, active_iteration in owners:
                        output.extend((write(active_loop, False), write(active_iteration, False)))
                    if conditional:
                        output.append(Instruction('endif'))
                    exits, can_return = True, True
                elif instruction.opcode not in _EXITS:
                    output.append(instruction)
                    continue
                else:
                    if owner is None:
                        raise ShaderGraphError('A loop exit appears outside a loop.')
                    conditional = instruction.opcode.endswith(('_nz', '_z'))
                    if len(instruction.operands) != int(conditional) or instruction.precise_mask or instruction.texel_offset:
                        raise ShaderGraphError('A loop exit has invalid native operands or modifiers.')
                    if conditional:
                        output.append(Instruction('if_nz' if instruction.opcode.endswith('_nz') else 'if_z', instruction.operands))
                    output.append(write(owner[1], False))
                    if instruction.opcode.startswith('break'):
                        output.append(write(owner[0], False))
                    if conditional:
                        output.append(Instruction('endif'))
                    exits = True
            can_exit |= exits
            if exits and index + 1 < len(nodes):
                tail, _, tail_returns = emit(nodes[index + 1:], owners)
                can_return |= tail_returns
                output.extend((Instruction('if_nz', (owner[1] if owner else function_live,)), *tail, Instruction('endif')))
                break
        return output, can_exit, can_return

    lowered, _, _ = emit(tree)
    if early_returns:
        lowered = [write(function_live, True), *lowered, Instruction('ret')]
    return replace(ir, instructions=tuple(lowered))
