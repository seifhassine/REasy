from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

FLOAT_BASE_TYPES = ('float', 'min16float', 'min10float', 'half')
INTEGER_BASE_TYPES = ('int', 'uint', 'min16int', 'min16uint', 'int64_t', 'uint64_t')
NUMERIC_BASE_TYPES = (*FLOAT_BASE_TYPES, *INTEGER_BASE_TYPES, 'bool')
NUMERIC_VALUE_TYPES = tuple(base + suffix for base in NUMERIC_BASE_TYPES for suffix in ('', '2', '3', '4'))


def numeric_suffix(value_type):
    return value_type[-1] if value_type[-1:] in '234' else ''


def value_width(value_type):
    value = str(value_type)
    return int(value[-1]) if value[-1:].isdigit() else 1


def is_float_type(value_type):
    return value_type.rstrip('234') in FLOAT_BASE_TYPES
INTEGER_OPERATORS = {'bit_and': '&', 'bit_or': '|', 'bit_xor': '^', 'shift_left': '<<', 'shift_right': '>>', 'integer_remainder': '%'}
INTEGER_UNARY = {'bit_not': '~', 'count_bits': 'countbits', 'reverse_bits': 'reversebits', 'first_bit_low': 'firstbitlow', 'first_bit_high': 'firstbithigh'}
FLOAT_PREDICATES = ('is_finite', 'is_infinite', 'is_nan')
HALF_CONVERSIONS = {'pack_half': ('f32tof16', 'float', 'uint'), 'unpack_half': ('f16tof32', 'uint', 'float')}
WAVE_FUNCTIONS = {
    'wave_read_first': 'WaveReadLaneFirst', 'wave_sum': 'WaveActiveSum',
    'wave_product': 'WaveActiveProduct', 'wave_minimum': 'WaveActiveMin',
    'wave_maximum': 'WaveActiveMax',
}
QUAD_FUNCTIONS = {'quad_read_x': 'QuadReadAcrossX', 'quad_read_y': 'QuadReadAcrossY', 'quad_read_diagonal': 'QuadReadAcrossDiagonal'}
LANE_FUNCTIONS = {
    'wave_lane_index': ('WaveGetLaneIndex', 'query', 'uint'),
    'wave_lane_count': ('WaveGetLaneCount', 'query', 'uint'),
    'wave_is_first': ('WaveIsFirstLane', 'query', 'bool'),
    'wave_any': ('WaveActiveAnyTrue', 'predicate', 'bool'),
    'wave_all': ('WaveActiveAllTrue', 'predicate', 'bool'),
    'wave_count': ('WaveActiveCountBits', 'predicate', 'uint'),
    'wave_prefix_count': ('WavePrefixCountBits', 'predicate', 'uint'),
    'wave_ballot': ('WaveActiveBallot', 'predicate', 'uint4'),
    'wave_all_equal': ('WaveActiveAllEqual', 'numeric', 'bool'),
    'wave_prefix_sum': ('WavePrefixSum', 'numeric', None),
    'wave_prefix_product': ('WavePrefixProduct', 'numeric', None),
    'wave_bit_and': ('WaveActiveBitAnd', 'integer', None),
    'wave_bit_or': ('WaveActiveBitOr', 'integer', None),
    'wave_bit_xor': ('WaveActiveBitXor', 'integer', None),
    'wave_read_lane': ('WaveReadLaneAt', 'indexed', None),
    'quad_read_lane': ('QuadReadLaneAt', 'indexed', None),
}
COLLECTIVE_NODES = WAVE_FUNCTIONS.keys() | QUAD_FUNCTIONS.keys() | LANE_FUNCTIONS.keys()
INTEGER_MATH_NODES = frozenset(INTEGER_OPERATORS) | frozenset(INTEGER_UNARY) | {'multiply_high', 'convert', 'reinterpret', 'extract_bits', 'insert_bits', 'extract_bits_dynamic', 'insert_bits_dynamic'}
TYPED_MATH_NODES = INTEGER_MATH_NODES | frozenset(FLOAT_PREDICATES) | COLLECTIVE_NODES | HALF_CONVERSIONS.keys() | {'float_remainder', 'arc_tangent2', 'step', 'precise'}
TYPED_MATH_TEMPLATES = tuple((kind, kind.replace('_', ' ').title(), {'value_type': 'uint', **({'source_type': 'float'} if kind in {'convert', 'reinterpret'} else {})})
                           for kind in sorted(INTEGER_MATH_NODES))
TYPED_MATH_CATEGORIES = (
    (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Calculation Order'), (('precise', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Preserve Calculation Order'), {'value_type': 'float'}),)),
    (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Float Arithmetic'), (('float_remainder', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Remainder'), {'value_type': 'float'}),
                          ('arc_tangent2', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Arc Tangent (Y, X)'), {'value_type': 'float'}),
                          ('step', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Step (Edge, X)'), {'value_type': 'float'}))),
    (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Value Selection'), (('indexed_select', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Select by Index'), {'value_type': 'float', 'count': 4}), ('array_read', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Array Element'), {'value_type': 'float', 'count': 4}))),
    (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Integer & Conversion'), TYPED_MATH_TEMPLATES),
    (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Float Tests'), tuple((kind, kind.replace('_', ' ').title(), {'value_type': 'float'}) for kind in FLOAT_PREDICATES)),
    (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Packed Values'), tuple((kind, label, {'value_type': HALF_CONVERSIONS[kind][1]}) for kind, label in
        (('pack_half', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Pack Half Float')), ('unpack_half', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Unpack Half Float'))))),
    (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Wave Operations'), tuple((kind, label, {'value_type': 'float'}) for kind, label in (
        ('wave_read_first', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'First Active Lane')), ('wave_sum', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Active Lane Sum')),
        ('wave_product', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Active Lane Product')), ('wave_minimum', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Active Lane Minimum')),
        ('wave_maximum', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Active Lane Maximum'))))),
    (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Quad Operations'), tuple((kind, label, {'value_type': 'float'}) for kind, label in (
        ('quad_read_x', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Horizontal Quad Neighbour')), ('quad_read_y', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Vertical Quad Neighbour')),
        ('quad_read_diagonal', QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Diagonal Quad Neighbour'))))),
    (QT_TRANSLATE_NOOP("ShaderGraphNodes", 'Lane Queries & Reductions'), tuple((kind, kind.replace('_', ' ').title(), {'value_type': result if mode == 'query' else 'bool' if mode == 'predicate' else 'uint'})
        for kind, (function, mode, result) in LANE_FUNCTIONS.items())),
)


def node_label(text: str) -> str:
    return QCoreApplication.translate("ShaderGraphNodes", text)


def numeric_type_label(value_type):
    base, suffix = value_type.rstrip('234'), numeric_suffix(value_type)
    label = {'float': 'Scalar' if not suffix else 'Vector', 'min16float': 'Minimum 16-bit Float', 'min10float': 'Minimum 10-bit Float', 'half': 'Native 16-bit Float',
             'min16int': 'Minimum 16-bit Integer', 'min16uint': 'Minimum 16-bit Unsigned Integer',
             'int': 'Integer', 'uint': 'Unsigned Integer', 'int64_t': '64-bit Integer', 'uint64_t': '64-bit Unsigned Integer', 'bool': 'Boolean'}[base]
    return label + (' ' + suffix if suffix else '')


def operation_value_types(kind):
    if kind == 'multiply_high':
        return tuple(base+suffix for base in ('int','uint') for suffix in ('','2','3','4'))
    if kind in LANE_FUNCTIONS:
        _, mode, result = LANE_FUNCTIONS[kind]
        if mode == 'query':
            return (result,)
        if mode == 'predicate':
            return ('bool',)
        bases = ('int', 'uint') if mode == 'integer' else ('float', 'int', 'uint', 'bool') if kind in {'wave_all_equal', 'wave_read_lane'} else ('float', 'int', 'uint')
        return tuple(value for value in NUMERIC_VALUE_TYPES if value.rstrip('234') in bases)
    if kind in HALF_CONVERSIONS:
        return tuple(value for value in NUMERIC_VALUE_TYPES if value.startswith(HALF_CONVERSIONS[kind][1]))
    if kind in WAVE_FUNCTIONS or kind in QUAD_FUNCTIONS:
        return NUMERIC_VALUE_TYPES if kind == 'wave_read_first' else tuple(value_type for value_type in NUMERIC_VALUE_TYPES if not value_type.startswith('bool'))
    if kind == 'compare':
        return NUMERIC_BASE_TYPES
    if kind in {'extract_bits', 'insert_bits', 'extract_bits_dynamic', 'insert_bits_dynamic'}:
        return ('uint', 'uint2', 'uint3', 'uint4')
    if kind in INTEGER_OPERATORS or kind in INTEGER_UNARY:
        return tuple(value_type for value_type in NUMERIC_VALUE_TYPES if value_type.rstrip('234') in INTEGER_BASE_TYPES)
    if kind in {'add', 'subtract', 'multiply', 'multiply_add', 'divide', 'minimum', 'maximum', 'clamp', 'absolute', 'negate'}:
        return tuple(value_type for value_type in NUMERIC_VALUE_TYPES if not value_type.startswith('bool'))
    if kind == 'reinterpret':
        return tuple(value_type for value_type in NUMERIC_VALUE_TYPES if value_type.rstrip('234') in {'float', 'int', 'uint'})
    if kind in {'compose', 'select', 'indexed_select', 'array_read', 'constant', 'convert', 'precise'}:
        return NUMERIC_VALUE_TYPES
    return tuple(value_type for value_type in NUMERIC_VALUE_TYPES if is_float_type(value_type))


_SAME_TYPE_SIGNATURES = {
    'precise': (('value',), 'Calculation-order constraints require a numeric value type.'),
    'float_remainder': (('a', 'b'), 'Floating remainder requires a floating-point value type.'),
    'arc_tangent2': (('y', 'x'), 'Two-argument arc tangent requires a floating-point value type.'),
    'step': (('edge', 'value'), 'Step requires a floating-point value type.'),
}


def typed_math_signature(kind, properties):
    target = properties.get('value_type', 'uint')
    if not isinstance(target, str):
        raise ValueError('A numeric operation must name its value type.')
    if kind in _SAME_TYPE_SIGNATURES:
        pins, message = _SAME_TYPE_SIGNATURES[kind]
        if target not in operation_value_types(kind):
            raise ValueError(message)
        return tuple((pin, target) for pin in pins), target
    if kind in LANE_FUNCTIONS:
        _, mode, result = LANE_FUNCTIONS[kind]
        if target not in operation_value_types(kind):
            raise ValueError('This lane operation does not accept the selected value type.')
        inputs = () if mode == 'query' else (('value', target),)
        if mode == 'indexed':
            inputs += (('lane', 'uint'),)
        if kind == 'wave_all_equal':
            result = 'bool' + numeric_suffix(target)
        return inputs, result or target
    if kind in {'extract_bits_dynamic', 'insert_bits_dynamic'}:
        if target not in operation_value_types(kind):
            raise ValueError('Calculated bit fields require unsigned 32-bit words.')
        inputs = (('value', target), ('width', target), ('offset', target))
        if kind == 'insert_bits_dynamic':
            inputs = (('base', target),) + inputs
        return inputs, target
    if kind in FLOAT_PREDICATES or kind in WAVE_FUNCTIONS or kind in QUAD_FUNCTIONS or kind in HALF_CONVERSIONS:
        if target not in operation_value_types(kind):
            raise ValueError('This operation does not accept the selected value type.')
        result = 'bool' + numeric_suffix(target) if kind in FLOAT_PREDICATES else target
        if kind in HALF_CONVERSIONS:
            result = HALF_CONVERSIONS[kind][2] + numeric_suffix(target)
        return (('value', target),), result
    if kind in {'extract_bits', 'insert_bits'}:
        width, offset = properties.get('width', 1), properties.get('offset', 0)
        if (target not in operation_value_types(kind) or type(width) is not int or type(offset) is not int
                or not 0 <= width <= 32 or not 0 <= offset < 32 or width + offset > 32):
            raise ValueError('The bit field must fit inside its unsigned 32-bit value.')
        return ((('value', target),) if kind == 'extract_bits' else (('base', target), ('value', target))), target
    if kind in {'convert', 'reinterpret'}:
        source = properties.get('source_type', 'float')
        if source not in NUMERIC_VALUE_TYPES or target not in NUMERIC_VALUE_TYPES or numeric_suffix(source) != numeric_suffix(target):
            raise ValueError('Conversion needs numeric types with matching component counts.')
        if kind == 'reinterpret' and (source not in operation_value_types(kind) or target not in operation_value_types(kind)):
            raise ValueError('Bit reinterpretation requires matching 32-bit float or integer words.')
        return (('value', source),), target
    if target.rstrip('234') not in INTEGER_BASE_TYPES or target not in operation_value_types(kind):
        raise ValueError('Integer operations need a signed or unsigned integer type.')
    inputs = (('a', target), ('b', target)) if kind in INTEGER_OPERATORS or kind == 'multiply_high' else (('value', target),)
    result = 'uint' + numeric_suffix(target) if kind in {'count_bits', 'reverse_bits', 'first_bit_low', 'first_bit_high'} else target
    if kind == 'reverse_bits' and target.rstrip('234').endswith('64_t'):
        result = 'uint64_t' + numeric_suffix(target)
    return inputs, result


def float_predicate_expression(kind, value, value_type='float'):
    relation = {'is_nan': '>', 'is_infinite': '==', 'is_finite': '<'}[kind]
    promoted = 'float' + numeric_suffix(value_type)
    return f'((asuint(({promoted})({value})) & 0x7fffffffu) {relation} 0x7f800000u)'


def typed_math_expression(kind, properties, incoming):
    inputs, target = typed_math_signature(kind, properties)
    values = {name: incoming(name, value_type) for name, value_type in inputs}
    if kind == 'multiply_high':
        return f"REasyMultiplyHigh_{target}({values['a']}, {values['b']})", target
    if kind == 'precise':
        return values['value'], target
    if kind == 'float_remainder':
        # HLSL '%' lowers to frem; fmod expands to division with different rounding.
        return f"({values['a']} % {values['b']})", target
    if kind == 'arc_tangent2':
        return f"atan2({values['y']}, {values['x']})", target
    if kind == 'step':
        # GLSL Step: NaN gives 0 on NVIDIA; spelled out, x < edge ? 0 : 1 gives 1.
        return f"step({values['edge']}, {values['value']})", target
    if kind in LANE_FUNCTIONS:
        return f"{LANE_FUNCTIONS[kind][0]}({', '.join(values.values())})", target
    if kind in {'extract_bits_dynamic', 'insert_bits_dynamic'}:
        width, offset = f"({values['width']} & 31u)", f"({values['offset']} & 31u)"
        mask = f'((1u << {width}) - 1u)'
        if kind == 'extract_bits_dynamic':
            return f"(({values['value']} >> {offset}) & {mask})", target
        return f"(({values['base']} & ~({mask} << {offset})) | (({values['value']} & {mask}) << {offset}))", target
    if kind in HALF_CONVERSIONS:
        return f"{HALF_CONVERSIONS[kind][0]}({values['value']})", target
    if kind in FLOAT_PREDICATES:
        return float_predicate_expression(kind, values['value'], properties['value_type']), target
    if kind in WAVE_FUNCTIONS or kind in QUAD_FUNCTIONS:
        function = WAVE_FUNCTIONS.get(kind) or QUAD_FUNCTIONS[kind]
        return f"{function}({values['value']})", target
    if kind in {'extract_bits', 'insert_bits'}:
        width, offset = properties.get('width', 1), properties.get('offset', 0)
        mask = (1 << width) - 1
        if kind == 'extract_bits':
            return f"(({values['value']} >> {offset}u) & {mask}u)", target
        mask <<= offset
        return f"(({values['base']} & ~{mask}u) | (({values['value']} << {offset}u) & {mask}u))", target
    if kind in INTEGER_OPERATORS:
        return f"({values['a']} {INTEGER_OPERATORS[kind]} {values['b']})", target
    if kind in INTEGER_UNARY:
        return f"{INTEGER_UNARY[kind]}({values['value']})", target
    if kind == 'convert':
        return f"{target}({values['value']})", target
    if target == properties.get('source_type', 'float'):
        return values['value'], target
    base = target.rstrip('234')
    return f"as{base}({values['value']})", target


def multiply_high_statements(domain, left, right, result, *, signed=False):
    p0,p1,p2 = (result+suffix for suffix in ('P0','P1','P2'))
    statements = [
        f'{domain} {p0} = ({left} & 65535u) * ({right} & 65535u);',
        f'{domain} {p1} = ({left} >> 16u) * ({right} & 65535u) + ({p0} >> 16u);',
        f'{domain} {p2} = ({left} & 65535u) * ({right} >> 16u) + ({p1} & 65535u);',
        f'{domain} {result} = ({left} >> 16u) * ({right} >> 16u) + ({p1} >> 16u) + ({p2} >> 16u);',
    ]
    if signed:
        statements.append(f'{result} -= (asuint(asint({left}) >> 31) & {right}) + (asuint(asint({right}) >> 31) & {left});')
    return statements


def typed_math_helpers(kind, properties):
    if kind != 'multiply_high':
        return ''
    _inputs,domain = typed_math_signature(kind,properties)
    carrier = 'uint'+numeric_suffix(domain)
    signed = domain.startswith('int')
    body = [f'{carrier} a = '+('asuint(left);' if signed else 'left;'),
            f'{carrier} b = '+('asuint(right);' if signed else 'right;')]
    body += multiply_high_statements(carrier,'a','b','high',signed=signed)
    body.append('return '+('asint(high);' if signed else 'high;'))
    return f'{domain} REasyMultiplyHigh_{domain}({domain} left, {domain} right)\n{{\n'+''.join('    '+line+'\n' for line in body)+'}'

UNARY_FUNCTIONS = {
    "absolute": "abs", "sine": "sin", "cosine": "cos", "floor": "floor",
    "ceiling": "ceil", "fraction": "frac", "square_root": "sqrt",
    "reciprocal": "rcp", "reciprocal_square_root": "rsqrt", "exponent2": "exp2",
    "logarithm2": "log2", "round_even": "round", "truncate": "trunc",
    "exponent": "exp", "logarithm": "log",
    "derivative_x": "ddx_coarse", "derivative_y": "ddy_coarse",
    "derivative_x_fine": "ddx_fine", "derivative_y_fine": "ddy_fine",
    # D3D's ddx is coarse; Vulkan OpDPdx leaves precision to the driver.
    "derivative_x_default": "ddx", "derivative_y_default": "ddy",
    "negate": "-",
    "tangent": "tan", "arc_sine": "asin", "arc_cosine": "acos", "arc_tangent": "atan",
    "hyperbolic_sine": "sinh", "hyperbolic_cosine": "cosh", "hyperbolic_tangent": "tanh",
}


def math_context_error(kind, contract):
    if (kind in WAVE_FUNCTIONS or kind in LANE_FUNCTIONS and kind != 'quad_read_lane') and contract.model_major < 6:
        return 'Wave operations require Shader Model 6 or later in the selected renderer pass.'
    if kind in QUAD_FUNCTIONS or kind == 'quad_read_lane':
        version = contract.model_major, contract.model_minor
        if not (contract.stage == 'pixel' and version >= (6, 0) or contract.stage == 'compute' and version >= (6, 6)):
            return 'Quad reads require a pixel pass using Shader Model 6, or a compute pass using Shader Model 6.6.'
    return ''

def unary_expression(kind, value, value_type):
    if value_type.rstrip('234') in {'uint', 'min16uint', 'uint64_t'}:
        if kind == 'negate':
            # FXC warns about unary minus on unsigned operands and rejects one-argument vector constructors.
            return f"(({value_type})0 - {value})"
        if kind == 'absolute':
            return value
    return f"{UNARY_FUNCTIONS[kind]}({value})"


def remap_expression(value_type, value, input_min, input_max, output_min, output_max):
    span = f"({input_max} - {input_min})"
    direction = f"(1.0 - 2.0 * {value_type}({span} < 0.0))"
    denominator = f"(max(abs({span}), 1.0e-6) * {direction})"
    return f"({output_min} + (({value} - {input_min}) / {denominator}) * ({output_max} - {output_min}))"


BASIC_MATH_NODES = (
    ("add", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Add")), ("subtract", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Subtract")), ("multiply", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Multiply")), ("divide", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Divide")),
    ("lerp", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Linear Interpolate")), ("power", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Power")), ("minimum", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Minimum")), ("maximum", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Maximum")),
    ("clamp", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Clamp")), ("saturate", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Saturate")), ("normalize", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Normalize")), ("dot", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Dot Product")),
    ("one_minus", QT_TRANSLATE_NOOP("ShaderGraphNodes", "One Minus")), ("absolute", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Absolute")), ("sine", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Sine")), ("cosine", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Cosine")),
    ("floor", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Floor")), ("ceiling", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Ceiling")), ("fraction", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Fraction")), ("square_root", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Square Root")),
    ("smoothstep", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Smoothstep")), ("distance", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Distance")), ("remap", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Remap Range")),
    ("normal_unpack", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Unpack Normal Map")), ("swizzle", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Component Mask")), ("compose", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Compose Vector")),
)
BASIC_MATH_KINDS = frozenset(kind for kind, _label in BASIC_MATH_NODES)
EXTRA_MATH_NODES = (
    ("tangent", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Tangent")), ("arc_sine", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Arc Sine")), ("arc_cosine", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Arc Cosine")), ("arc_tangent", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Arc Tangent")),
    ("hyperbolic_sine", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Hyperbolic Sine")), ("hyperbolic_cosine", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Hyperbolic Cosine")), ("hyperbolic_tangent", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Hyperbolic Tangent")),
    ("reciprocal", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Reciprocal")), ("reciprocal_square_root", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Reciprocal Square Root")),
    ("exponent2", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Exponent (base 2)")), ("logarithm2", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Logarithm (base 2)")),
    ("exponent", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Exponent (base e)")), ("logarithm", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Natural Logarithm")),
    ("round_even", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Round to Even")), ("truncate", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Truncate")),
    ("multiply_add", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Multiply Add")),
    ("negate", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Negate")),
    ("compare", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Compare")), ("select", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Select")),
    ("derivative_x", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Derivative X (coarse)")), ("derivative_y", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Derivative Y (coarse)")),
    ("derivative_x_fine", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Derivative X (fine)")), ("derivative_y_fine", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Derivative Y (fine)")),
    ("derivative_x_default", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Derivative X (driver precision)")), ("derivative_y_default", QT_TRANSLATE_NOOP("ShaderGraphNodes", "Derivative Y (driver precision)")),
)

COMPARE_RELATIONS = {"equal": "==", "not_equal": "!=", "less": "<", "less_equal": "<=", "greater": ">", "greater_equal": ">="}


def comparison_expression(properties, a, b):
    relation = properties.get("relation", "less")
    if relation not in COMPARE_RELATIONS or type(properties.get("nan_result", False)) is not bool:
        raise ValueError("The floating-point comparison has an invalid relation or NaN result.")
    expression = f"({a} {COMPARE_RELATIONS[relation]} {b})"
    nan_result = properties.get("nan_result", False)
    value_type = properties.get('value_type', 'float')
    if value_type not in NUMERIC_BASE_TYPES:
        raise ValueError('Compare needs a scalar float, integer or boolean type.')
    if not is_float_type(value_type):
        if nan_result or value_type == 'bool' and relation not in {'equal', 'not_equal'}:
            raise ValueError('This comparison is not defined for the selected integer/boolean type.')
        return expression
    if nan_result != (relation == "not_equal"):
        # HLSL 2021 || and && short-circuit into branches; the bitwise form is branch-free.
        a_nan = float_predicate_expression('is_nan', a, value_type)
        b_nan = float_predicate_expression('is_nan', b, value_type)
        unordered = f"(uint({a_nan}) | uint({b_nan}))"
        expression = (f"bool({unordered} | uint({expression}))" if nan_result
                      else f"bool((1u - {unordered}) & uint({expression}))")
    return expression
