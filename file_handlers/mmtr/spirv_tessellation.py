from dataclasses import dataclass

from .vanilla_function import VanillaFunctionError

OP_FUNCTION_CALL = 57
OP_VARIABLE = 59
OP_STORE = 62
OP_CONTROL_BARRIER = 224
OP_LOOP_MERGE = 246
OP_SELECTION_MERGE = 247
OP_LABEL = 248
OP_BRANCH = 249
OP_BRANCH_CONDITIONAL = 250
OP_SWITCH = 251
OP_RETURN = 253

# Storage classes: a function-scope variable is private to one invocation.
FUNCTION_STORAGE = 7

def hull_phase_boundary(instructions):
    rows = tuple(instructions)
    found = [index for index, row in enumerate(rows) if row.opcode == OP_CONTROL_BARRIER]
    if len(found) != 1:
        raise VanillaFunctionError(
            f'A hull phase split needs exactly one control barrier, found {len(found)}.')
    return found[0]


@dataclass(frozen=True)
class HullPhaseBodies:
    control: tuple
    patch: tuple


def _dispatch(rows):
    for index, row in enumerate(rows):
        if row.opcode == OP_SELECTION_MERGE:
            branch = rows[index + 1] if index + 1 < len(rows) else None
            if not row.values or branch is None or branch.opcode != OP_BRANCH_CONDITIONAL or len(branch.values) != 3:
                raise VanillaFunctionError('A hull phase dispatch must select its phases with a conditional branch.')
            return row.values[0], branch.values[1], branch.values[2]
    raise VanillaFunctionError('A hull patch phase needs its dispatch after the control barrier.')


def _block_rows(rows, label):
    start = next((index for index, row in enumerate(rows) if row.opcode == OP_LABEL and row.values[0] == label), None)
    if start is None:
        raise VanillaFunctionError('The hull patch phase has no basic block to lift.')
    end = next((index for index in range(start + 1, len(rows)) if rows[index].opcode == OP_LABEL), len(rows))
    return rows[start:end]


def _patch_dependencies(rows, boundary, patch):
    definitions = {row.result: index for index, row in enumerate(rows) if row.result is not None}
    needed, selected = set(), set()
    for row in patch:
        needed.update(operand.value for operand in row.operands if operand.kind == 'IdRef')
    while True:
        additions = {index for index in (definitions.get(value) for value in needed)
                     if index is not None and index < boundary} - selected
        if not additions:
            break
        selected |= additions
        for index in additions:
            row = rows[index]
            needed.update(operand.value for operand in row.operands if operand.kind == 'IdRef')
            if row.opcode == OP_VARIABLE and row.values[2] == FUNCTION_STORAGE:
                for other_index, other in enumerate(rows[:boundary]):
                    if other.opcode == OP_STORE and other.values[0] == row.result:
                        selected.add(other_index)
                        needed.update(operand.value for operand in other.operands if operand.kind == 'IdRef')
    return tuple(row for index, row in enumerate(rows) if index in selected)


def hull_phase_bodies(instructions):
    rows = tuple(instructions)
    boundary = hull_phase_boundary(rows)
    control, after = rows[:boundary], rows[boundary + 1:]
    merge, *targets = _dispatch(after)
    blocks = [label for label in targets if label != merge]
    if len(blocks) != 1:
        raise VanillaFunctionError('The hull phase dispatch must select exactly one patch-constant block.')
    patch = _patch_region(after, blocks[0], merge)
    returns = [row for row in rows if row.opcode == OP_RETURN]
    if not returns:
        raise VanillaFunctionError('A hull phase body needs a terminating return.')
    return HullPhaseBodies(control=(*control, returns[0]),
                           patch=(patch[0], *_patch_dependencies(rows, boundary, patch),
                                  *patch[1:], *_merge_block(after, merge)))


def _patch_region(rows, entry, merge):
    start = next((index for index, row in enumerate(rows) if row.opcode == OP_LABEL and row.values[0] == entry), None)
    end = next((index for index, row in enumerate(rows) if row.opcode == OP_LABEL and row.values[0] == merge), None)
    if start is None or end is None or end <= start:
        raise VanillaFunctionError('The hull patch-constant region must precede its dispatch merge.')
    region = rows[start:end]
    labels = {row.values[0] for row in region if row.opcode == OP_LABEL} | {merge}
    for row in region:
        if row.opcode == OP_FUNCTION_CALL:
            raise VanillaFunctionError(
                'The patch-constant phase of a tessellation stage must be inlined; OpFunctionCall is not modelled.')
        if row.opcode == OP_BRANCH:
            targets = row.values[:1]
        elif row.opcode == OP_BRANCH_CONDITIONAL:
            targets = row.values[1:3]
        elif row.opcode == OP_SWITCH:
            targets = row.values[1::2]
        elif row.opcode in {OP_LOOP_MERGE, OP_SELECTION_MERGE}:
            targets = row.values[:2] if row.opcode == OP_LOOP_MERGE else row.values[:1]
        else:
            continue
        if not set(targets) <= labels:
            raise VanillaFunctionError('The hull patch-constant region must complete into its dispatch merge.')
    return region


def _merge_block(rows, merge):
    block = _block_rows(rows, merge)
    if len(block) != 2 or block[-1].opcode != OP_RETURN:
        raise VanillaFunctionError('The hull phase dispatch merge must only return.')
    return block
