"""Display native identities as hex and ordinary integer values as decimal."""
import json

HASH_FIELDS = frozenset({
    'id_hash', 'name_hash', 'fullname_hash', 'parent',
    'mTransitions', 'mStartState', 'mAllState', 'v0_ID', 'v1_ID', 'v0_UID',
})


def value_text(name, value):
    if name.rsplit('.', 1)[-1] in HASH_FIELDS and isinstance(value, int) and not isinstance(value, bool):
        return f'0x{value & 0xFFFFFFFF:08X}'
    return str(value)


def rsz_value_text(field):
    value = field.value
    for member in field.enum_values:
        if member['value'] == value:
            return member['name']
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (tuple, list, dict)) else value_text(field.name, value)
