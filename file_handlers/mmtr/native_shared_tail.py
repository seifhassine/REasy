import re
from functools import lru_cache

from file_handlers.sdf.shader_graph import ShaderGraphError
from .native_loop_flow import (
    LABEL, _phi, _scalar_result_type, branch_to, flow_loops, fresh_namer, phi_rows, predecessor_map,
)
from .profiles.re_engine_dxil_recovery import LLVM_NAME, DxilSemanticRecord


_LOCAL = re.compile(r'%' + LABEL)
_CALLEE = re.compile('@(' + LLVM_NAME + r')\((.*)')


def convergence_sensitive_calls(ir):
    def direct(match):
        if match is None or not match[1].startswith('dx.op.'):
            return False
        operation = match[1][6:].split('.')[0]
        if operation in {'waveGetLaneIndex', 'waveGetLaneCount'}:
            return False
        if operation.startswith(('wave', 'quad', 'barrier', 'textureGather')):
            return True
        if operation in {'sample', 'sampleBias', 'sampleCmp', 'sampleCmpBias', 'calculateLOD'}:
            return True
        return operation == 'unary' and re.match(r'i32 (83|84|85|86)(?=\s*[,\)])', match[2]) is not None

    dependencies, sensitive, current = {}, set(), None
    for record in ir.records:
        if record.kind == 'function':
            match = _CALLEE.search(record.text) if record.text != '}' else None
            current = match[1] if match is not None else None
            if current is not None:
                dependencies.setdefault(current, set())
        elif current is not None and record.kind == 'instruction' and record.opcode == 'call':
            match = _CALLEE.search(record.text)
            if match is not None:
                dependencies[current].add(match[1])
                if direct(match):
                    sensitive.add(current)
    changed = True
    while changed:
        changed = False
        for name, called in dependencies.items():
            if name not in sensitive and called & sensitive:
                sensitive.add(name)
                changed = True

    @lru_cache(maxsize=None)
    def sensitive_call(record):
        match = _CALLEE.search(record.text)
        return direct(match) or match is not None and match[1] in sensitive

    return sensitive_call


def _escaping_scalar_type(record, types=None):
    domain = _scalar_result_type(record, types)
    body = record.text.partition('=')[2].strip()
    if ((record.opcode == 'call' and not body.partition('@')[0].rstrip().endswith(' ' + domain))
            or (record.opcode == 'load' and not re.match(r'load ' + domain + r'\s*,', body))
            or (record.opcode == 'phi' and not re.match(r'phi ' + domain + r'\s+\[', body))):
        raise ShaderGraphError('A shared-tail export needs a supported scalar value, not a pointer or aggregate.')
    return domain


def _contained_lifetimes(blocks, prefix, head, types):
    local_rows = [row for label in prefix - {head} for row in blocks[label]]
    if any(row.opcode == 'alloca' for row in local_rows):
        return False
    lifetimes = [row for row in local_rows if '@llvm.lifetime.' in row.text]
    if not lifetimes:
        return True
    from .native_local_memory import _LIFETIME, split
    rows = [(label, row) for label, records in blocks.items() for row in records]
    definitions = {row.result: row for _, row in rows if row.result}

    def pointer_source(row):
        body = row.text.partition('=')[2].strip()
        if row.opcode == 'bitcast':
            match = re.fullmatch(r'bitcast .+\* (%' + LABEL + r') to .+\*', body)
            return match[1] if match else None
        if row.opcode == 'getelementptr':
            parts = split(body.removeprefix('getelementptr ').removeprefix('inbounds '))
            match = re.fullmatch(r'.+\* (%' + LABEL + r')', parts[1]) if len(parts) >= 2 else None
            return match[1] if match else None
        return None

    allocations = set()
    for row in lifetimes:
        match = _LIFETIME.fullmatch(row.text)
        if match is None or not re.fullmatch(r'%' + LABEL, match[3]):
            return False
        name, visited = match[3], set()
        while name in definitions and name not in visited and name not in (types or {}):
            visited.add(name)
            definition = definitions[name]
            if definition.opcode == 'alloca':
                allocations.add(name)
                break
            name = pointer_source(definition)
        else:
            return False

    sources = {row.result: pointer_source(row) for _, row in rows
               if row.result and row.opcode in {'bitcast', 'getelementptr'}}
    pointers = set(allocations)
    changed = True
    while changed:
        changed = False
        for name, source in sources.items():
            if name not in pointers and source in pointers:
                pointers.add(name)
                changed = True
    for label, row in rows:
        body = row.text.partition('=')[2] if row.result else row.text
        if not pointers.intersection(_LOCAL.findall(body)):
            continue
        if row.result in pointers and sources.get(row.result) in pointers:
            continue
        if label not in prefix:
            return False
        if row.opcode in {'load', 'store'}:
            parts = split(body.strip().removeprefix(row.opcode + ' '))
            address = re.fullmatch(r'(?:float|half|i32|i16|i8|i1)\* (%' + LABEL + r')', parts[1]) if len(parts) >= 2 else None
            scalar = re.fullmatch(r'(?:float|half|i32|i16|i8|i1)' + (r' .+' if row.opcode == 'store' else ''), parts[0]) if parts else None
            if address and scalar and address[1] in pointers and not pointers.intersection(_LOCAL.findall(parts[0])):
                continue
        if row.opcode == 'call' and _LIFETIME.fullmatch(body):
            continue
        return False
    return True


def normalize_shared_tails(blocks, successors, conditions, edge_origins, loops, context, *, types=None):
    fresh = fresh_namer(blocks)

    def candidates():
        found = []
        for parent in (None, *loops):
            children, edges, postdominators = context(parent)
            descendants = {}

            def expanded(nodes):
                return set().union(*(children[node]['members'] if node in children else {node} for node in nodes))

            def visit(label, recurse):
                if label not in descendants:
                    descendants[label] = {label} | set().union(*(recurse(target, recurse) for target in edges[label]))
                return descendants[label]

            visit(parent or next(iter(blocks)), visit)
            predecessors = predecessor_map(edges, edges)
            for head, targets in edges.items():
                if head in children or len(targets) != 2:
                    continue
                joins = postdominators[head] - {head}
                if not joins:
                    continue
                join = max(joins, key=lambda node: len(postdominators[node]))
                for tail in (descendants[targets[0]] & descendants[targets[1]]) - descendants[join]:
                    suffix = descendants[tail] - descendants[join]
                    if any(predecessors[node] - suffix for node in suffix - {tail}):
                        continue
                    prefix, pending = set(), [head]
                    while pending:
                        node = pending.pop()
                        if node in prefix or node in {tail, join}:
                            continue
                        prefix.add(node)
                        pending.extend(edges[node])
                    if prefix & suffix:
                        continue
                    if any(predecessors[node] - prefix for node in prefix - {head}):
                        continue
                    full_prefix = expanded(prefix)
                    if not _contained_lifetimes(blocks, full_prefix, head, types):
                        continue
                    exits = {(children[node]['latch'] if node in children else node, target)
                             for node in prefix for target in edges[node] if target not in prefix}
                    if not exits or any(target not in {tail, join} for _, target in exits):
                        continue
                    full_suffix = expanded(suffix)
                    weight = sum(sum(record.opcode not in {'phi', 'br', 'ret'} for record in blocks[node])
                                 for node in full_suffix)
                    if weight:
                        found.append((weight, head, tail, join, full_prefix, exits))
        return sorted(found, key=lambda row: (-row[0], row[1], row[2], row[3]))

    while True:
        choices = candidates()
        if not choices:
            break
        _, _, dominators = flow_loops(blocks, successors)
        changed = False
        for _, head, tail, join, prefix, exits in choices:
            definitions = {r.result: (node, r) for node in prefix - {head} for r in blocks[node] if r.result}
            used = {value for node, rows in blocks.items() if node not in prefix for record in rows
                    for value in _LOCAL.findall(record.text.partition('=')[2] if record.result else record.text)}
            escaping = sorted(used & definitions.keys())
            if set(escaping) & (types or {}).keys():
                continue
            try:
                domains = {name: _escaping_scalar_type(definitions[name][1], types) for name in escaping}
            except ShaderGraphError:
                continue
            boundary = fresh(f'reasy_tail_{tail}')
            bridges = {edge: fresh(f'reasy_tail_edge_{edge[0]}_{edge[1]}') for edge in sorted(exits)}
            boundary_phis = []

            def joined(domain, operands, label):
                name = '%' + fresh(label)
                boundary_phis.append(_phi(name, domain, {bridge: operands.get(edge, 'undef') for edge, bridge in bridges.items()}))
                return name

            reached = joined('i1', {edge: 'true' if edge[1] == tail else 'false' for edge in exits}, 'reasy_reached_tail')
            rewritten = {}
            for target in (tail, join):
                target_edges = {edge for edge in exits if edge[1] == target}
                removed = {edge_origins.get(source, source) for source, _ in target_edges}
                retained = {edge_origins.get(source, source) for source, targets in successors.items()
                            if target in targets and (source, target) not in target_edges}
                phis = []
                for name, domain, operands in phi_rows(blocks[target]):
                    try:
                        values = {edge: operands[edge_origins.get(edge[0], edge[0])] for edge in target_edges}
                    except KeyError as exc:
                        raise ShaderGraphError('A shared-tail phi is missing its original predecessor.') from exc
                    selected = joined(domain, values, 'reasy_tail_result')
                    phis.append(_phi(name, domain, {**{edge: value for edge, value in operands.items()
                                                     if edge not in removed or edge in retained}, boundary: selected}))
                rewritten[target] = phis + [r for r in blocks[target] if r.opcode != 'phi']

            renames = {name: joined(domains[name], {edge: name for edge in exits
                           if definitions[name][0] in dominators[edge[0]]}, 'reasy_tail_export') for name in escaping}
            for node in tuple(blocks):
                if node in rewritten:
                    blocks[node] = rewritten[node]
                if node not in prefix and renames:
                    blocks[node] = [DxilSemanticRecord(record.kind,
                        _LOCAL.sub(lambda match: renames.get(match[0], match[0]), record.text), record.opcode, record.result)
                        for record in blocks[node]]
                    if isinstance(conditions.get(node), str):
                        conditions[node] = renames.get(conditions[node], conditions[node])
                    elif node in conditions:
                        case, operand, number = conditions[node]
                        conditions[node] = case, renames.get(operand, operand), number
            for (source, target), bridge in bridges.items():
                successors[source] = tuple(bridge if other == target else other for other in successors[source])
                blocks[bridge], successors[bridge] = [branch_to(boundary)], (boundary,)
                for loop in loops.values():
                    if loop['latch'] == source and loop['exit'] == target:
                        loop['exit'] = bridge
            blocks[boundary] = boundary_phis + [DxilSemanticRecord('instruction',
                f'br i1 {reached}, label %{tail}, label %{join}', 'br')]
            successors[boundary], conditions[boundary] = (tail, join), reached
            for loop in loops.values():
                if head in loop['members']:
                    loop['members'].update((boundary, *bridges.values()))
            context.cache_clear()
            changed = True
            break
        if not changed:
            break
    return predecessor_map(blocks, successors)
