import re

from file_handlers.sdf.shader_graph_indexed_value import MAX_INDEXED_VALUES


def case_value_table(head, join, blocks, successors, conditions, predecessors, phis, uses):
    current, selector, domain = head, None, None
    cases, visited, comparison_ids = {}, set(), set()
    first_comparison = None
    while current != join:
        if current in visited or current != head and len(predecessors[current]) != 1:
            return None
        visited.add(current)
        ordinary = [r for r in blocks[current][:-1] if r.opcode != 'phi']
        if not ordinary or current != head and (len(ordinary) != 1 or len(blocks[current]) != 2):
            return None
        comparison = ordinary[-1]
        targets = successors[current]
        if len(targets) != 2 or conditions.get(current) != comparison.result or uses.get(comparison.result) != 1:
            return None
        match = re.fullmatch(r'%[-\w.$]+ = (?:fcmp(?: (?:fast|nnan|ninf|nsz|arcp|contract|afn|reassoc))* oeq (float)|icmp eq (i32)) (%[-\w.$]+), ([0-9.eE+-]+)', comparison.text)
        if match is None:
            return None
        kind = 'float' if match[1] else 'uint'
        try:
            number = float(match[4]) if kind == 'float' else int(match[4])
            choice = int(number)
        except (ValueError, OverflowError):
            return None
        if number != choice or not 0 <= choice < MAX_INDEXED_VALUES or choice in cases:
            return None
        if selector is not None and (selector, domain) != (match[3], kind):
            return None
        selector, domain = match[3], kind
        first_comparison = first_comparison or comparison
        comparison_ids.add(comparison.result)
        target, edge = targets[0], current
        while target != join:
            if target in visited or len(blocks[target]) != 1 or len(successors[target]) != 1 or predecessors[target] != {edge}:
                return None
            visited.add(target)
            edge, target = target, successors[target][0]
        cases[choice] = edge
        default_edge, current = current, targets[1]
    if sorted(cases) != list(range(len(cases))) or len(cases) < 2:
        return None
    rows = phis(join)
    expected = {default_edge, *cases.values()}
    if not rows or len(expected) != len(cases) + 1:
        return None
    results = []
    for name, kind, edges in rows:
        if set(edges) != expected or edges[default_edge] != 'undef':
            return None
        choices = tuple(edges[cases[i]] for i in range(len(cases)))
        if any(value == 'undef' or value in comparison_ids for value in choices):
            return None
        results.append((name, kind, choices))
    return dict(selector=selector, domain=domain, results=tuple(results), comparison=first_comparison.result)


# Pure, total instructions (DXIL udiv/urem by zero and OOB buffer reads are defined) can be evaluated in every case.
_SPECULATABLE = frozenset({
    'bitcast', 'zext', 'sext', 'trunc', 'fptosi', 'fptoui', 'sitofp', 'uitofp', 'fpext', 'fptrunc',
    'add', 'sub', 'mul', 'udiv', 'urem', 'and', 'or', 'xor', 'shl', 'lshr', 'ashr',
    'fadd', 'fsub', 'fmul', 'fdiv', 'icmp', 'fcmp', 'select', 'extractvalue'})
_SPECULATABLE_CALLS = re.compile(
    r'.*@dx\.op\.(?:cbufferLoadLegacy|cbufferLoad|rawBufferLoad|bufferLoad|textureLoad|unary|binary|tertiary|'
    r'dot[234]|legacyF16ToF32|legacyF32ToF16|unaryBits|createHandle|createHandleFromBinding|annotateHandle)\.?[\w.]*\(')
_PHI_KINDS = {"float": "float", "half": "native_half", "i32": "uint", "i1": "bool"}


def immediate_postdominators(successors):
    exit_ = object()
    reverse = {label: [] for label in successors}
    reverse[exit_] = []
    for label, targets in successors.items():
        for target in targets or (exit_,):
            reverse.setdefault(target, []).append(label)
    order, seen, stack = [], {exit_}, [(exit_, iter(reverse[exit_]))]
    while stack:
        label, children = stack[-1]
        child = next(children, None)
        if child is None:
            order.append(label)
            stack.pop()
        elif child not in seen:
            seen.add(child)
            stack.append((child, iter(reverse.get(child, ()))))
    number = {label: index for index, label in enumerate(order)}
    dominator = {exit_: exit_}

    def intersect(a, b):
        while a != b:
            while number[a] < number[b]:
                a = dominator[a]
            while number[b] < number[a]:
                b = dominator[b]
        return a

    changed = True
    while changed:
        changed = False
        for label in reversed(order):
            if label is exit_:
                continue
            candidates = [target for target in (successors.get(label) or (exit_,)) if target in dominator]
            if not candidates:
                continue
            new = candidates[0]
            for other in candidates[1:]:
                new = intersect(other, new)
            if dominator.get(label) != new:
                dominator[label] = new
                changed = True
    return {label: (None if value is exit_ else value) for label, value in dominator.items() if label is not exit_}


def fold_value_switches(blocks):
    from .native_loop_flow import LABEL, branch_to, phi_rows, predecessor_map, terminator
    from .profiles.re_engine_dxil_recovery import DxilSemanticRecord

    def exits(label):
        kind, tested, direct, cases = terminator(blocks[label][-1])
        return kind, tested, direct, dict(cases)

    def targets(label):
        _kind, _tested, direct, cases = exits(label)
        return (*direct, *cases.values())

    if not any(terminator(records[-1])[0] == 'switch' for records in blocks.values()):
        return {}
    successors = {label: tuple(dict.fromkeys(targets(label))) for label in blocks}
    postdominator = immediate_postdominators(successors)
    predecessors = predecessor_map(blocks, successors)
    order = {label: index for index, label in enumerate(blocks)}
    tables = {}
    for head in tuple(blocks):
        if head not in blocks:
            continue
        shape = exits(head)
        join = postdominator.get(head)
        if shape[0] != 'switch' or not shape[1].startswith('%') or len(shape[3]) < 2 or join not in blocks:
            continue
        selector = shape[1]
        region, pending, valid = set(), list(successors[head]), True
        while pending and valid:
            label = pending.pop()
            if label == join or label in region:
                continue
            inner = exits(label)
            valid = (label != head and len(region) < MAX_INDEXED_VALUES
                     and (inner[0] == 'jump' or inner[0] == 'switch' and inner[1] == selector)
                     and predecessors[label] <= set(blocks) and all(
                         record.opcode == 'phi' or record.opcode in _SPECULATABLE
                         or record.opcode == 'call' and _SPECULATABLE_CALLS.match(record.text)
                         for record in blocks[label][:-1]))
            region.add(label)
            pending.extend(successors[label])
        if not valid or any(not predecessors[label] <= region | {head} for label in region | {join}):
            continue
        keys = set(shape[3])
        for label in region:
            inner = exits(label)
            if inner[0] == 'switch':
                keys |= set(inner[3])
        # Case keys are i32: order them signed, so -6..9 is one compact range.
        signed = sorted(key - (1 << 32) if key >= 1 << 31 else key for key in keys)
        low, count = signed[0], signed[-1] - signed[0] + 1
        if count > MAX_INDEXED_VALUES or 2 * len(keys) < count:
            continue
        rows = phi_rows(blocks[join])
        if not rows or any(domain not in _PHI_KINDS for _name, domain, _edges in rows):
            continue
        region_phis = {}
        for label in region:
            for name, _domain, edges in phi_rows(blocks[label]):
                region_phis[name] = (label, edges)
        if region_phis:
            read = {value for label, records in blocks.items() for record in records if record.opcode != 'phi'
                    for value in re.findall(r'%' + LABEL,
                                            record.text.partition('=')[2] if record.result else record.text)}
            if read & set(region_phis):
                continue

        def path(key):
            edges, label = [], head
            while label != join:
                inner = exits(label)
                target = inner[3].get(key, inner[2][0])
                edges.append((label, target))
                if len(edges) > len(region) + 1:
                    return None
                label = target
            return edges

        def resolve(value, edges):
            while value in region_phis:
                block, incoming = region_phis[value]
                entry = next((source for source, target in reversed(edges) if target == block), None)
                if entry not in incoming:
                    return None
                value = incoming[entry]
            return value

        outside = next(value for value in range(len(keys) + 1) if (low + value) & 0xffffffff not in keys)
        paths = {key: path(key) for key in keys}
        paths[None] = path((low + outside) & 0xffffffff)
        if any(edges is None for edges in paths.values()):
            continue
        results = []
        for name, domain, edges in rows:
            def value_for(key):
                entry = paths[key][-1][0]
                return resolve(edges[entry], paths[key]) if entry in edges else None
            fallback = value_for(None)
            choices = tuple(value_for((low + index) & 0xffffffff) if (low + index) & 0xffffffff in paths
                            else fallback for index in range(count))
            if fallback is None or None in choices or 'undef' in choices:
                break
            results.append((name, domain, choices, None if fallback == 'undef' else fallback))
        else:
            index = f'%reasy.switch.{head}'
            hoisted = [record for label in sorted(region, key=order.get) for record in blocks[label][:-1]
                       if record.opcode != 'phi']
            selected = {name: f'%reasy.case.{name[1:]}' for name, _domain, _choices, _fallback in results}
            operands = [f'i32 {index}'] + [f'{domain} {item}' for _name, domain, choices, fallback in results
                                           for item in (*choices, 'undef' if fallback is None else fallback)]
            table = f'call void @{TABLE_CALL}({", ".join(operands)})'
            blocks[head] = [*blocks[head][:-1], *hoisted,
                            DxilSemanticRecord('instruction', f'{index} = add i32 {selector}, {-low}',
                                               'add', index),
                            DxilSemanticRecord('instruction', table, 'call'),
                            branch_to(join)]
            for label in region:
                del blocks[label]
            blocks[join] = [*(DxilSemanticRecord('instruction', f'{name} = phi {domain} [ {selected[name]}, %{head} ]',
                                                 'phi', name) for name, domain, _choices, _fallback in results),
                            *(record for record in blocks[join] if record.opcode != 'phi')]
            tables[head] = tuple((selected[name], _PHI_KINDS[domain], count, fallback is not None)
                                 for name, domain, _choices, fallback in results)
    return tables


TABLE_CALL = 'reasy.case.table'


def table_event(record, shape):
    from .native_local_memory import split
    body = record.text[record.text.index('(') + 1:record.text.rindex(')')]
    operands = [operand.split(' ', 1)[1] for operand in split(body)]
    selector, cursor, results = operands[0], 1, []
    for name, kind, count, has_fallback in shape:
        choices, fallback = tuple(operands[cursor:cursor + count]), operands[cursor + count]
        cursor += count + 1
        results.append((name, kind, choices, fallback if has_fallback else None))
    return dict(selector=selector, domain='uint', comparison=None, results=tuple(results))
