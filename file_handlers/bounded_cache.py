from collections import OrderedDict
from threading import RLock


class BoundedCache:
    def __init__(self):
        self.entries = OrderedDict()
        self.retained_bytes = self.hits = self.misses = self.evictions = 0
        self.lock = RLock()

    def get(self, key, default=None):
        with self.lock:
            entry = self.entries.pop(key, None)
            if entry is None:
                self.misses += 1
                return default
            self.entries[key] = entry
            self.hits += 1
            return entry[0]

    def pop(self, key, default=None):
        with self.lock:
            entry = self.entries.pop(key, None)
            if entry is None:
                return default
            self.retained_bytes -= entry[1]
            return entry[0]

    def put(self, key, value, cost: int, max_entries: int, max_bytes: int) -> bool:
        with self.lock:
            self.pop(key)
            if max_entries <= 0 or cost > max_bytes:
                return False
            self.entries[key] = value, cost
            self.retained_bytes += cost
            while len(self.entries) > max_entries or self.retained_bytes > max_bytes:
                _, (_, removed) = self.entries.popitem(last=False)
                self.retained_bytes -= removed
                self.evictions += 1
            return True

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()
            self.retained_bytes = self.hits = self.misses = self.evictions = 0
