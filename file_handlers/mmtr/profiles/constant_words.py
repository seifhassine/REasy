from file_handlers.sdf.shader_graph import ShaderGraphError


OVERRIDE_PATTERN = r"REasyOverride_b(?P<bind>\d+)_o(?P<offset>\d+)(?:_s(?P<space>\d+))?"
ConstantWord = tuple[int, int] | tuple[int, int, int]


def constant_word(binding, offset, space=0):
    if any(not isinstance(value, int) or value < 0 for value in (binding, offset, space)):
        raise ShaderGraphError("Constant word has an invalid binding, offset or register space")
    return (binding, offset, space) if space else (binding, offset)


def word_parts(key):
    if len(key) not in {2, 3}:
        raise ShaderGraphError("A constant word needs a binding, byte offset and optional space")
    binding, offset, *space = key
    space = space[0] if space else 0
    constant_word(binding, offset, space)
    return binding, offset, space


def word_tag(key):
    binding, offset, space = word_parts(key)
    return f"b{binding}_o{offset}" + (f"_s{space}" if space else "")


def override_word(match):
    return constant_word(int(match["bind"]), int(match["offset"]), int(match["space"] or 0))


def dynamic_word(key):
    binding, offset, space = word_parts(key)
    return constant_word(binding, offset % 16 // 4, space)


def normalize_word_expressions(expressions):
    result = {}
    for key,expression in (expressions or {}).items():
        canonical = constant_word(*word_parts(key))
        if canonical[1] % 4:
            raise ShaderGraphError("Constant expression must address an aligned 32-bit word")
        if canonical in result and result[canonical] != expression:
            raise ShaderGraphError("Two expressions address the same constant word")
        result[canonical] = expression
    return result
