from dataclasses import replace
import re

from file_handlers.sdf.shader_graph import ShaderGraphError
from .native_dxbc_precision import operand_precision


_WRITERS = frozenset(('mov movc add mul div min max mad dp2 dp3 dp4 frc sqrt round_ni round_pi rsq rcp exp log '
    'round_ne round_z deriv_rtx_coarse deriv_rty_coarse deriv_rtx_fine deriv_rty_fine lt ge eq ne '
    'iadd imul umul and or xor ishl ishr ushr imin imax umin umax not ineg countbits f32tof16 f16tof32 '
    'bfrev firstbit_lo firstbit_hi firstbit_shi ieq ine ilt ige ult uge ftoi ftou itof utof ubfe ibfe bfi imad '
    'udiv sincos swapc uaddc usubb resinfo resinfo_uint resinfo_rcpfloat bufinfo ld ldms ld_raw ld_structured '
    'ld_uav_typed sample sample_l sample_b sample_d sample_c sample_c_lz '
    'gather4 gather4_c gather4_po gather4_po_c').split())
_MULTI_RESULT = {'imul', 'umul', 'udiv', 'sincos', 'swapc', 'uaddc', 'usubb'}
_COMPONENTWISE = _WRITERS - {
    'dp2', 'dp3', 'dp4', 'resinfo', 'resinfo_uint', 'resinfo_rcpfloat', 'bufinfo',
    'ld', 'ldms', 'ld_raw', 'ld_structured', 'ld_uav_typed',
    'sample', 'sample_l', 'sample_b', 'sample_d', 'sample_c', 'sample_c_lz',
    'gather4', 'gather4_c', 'gather4_po', 'gather4_po_c',
}
_REGISTER = r'(?:[ro]\d+|odepth(?:ge|le)?|omask)'


def destination_register(operand):
    text, _, _ = operand_precision(operand)
    if text.casefold() in {'odepth', 'odepthge', 'odepthle', 'omask'}:
        text += '.x'
    return re.fullmatch('(' + _REGISTER + r')(?:\.([xyzw]{1,4}))?', text.casefold())


def destination_count(instruction):
    # FXC spells typed queries resinfo_indexable_uint, so the tag is not always a suffix.
    opcode = instruction.opcode.removesuffix('_sat').replace('_aoffimmi', '').replace('_indexable', '')
    clamped = opcode.endswith('_cl_s')
    opcode = opcode.removesuffix('_cl_s')
    return 2 if opcode in _MULTI_RESULT or clamped else 1 if opcode in _WRITERS or opcode.startswith('imm_atomic_') else 0


def lower_indexable_temps(ir):
    if not ir.indexable_temps:
        return ir
    from .profiles.dxbc_recovery import DxbcInstruction as Instruction
    declarations = {index: (rows, components) for index, rows, components in ir.indexable_temps}
    slots, next_register = {}, ir.temp_count
    for index,(rows,components) in declarations.items():
        for row in range(rows):
            slots[index,row] = f'r{next_register}'
            next_register += 1
    undefined = f'r{next_register}'
    scratch_start = next_register + 1
    maximum = scratch_start
    instructions = []
    pattern = re.compile(r'\bx(\d+)\[([^\[\]]+)\](?:\.([xyzw]{1,4}))?')
    literal = lambda number:f'l(0x{number & 0xffffffff:08x})'
    for instruction in ir.instructions:
        next_register = scratch_start
        before, after = [], []
        def temporary():
            nonlocal next_register
            result = f'r{next_register}'
            next_register += 1
            return result

        def operand(text,destination):
            def row(match):
                index,address,mask = int(match[1]),match[2].strip(),match[3]
                if index not in declarations:
                    raise ShaderGraphError('An indexed temporary references an undeclared array.')
                rows,components = declarations[index]
                mask = mask or 'xyzw'[:components]
                if any('xyzw'.index(axis)>=components for axis in mask):
                    raise ShaderGraphError('A temporary access is outside its declared component extent.')
                if address.isdigit():
                    if int(address)>=rows:
                        raise ShaderGraphError('A static temporary access is outside its declared row extent.')
                    return slots[index,int(address)]+'.'+mask
                parts = [part.strip() for part in address.split('+')]
                registers = [part for part in parts if re.fullmatch(r'r\d+\.[xyzw]',part)]
                constants = [part for part in parts if re.fullmatch(r'-?\d+',part)]
                if len(registers)!=1 or len(constants)>1 or len(parts)!=len(registers)+len(constants):
                    raise ShaderGraphError('A temporary index needs a scalar register and optional immediate displacement.')
                address_value = temporary()+'.x'
                before.append(Instruction('iadd',(address_value,registers[0],literal(int(constants[0])))) if constants
                              else Instruction('mov',(address_value,registers[0])))
                writing = destination and text[:match.start()].count('[')==text[:match.start()].count(']')
                selected = temporary()
                condition = temporary()+'.x'
                if not writing:
                    before.append(Instruction('mov',(selected+'.'+'xyzw'[:components],undefined+'.'+'xyzw'[:components])))
                for position in range(rows):
                    target = after if writing else before
                    target.append(Instruction('ieq',(condition,address_value,literal(position))))
                    target.append(Instruction('movc',(
                        (slots[index,position] if writing else selected)+'.'+(mask if writing else 'xyzw'[:components]),
                        condition,(selected if writing else slots[index,position])+'.'+(mask if writing else 'xyzw'[:components]),
                        (slots[index,position] if writing else selected)+'.'+(mask if writing else 'xyzw'[:components]))))
                return selected+'.'+mask
            while pattern.search(text):
                text = pattern.sub(row,text)
            return text

        operands = tuple(operand(text,index<destination_count(instruction)) for index,text in enumerate(instruction.operands))
        instructions.extend((*before,replace(instruction,operands=operands),*after))
        maximum = max(maximum,next_register)
    return replace(ir,instructions=tuple(instructions),indexable_temps=(),temp_count=maximum)


def _references(text):
    return {(match[1], 'xyzw'.index(axis)) for match in re.finditer(r'\b(' + _REGISTER + r')(?:\.([xyzw]{1,4}))?\b', text.casefold())
            for axis in match[2] or ('x' if not match[1][-1].isdigit() else 'xyzw')}


def source_references(instruction, index, *, result_lanes=None):
    text, _, _ = operand_precision(instruction.operands[index])
    opcode = instruction.opcode.removesuffix('_sat')
    if opcode in _COMPONENTWISE:
        lanes = result_lanes if result_lanes is not None else {
            lane for operand in instruction.operands[:destination_count(instruction)]
            for _, lane in _references(operand_precision(operand)[0])}
    elif opcode in {'dp2', 'dp3', 'dp4'}:
        lanes = set(range(int(opcode[-1])))
    elif opcode in {'if_nz', 'if_z', 'breakc_nz', 'breakc_z', 'continuec_nz', 'continuec_z', 'retc_nz', 'retc_z', 'discard_nz', 'discard_z'}:
        lanes = {0}
    else:
        return _references(text)
    if '[' in text:
        return _references(text)
    result = set()
    for match in re.finditer(r'\b(' + _REGISTER + r')(?:\.([xyzw]{1,4}))?\b', text.casefold()):
        mask = match[2] or ('x' if not match[1][-1].isdigit() else 'xyzw')
        if len(mask) not in {1, 4}:
            return _references(text)
        result.update((match[1], 'xyzw'.index(mask[0 if len(mask) == 1 else lane])) for lane in lanes)
    return result


def pure_register_calculation(instruction):
    return instruction.opcode.removesuffix('_sat') in _COMPONENTWISE | {'dp2', 'dp3', 'dp4'}


def register_liveness(ir, output_lanes, *, stream_lanes=None):
    records = ir.instructions
    successors = [{i + 1} if i + 1 < len(records) else set() for i in range(len(records))]
    stack, owners, loops = [], {}, {}
    for i, instruction in enumerate(records):
        opcode = instruction.opcode
        owners[i] = next((start for kind, start, _ in reversed(stack) if kind == 'loop'), None)
        if opcode in {'if_nz', 'if_z', 'loop'}:
            stack.append(('loop' if opcode == 'loop' else 'if', i, None))
        elif opcode == 'else':
            if not stack or stack[-1][0] != 'if' or stack[-1][2] is not None:
                raise ShaderGraphError('A register-flow else has no matching branch.')
            _, start, _ = stack[-1]
            stack[-1] = ('if', start, i)
        elif opcode in {'endif', 'endloop'}:
            expected = 'if' if opcode == 'endif' else 'loop'
            if not stack or stack[-1][0] != expected:
                raise ShaderGraphError('The register-flow region is not properly nested.')
            _, start, alternate = stack.pop()
            if expected == 'loop':
                loops[start] = i
                successors[i] = {start + 1}
            else:
                successors[start].add((alternate if alternate is not None else i) + 1)
                if alternate is not None:
                    successors[alternate] = {i + 1}
        elif opcode == 'ret':
            successors[i] = set()
    if stack:
        raise ShaderGraphError('The register-flow region has no end marker.')
    for i, instruction in enumerate(records):
        if instruction.opcode in {'break', 'breakc_nz', 'breakc_z', 'continue', 'continuec_nz', 'continuec_z'}:
            if owners[i] is None:
                raise ShaderGraphError('A register-flow break has no enclosing loop.')
            target = owners[i] + 1 if instruction.opcode.startswith('continue') else loops[owners[i]] + 1
            successors[i] = {target} | (successors[i] if instruction.opcode.endswith(('_nz', '_z')) else set())
        if any(target >= len(records) for target in successors[i]):
            raise ShaderGraphError('Register flow reaches past the native entry.')

    reads, writes, component_reads = [], [], []
    for instruction in records:
        count = destination_count(instruction)
        defined, used = set(), set()
        for index, operand in enumerate(instruction.operands):
            text, _, _ = operand_precision(operand)
            destination = destination_register(text) if index < count else None
            if destination:
                defined.update(_references(text))
            else:
                used.update(source_references(instruction, index))
        if instruction.opcode in {'ret', 'retc_nz', 'retc_z'} and stream_lanes is None:
            used.update(output_lanes)
        if stream_lanes is not None and instruction.opcode in {'emit','emitthencut','emit_stream','emitthencut_stream'}:
            stream = int(instruction.operands[0][1:]) if instruction.operands else 0
            if stream not in stream_lanes:
                raise ShaderGraphError('Geometry liveness references an undeclared stream.')
            used.update(stream_lanes[stream])
            defined.update(output_lanes)
        reads.append(used)
        writes.append(defined)
        component_reads.append({lane: set().union(*(source_references(instruction, index, result_lanes={lane})
            for index in range(count, len(instruction.operands)))) for _, lane in defined}
            if pure_register_calculation(instruction) else None)
    before, after = [set() for _ in records], [set() for _ in records]
    changed = True
    while changed:
        changed = False
        for i in reversed(range(len(records))):
            outgoing = set().union(*(before[target] for target in successors[i]))
            used = reads[i]
            if component_reads[i] is not None:
                lanes = {lane for _, lane in outgoing & writes[i]}
                used = set().union(*(component_reads[i][lane] for lane in lanes))
            incoming = used | (outgoing - writes[i])
            changed |= incoming != before[i]
            before[i], after[i] = incoming, outgoing
    return before, after
