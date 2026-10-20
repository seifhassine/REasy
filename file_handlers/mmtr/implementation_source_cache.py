from dataclasses import fields, is_dataclass, MISSING
import dataclasses
import contextlib
from functools import _lru_cache_wrapper
from importlib import import_module
import inspect
import json
import re
from types import FunctionType
from copy import deepcopy

lazy from file_handlers.sdf.graph_snapshot import _Object, _Array, compiler_graph_copy
from file_handlers.sdf.shader_graph import (
    GeneratedShaderSource, ShaderContract, ShaderGraph, ShaderGraphNode,
    ShaderGraphLink, ShaderGraphError, ShaderSignatureField, ShaderContractResource,
    ShaderContractBuffer, ShaderContractVariable,
)
from file_handlers.sdf.shader_stage_execution import ShaderPrimitiveExecution
lazy from file_handlers.sdf import shader_graph
lazy from .material_region_read import REGION_ASSET_FIELDS
from .native_build_cache import NativeBuildCache, _Uncacheable, native_build_key


_SOURCE_CACHE = NativeBuildCache(max_entries=32, max_bytes=16 * 1024 * 1024)
_SOURCE_CONFIGURATION = ('authored-dxil-source-v1',)

_MODULES = tuple('file_handlers.' + name for name in (
    'sdf.shader_graph', 'sdf.shader_graph_math', 'sdf.shader_graph_texture',
    'sdf.shader_graph_buffer', 'sdf.shader_graph_resource',
    'sdf.shader_graph_dimensions', 'sdf.shader_graph_indexed_value',
    'sdf.shader_graph_unassigned', 'sdf.shader_graph_execution',
    'sdf.shader_graph_precision', 'sdf.shader_graph_constant_buffer',
    'sdf.aggregate_values', 'sdf.hlsl_contract',
    'sdf.graph_snapshot', 'material_names', 'mmtr.material_function', 'mmtr.material_region_read',
    'mmtr.material_branch', 'mmtr.material_loop', 'mmtr.region_dependencies',
    'mmtr.region_codegen', 'mmtr.function_region', 'mmtr.region_inline_policy',
))
_DIRECT_FUNCTIONS = {'file_handlers.sdf.dxil_structured_views': ('structured_view_spaces',),
                     'file_handlers.sdf.models': ('shader_target',), 'utils.hex_util': ('align',)}
_LIBRARY_MODULES = frozenset((
    'builtins', 'copy', 'dataclasses', 'contextlib', 'functools', 'uuid', 'typing', 'pathlib', '_contextvars', 'collections.abc',
    'file_handlers.sdf.dxbc', 'file_handlers.sdf.dxc',
    'file_handlers.sdf.shader_stage_execution', 'file_handlers.sdf.spirv_execution',
    # Node labels are translated; the cache is per session, so its language cannot change.
    'PySide6.QtCore',
))
_CONTEXTMANAGER_CODE = contextlib.contextmanager(lambda: None).__code__
_GENERATED_METHODS = frozenset(('__repr__', '__eq__', '__hash__', '__setattr__',
                              '__delattr__', '__getstate__', '__setstate__'))
# A class's annotations compile into a function of their own; they do not change generation.
_ANNOTATIONS = frozenset(('__annotate_func__',))
_CONTRACT_RECORDS = frozenset((ShaderContract, ShaderSignatureField, ShaderContractResource,
                             ShaderContractBuffer, ShaderContractVariable, ShaderPrimitiveExecution))


def source_cache_info():
    return _SOURCE_CACHE.info()


def _plain_contract(contract):
    active, pending = set(), [(contract, False)]
    while pending:
        value, leaving = pending.pop()
        kind = type(value)
        if kind in (type(None), bool, int, float, str, bytes):
            continue
        if kind not in _CONTRACT_RECORDS and kind not in (tuple, list):
            return False
        if leaving:
            active.remove(id(value))
            continue
        if id(value) in active:
            return False
        active.add(id(value))
        pending.append((value, True))
        pending.extend((item, False) for item in (value if kind in (tuple, list) else
                       (getattr(value, field.name) for field in fields(value))))
    return True


def _plain_graph_document(graph, *, checked_sealed=None):
    if (type(graph) is not ShaderGraph or type(graph.stage) is not str
            or type(graph.contract_fingerprint) is not str
            or type(graph.nodes) is not list or type(graph.links) is not list):
        return None
    for node in graph.nodes:
        if (type(node) is not ShaderGraphNode or type(node.node_id) is not str
                or type(node.kind) is not str or type(node.x) is not float
                or type(node.y) is not float):
            return None
    for link in graph.links:
        if type(link) is not ShaderGraphLink or any(type(value) is not str for value in
                (link.source_node, link.source_pin, link.target_node, link.target_pin)):
            return None
    document = graph.to_dict()
    checked_sealed = {} if checked_sealed is None else checked_sealed
    scalars = (str, float, int, bool, type(None))
    containers = (dict, list, _Object, _Array)
    sealed = (_Object, _Array)
    active, pending = set(), [(document, None)]
    mutable_reads = 0
    while pending:
        value, entered_at = pending.pop()
        kind = type(value)
        if kind not in containers:
            return None
        identity = id(value)
        if entered_at is not None:
            active.remove(identity)
            if kind in sealed and entered_at == mutable_reads:
                checked_sealed[identity] = value
            continue
        if identity in active:
            return None
        if kind in sealed and checked_sealed.get(identity) is value:
            continue
        active.add(identity)
        if kind not in sealed:
            mutable_reads += 1
        pending.append((value, mutable_reads))
        if kind in (dict, _Object):
            if any(type(key) is not str for key in value):
                return None
        for item in value.values() if kind in (dict, _Object) else value:
            if type(item) not in scalars:
                pending.append((item, None))
    from file_handlers.sdf.shader_graph import _json_properties, immutable_property_validation
    with immutable_property_validation():
        if not _json_properties(document):
            return None
    return document


def _runtime_state(generator, graph_builder, kinds):
    identities, functions, active = [], {}, set()

    def identity(value):
        identities.append(value)
        return len(identities) - 1

    def state(value):
        kind = type(value)
        if kind in (type(None), bool, int, float, str, bytes):
            return value
        if kind is sentinel:
            return ('sentinel', identity(value))
        if isinstance(value, re.Pattern):
            return ('regex', value.pattern, value.flags)
        if isinstance(value, _lru_cache_wrapper):
            return ('memoized-function', identity(value), state(value.__wrapped__))
        if isinstance(value, FunctionType):
            owner = value.__module__
            if owner not in _MODULES and owner not in _DIRECT_FUNCTIONS and owner not in _LIBRARY_MODULES:
                raise _Uncacheable(f'External generation callback: {owner}.{value.__qualname__}')
            if value.__globals__ is not vars(import_module(owner)):
                if (value.__globals__ is not vars(contextlib)
                        or value.__code__ is not _CONTEXTMANAGER_CODE):
                    raise _Uncacheable(f'Callback has external globals: {owner}.{value.__qualname__}')
            if value in functions:
                return ('function-reference', functions[value])
            index = identity(value)
            functions[value] = index
            identity(value.__code__)
            return ('function', index, state(value.__defaults__), state(value.__kwdefaults__),
                    tuple(state(cell.cell_contents) for cell in value.__closure__ or ()))
        if inspect.isbuiltin(value):
            return ('runtime-object', identity(value))
        if isinstance(value, type):
            if value.__module__ not in _MODULES and value.__module__ not in _LIBRARY_MODULES:
                raise _Uncacheable(f'External generation class: {value.__module__}.{value.__qualname__}')
            return ('runtime-class', identity(value))
        if value is MISSING or value is dataclasses._HAS_DEFAULT_FACTORY:
            return ('dataclass-default-marker', identity(value))
        if id(value) in active:
            raise _Uncacheable('Cyclic generation configuration')
        active.add(id(value))
        try:
            if kind in (tuple, list, set, frozenset):
                return (kind.__name__, tuple(state(item) for item in value))
            if kind is dict:
                return ('dict', tuple((state(key), state(item)) for key, item in value.items()))
            if is_dataclass(value) and not isinstance(value, type):
                return ('record', identity(kind), tuple((field.name, state(getattr(value, field.name)))
                                                       for field in fields(value)))
            raise _Uncacheable(f'Unrepresented generation configuration: {kind.__module__}.{kind.__qualname__}')
        finally:
            active.remove(id(value))

    try:
        configuration = [('entry', state(generator), state(graph_builder)),
                         ('policy', _SOURCE_CONFIGURATION)]
        for name, names in _DIRECT_FUNCTIONS.items():
            module = import_module(name)
            configuration.append((name, tuple((name, state(getattr(module, name))) for name in names)))
        modules = [import_module(name) for name in _MODULES]
        graph_module = modules[0]
        from . import region_inline_policy
        stock_kinds = {kind for kind, _extension in region_inline_policy._STOCK_EXTENSIONS}
        configuration.append(('inline-stock-registrations', tuple(
            (kind, graph_module.shader_graph_node_extension(kind) is extension)
            for kind, extension in region_inline_policy._STOCK_EXTENSIONS)))
        for kind in sorted(kinds):
            extension = graph_module.shader_graph_node_extension(kind)
            if extension is None:
                if kind not in graph_module._NODE_KINDS:
                    raise _Uncacheable('Unknown graph node')
                continue
            if kind not in stock_kinds:
                raise _Uncacheable('External graph extension')
            for name in region_inline_policy._CALLBACK_FIELDS:
                callback = getattr(extension, name)
                if callback is not None and (not isinstance(callback, FunctionType)
                                             or callback.__module__ not in _MODULES):
                    raise _Uncacheable('External graph extension callback')
            configuration.append(('extension', kind, state(extension)))
        for module in modules:
            members = []
            for name, value in vars(module).items():
                if isinstance(value, (FunctionType, _lru_cache_wrapper)) or inspect.isbuiltin(value):
                    members.append((name, state(value)))
                elif isinstance(value, type) and value.__module__ == module.__name__:
                    methods = []
                    for method_name, method in vars(value).items():
                        if method_name in _ANNOTATIONS:
                            continue
                        if isinstance(method, (staticmethod, classmethod)):
                            method = method.__func__
                        # Dataclasses wrap the generated __repr__ in reprlib.recursive_repr.
                        generated = inspect.unwrap(method) if isinstance(method, FunctionType) else method
                        if (method_name in _GENERATED_METHODS and isinstance(generated, FunctionType)
                                and (generated.__code__.co_filename == '<string>'
                                     or generated.__globals__ is vars(dataclasses))):
                            continue
                        if isinstance(method, property):
                            methods.append((method_name, tuple(state(fn) for fn in
                                           (method.fget, method.fset, method.fdel) if fn is not None)))
                        elif callable(method):
                            methods.append((method_name, state(method)))
                    members.append((name, identity(value), tuple(methods)))
                elif isinstance(value, type):
                    members.append((name, state(value)))
                elif callable(value) and name not in {'Any', 'Callable', 'Iterable'}:
                    members.append((name, state(value)))
                elif ((name.isupper() and name not in {'_NODE_EXTENSIONS', '_MISSING'})
                      or name == '_fields'):
                    members.append((name, state(value)))
            configuration.append((module.__name__, tuple(members)))
        return tuple(identities), tuple(configuration)
    finally:
        state = None


def _graph_kinds(document):
    pending, kinds = [document], set()
    while pending:
        graph = pending.pop()
        for node in graph['nodes']:
            kind = node['kind']
            kinds.add(kind)
            pending.extend(node['properties'][name]['graph'] for name in REGION_ASSET_FIELDS.get(kind, ()))
    return kinds


def generate_implementation_source(implementation, contract, *, early_depth_stencil,
                                   source_profile, generator, graph_builder):
    def ordinary():
        return generator(graph_builder(implementation.graph, source_profile=source_profile),
                         contract, early_depth_stencil=early_depth_stencil)

    if (type(early_depth_stencil) is not bool
            or (source_profile is not None and type(source_profile) is not str)
            or type(contract) is not ShaderContract or type(implementation.contract) is not ShaderContract
            or contract.model_major < 6 or contract.stage not in {'pixel', 'vertex'}
            or contract.vulkan_execution is not None
            or not _plain_contract(contract) or not _plain_contract(implementation.contract)):
        return ordinary()
    checked_sealed = {}
    initial_document = _plain_graph_document(implementation.graph, checked_sealed=checked_sealed)
    if initial_document is None:
        return ordinary()
    try:
        _runtime_state(generator, graph_builder, _graph_kinds(initial_document))
    except (TypeError, ValueError, KeyError, RecursionError, OverflowError):
        return ordinary()
    from .material_region_read import region_read_session
    with region_read_session():
        owned = compiler_graph_copy(implementation.graph)
        source_contract, owned_contract = deepcopy(implementation.contract), deepcopy(contract)
        if not _plain_contract(source_contract) or not _plain_contract(owned_contract):
            return ordinary()
        graph = graph_builder(owned, source_profile=source_profile)
        errors = [item.message for item in shader_graph.validate_graph(graph, owned_contract)
                  if item.severity == 'error']
        if errors:
            raise ShaderGraphError('\n'.join(errors))
        document = _plain_graph_document(graph, checked_sealed=checked_sealed)
        if document is None:
            return ordinary()
        key = runtime_key = None
        try:
            payload = json.dumps(document, separators=(',', ':'), allow_nan=False)
            kinds = _graph_kinds(document)
            runtime, configuration = _runtime_state(generator, graph_builder, kinds)
            runtime_key = native_build_key(runtime, configuration)
            key = native_build_key(runtime, _SOURCE_CONFIGURATION, payload, source_contract,
                                   owned_contract, early_depth_stencil, source_profile, configuration)
        except (TypeError, ValueError, RecursionError, OverflowError):
            pass
        cached = _SOURCE_CACHE.get(key)
        if cached is not None:
            return cached
        result = generator(graph, owned_contract, early_depth_stencil=early_depth_stencil)
        if key is not None and type(result) is GeneratedShaderSource:
            try:
                after_runtime, after_configuration = _runtime_state(generator, graph_builder, kinds)
                if runtime_key == native_build_key(after_runtime, after_configuration):
                    _SOURCE_CACHE.put(key, result)
            except (TypeError, ValueError, RecursionError, OverflowError):
                pass
        return result
