import re
from collections.abc import Set

from file_handlers.sdf.shader_graph import ShaderGraphError
from .profiles.re_engine_dxil_recovery import DxilSemanticRecord


LABEL = r'[A-Za-z0-9$._-]+'


def phi_rows(records):
    result, ordinary = [], False
    for record in records[:-1]:
        if record.opcode != 'phi':
            ordinary = True
            continue
        if ordinary:
            raise ShaderGraphError('A phi must precede ordinary block instructions.')
        match = re.fullmatch(r'%' + LABEL + r' = phi (float|half|i64|i32|i16|i8|i1) (.+)', record.text)
        if match is None:
            raise ShaderGraphError('This phi needs a precision/aggregate behavior type.')
        pattern = r'\[\s*([^,\[\]]+),\s*%(' + LABEL + r')\s*\]'
        edges = re.findall(pattern, match[2])
        incoming = {}
        for value, edge in edges:
            value = value.strip()
            if edge in incoming and incoming[edge] != value:
                raise ShaderGraphError('A repeated DXIL predecessor has conflicting phi values.')
            incoming[edge] = value
        # LLVM repeats a predecessor in a PHI for each switch edge to the same successor.
        if re.sub(pattern, '', match[2]).strip(' ,') or not edges:
            raise ShaderGraphError('A DXIL phi has an invalid edge list.')
        result.append((record.result, match[1], incoming))
    return result


class _DominatorSet(Set):
    __slots__ = ('bits', 'labels', 'positions')

    def __init__(self, bits, labels, positions):
        self.bits, self.labels, self.positions = bits, labels, positions

    def __contains__(self, label):
        position = self.positions.get(label)
        return position is not None and bool(self.bits & (1 << position))

    def __len__(self):
        return self.bits.bit_count()

    def __iter__(self):
        bits = self.bits
        while bits:
            bit = bits & -bits
            yield self.labels[bit.bit_length() - 1]
            bits ^= bit

def predecessor_map(labels, successors):
    result = {label: set() for label in labels}
    for source, targets in successors.items():
        for target in targets:
            result.setdefault(target, set()).add(source)
    return result


def dominator_sets(blocks, predecessors, *, unreachable=False):
    labels = tuple(blocks)
    positions = {label: index for index, label in enumerate(labels)}
    full = (1 << len(labels)) - 1
    dominators = {label: (1 if index == 0 else full) for index, label in enumerate(labels)}
    changed = True
    while changed:
        changed = False
        for index, label in enumerate(labels[1:], 1):
            if predecessors[label]:
                shared = full
                for predecessor in predecessors[label]:
                    shared &= dominators[predecessor]
            elif unreachable:
                shared = 0
            else:
                raise ShaderGraphError('Unreachable DXIL blocks need explicit execution-flow validation.')
            candidate = (1 << index) | shared
            if candidate != dominators[label]:
                dominators[label] = candidate
                changed = True
    return {label: _DominatorSet(mask, labels, positions) for label, mask in dominators.items()}


def reachable_blocks(start, successors, stop=None):
    seen, pending = set(), [start]
    while pending:
        block = pending.pop()
        if block in seen or block == stop:
            continue
        seen.add(block)
        pending.extend(successors.get(block, ()))
    return seen


def region_postorder(start, edges, cycle_error, exit_error):
    visiting, visited, order = set(), set(), []
    pending = [(start, False)]
    while pending:
        label, finished = pending.pop()
        if finished:
            visiting.remove(label)
            visited.add(label)
            order.append(label)
            continue
        if label in visiting:
            raise ShaderGraphError(cycle_error)
        if label in visited:
            continue
        if label not in edges:
            raise ShaderGraphError(exit_error)
        visiting.add(label)
        pending.append((label, True))
        pending.extend((target, False) for target in reversed(edges[label]))
    return order


def region_postdominators(order, edges, dead=frozenset()):
    postdominators = {}
    for label in order:
        targets = edges[label]
        live = [postdominators[target] for target in targets if postdominators[target] is not None]
        if label in dead or targets and not live:
            postdominators[label] = None
        else:
            postdominators[label] = {label} | (set.intersection(*live) if live else set())
    return postdominators


def flow_loops(blocks, successors):
    predecessors = predecessor_map(blocks, successors)
    dominators = dominator_sets(blocks, predecessors)
    loops = {}
    for latch, targets in successors.items():
        for header in targets:
            if header not in dominators[latch]:
                continue
            loop = loops.setdefault(header, dict(members={header}, latches=set()))
            loop['latches'].add(latch)
            pending = [latch]
            while pending:
                member = pending.pop()
                if member not in loop['members']:
                    loop['members'].add(member)
                    pending.extend(predecessors[member])
    for loop in loops.values():
        loop['exits'] = {(source, target) for source in loop['members'] for target in successors[source] if target not in loop['members']}
    for loop in loops.values():
        for other in loops.values():
            if loop['members'] & other['members'] and not (loop['members'] <= other['members'] or other['members'] <= loop['members']):
                raise ShaderGraphError('Overlapping irreducible loops need explicit execution ownership.')
    return loops, predecessors, dominators


def _phi(name, domain, edges):
    text = f'{name} = phi {domain} ' + ', '.join(f'[ {value}, %{edge} ]' for edge, value in edges.items())
    return DxilSemanticRecord('instruction', text, 'phi', name)


def _scalar_result_type(record, types=None):
    body = record.text.partition('=')[2].strip()
    if record.opcode == 'extractvalue':
        from .native_local_memory import split
        operands = split(body.removeprefix('extractvalue '))
        typed = re.fullmatch(r'(%[-\w.$]+) %[-\w.$]+',operands[0]) if operands else None
        if typed is None or len(operands)<2 or any(not index.isdigit() for index in operands[1:]):
            raise ShaderGraphError('A loop projection needs a declared aggregate type and constant field indices.')
        domain = typed[1]
        for index in map(int,operands[1:]):
            domain = (types or {}).get(domain,domain)
            if domain.startswith('{') and domain.endswith('}'):
                fields = split(domain[1:-1])
                if index >= len(fields):
                    raise ShaderGraphError('A loop projection is outside its aggregate declaration.')
                domain = fields[index]
            elif (array := re.fullmatch(r'\[(\d+) x (.+)\]',domain)) and index < int(array[1]):
                domain = array[2]
            else:
                raise ShaderGraphError('A loop projection has an unidentified aggregate layout.')
        if domain not in {'float','half','i64','i32','i16','i8','i1'}:
            raise ShaderGraphError('An escaping aggregate field is not a supported scalar type.')
        return domain
    if record.opcode in {'icmp', 'fcmp'}:
        return 'i1'
    if record.opcode in {'bitcast', 'fptoui', 'fptosi', 'uitofp', 'sitofp', 'zext', 'sext', 'trunc', 'fptrunc', 'fpext'}:
        match = re.search(r' to (float|half|i64|i32|i16|i8|i1)$', body)
    elif record.opcode == 'select':
        match = re.search(r', (float|half|i64|i32|i16|i8|i1) ', body)
    else:
        # Calls to inlined scalar helpers carry a calling convention (fastcc).
        match = re.match(r'(?:(?:tail |notail |musttail )?call|phi|load|fadd|fsub|fmul|fdiv|frem|add|sub|mul|udiv|sdiv|urem|srem|and|or|xor|shl|lshr|ashr)(?: (?:fast|nnan|ninf|nsz|arcp|contract|afn|reassoc|nuw|nsw|exact|fastcc|coldcc))* (float|half|i64|i32|i16|i8|i1)\b', body)
    if match is None:
        raise ShaderGraphError(f'An escaping loop value needs its explicit scalar type: {record.text}')
    return match[1]


def fresh_namer(blocks):
    symbols = set(blocks) | {record.result.removeprefix('%') for records in blocks.values() for record in records
                             if record.result}

    def fresh(prefix):
        name = prefix
        while name in symbols:
            name += '_'
        symbols.add(name)
        return name
    return fresh


def branch_to(target):
    return DxilSemanticRecord('instruction', f'br label %{target}', 'br')


_JUMP = re.compile(r'br label %(' + LABEL + r')(?:, !.*)?')
_BRANCH = re.compile(r'br i1 (\S+), label %(' + LABEL + r'), label %(' + LABEL + r')(?:, !.*)?')
_CONDITION = re.compile(r'%' + LABEL + r'|true|false')
_SWITCH = re.compile(r'switch i32 (%' + LABEL + r'|-?\d+), label %(' + LABEL + r') \[\s*(.*?)\s*\](?:, !.*)?')
_CASE = re.compile(r'i32 (-?\d+), label %(' + LABEL + r')')


def terminator(record):
    text = record.text
    if match := _JUMP.fullmatch(text):
        return 'jump', None, (match[1],), ()
    if match := _BRANCH.fullmatch(text):
        tested = match[1] if _CONDITION.fullmatch(match[1]) else None
        return 'branch' if tested else None, tested, (match[2], match[3]), ()
    if match := _SWITCH.fullmatch(text):
        cases = tuple((int(value) & 0xffffffff, target) for value, target in _CASE.findall(match[3]))
        if _CASE.sub('', match[3]).strip() or len({value for value, _target in cases}) != len(cases):
            raise ShaderGraphError('A DXIL switch has an invalid or duplicated case value.')
        return 'switch', match[1], (match[2],), cases
    if text == 'ret void':
        return 'return', None, (), ()
    return None, None, (), ()


def normalize_loop_boundaries(blocks, successors, conditions, edge_origins, *, types=None):
    fresh = fresh_namer(blocks)

    while True:
        loops, _predecessors, dominators = flow_loops(blocks, successors)
        pending = [(header, loop) for header, loop in loops.items()
                   if not (len(loop['latches']) == 1 and len(loop['exits']) == 1
                           and next(iter(loop['exits']))[0] in loop['latches'])]
        if not pending:
            return {header: dict(members=loop['members'], latch=next(iter(loop['latches'])), exit=next(iter(loop['exits']))[1])
                    for header, loop in loops.items()}
        header, loop = min(pending, key=lambda item: len(item[1]['members']))
        members, exits = loop['members'], sorted(loop['exits'])
        if not exits:
            raise ShaderGraphError('A non-terminating native loop needs an explicit non-returning execution region.')
        boundary = fresh(f'reasy_boundary_{header}')
        dispatch = fresh(f'reasy_exit_{header}')
        edges = [(source, header) for source in sorted(loop['latches'])] + exits
        bridges = {(source, target): fresh(f'reasy_edge_{source}_{target}') for source, target in edges}
        from .native_loop_values import prepare_loop_exports
        prepare_loop_exports(blocks,members,dominators,fresh)
        outside = {label: list(records) for label, records in blocks.items() if label not in members}
        definitions = {record.result: record for member in members for record in blocks[member] if record.result}
        definition_blocks = {record.result: member for member in members for record in blocks[member] if record.result}
        boundary_phis = {}  # name -> (domain, value per edge)

        def joined(domain, values, prefix):
            name = '%' + fresh(prefix)
            boundary_phis[name] = (domain, {edge: values.get(edge, 'undef') for edge in edges})
            return name

        continuing = [edge for edge in edges if edge[1] == header]
        states, pinned = [], {}

        def result(domain, values, prefix):
            values = {edge: value for edge, value in values.items() if value != 'undef'}
            for update, current in states:
                state_domain, row = boundary_phis[update]
                following = {row[edge] for edge in continuing}
                if state_domain != domain or not all(value in following or value == current for value in values.values()):
                    continue
                if any(edge in pinned[update] and row[edge] != value for edge, value in values.items()):
                    continue
                row.update(values)
                pinned[update].update(values)
                return update
            return joined(domain, values, prefix)

        continuation = joined('i1', {edge: 'true' if edge[1] == header else 'false' for edge in edges}, 'reasy_continue')
        header_phis = []
        for name, domain, values in phi_rows(blocks[header]):
            update = joined(domain, {edge: values[edge_origins.get(edge[0], edge[0])] if edge[1] == header else name for edge in edges}, 'reasy_update')
            states.append((update, name))
            pinned[update] = set(continuing)
            origins = {edge_origins.get(edge, edge) for edge in loop['latches']}
            retained = {edge: value for edge, value in values.items() if edge not in origins}
            retained[boundary] = update
            header_phis.append(_phi(name, domain, retained))
        blocks[header] = header_phis + [record for record in blocks[header] if record.opcode != 'phi']

        targets = sorted({target for _, target in exits})
        exit_bridges = {target: fresh(f'reasy_dispatch_{target}') for target in targets}
        for target in targets:
            rewritten = []
            for name, domain, values in phi_rows(blocks[target]):
                selected = result(domain, {edge: values[edge_origins.get(edge[0], edge[0])] for edge in exits if edge[1] == target}, 'reasy_result')
                removed = {edge_origins.get(source, source) for source, end in exits if end == target}
                rewritten.append(_phi(name, domain, {**{edge: value for edge, value in values.items() if edge not in removed}, exit_bridges[target]: selected}))
            blocks[target] = rewritten + [record for record in blocks[target] if record.opcode != 'phi']

        used = {value for label in outside for record in blocks[label]
                for value in re.findall(r'%' + LABEL, record.text.partition('=')[2] if record.result else record.text)}
        renames = {name: result(_scalar_result_type(definitions[name],types),
                   {edge: name for edge in exits if definition_blocks[name] in dominators[edge[0]]}, 'reasy_export')
                   for name in sorted(used & definitions.keys())}
        if renames:
            for label in outside:
                blocks[label] = [DxilSemanticRecord(record.kind,
                    re.sub(r'%' + LABEL, lambda match: renames.get(match[0], match[0]), record.text), record.opcode, record.result)
                    for record in blocks[label]]
                if isinstance(conditions.get(label), str):
                    conditions[label] = renames.get(conditions[label], conditions[label])
                elif label in conditions:
                    case, operand, value = conditions[label]
                    conditions[label] = (case, renames.get(operand, operand), value)
        for (source, target), bridge in bridges.items():
            successors[source] = tuple(bridge if end == target else end for end in successors[source])
            blocks[bridge], successors[bridge] = [branch_to(boundary)], (boundary,)
        if len(targets) == 1:
            blocks[dispatch], successors[dispatch] = [branch_to(exit_bridges[targets[0]])], (exit_bridges[targets[0]],)
        else:
            destination = joined('i32', {edge: str(targets.index(edge[1])) for edge in exits}, 'reasy_destination')
            current = dispatch
            for index, target in enumerate(targets[:-1]):
                following = fresh('reasy_exit_choice') if index + 2 < len(targets) else exit_bridges[targets[-1]]
                predicate = '%' + fresh('reasy_exit_test')
                blocks[current] = [DxilSemanticRecord('instruction', f'{predicate} = icmp eq i32 {destination}, {index}', 'icmp', predicate),
                                   DxilSemanticRecord('instruction', f'br i1 {predicate}, label %{exit_bridges[target]}, label %{following}', 'br')]
                successors[current] = (exit_bridges[target], following)
                conditions[current] = predicate
                current = following
        for update, _current in states:
            _domain, row = boundary_phis[update]
            following = {row[edge] for edge in continuing}
            if len(following) != 1:
                continue
            value = next(iter(following))
            for edge in exits:
                if edge not in pinned[update] and (value not in definition_blocks
                                                   or definition_blocks[value] in dominators[edge[0]]):
                    row[edge] = value
        blocks[boundary] = [_phi(name, domain, {bridges[edge]: value for edge, value in row.items()})
                            for name, (domain, row) in boundary_phis.items()]
        blocks[boundary].append(DxilSemanticRecord('instruction', f'br i1 {continuation}, label %{header}, label %{dispatch}', 'br'))
        successors[boundary], conditions[boundary] = (header, dispatch), continuation
        for target, bridge in exit_bridges.items():
            blocks[bridge], successors[bridge] = [branch_to(target)], (target,)
