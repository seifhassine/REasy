import re

lazy from file_handlers.sdf.shader_graph import ShaderGraphError
from .native_local_memory import split
from .profiles.re_engine_dxil_recovery import LLVM_NAME, DxilSemanticRecord


_GLOBAL = '@' + LLVM_NAME
_LOCAL = re.compile('%' + LLVM_NAME)
_SCALAR = r'(?:float|half|i(?:1|8|16|32|64))'
_FUNCTION = re.compile(r'^define\b.*?\b(' + _SCALAR + r') (' + _GLOBAL + r')\((.*?)\)[^{]*\{$')
_CALL = re.compile(r'^(?:tail |musttail |notail )?call\b.*?\b(' + _SCALAR + r') (' + _GLOBAL + r')\((.*?)\)(?: #\d+)?(?:, ![\w.]+ !\d+)*$')
_PARAMETER = re.compile('(' + _SCALAR + r') (?:\w+ )*(%' + LLVM_NAME + ')$')


def inline_scalar_calls(events, ir):
    functions, current = {}, None
    for record in ir.records:
        if record.kind == 'function':
            current = None if record.text == '}' else _FUNCTION.fullmatch(record.text)
            if current:
                functions[current[2]] = (current[1], split(current[3]), [])
        elif current:
            functions[current[2]][2].append(record)
    if not functions:
        yield from events
        return
    reserved = {*_LOCAL.findall(record.text) for record in ir.records}
    serial = 0

    def expand(record, stack, recurse):
        nonlocal serial
        if record.opcode != 'call' or '@dx.op.' in record.text:
            yield record
            return
        body = record.text.split('=', 1)[1].strip() if record.result else record.text
        call = _CALL.fullmatch(body)
        if call is None or call[2] not in functions:
            yield record
            return
        if call[2] in stack or len(stack) >= 128:
            raise ShaderGraphError('Recursive native scalar calls cannot form a finite value graph.')
        result_type, parameters, records = functions[call[2]]
        arguments = split(call[3])
        if result_type != call[1] or len(parameters) != len(arguments) or not record.result:
            raise ShaderGraphError('A native scalar call disagrees with its function signature.')
        substitutions = {}
        for parameter, argument in zip(parameters, arguments):
            formal = _PARAMETER.fullmatch(parameter)
            actual = re.fullmatch('(' + _SCALAR + r') (.+)', argument)
            if formal is None or actual is None or formal[1] != actual[1]:
                raise ShaderGraphError('A native call requires non-scalar argument semantics.')
            substitutions[formal[2]] = actual[2]
        serial += 1
        returned, started = False, False
        for instruction in records:
            if instruction.kind == 'label' and not started:
                started = True
                continue
            if instruction.kind != 'instruction' or returned:
                raise ShaderGraphError('A native scalar function needs control-flow call recovery.')
            started = True
            if instruction.opcode in {'br', 'switch', 'unreachable', 'alloca', 'load', 'store', 'getelementptr'}:
                raise ShaderGraphError('A native scalar function needs control-flow or local-memory call recovery.')
            if instruction.result:
                while (identity := f'%scalar_call_{serial}') in reserved:
                    serial += 1
                reserved.add(identity)
                substitutions[instruction.result] = identity
                serial += 1
            text = _LOCAL.sub(lambda match: substitutions.get(match[0], match[0]), instruction.text)
            if instruction.opcode == 'ret':
                match = re.fullmatch(r'ret (' + _SCALAR + r') (.+)', text)
                if match is None or match[1] != result_type:
                    raise ShaderGraphError('A native scalar return disagrees with its declared type.')
                text = f'{record.result} = select i1 true, {result_type} {match[2]}, {result_type} {match[2]}'
                yield DxilSemanticRecord('instruction', text, 'select', record.result)
                returned = True
            else:
                result = substitutions.get(instruction.result, '')
                yield from recurse(DxilSemanticRecord('instruction', text, instruction.opcode, result), (*stack, call[2]), recurse)
        if not returned:
            raise ShaderGraphError('A native scalar function has no declared return value.')

    for event, record in events:
        if event == 'instruction':
            for instruction in expand(record, (), expand):
                yield event, instruction
        else:
            yield event, record
