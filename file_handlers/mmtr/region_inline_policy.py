import importlib
import json
import re
from types import FunctionType

from file_handlers.sdf import shader_graph

from .material_region_read import REGION_ASSET_FIELDS


MIN_ROOT_NODES = 128
MIN_REGIONS = 64
MIN_REGION_DEPTH = 8
MAX_REGION_DEPTH = 24
MAX_EXPANDED_NODES = 32768
MAX_EXPANDED_LINKS = 65536
MAX_REGION_PORTS = 20000
MAX_ARM_PORTS = 512
MAX_NODE_PROPERTY_BYTES = 16 * 1024
MAX_PROPERTY_BYTES = 4 * 1024 * 1024
MAX_REPEATED_NODES = 8192
MAX_REPEATED_SUBTREE_NODES = 1024
MAX_REPEATED_FRACTION_DENOMINATOR = 4
_RECOVERED_ID = re.compile(r'(?:region|repeat)\.[0-9a-f]{64}\Z')
_CORE_KINDS = shader_graph._NODE_KINDS - {
    'material_function', 'material_surface', 'material_parameter', 'material_texture',
}
_CALLBACK_FIELDS = (
    'expression', 'pins_provider', 'hlsl_helpers_provider', 'evaluation',
    'required_buffers_provider', 'entry_parameters_provider',
)
_STOCK_EXTENSION_KINDS = (
    'branch', 'repeat', 'constant_buffer_load', 'constant_buffer_row', 'parameter_element', 'resource_index',
    'descriptor_heap', 'resource_dimensions', 'indexed_select', 'array_read', 'constant_table', 'unassigned_value',
    'execution_begin', 'capture_value', 'discard_pixel', 'memory_load', 'memory_store', 'memory_atomic',
    'memory_barrier', 'image_load', 'image_store', 'image_atomic',
)


def _stock_extension(kind):
    module_name, = shader_graph.EXTENSION_MODULES[kind]
    module = importlib.import_module(module_name)
    extension = shader_graph.shader_graph_node_extension(kind)
    if extension is None:
        return None
    for field in _CALLBACK_FIELDS:
        callback = getattr(extension, field)
        if callback is None:
            continue
        if (type(callback) is not FunctionType or callback.__module__ != module.__name__
                or callback.__globals__ is not vars(module)):
            return None
        if callback.__name__ == '<lambda>':
            if field != 'pins_provider' or kind not in {'constant_buffer_load', 'unassigned_value'}:
                return None
        elif callback is not getattr(module, callback.__name__, None):
            return None
    return extension


_STOCK_EXTENSIONS = tuple((kind, _stock_extension(kind)) for kind in _STOCK_EXTENSION_KINDS)


def inline_recovered_regions(graph, contract):
    if (type(graph) is not shader_graph.ShaderGraph or type(contract) is not shader_graph.ShaderContract
            or contract.stage not in {'pixel', 'vertex'} or contract.model_major < 6
            or contract.vulkan_execution is not None or contract.primitive_execution is not None
            or contract.mesh_execution is not None or len(graph.nodes) < MIN_ROOT_NODES):
        return False
    roots = [node for node in graph.nodes if node.kind in {'branch', 'repeat'}]
    if not roots:
        return False
    from .function_region import _compiler_body
    from .region_codegen import region_assets, region_codegen_key

    extensions = dict(_STOCK_EXTENSIONS)
    kinds = set()
    total_nodes = total_links = total_ports = property_bytes = region_count = depth = 0
    repeated_nodes = 0
    seen = set()
    pending = [('graph', graph, 0)]
    while pending:
        operation, value, position = pending.pop()
        if operation == 'end_region':
            size = 1 + total_nodes - position
            key = region_codegen_key(value)
            if key in seen:
                repeated_nodes += size
                if size > MAX_REPEATED_SUBTREE_NODES or repeated_nodes > MAX_REPEATED_NODES:
                    return False
            seen.add(key)
            continue
        if operation == 'region':
            region_count += 1
            depth = max(depth, position)
            if position > MAX_REGION_DEPTH:
                return False
            assets = region_assets(value)
            pending.append(('end_region', value, total_nodes))
            for asset in assets:
                if (not _RECOVERED_ID.fullmatch(asset.function_id) or asset.category != 'Control Flow'
                        or asset.source_profile):
                    return False
                count = len(asset.inputs) + len(asset.outputs)
                total_ports += count
                if count > MAX_ARM_PORTS or total_ports > MAX_REGION_PORTS:
                    return False
                pending.append(('graph', _compiler_body(asset), position))
            continue

        if (type(value) is not shader_graph.ShaderGraph
                or any(type(link) is not shader_graph.ShaderGraphLink for link in value.links)):
            return False
        total_nodes += len(value.nodes)
        total_links += len(value.links)
        if total_nodes > MAX_EXPANDED_NODES or total_links > MAX_EXPANDED_LINKS:
            return False
        for node in value.nodes:
            if type(node) is not shader_graph.ShaderGraphNode:
                return False
            kind = node.kind
            if kind not in kinds:
                extension = shader_graph.shader_graph_node_extension(kind)
                if ((kind in _CORE_KINDS and extension is not None)
                        or (kind not in _CORE_KINDS and (extension is None or extension is not extensions.get(kind)))):
                    return False
                kinds.add(kind)
            nested = () if kind == 'material_function' else REGION_ASSET_FIELDS.get(kind, ())
            properties = {key: item for key, item in node.properties.items() if key not in nested}
            try:
                size = len(json.dumps(properties, separators=(',', ':'), allow_nan=False).encode('utf-8'))
            except (TypeError, ValueError, OverflowError, RecursionError):
                return False
            property_bytes += size
            if size > MAX_NODE_PROPERTY_BYTES or property_bytes > MAX_PROPERTY_BYTES:
                return False
            if nested:
                pending.append(('region', node, position + 1))
    return (region_count >= MIN_REGIONS and depth >= MIN_REGION_DEPTH
            and repeated_nodes * MAX_REPEATED_FRACTION_DENOMINATOR <= total_nodes)
