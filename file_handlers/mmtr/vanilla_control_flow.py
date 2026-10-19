from functools import lru_cache
import re

from file_handlers.sdf.shader_graph import ShaderGraphError
from .native_loop_flow import (
    LABEL, fresh_namer, normalize_loop_boundaries, phi_rows, reachable_blocks, region_postdominators,
    region_postorder, terminator,
)


def structured_dxil_records(ir, entry):
    blocks = {}
    current = None
    active = False
    next_slot = 0
    for record in ir.records:
        if record.kind == "function":
            if record.text == "}":
                if active:
                    break
            else:
                active = bool(re.search(r'@(?:"' + re.escape(entry) + r'"|' + re.escape(entry) + r')\(\)', record.text))
            continue
        if not active:
            continue
        if record.kind == "label":
            label = record.text.removesuffix(":")
            if current is not None and blocks[current]:
                raise ShaderGraphError("A DXIL block is missing its terminator.")
            if label in blocks:
                raise ShaderGraphError("DXIL block labels are duplicated.")
            current = label
            blocks[current] = []
            if label.isdigit():
                if int(label) != next_slot:
                    raise ShaderGraphError("DXIL unnamed block slots are inconsistent.")
                next_slot += 1
            continue
        if record.kind != "instruction":
            raise ShaderGraphError("Unexpected record in the DXIL entry.")
        if current is None:
            current = str(next_slot)
            next_slot += 1
            if current in blocks:
                raise ShaderGraphError("DXIL implicit block identity is duplicated.")
            blocks[current] = []
        if record.result.removeprefix("%").isdigit():
            if int(record.result[1:]) != next_slot:
                raise ShaderGraphError("DXIL unnamed SSA slots are inconsistent.")
            next_slot += 1
        blocks[current].append(record)
        if record.opcode in {"br", "ret", "switch", "unreachable"}:
            current = None
    if not blocks or current is not None:
        raise ShaderGraphError("The DXIL entry has no complete control-flow graph.")
    from .native_case_values import TABLE_CALL, fold_value_switches, table_event
    value_switches = fold_value_switches(blocks)

    successors, conditions, edge_origins = {}, {}, {}
    returns = []
    fresh = fresh_namer(blocks)

    for label, records in tuple(blocks.items()):
        if not records:
            raise ShaderGraphError("An empty DXIL block has no terminator.")
        kind, tested, targets, cases = terminator(records[-1])
        if kind == 'jump':
            successors[label] = targets
        elif kind == 'branch':
            conditions[label] = tested
            successors[label] = targets
        elif kind == 'return':
            successors[label] = ()
            returns.append(label)
        elif kind == 'switch':
            if not cases:
                successors[label] = targets
                continue
            # Partition switch keys so recovery depth grows logarithmically (helpers compile to hundreds of cases).
            pending = [(label, sorted(cases))]
            while pending:
                dispatch, choices = pending.pop()
                if len(choices) == 1:
                    value, target = choices[0]
                    conditions[dispatch] = ('case', tested, value)
                    successors[dispatch] = (target, targets[0])
                    continue
                middle = len(choices) // 2
                children = []
                for name, subset in (('lower', choices[:middle]), ('upper', choices[middle:])):
                    following = fresh(f'reasy_switch_{dispatch}_{name}')
                    blocks[following] = [records[-1]]
                    edge_origins[following] = label
                    children.append(following)
                    pending.append((following, subset))
                conditions[dispatch] = ('case_less', tested, choices[middle][0])
                successors[dispatch] = tuple(children)
        else:
            raise ShaderGraphError("This DXIL terminator needs a switch/ordered-exit behavior region.")
    if len(returns) > 1:
        exit_label = fresh('reasy_return')
        blocks[exit_label] = [blocks[returns[0]][-1]]
        successors[exit_label] = ()
        for label in returns:
            successors[label] = (exit_label,)
    for label in blocks:
        if any(target not in blocks for target in successors[label]):
            raise ShaderGraphError("A DXIL branch targets a missing block.")

    start = next(iter(blocks))
    if reachable_blocks(start, successors) != set(blocks):
        raise ShaderGraphError("Unreachable DXIL blocks need explicit execution-flow validation.")
    types = {record.text.partition(' = type ')[0]:record.text.partition(' = type ')[2]
             for record in ir.records if record.text.startswith('%') and ' = type ' in record.text}
    loops = normalize_loop_boundaries(blocks, successors, conditions, edge_origins,types=types)

    @lru_cache(maxsize=None)
    def context(parent=None):
        domain = loops[parent]['members'] if parent else set(blocks)
        children = dict(loops) if parent is None else {
            header: loop for header, loop in loops.items() if header != parent and loop['members'] < domain}
        owner = {}
        for header, loop in sorted(children.items(), key=lambda item: len(item[1]['members']), reverse=True):
            for member in loop['members']:
                owner.setdefault(member, header)
        visible = {label for label in domain if owner.get(label, label) == label}
        edges = {}
        for label in visible:
            if label in children:
                edges[label] = (children[label]['exit'],)
            elif parent and label == loops[parent]['latch']:
                edges[label] = ()
            else:
                edges[label] = successors[label]
            if any(target not in visible for target in edges[label]):
                raise ShaderGraphError('A loop region has an entry or exit outside its structured boundary.')
        order = region_postorder(parent or start, edges, 'Native cyclic flow needs an irreducible execution region.',
                                 'A loop region has an entry or exit outside its structured boundary.')
        return ({header: children[header] for header in visible if header in children}, edges,
                region_postdominators(order, edges))

    from .native_shared_tail import normalize_shared_tails
    predecessors = normalize_shared_tails(blocks, successors, conditions, edge_origins, loops, context, types=types)

    @lru_cache(maxsize=None)
    def phis(label):
        result = []
        for name, domain, edges in phi_rows(blocks[label]):
            if domain in {'i8', 'i16'}:
                mask = (1 << int(domain[1:])) - 1
                edges = {edge: str(int(value) & mask) if re.fullmatch(r'-?\d+', value) else value for edge, value in edges.items()}
            result.append((name, {"float": "float", "half": "native_half", "i64": "uint64_t", "i32": "uint", "i16": "uint", "i8": "uint", "i1": "bool"}[domain], edges))
        return tuple(result)

    def incoming(label, predecessor):
        try:
            return tuple((name, kind, edges[edge_origins.get(predecessor, predecessor)]) for name, kind, edges in phis(label))
        except KeyError as exc:
            raise ShaderGraphError("A DXIL phi is missing its actual predecessor.") from exc

    from collections import Counter
    from .native_case_values import case_value_table
    uses = Counter(value for records in blocks.values() for record in records
                   for value in re.findall(r'%' + LABEL, record.text.partition('=')[2] if record.result else record.text))

    def region(recurse, label, stop=None, predecessor=None, *, parent=None, ready_phis=False):
        child_loops, edges, postdominators = context(parent)
        while label != stop:
            if label in child_loops:
                loop = child_loops[label]
                latch = loop['latch']
                yield 'loop_begin', incoming(label, predecessor)
                if latch != label:
                    aliases = yield from recurse(recurse, label, latch, parent=label, ready_phis=True)
                    yield 'aliases', aliases
                for record in blocks[latch][:-1]:
                    if record.opcode != 'phi':
                        yield 'instruction', record
                definitions = {record.result for member in loop['members'] for record in blocks[member] if record.result}
                outside_uses = {value for other, records in blocks.items() if other not in loop['members']
                                for record in records for value in re.findall(r'%' + LABEL, record.text)}
                yield 'loop_end', dict(continuation=conditions[latch], when_true=successors[latch][0] == label,
                                      states=incoming(label, latch), exports=tuple(sorted(definitions & outside_uses)))
                predecessor, label = latch, loop['exit']
                ready_phis = False
                continue
            if not ready_phis:
                yield "aliases", incoming(label, predecessor)
            targets = edges[label]
            joins = postdominators[label] - {label} if len(targets) == 2 else set()
            join = max(joins, key=lambda target: len(postdominators[target])) if joins else None
            table = case_value_table(label, join, blocks, successors, conditions, predecessors, phis, uses) if join else None
            if table is not None:
                for record in blocks[label][:-1]:
                    if record.opcode != 'phi' and record.result != table['comparison']:
                        yield 'instruction', record
                yield 'case_values', table
                if join == stop:
                    return tuple((name, kind, name) for name, kind, _ in phis(join))
                label, ready_phis = join, True
                continue
            for record in blocks[label][:-1]:
                if label in value_switches and record.opcode == 'call' and TABLE_CALL in record.text:
                    yield 'case_values', table_event(record, value_switches[label])
                elif record.opcode != "phi":
                    yield "instruction", record
            if not targets:
                if stop is not None:
                    raise ShaderGraphError("An early return needs an ordered-exit behavior region.")
                yield "instruction", blocks[label][-1]
                return ()
            if len(targets) == 1:
                predecessor, label = label, targets[0]
                ready_phis = False
                continue
            if not joins:
                raise ShaderGraphError("Conditional exits need an ordered-exit behavior region.")
            yield "begin", conditions[label]
            left = yield from recurse(recurse, targets[0], join, label, parent=parent)
            yield "else", left
            right = yield from recurse(recurse, targets[1], join, label, parent=parent)
            yield "end", right
            if join == stop:
                return tuple((name, kind, name) for name, kind, _ in phis(join))
            label = join
            ready_phis = True
        return incoming(label, predecessor)

    from .native_shared_tail import convergence_sensitive_calls
    sensitive_call = convergence_sensitive_calls(ir)
    emitted_sensitive = set()
    for event, record in region(region, start):
        if event == 'instruction' and record.opcode == 'call' and sensitive_call(record):
            identity = id(record)
            if identity in emitted_sensitive:
                raise ShaderGraphError(
                    'A shared native lane or gradient operation needs a supported reconvergence boundary; '
                    'duplicating it across branch arms would change its active lanes.')
            emitted_sensitive.add(identity)
        yield event, record
