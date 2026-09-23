"""Resolve native player FSM tag IDs through the document's enum catalog."""
from functools import lru_cache

from utils.enum_manager import registry_enums


PLAYER_TAG_TYPES = frozenset(
    'snow.player.' + name for name in
    ('Situation', 'ActStatus', 'ActionNoAttack', 'ForCamera', 'ForEnemy', 'ForOtomo', 'ServantAct')
)


@lru_cache(maxsize=16)
def tag_names(registry_path):
    result = {}
    for type_name, members in registry_enums(registry_path).items():
        if type_name not in PLAYER_TAG_TYPES and not (
                type_name.startswith('snow.player.') and type_name.endswith('Tag')):
            continue
        for member in members:
            value = int(member['value']) & 0xFFFFFFFF
            name = f"{type_name}.{member['name']}"
            if name not in result.setdefault(value, []):
                result[value].append(name)
    return result
