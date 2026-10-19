from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import json

from file_handlers.sdf.shader_graph import ShaderGraphError, immutable_property_validation


_validated = ContextVar('validated_material_regions', default=None)
_session = ContextVar('material_region_read_session', default=None)
_fields = {'branch': ('when_true', 'when_false'), 'repeat': ('body', 'states', 'continuation'),
           'material_function': ('function', 'function_hash')}
REGION_ASSET_FIELDS = {'branch': ('when_true', 'when_false'), 'repeat': ('body',), 'material_function': ('function',)}


@dataclass
class _Snapshot:
    result: object
    validated: dict
    heights: dict
    document: dict


@dataclass
class _ReadSession:
    validated: dict = field(default_factory=dict)
    heights: dict = field(default_factory=dict)
    owners: list = field(default_factory=list)
    derived: dict = field(default_factory=dict)
    property_checks: dict = field(default_factory=dict)
    content: dict = field(default_factory=dict)
    reuse_content: bool = True


def region_derived_cache(name):
    session = _session.get()
    return session.derived.setdefault(name, {}) if session is not None else None


def publish_region_properties(properties):
    session = _session.get()
    if session is None:
        return properties
    derived = region_derived_cache('sealed_properties')
    from file_handlers.sdf.graph_snapshot import freeze_properties
    key = _content_key(properties) if session.reuse_content else None
    if key is not None:
        sealed = derived.get(key)
        if sealed is not None:
            return sealed
    sealed = freeze_properties(properties)
    if key is not None:
        derived[key] = sealed
    return sealed


def region_content_hash(owner, document):
    cache = region_derived_cache('content_hashes')
    key = id(owner)
    if cache is not None and key in cache:
        return cache[key][1]
    result = hashlib.sha256(json.dumps(document(), sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if cache is not None:
        cache[key] = owner, result
    return result


@contextmanager
def region_read_session(*, reuse_content=True):
    if _session.get() is not None:
        yield
        return
    session = _ReadSession(reuse_content=reuse_content)
    token = _session.set(session)
    try:
        with immutable_property_validation(session.property_checks):
            yield
    finally:
        _session.reset(token)


@contextmanager
def suspend_region_read_session():
    session_token = _session.set(None)
    validated_token = _validated.set(None)
    try:
        with immutable_property_validation():
            yield
    finally:
        _validated.reset(validated_token)
        _session.reset(session_token)


def _key(kind, properties):
    return kind, tuple(id(properties.get(name)) for name in _fields[kind])


def region_identity(kind, properties):
    read_region(kind, properties)
    return _key(kind, properties)


def _content_key(document):
    try:
        return json.dumps(document, sort_keys=True, separators=(',', ':'))
    except (TypeError, ValueError):
        return None


def read_region(kind, properties):
    validated = _validated.get()
    if validated is not None:
        key = _key(kind, properties)
        if key not in validated:
            raise ShaderGraphError('An embedded region was accessed before its dependencies were validated.')
        return validated[key]
    session = _session.get()
    key = _key(kind, properties)
    if session is not None and key in session.validated:
        return session.validated[key]
    document = {name: properties[name] for name in _fields[kind] if name in properties}
    if session is None:
        snapshot = _read(kind, json.dumps(document, sort_keys=True, separators=(',', ':')))
    else:
        content = _content_key(document) if session.reuse_content else None
        if content is not None and (kind, content) in session.content:
            result, height = session.content[(kind, content)]
            session.validated[key] = result
            session.heights[key] = height
            session.owners.append(properties)
            return result
        with immutable_property_validation(session.property_checks):
            snapshot = _validate_snapshot(kind, document)
        if content is not None:
            session.content[(kind, content)] = (snapshot.result, snapshot.heights[key])
        session.validated.update(snapshot.validated)
        session.heights.update(snapshot.heights)
        session.validated[key] = snapshot.result
        session.owners.extend((snapshot, properties))
    return snapshot.result


@lru_cache(maxsize=16)
def _read(kind, payload):
    with immutable_property_validation():
        return _validate_snapshot(kind, json.loads(payload))


def _validate_snapshot(kind, root):
    from .material_function import MAX_FUNCTION_DEPTH, _parse_function_snapshot
    from .material_branch import _parse_branch
    from .material_loop import _parse_loop
    parsers = {'branch': _parse_branch, 'repeat': _parse_loop, 'material_function': _parse_function_snapshot}
    validated, heights, children, active = {}, {}, {}, set()
    session = _session.get()
    token = _validated.set(validated)
    pending = [(kind, root, 1, False)]
    try:
        while pending:
            current_kind, properties, depth, leaving = pending.pop()
            key = _key(current_kind, properties)
            if leaving:
                validated[key] = parsers[current_kind](properties)
                heights[key] = 1 + max((heights[child] for child in children[key]), default=0)
                active.remove(key)
                continue
            if key in active or depth > MAX_FUNCTION_DEPTH:
                raise ShaderGraphError(f'Embedded regions exceed the {MAX_FUNCTION_DEPTH}-region depth budget or contain a cycle.')
            if key in validated:
                if depth + heights[key] - 1 > MAX_FUNCTION_DEPTH:
                    raise ShaderGraphError(f'Embedded regions exceed the {MAX_FUNCTION_DEPTH}-region depth budget.')
                continue
            if session is not None and key in session.validated:
                if depth + session.heights[key] - 1 > MAX_FUNCTION_DEPTH:
                    raise ShaderGraphError(f'Embedded regions exceed the {MAX_FUNCTION_DEPTH}-region depth budget.')
                validated[key] = session.validated[key]
                heights[key] = session.heights[key]
                continue
            active.add(key)
            children[key] = []
            pending.append((current_kind, properties, depth, True))
            for name in REGION_ASSET_FIELDS[current_kind]:
                asset = properties[name]
                for node in asset['graph']['nodes']:
                    if node['kind'] in _fields:
                        children[key].append(_key(node['kind'], node['properties']))
                        pending.append((node['kind'], node['properties'], depth + 1, False))
        return _Snapshot(validated[_key(kind, root)], validated, heights, root)
    except ShaderGraphError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ShaderGraphError(f'Invalid embedded material region: {exc}') from exc
    finally:
        _validated.reset(token)
