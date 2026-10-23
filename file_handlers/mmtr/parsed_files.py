from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
import hashlib
import threading

from settings import load_settings

# A master whose every program is read (MmtrDocument.skinning_contracts) keeps up to 7.5x its size in memory, its
# bytes included; drawn alone, masters keep 1.2 to 2.3x and light-pass files 1.1x, which this over-counts.
COST_PER_BYTE = 8


@dataclass(slots=True)
class _Entry:
    value: object
    cost: int
    holders: int


class _ParsedFiles:
    def __init__(self, budget: int):
        self._lock = threading.Lock()
        self._budget = budget
        self._entries: OrderedDict[tuple, _Entry] = OrderedDict()
        self._parsing: dict[tuple, threading.Event] = {}

    def set_budget(self, budget: int) -> None:
        with self._lock:
            self._budget = budget
            self._trim()

    def take[T](self, key: tuple, cost: int, parse: Callable[[], T]) -> T:
        """Each call must be matched by a ``give_back`` of the key."""
        while True:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None:
                    entry.holders += 1
                    self._entries.move_to_end(key)
                    return entry.value
                waiting = self._parsing.get(key)
                if waiting is None:
                    parsed = self._parsing[key] = threading.Event()
                    break
            waiting.wait()
        try:
            value = parse()
        except BaseException:
            with self._lock:
                del self._parsing[key]
                parsed.set()
            raise
        with self._lock:
            self._entries[key] = _Entry(value, cost, 1)
            del self._parsing[key]
            parsed.set()
        return value

    def give_back(self, keys) -> None:
        with self._lock:
            for key in keys:
                self._entries[key].holders -= 1
            self._trim()

    def _trim(self) -> None:
        free = [(key, entry) for key, entry in self._entries.items() if not entry.holders]
        kept = sum(entry.cost for _key, entry in free)
        for key, entry in free:
            if kept <= self._budget:
                break
            del self._entries[key]
            kept -= entry.cost


_files = _ParsedFiles(load_settings()["parsed_shader_cache_mb"] << 20)


def set_budget(megabytes: int) -> None:
    """Counts estimated parsed memory (``COST_PER_BYTE`` x file size) of files no lease holds."""
    _files.set_budget(megabytes << 20)


class Lease:
    """Must be closed to release its files; not thread-safe."""

    def __init__(self):
        self._found: dict[tuple, object] = {}
        self._keys: list[tuple] = []

    def parsed[T](self, path: str, data: bytes, parse: Callable[..., T], *arguments: Hashable) -> T:
        name = (path, parse.__module__, parse.__qualname__, arguments)
        if name not in self._found:
            key = (hashlib.sha256(data).digest(), *name[1:])
            self._found[name] = _files.take(key, COST_PER_BYTE * len(data), lambda: parse(data, *arguments))
            self._keys.append(key)
        return self._found[name]

    def close(self) -> None:
        keys, self._keys, self._found = self._keys, [], {}
        _files.give_back(keys)
