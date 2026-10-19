from dataclasses import replace
import re

from file_handlers.sdf.shader_graph import ShaderGraphError
from .legacy_loop_flow import joined_complementary_branches, lowered_switches, normalized_loop_exits, register_extent
from .legacy_register_flow import lower_indexable_temps, destination_count, destination_register
from .profiles.dxbc_recovery import DxbcInstruction as Instruction


def normalized_hull_phases(ir):
    if ir.stage != 'hs' or not ir.hull_phases:
        raise ShaderGraphError('A legacy hull program requires explicit phase ownership.')
    control, records, occupied, fork_outputs = None, [], set(), set()
    next_temp, joined = 0, False
    for index, phase in enumerate(ir.hull_phases):
        stop = ir.hull_phases[index+1].start if index+1 < len(ir.hull_phases) else len(ir.instructions)
        body = replace(ir, instructions=ir.instructions[phase.start:stop], hull_phases=(),
                       temp_count=phase.temp_count, indexable_temps=phase.indexable_temps)
        if phase.kind == 'control_point':
            control = body
            continue
        if phase.kind == 'fork' and joined:
            raise ShaderGraphError('A hull fork cannot run after a join phase.')
        joined |= phase.kind == 'join'
        for instance in range(phase.instance_count):
            indices, depth = {}, 0
            def specialize(operand):
                def instance_id(match):
                    if match[1].casefold() != phase.kind or ('v'+phase.kind+'instanceid') not in ir.primitive_system_inputs:
                        raise ShaderGraphError('A hull instance ID belongs to a different or undeclared phase.')
                    before = operand[:match.start()]
                    return str(instance) if before.count('[') > before.count(']') else f'l(0x{instance:08x})'
                operand = re.sub(r'\bv(Fork|Join)InstanceID(?:\.x+)?\b', instance_id, operand, flags=re.I)
                operand = re.sub(r'\[([^\[\]]+)\]', lambda m:'['+re.sub(r'\br\d+\.[xyzw]\b',
                    lambda register:str(indices.get(register[0],register[0])),m[1])+']',operand)
                operand = re.sub(r'\[(\d+)\s*\+\s*(\d+)\]', lambda m:f'[{int(m[1])+int(m[2])}]', operand)
                return re.sub(r'\bo\[(\d+)\]', r'o\1', operand)
            specialized = []
            for row in body.instructions:
                if row.opcode in {'if_z','if_nz','loop','switch'}:
                    depth += 1
                    indices.clear()
                elif row.opcode in {'endif','endloop','endswitch'}:
                    depth -= 1
                    indices.clear()
                elif row.opcode in {'else','case','default'}:
                    indices.clear()
                row = replace(row,operands=tuple(map(specialize,row.operands)))
                # FXC often moves the phase instance ID into a temp before indexing outputs.
                values = {}
                if depth == 0 and row.opcode == 'mov' and (dest := destination_register(row.operands[0])):
                    literal = re.fullmatch(r'l\((0x[0-9a-fA-F]{8})\)',row.operands[1])
                    scalar = int(literal[1],16) if literal else indices.get(row.operands[1])
                    if scalar is not None:
                        values = {dest[1]+'.'+axis:scalar for axis in dest[2] or 'xyzw'}
                for operand in row.operands[:destination_count(row)]:
                    if dest := destination_register(operand):
                        for axis in dest[2] or 'xyzw':
                            indices.pop(dest[1]+'.'+axis,None)
                indices.update(values)
                specialized.append(row)
            instance_body = replace(body, instructions=tuple(specialized))
            instance_body = normalized_loop_exits(joined_complementary_branches(lowered_switches(lower_indexable_temps(instance_body))))
            if (not instance_body.instructions or instance_body.instructions[-1].opcode != 'ret'
                    or any(row.opcode.startswith('ret') for row in instance_body.instructions[:-1])):
                raise ShaderGraphError('A hull phase needs a normalized terminating return.')
            extent = register_extent(instance_body)
            writes = set()
            for row in instance_body.instructions[:-1]:
                destinations = destination_count(row)
                operands = []
                for operand_index, operand in enumerate(row.operands):
                    destination = operand_index < destinations
                    if destination and (match := destination_register(operand)) and match[1].startswith('o'):
                        writes.update((match[1],axis) for axis in match[2] or 'xyzw')
                    elif destination and re.search(r'\bo\[',operand):
                        raise ShaderGraphError('A hull patch output index was not resolved by its declared phase instance.')
                    patch = re.fullmatch(r'vpc(\d+)(?:\.([xyzw]{1,4}))?',operand)
                    if patch:
                        lanes = {(f'o{patch[1]}',axis) for axis in patch[2] or 'xyzw'}
                        if destination or phase.kind != 'join' or not lanes <= fork_outputs:
                            raise ShaderGraphError('A hull join may only read completed fork output components.')
                        operand = 'o'+operand[3:]
                    elif not destination and re.search(r'\bo(?:\d+|\[)',operand):
                        raise ShaderGraphError('Native hull output registers are write-only.')
                    operand = re.sub(r'\br(\d+)\b',lambda m:f'r{next_temp+int(m[1])}',operand)
                    operands.append(operand)
                records.append(replace(row,operands=tuple(operands)))
            if writes & occupied:
                raise ShaderGraphError('Independent hull phase instances write overlapping patch components.')
            occupied |= writes
            if phase.kind == 'fork':
                fork_outputs |= writes
            next_temp += extent
    records.append(Instruction('ret',()))
    return control, replace(ir,instructions=tuple(records),temp_count=next_temp,indexable_temps=(),hull_phases=())


def passthrough_hull_behavior(contract, name):
    from .vanilla_function import _Builder
    execution = contract.primitive_execution
    if execution.input_control_points != execution.output_control_points:
        raise ShaderGraphError('A passthrough hull cannot change its control-point count.')
    builder = _Builder(contract)
    point = builder.invocation_input('output_control_point_id')
    outputs = []
    for field in contract.outputs:
        source = contract.input(*field.key)
        if (source is None or source.component_type != field.component_type
                or source.register != field.register or source.mask != field.mask):
            raise ShaderGraphError('An omitted hull control-point phase requires identical input/output registers and semantics.')
        outputs.append((field,[builder.stage_input(source,lane,vertex=point) for lane in range(field.mask.bit_count())]))
    return builder.finish(name,outputs)
