from collections import namedtuple
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import _FIELD, Field, dataclass, fields, is_dataclass
from functools import lru_cache
import hashlib
import struct
import sys

lazy from file_handlers.bounded_cache import BoundedCache
lazy from utils.native_build import ensure_fastkey


class _Uncacheable(TypeError):
    pass


_SIGNATURE_BUFFER_SIZE = 64 * 1024
_TRAVERSAL_MEMO_BYTES = 1024 * 1024
_TRAVERSAL_MEMO_ENTRIES = 4096
_IMMUTABLE_ATOMS = frozenset((type(None), bool, int, float, str, bytes))


class _TraversalMemo:
    __slots__ = ("entries", "schemas", "generation", "bytes")

    def __init__(self):
        self.entries = {}
        self.schemas = {}
        self.generation = self.bytes = 0

    def invalidate(self):
        self.entries.clear()
        self.schemas.clear()
        self.bytes = 0
        self.generation += 1

    def schema(self, kind):
        if type(kind) is not type:
            return None
        if kind in self.schemas:
            return self.schemas[kind]
        schema = ensure_fastkey().plain_record_schema(kind, _KEY_RULES)
        if len(self.schemas) < 128:
            self.schemas[kind] = schema
        return schema

    def remember(self, value, result, cost):
        if (len(self.entries) < _TRAVERSAL_MEMO_ENTRIES
                and self.bytes + cost <= _TRAVERSAL_MEMO_BYTES):
            self.entries[id(value)] = value, result
            self.bytes += cost


@lru_cache(maxsize=128)
def _dataclass_schema(name, field_names):
    raw = name.encode("utf-8")
    prefix = b"cs" + struct.pack("!Q", len(raw)) + raw
    members = []
    for field_name in field_names:
        if name == "file_handlers.sdf.shader_graph.ShaderContract" and field_name == "_fingerprint":
            continue
        raw = field_name.encode("utf-8")
        members.append((field_name, b"s" + struct.pack("!Q", len(raw)) + raw))
    return prefix, tuple(members)


@dataclass(frozen=True, slots=True)
class _BuildKey:
    runtime: tuple
    digest: bytes


_DATACLASS_PARAMS_TYPE = type(_BuildKey.__dataclass_params__)


_KEY_RULES = (_dataclass_schema, is_dataclass, fields, _FIELD, Field, _DATACLASS_PARAMS_TYPE, Mapping, _Uncacheable)


def native_build_key(runtime, *inputs):
    try:
        runtime = tuple(runtime)
        hash(runtime)
        digest = hashlib.sha256(b"REasy native build cache\0v1\0")
        ensure_fastkey().write_key(inputs, digest.update, _SIGNATURE_BUFFER_SIZE, _KEY_RULES)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None
    return _BuildKey(runtime, digest.digest())


def _immutable_size(value):
    return _measure_immutable(value, set(), _TraversalMemo())


def _measure_immutable(value, active, memo):
    kind = type(value)
    if type(kind) is type and kind in _IMMUTABLE_ATOMS:
        return sys.getsizeof(value)
    identity = id(value)
    if identity in active:
        raise _Uncacheable("Cyclic native result")
    if memo is not None and (entry := memo.entries.get(identity)) is not None:
        return entry[1]
    owner = memo
    schema = memo.schema(kind) if memo is not None and kind is not tuple else None
    if memo is not None and kind is not tuple and schema is None:
        memo.invalidate()
        memo = None
    generation = memo.generation if memo is not None else None
    active.add(identity)
    try:
        if kind is tuple:
            cost = sys.getsizeof(value)
            for item in value:
                item_type = type(item)
                cost += (sys.getsizeof(item) if type(item_type) is type and item_type in _IMMUTABLE_ATOMS
                         else _measure_immutable(item, active, memo))
        elif is_dataclass(value) and not isinstance(value, type) and kind.__dataclass_params__.frozen:
            cost = sys.getsizeof(value)
            field_names = schema[1] if schema is not None else (field.name for field in fields(value))
            for field_name in field_names:
                item = getattr(value, field_name)
                item_type = type(item)
                cost += (sys.getsizeof(item) if type(item_type) is type and item_type in _IMMUTABLE_ATOMS
                         else _measure_immutable(item, active, memo))
                if field_name == "_fingerprint":
                    cost += 256
        else:
            raise _Uncacheable("Native result contains mutable data")
    finally:
        active.remove(identity)
        if owner is not memo:
            owner.invalidate()
    if memo is not None and memo.generation == generation:
        memo.remember(value, cost, 128)
    return cost


NativeBuildCacheInfo = namedtuple("NativeBuildCacheInfo", "hits misses evictions entries retained_bytes")


@contextmanager
def fresh_native_build_cache(*caches):
    with ExitStack() as scopes:
        for cache in caches:
            fresh = NativeBuildCache(max_entries=cache.max_entries, max_bytes=cache.max_bytes)
            token = cache._scope.set(fresh)
            scopes.callback(cache._scope.reset, token)
        yield


class NativeBuildCache:
    def __init__(self, *, max_entries=2048, max_bytes=64 * 1024 * 1024):
        self.max_entries = max(0, int(max_entries))
        self.max_bytes = max(0, int(max_bytes))
        self._cache = BoundedCache()
        self._scope = ContextVar("native_build_cache_scope", default=None)

    def get(self, key):
        scoped = self._scope.get()
        if scoped is not None:
            return scoped.get(key)
        return None if key is None else self._cache.get(key)

    def put(self, key, result, size=None):
        """``size``: ``_immutable_size(result)``, when a worker measured it."""
        scoped = self._scope.get()
        if scoped is not None:
            return scoped.put(key, result, size)
        if key is None:
            return False
        try:
            cost = (sys.getsizeof(key) + sys.getsizeof(key.runtime) + sys.getsizeof(key.digest)
                    + (_immutable_size(result) if size is None else size) + 256)  # LRU entry overhead.
        except (TypeError, ValueError, RecursionError):
            return False
        return self._cache.put(key, result, cost, self.max_entries, self.max_bytes)

    def clear(self):
        scoped = self._scope.get()
        if scoped is not None:
            return scoped.clear()
        self._cache.clear()

    def info(self):
        scoped = self._scope.get()
        if scoped is not None:
            return scoped.info()
        cache = self._cache
        with cache.lock:
            return NativeBuildCacheInfo(cache.hits, cache.misses, cache.evictions, len(cache.entries),
                                        cache.retained_bytes)
