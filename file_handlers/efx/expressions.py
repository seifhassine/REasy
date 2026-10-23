"""Safe EFX postfix compiler, decompiler and structural validator.

Native token order/arity: REE-Lib EfxExpressionParser.cs / ExpressionTree.cs.
No Python evaluation or attribute access is allowed.
"""
import ast
import math

from utils.hash_util import murmur3_hash

FUNCTIONS = {name: (i, arity) for name, i, arity in (
    ('sin', 0, 1), ('cos', 1, 1), ('asin', 2, 1), ('acos', 3, 1), ('floor', 4, 1),
    ('ceil', 5, 1), ('log', 6, 1), ('log10', 7, 1), ('exp', 8, 1), ('abs', 9, 1),
    ('saturate', 10, 1), ('sindeg', 11, 1), ('cosdeg', 12, 1), ('lerp', 15, 3),
    ('clamp', 16, 3), ('smoothstep', 17, 3), ('min', 18, 2), ('max', 19, 2), ('pow', 20, 2), ('remap', 21, 5))}
FUNCTION_IDS = {i: (name, arity) for name, (i, arity) in FUNCTIONS.items()}
OPERATORS = {ast.Pow: 0, ast.Mult: 1, ast.Div: 2, ast.Mod: 3, ast.Add: 4, ast.Sub: 5}
SYMBOLS = ('**', '*', '/', '%', '+', '-')


def compile_expression(text, global_parameters=()):
    try:
        tree = ast.parse(text, mode='eval')
    except (SyntaxError, RecursionError) as error:
        raise ValueError(f'Invalid expression: {error}') from error
    pending, count = [(tree, 0)], 0
    while pending:
        node, depth = pending.pop(); count += 1
        if count > 4096 or depth > 128:
            raise ValueError('Expression exceeds the syntax size/depth limit')
        pending.extend((child, depth+1) for child in ast.iter_child_nodes(node))
    globals_by_name = {p.name: p['utf8Hash'] for p in global_parameters}
    globals_by_hash = set(globals_by_name.values())
    parameters, components = {}, []

    def emit(kind, value):
        components.append({'type': kind, 'data': {'parameterHash' if kind == 4 else 'value': value}})

    def parameter(name, source, constant=0.):
        if type(name) not in (str, int) or isinstance(name, int) and not 0 <= name <= 0xffffffff:
            raise ValueError('Parameter names must be strings or unsigned hashes')
        hash_value = murmur3_hash(name.encode('utf-8')) if isinstance(name, str) else name
        if source == 0 and hash_value not in globals_by_hash:
            raise ValueError(f'Unknown global EFX parameter: {name}')
        item = {'parameterNameHash': hash_value, 'constantValue': float(constant), 'source': source}
        if hash_value in parameters and parameters[hash_value] != item:
            raise ValueError('Conflicting parameter sources for the same hash')
        parameters[hash_value] = item
        emit(4, hash_value)

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if not math.isfinite(node.value):
                raise ValueError('Expression literals must be finite')
            emit(0, float(node.value))
        elif isinstance(node, ast.Name):
            parameter(node.id, 0)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            visit(node.operand)
            if isinstance(node.op, ast.USub):
                emit(2, 0)
        elif isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            visit(node.left); visit(node.right); emit(1, OPERATORS[type(node.op)])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id.lower()
            if name in ('p', 'ext', 'const'):
                expected = 2 if name == 'const' else 1
                if len(node.args) != expected or not isinstance(node.args[0], ast.Constant):
                    raise ValueError(f'{name} requires a literal parameter name/hash' + (' and constant value' if expected == 2 else ''))
                constant = 0.
                if name == 'const':
                    arg = node.args[1]
                    if isinstance(arg, ast.UnaryOp) and not isinstance(arg.op, (ast.USub, ast.UAdd)):
                        raise ValueError('Constant value must be a signed number')
                    sign = -1 if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub) else 1
                    arg = arg.operand if isinstance(arg, ast.UnaryOp) else arg
                    if not isinstance(arg, ast.Constant) or type(arg.value) not in (int, float) or not math.isfinite(arg.value):
                        raise ValueError('Constant value must be a finite number')
                    constant = sign * arg.value
                parameter(node.args[0].value, {'p': 0, 'const': 1, 'ext': 2}[name], constant)
            else:
                if name not in FUNCTIONS or len(node.args) != FUNCTIONS[name][1]:
                    raise ValueError(f'Unknown function or invalid argument count: {name}')
                for arg in node.args:
                    visit(arg)
                emit(3, FUNCTIONS[name][0])
        else:
            raise ValueError(f'Unsupported expression syntax: {type(node).__name__}')

    if isinstance(tree.body, ast.BinOp) and isinstance(tree.body.op, ast.BitOr):
        visit(tree.body.right); visit(tree.body.left)
    else:
        visit(tree.body)
    return {'parameters': list(parameters.values()), 'components': components}


def _value(record, key):
    return record[key]


def expression_text(expression, global_parameters=()):
    import json
    names = {p['utf8Hash']: p.name for p in global_parameters}
    params = {p['parameterNameHash']: p for p in expression['parameters']}
    if len(params) != len(expression['parameters']):
        raise ValueError('Duplicate expression parameter hashes')
    stack = []
    for component in expression['components']:
        kind, payload = component['type'], component['data']
        if kind == 0:
            stack.append(repr(payload['value']))
        elif kind == 4:
            hash_value = payload['parameterHash']
            if hash_value not in params:
                raise ValueError(f'Expression uses undeclared parameter {hash_value}')
            param = params[hash_value]
            source = param['source']
            if source not in (0, 1, 2):
                raise ValueError(f'Unresolved expression source {source}')
            name = json.dumps(names[hash_value], ensure_ascii=False) if source == 0 and hash_value in names else str(hash_value)
            stack.append(f"{('p', 'const', 'ext')[source]}({name}" + (f", {param['constantValue']!r})" if source == 1 else ')'))
        else:
            value = payload['value']
            if kind == 1 and 0 <= value < len(SYMBOLS):
                arity = 2
            elif kind == 2 and value == 0:
                arity = 1
            elif kind == 3 and value in FUNCTION_IDS:
                arity = FUNCTION_IDS[value][1]
            else:
                raise ValueError(f'Unknown expression token: {kind}/{value}')
            if len(stack) < arity:
                raise ValueError('Expression stack underflow')
            args = stack[-arity:]; del stack[-arity:]
            if kind == 1:
                stack.append(f'({args[0]} {SYMBOLS[value]} {args[1]})')
            elif kind == 2:
                stack.append(f'(-{args[0]})')
            else:
                stack.append(f"{FUNCTION_IDS[value][0]}({', '.join(args)})")
    if len(stack) not in (1, 2):
        raise ValueError(f'Expression requires one or two root values, found {len(stack)}')
    return ' | '.join(reversed(stack))


def evaluate_expression(expression, values):
    """Deterministic numeric preview; external values must be supplied explicitly."""
    expression_text(expression)
    params = {p['parameterNameHash']: p for p in expression['parameters']}
    clamp = lambda x, lo, hi: min(max(x, lo), hi)
    def smoothstep(lo, hi, value):
        t = clamp((value - lo) / (hi - lo), 0, 1)
        return t * t * (3 - 2 * t)
    functions = {0: math.sin, 1: math.cos, 2: math.asin, 3: math.acos, 4: math.floor, 5: math.ceil,
                 6: math.log, 7: math.log10, 8: math.exp, 9: abs, 10: lambda v: clamp(v, 0, 1),
                 11: lambda v: math.sin(math.radians(v)), 12: lambda v: math.cos(math.radians(v)),
                 15: lambda a, b, t: a + (b-a)*t, 16: clamp, 17: smoothstep, 18: min, 19: max,
                 20: math.pow, 21: lambda x, a, b, c, d: c + (x-a)/(b-a)*(d-c)}
    binary = (math.pow, lambda a,b: a*b, lambda a,b: a/b, math.fmod, lambda a,b: a+b, lambda a,b: a-b)
    stack = []
    for component in expression['components']:
        kind, payload = component['type'], component['data']
        if kind == 0:
            stack.append(payload['value'])
        elif kind == 4:
            h = payload['parameterHash']; param = params[h]
            if param['source'] == 1:
                stack.append(param['constantValue'])
            elif h in values:
                stack.append(values[h])
            else:
                raise ValueError(f'Missing preview value for parameter {h}')
        else:
            value = payload['value']
            arity = 2 if kind == 1 else 1 if kind == 2 else FUNCTION_IDS[value][1]
            args = stack[-arity:]; del stack[-arity:]
            stack.append(binary[value](*args) if kind == 1 else -args[0] if kind == 2 else functions[value](*args))
    return stack[0] if len(stack) == 1 else tuple(reversed(stack))
