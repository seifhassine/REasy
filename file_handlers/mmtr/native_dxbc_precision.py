import re

lazy from file_handlers.sdf.shader_graph import ShaderGraphError


PRECISION_TYPES = {
    'min16f': 'min16float', 'min2_8f': 'min10float',
    'min16i': 'min16int', 'min16u': 'min16uint',
}
FULL_TYPES = {'min16float': 'float', 'min10float': 'float', 'min16int': 'int', 'min16uint': 'uint'}


def operand_precision(text):
    match = re.fullmatch(r'(.*?)\s+\{(\w+)(?: as (\w+))?\}', text.strip())
    if match is None:
        if '{' in text or '}' in text:
            raise ShaderGraphError('Invalid native operand precision annotation: ' + text)
        return text.strip(), None, None
    source, target = match[2], match[3] or match[2]
    if source not in PRECISION_TYPES.keys() | {'def32'} or target not in PRECISION_TYPES.keys() | {'def32'}:
        raise ShaderGraphError('Unknown native operand precision annotation: ' + text)
    domain = PRECISION_TYPES.get(source) or PRECISION_TYPES.get(target)
    if domain is None:
        raise ShaderGraphError('A default-to-default precision annotation carries no numeric domain.')
    full = FULL_TYPES[domain]
    return match[1], PRECISION_TYPES.get(source, full), PRECISION_TYPES.get(target, full)


def word_value(builder, value, domain):
    if value.value_type == domain:
        return value
    full_source = FULL_TYPES.get(value.value_type)
    if full_source:
        value = builder.convert(value, full_source)
    full_target = FULL_TYPES.get(domain, domain)
    value = builder.convert(value, full_target, bits=True)
    return builder.convert(value, domain) if full_target != domain else value
