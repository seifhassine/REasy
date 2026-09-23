"""Parse typed values using the field's native enum identity, never a name guess."""
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def enums():
    path = Path(__file__).resolve().parents[2] / 'resources/data/enums/mhrise_enums.json'
    return json.loads(path.read_text(encoding='utf-8'))


def parse_field(field, text):
    if field.binding is None:
        raise ValueError(f'{field.name} is not an editable scalar')
    enum_type = getattr(field.data, 'orig_type', '')
    members = enums().get(enum_type, [])
    matches = [entry['value'] for entry in members if entry['name'] == text.strip()]
    value = matches[0] if len(matches) == 1 else field.binding.parse(text)
    field.binding.encode(value)
    return value


def assignments(instance, values):
    fields = {field.name: field for field in instance.fields}
    result = {}
    for item in values:
        name, separator, text = item.partition('=')
        name = name.strip()
        if not separator or name not in fields:
            raise ValueError(f'Unknown assignment {item!r} for {instance.class_name}')
        result[name] = parse_field(fields[name], text)
    return result
