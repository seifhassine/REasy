"""See docs/internals/strands-rendering.md, Colliders."""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from file_handlers.bounded_cache import BoundedCache
from utils.resource_file_utils import resource_key

_NAMED = BoundedCache()
_NAMED_ENTRIES, _NAMED_BYTES = 4096, 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Registered:
    presets: tuple[str, ...]
    parts: tuple[str, ...]


def _strings(value) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def named(registry, game: str, path: str, data: bytes) -> frozenset[str]:
    from file_handlers.rsz.plain_values import plain_value
    from file_handlers.rsz.rsz_file import RszFile

    cache_key = (game, resource_key(path), hashlib.blake2b(data, digest_size=16).digest())
    held = _NAMED.get(cache_key)
    if held is not None:
        return held
    rsz = RszFile()
    rsz.filepath, rsz.game_version, rsz.type_registry = path, game, registry
    try:
        rsz.read(data, validate_type_registry=True)
    except (ValueError, KeyError, IndexError, TypeError, OSError):
        found = frozenset()
    else:
        elements = rsz.parsed_elements
        texts = []
        for fields in elements.values():
            for value in fields.values():
                try:
                    texts.extend(_strings(plain_value(value, elements.__getitem__)))
                except (TypeError, KeyError):
                    continue
        found = frozenset(resource_key(text) for text in texts if "/" in text and "." in text.rsplit("/", 1)[-1])
    _NAMED.put(cache_key, found, 64 * len(found) + 64, _NAMED_ENTRIES, _NAMED_BYTES)
    return found


def registered(prefab: str, users: tuple[str, ...], named_by: Callable[[str], frozenset[str] | None]) -> Registered:
    prefab_key, presets, parts = resource_key(prefab), {}, {}
    for path in sorted(users):
        paths = named_by(path)
        if paths is None or prefab_key not in paths:
            continue
        presets.update(dict.fromkeys(sorted(item for item in paths if item.endswith(".clsp"))))
        parts.update(dict.fromkeys(sorted(item for item in paths if item.endswith(".pfb") and item != prefab_key)))
    return Registered(tuple(presets), tuple(parts))


__all__ = ["Registered", "named", "registered"]
