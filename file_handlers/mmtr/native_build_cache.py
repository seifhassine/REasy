from collections import namedtuple
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import Field, dataclass, fields, is_dataclass
from functools import lru_cache
import hashlib
import struct
import sys
from types import MemberDescriptorType

from file_handlers.bounded_cache import BoundedCache


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
        schema = _plain_record_schema(kind)
        if len(self.schemas) < 128:
            self.schemas[kind] = schema
        return schema

    def remember(self, value, result, cost):
        if (len(self.entries) < _TRAVERSAL_MEMO_ENTRIES
                and self.bytes + cost <= _TRAVERSAL_MEMO_BYTES):
            self.entries[id(value)] = value, result
            self.bytes += cost


def _class_member(kind, name):
    for base in kind.__mro__:
        if name in base.__dict__:
            return base.__dict__[name]
    return None


def _plain_record_schema(kind):
    if (kind.__dictoffset__ or _class_member(kind, "__getattribute__") is not object.__getattribute__
            or _class_member(kind, "__sizeof__") is not object.__sizeof__):
        return None
    namespace = kind.__dict__
    schema = namespace.get("__dataclass_fields__")
    parameters = namespace.get("__dataclass_params__")
    if type(schema) is not dict or type(parameters) is not _DATACLASS_PARAMS_TYPE:
        return None
    for metadata_kind, names in ((Field, ("name", "_field_type")), (_DATACLASS_PARAMS_TYPE, ("frozen",))):
        if _class_member(metadata_kind, "__getattribute__") is not object.__getattribute__:
            return None
        for name in names:
            member = _class_member(metadata_kind, name)
            if type(member) is not MemberDescriptorType or member.__objclass__ is not metadata_kind:
                return None
    if (type(parameters.frozen) is not bool or not parameters.frozen
            or any(type(field) is not Field for field in schema.values())):
        return None
    field_names = tuple(field.name for field in fields(kind))
    if any(type(name) is not str or type(namespace.get(name)) is not MemberDescriptorType
           for name in field_names):
        return None
    module = namespace.get("__module__")
    qualname = type.__dict__["__qualname__"].__get__(kind)
    if type(module) is not str or type(qualname) is not str:
        return None
    return module + "." + qualname, field_names


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


def _signature(digest, value, active, buffer, memo=None):
    if len(buffer) >= _SIGNATURE_BUFFER_SIZE:
        digest.update(buffer)
        buffer.clear()
        if memo is not None:
            memo.generation += 1
    kind = type(value)
    if value is None:
        buffer.extend(b"n")
    elif kind is bool:
        buffer.extend(b"t" if value else b"f")
    elif kind is int:
        raw = str(value).encode("utf-8")
        buffer.extend(b"is" + struct.pack("!Q", len(raw)) + raw)
    elif kind is float:
        buffer.extend(b"d" + struct.pack("!d", value))
    elif kind in (str, bytes):
        raw = value.encode("utf-8") if kind is str else value
        prefix = (b"s" if kind is str else b"b") + struct.pack("!Q", len(raw))
        if len(raw) >= _SIGNATURE_BUFFER_SIZE:
            digest.update(buffer)
            buffer.clear()
            if memo is not None:
                memo.generation += 1
            digest.update(prefix)
            digest.update(raw)
        else:
            buffer.extend(prefix)
            buffer.extend(raw)
    else:
        identity = id(value)
        if identity in active:
            raise _Uncacheable("Cyclic compiler input")
        if memo is not None and (entry := memo.entries.get(identity)) is not None:
            buffer.extend(entry[1])
            return
        owner = memo
        schema = memo.schema(kind) if memo is not None and kind not in (tuple, list, dict) else None
        if memo is not None and kind not in (tuple, list, dict) and schema is None:
            memo.invalidate()
            memo = None
        generation = memo.generation if memo is not None else None
        start = len(buffer)
        active.add(identity)
        try:
            if kind in (tuple, list):
                buffer.extend((b"(" if kind is tuple else b"[") + struct.pack("!Q", len(value)))
                for item in value:
                    _signature(digest, item, active, buffer, memo)
            elif isinstance(value, Mapping):
                buffer.extend(b"{" + struct.pack("!Q", len(value)))
                for key, item in value.items():
                    _signature(digest, key, active, buffer, memo)
                    _signature(digest, item, active, buffer, memo)
            elif is_dataclass(value) and not isinstance(value, type):
                if schema is None:
                    name = kind.__module__ + "." + kind.__qualname__
                    if type(name) is not str:
                        raise _Uncacheable("Unsupported dataclass name")
                    field_names = []
                    for field in fields(value):
                        if type(field.name) is not str:
                            raise _Uncacheable("Unsupported dataclass field name")
                        field_names.append(field.name)
                    schema = name, tuple(field_names)
                prefix, members = _dataclass_schema(*schema)
                buffer.extend(prefix)
                for field_name, encoded in members:
                    buffer.extend(encoded)
                    _signature(digest, getattr(value, field_name), active, buffer, memo)
                buffer.extend(b";")
            else:
                raise _Uncacheable("Unsupported compiler input")
        finally:
            active.remove(identity)
            if owner is not memo:
                owner.invalidate()
        if memo is not None and memo.generation == generation:
            encoded = bytes(buffer[start:])
            memo.remember(value, encoded, len(encoded) + 128)


@dataclass(frozen=True, slots=True)
class _BuildKey:
    runtime: tuple
    digest: bytes


_DATACLASS_PARAMS_TYPE = type(_BuildKey.__dataclass_params__)


def native_build_key(runtime, *inputs):
    try:
        runtime = tuple(runtime)
        hash(runtime)
        digest = hashlib.sha256(b"REasy native build cache\0v1\0")
        buffer = bytearray()
        _signature(digest, inputs, set(), buffer, _TraversalMemo())
        digest.update(buffer)
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
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

    def put(self, key, result):
        scoped = self._scope.get()
        if scoped is not None:
            return scoped.put(key, result)
        if key is None:
            return False
        try:
            cost = (sys.getsizeof(key) + sys.getsizeof(key.runtime) + sys.getsizeof(key.digest)
                    + _immutable_size(result) + 256)  # LRU entry overhead.
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
