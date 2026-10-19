import re
from file_handlers.sdf.shader_graph import ShaderGraphError


_QUERIES = {
    'waveIsFirstLane': (110, 'wave_is_first', 'bool'),
    'waveGetLaneIndex': (111, 'wave_lane_index', 'uint'),
    'waveGetLaneCount': (112, 'wave_lane_count', 'uint'),
}
_PREDICATES = {
    'waveAnyTrue': (113, 'wave_any'), 'waveAllTrue': (114, 'wave_all'),
    'waveActiveBallot': (116, 'wave_ballot'), 'waveAllOp': (135, 'wave_count'),
    'wavePrefixOp': (136, 'wave_prefix_count'),
}
_TYPED = {
    'waveReadLaneFirst': (118, 2), 'waveReadLaneAt': (117, 3),
    'waveActiveAllEqual': (115, 2), 'waveActiveOp': (119, 4),
    'waveActiveBit': (120, 3), 'wavePrefixOp': (121, 4),
    'quadReadLaneAt': (122, 3), 'quadOp': (123, 3),
}


def collective_call(operation, arguments):
    def integer(text, prefix='i32 '):
        if not text.startswith(prefix) or not re.fullmatch(r'-?\d+', text[len(prefix):]):
            raise ShaderGraphError('The lane operation has a nonconstant native selector.')
        return int(text[len(prefix):])

    def declaration(count, opcode):
        if len(arguments) != count or integer(arguments[0]) != opcode:
            raise ShaderGraphError('The lane operation has an inconsistent declaration.')

    def operand(pin, index, prefix, kind):
        if not arguments[index].startswith(prefix):
            raise ShaderGraphError('The lane operand has a different native type.')
        return pin, kind, arguments[index][len(prefix):]

    if operation in _QUERIES:
        opcode, kind, result = _QUERIES[operation]
        declaration(1, opcode)
        return kind, result, ()
    if operation in _PREDICATES:
        opcode, kind = _PREDICATES[operation]
        declaration(2, opcode)
        return kind, 'bool', (operand('value', 1, 'i1 ', 'bool'),)
    base, _, suffix = operation.rpartition('.')
    if base not in _TYPED:
        return None
    if suffix not in {'f32', 'i32', 'i1'}:
        raise ShaderGraphError('The lane operation requires an additional precision domain.')
    opcode, count = _TYPED[base]
    declaration(count, opcode)
    prefix, domain = {'f32': ('float ', 'float'), 'i32': ('i32 ', 'uint'), 'i1': ('i1 ', 'bool')}[suffix]
    if base == 'waveReadLaneFirst':
        kind = 'wave_read_first'
    elif base in {'waveReadLaneAt', 'quadReadLaneAt'}:
        kind = 'wave_read_lane' if base == 'waveReadLaneAt' else 'quad_read_lane'
        return kind, domain, (operand('value', 1, prefix, domain), operand('lane', 2, 'i32 ', 'uint'))
    elif base == 'waveActiveAllEqual':
        kind = 'wave_all_equal'
    elif base == 'quadOp':
        kind = {0: 'quad_read_x', 1: 'quad_read_y', 2: 'quad_read_diagonal'}.get(integer(arguments[2], 'i8 '))
        if kind is None:
            raise ShaderGraphError('The quad read direction is invalid.')
    elif base == 'waveActiveBit':
        kind = {0: 'wave_bit_and', 1: 'wave_bit_or', 2: 'wave_bit_xor'}.get(integer(arguments[2], 'i8 '))
        if kind is None or suffix != 'i32':
            raise ShaderGraphError('The active-lane bit reduction mode or type is invalid.')
    else:
        modes = ({0: 'wave_sum', 1: 'wave_product', 2: 'wave_minimum', 3: 'wave_maximum'}
                 if base == 'waveActiveOp' else {0: 'wave_prefix_sum', 1: 'wave_prefix_product'})
        kind, unsigned = modes.get(integer(arguments[2], 'i8 ')), integer(arguments[3], 'i8 ')
        if kind is None or unsigned not in {0, 1} or suffix == 'i1' or suffix == 'f32' and unsigned:
            raise ShaderGraphError('The active-lane reduction mode or signedness is invalid.')
        domain = 'float' if suffix == 'f32' else 'uint' if unsigned else 'int'
    return kind, domain, (operand('value', 1, prefix, domain),)
