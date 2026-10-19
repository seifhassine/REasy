from __future__ import annotations

from dataclasses import dataclass, replace
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
import gc
import hashlib
import json
import math
import re
import struct

from file_handlers.sdf.aggregate_values import constant_value_fields
from file_handlers.sdf.shader_graph import (
    MAX_GRAPH_LINKS, ShaderContract, ShaderGraph, ShaderGraphError, ShaderGraphLink,
)
from file_handlers.sdf.shader_graph_layout import arrange_shader_graph
from file_handlers.sdf.shader_graph_math import (
    TYPED_MATH_NODES, COLLECTIVE_NODES, typed_math_signature, NUMERIC_BASE_TYPES, is_float_type, value_width,
)
from file_handlers.sdf.shader_graph_texture import TextureOperation, TEXTURE_SPATIAL_WIDTHS, TEXTURE_LOAD_COORDINATES
from file_handlers.sdf.shader_graph_buffer import BufferRead
from .native_local_memory import split
from .material_function import (
    MaterialFunctionAsset, MaterialFunctionError, MaterialFunctionPin,
    _compiler_function_asset,
)
from .profiles.dxbc_recovery import DxbcSemanticIr
from .profiles.re_engine_dxil_recovery import ReEngineDxilSemanticIr
from .profiles.re_engine_dxil_signatures import DxilEntryMetadata


class VanillaFunctionError(MaterialFunctionError):
    pass


@dataclass(slots=True)
class _RecoveryAllocation:
    needs_collection: bool


_RECOVERY_ALLOCATION = ContextVar("mmtr_recovery_allocation", default=None)


@contextmanager
def _recovery_allocation_batch(ir):
    needs_collection = type(ir) is not DxbcSemanticIr and type(ir) is not ReEngineDxilSemanticIr
    enabled = gc.isenabled()
    if not enabled:
        allocation = _RECOVERY_ALLOCATION.get()
        if allocation is not None:
            allocation.needs_collection |= needs_collection
        yield
        return
    allocation = _RecoveryAllocation(needs_collection)
    gc.disable()
    token = _RECOVERY_ALLOCATION.set(allocation)
    try:
        yield
    finally:
        try:
            if allocation.needs_collection:
                gc.collect()
        finally:
            _RECOVERY_ALLOCATION.reset(token)
            gc.enable()


def _writable(resource):
    return getattr(resource, 'input_type', None) not in {'texture', 'structured-buffer', 'byte-address-buffer'}


class _RecoveryGraph(ShaderGraph):
    __slots__ = ("_connected_targets", "_nodes_by_id", "_node_positions", "_incoming_links")

    def node(self, node_id):
        indexed = getattr(self, "_nodes_by_id", None)
        if indexed is None:
            indexed = {}
            positions = {}
            for position, node in enumerate(self.nodes):
                indexed.setdefault(node.node_id, node)
                positions.setdefault(node.node_id, position)
            self._nodes_by_id = indexed
            self._node_positions = positions
        return indexed.get(node_id)

    def nodes_since(self, start):
        return {node.node_id for node in self.nodes[start:]}

    def add_node(self, *args, **kwargs):
        node = super().add_node(*args, **kwargs)
        self._nodes_by_id[node.node_id] = node
        self._node_positions[node.node_id] = len(self.nodes) - 1
        return node

    def append_node(self, node):
        self.nodes.append(node)
        indexed = getattr(self, "_nodes_by_id", None)
        if indexed is not None:
            indexed.setdefault(node.node_id, node)
            self._node_positions.setdefault(node.node_id, len(self.nodes) - 1)

    def discard_nodes(self, identities):
        self.node(None)  # Initialize both indices for pre-populated graphs.
        positions = self._node_positions
        affected = [positions[identity] for identity in identities if identity in positions]
        if not affected:
            return
        start = min(affected)
        kept = [node for node in self.nodes[start:] if node.node_id not in identities]
        self.nodes[start:] = kept
        for identity in identities:
            self._nodes_by_id.pop(identity, None)
            positions.pop(identity, None)
        for position, node in enumerate(kept, start):
            positions[node.node_id] = position

    def incoming_links(self):
        table = getattr(self, '_incoming_links', None)
        if table is None:
            table = {(link.target_node, link.target_pin): link for link in self.links}
            self._incoming_links = table
        return table

    def discard_links(self, identities):
        table = getattr(self, '_incoming_links', None)
        kept = []
        for link in self.links:
            if link.source_node in identities or link.target_node in identities:
                if table is not None:
                    table.pop((link.target_node, link.target_pin), None)
            else:
                kept.append(link)
        self.links[:] = kept

    def keep_nodes(self, identities):
        self.discard_nodes({node.node_id for node in self.nodes} - identities)

    def connect(self, source_node, source_pin, target_node, target_pin):
        if len(self.links) >= MAX_GRAPH_LINKS:
            raise ShaderGraphError(f"shader graph is limited to {MAX_GRAPH_LINKS} links")
        if source_node == target_node:
            raise ShaderGraphError("a node cannot connect to itself")
        targets = getattr(self, "_connected_targets", None)
        if targets is None:
            targets = {(link.target_node, link.target_pin) for link in self.links}
            self._connected_targets = targets
        key = target_node, target_pin
        if key in targets:
            link = super().connect(source_node, source_pin, target_node, target_pin)
            table = getattr(self, '_incoming_links', None)
            if table is not None:
                table[(link.target_node, link.target_pin)] = link
            return link
        link = ShaderGraphLink(str(source_node), str(source_pin), str(target_node), str(target_pin))
        targets.add((link.target_node, link.target_pin))
        self.links.append(link)
        table = getattr(self, '_incoming_links', None)
        if table is not None:
            table[(link.target_node, link.target_pin)] = link
        return link

    def publish(self):
        return ShaderGraph(self.stage, self.contract_fingerprint, self.nodes, self.links)


def _recovery_graph_digest(graph):
    from .material_region_read import REGION_ASSET_FIELDS, region_derived_cache
    from file_handlers.sdf.graph_snapshot import IMMUTABLE_JSON
    entries = region_derived_cache("recovery_graph_hashes")
    if entries is None:
        entries = {}

    def digest(document, domain):
        encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(domain + encoded).hexdigest()

    def graph_digest(document, read_asset):
        result = dict(document)
        nodes = []
        for node in document["nodes"]:
            names = REGION_ASSET_FIELDS.get(node["kind"])
            if names:
                properties = dict(node["properties"])
                for name in names:
                    properties[name] = {"recovered_asset_digest": read_asset(read_asset, properties[name])}
                node = {**node, "properties": properties}
            nodes.append(node)
        result["nodes"] = nodes
        return digest(result, b"reasy.recovered.graph.v2\0")

    def asset_digest(recurse, document):
        key = id(document)
        cached = entries.get(key)
        if cached is not None:
            return cached[1]
        result = digest({**document, "graph": {"recovered_graph_digest": graph_digest(document["graph"], recurse)}},
                        b"reasy.recovered.asset.v2\0")
        if isinstance(document, IMMUTABLE_JSON):
            entries[key] = document, result
        return result

    return graph_digest(graph.to_dict(), asset_digest)


@dataclass(frozen=True)
class _Value:
    node: str
    pin: str = "value"
    value_type: str = "float"


@dataclass(frozen=True)
class _BufferLoad:
    resource: object
    address: _Value
    record_offset: int | _Value | None
    mask: int


@dataclass(frozen=True)
class _LoadedWords:
    words: tuple[_Value | None, ...]


@dataclass(frozen=True)
class _ResourceHandle:
    resource: object
    index: _Value | None = None
    non_uniform: bool = False
    heap: bool = False
    globally_coherent: bool = False

    @property
    def bind_count(self):
        return 1  # One selected object; the table extent belongs to resource.

    def __getattr__(self, name):
        return getattr(self.resource, name)


@dataclass(frozen=True)
class _HeapIndex:
    index: _Value
    sampler_heap: bool
    non_uniform: bool


def _copy_region_value(value, graph, nodes, links, region, copied, capture, ports, shared=()):
    from file_handlers.sdf.shader_graph import node_pins
    def pins(identity):
        if identity not in ports:
            ports[identity] = node_pins(nodes[identity], None)
        return ports[identity]
    def external(value):
        if nodes[value.node].kind in {'constant', 'unassigned_value'}:
            if value.node not in copied:
                copied.add(value.node)
                graph.append_node(deepcopy(nodes[value.node]))
            return value.node,value.pin
        return capture(value)
    if value.node not in region:
        return external(value)
    pending = [value.node]
    while pending:
        identity = pending.pop()
        if identity in copied:
            continue
        copied.add(identity)
        node = nodes[identity]
        graph.append_node(deepcopy(node) if identity in shared else replace(node))
        for pin in pins(identity):
            link = links.get((identity, pin.name)) if pin.direction == 'input' else None
            if link is None:
                continue
            if link.source_node in region:
                source = link.source_node, link.source_pin
                pending.append(link.source_node)
            else:
                source_type = next(p.value_type for p in pins(link.source_node)
                                   if p.direction == 'output' and p.name == link.source_pin)
                source = external(_Value(link.source_node, link.source_pin, source_type))
            graph.connect(*source, identity, pin.name)
    return value.node, value.pin


@dataclass(frozen=True)
class RecoveredBehavior:
    function: MaterialFunctionAsset
    dependencies: tuple[tuple[str, tuple], ...]
    clip_output: str = ""
    execution_output: str = ''
    patch_output_count: int = 0
    patch_execution_output: str = ''

    def bind(self, contract: ShaderContract) -> ShaderGraph:
        from .material_region_read import region_read_session
        from file_handlers.sdf.graph_snapshot import thaw_graph
        with region_read_session():
            graph = self._bind(contract)
        return thaw_graph(graph)

    def _bind(self, contract: ShaderContract) -> ShaderGraph:
        from .material_function import _inline_function
        graph = ShaderGraph(contract.stage, contract.fingerprint)
        instance = graph.add_node("material_function", node_id="behavior")
        for pin_id, key in self.dependencies:
            if key[0] == "input":
                node = graph.add_node("input", properties={"semantic": key[1], "semantic_index": key[2]})
            elif key[0] in {'primitive_input','output_control_point'}:
                output_point = key[0] == 'output_control_point'
                field = next((field for field in contract.outputs if field.key == key[1:3]),None) if output_point else contract.input(key[1],key[2])
                count = (contract.primitive_execution.output_control_points if output_point else contract.primitive_execution.primitive_vertices) if contract.primitive_execution else 0
                if field is None or not 0 <= key[3] < count or output_point and contract.stage != 'hull':
                    raise VanillaFunctionError('The primitive input no longer matches its renderer declaration.')
                node = graph.add_node(key[0],properties={'semantic':field.semantic_name,'semantic_index':field.semantic_index})
                index = graph.add_node('constant',properties={'value_type':'uint','values':[key[3]]})
                graph.connect(index.node_id,'value',node.node_id,'index')
            elif key[0]=='domain_coordinates':
                node = graph.add_node('domain_coordinates')
            elif key[0]=='patch_constant':
                from file_handlers.sdf.shader_graph_tessellation import patch_input_properties
                field = next((f for f in contract.patch_constants if f.key==key[1:]),None)
                if field is None:
                    raise VanillaFunctionError('The patch value no longer has its declared semantic.')
                node = graph.add_node('patch_input',properties=patch_input_properties(contract,field))
            elif key[0] == 'invocation':
                node = graph.add_node('invocation_input', properties={'meaning': key[1]})
            elif key[0] == "constant":
                buffer = contract.buffer(key[1])
                variable = next((v for v in buffer.variables if v.name == key[3]), None) if buffer else None
                if variable is None or buffer.register_space != key[2] or not any(field.path == key[4] for field in constant_value_fields(variable)):
                    raise VanillaFunctionError("The value dependency no longer has its declared renderer field.")
                properties = {"buffer": buffer.name, "variable": variable.name}
                if key[4] or variable.graph_type is None:
                    properties["field_path"] = key[4]
                node = graph.add_node("parameter", properties=properties)
            elif key[0] == 'constant_view':
                resource = contract.resource(key[1])
                if resource is None or (resource.register_space, resource.bind_point) != key[2:4]:
                    raise VanillaFunctionError("The constant buffer has a different renderer binding.")
                node = graph.add_node("shader_resource", properties={"resource": resource.name, 'globally_coherent': False})
            elif key[0] == "resource":
                resource = contract.resource(key[1])
                if resource is None or (resource.register_space, resource.bind_point) != key[2:4]:
                    raise VanillaFunctionError("The resource dependency has a different renderer binding.")
                node = graph.add_node("shader_resource", properties={"resource": resource.name, 'globally_coherent': key[5],
                    **({'word_bits':key[6]} if len(key)>6 else {})})
            else:
                raise VanillaFunctionError("This dependency has no renderer adapter.")
            graph.connect(node.node_id, "value", instance.node_id, pin_id)
        output = graph.add_node("output", node_id="output")
        value_outputs = contract.outputs if contract.stage not in {'geometry','mesh'} else ()
        if self.patch_output_count:
            if contract.stage != 'hull' or self.patch_output_count != len(contract.patch_constants):
                raise VanillaFunctionError('Patch outputs do not match the hull renderer contract.')
            value_outputs += contract.patch_constants
        if len(self.function.outputs) != len(value_outputs) + bool(self.clip_output) + bool(self.execution_output) + bool(self.patch_execution_output):
            raise VanillaFunctionError("The function outputs do not match this renderer contract.")
        for index,(pin, field) in enumerate(zip(self.function.outputs, value_outputs)):
            if pin.name != field.display_name or pin.value_type != field.value_type:
                raise VanillaFunctionError("The function output has a different renderer meaning or type.")
            from file_handlers.sdf.shader_graph_hull import patch_output_pin
            target = patch_output_pin(field) if self.patch_output_count and index >= len(contract.outputs) else field.pin_name
            graph.connect(instance.node_id, pin.pin_id, output.node_id, target)
        if self.clip_output:
            graph.connect(instance.node_id, self.clip_output, output.node_id, "clip")
        if self.execution_output:
            graph.connect(instance.node_id, self.execution_output, output.node_id, 'execution')
        if self.patch_execution_output:
            graph.connect(instance.node_id, self.patch_execution_output, output.node_id, 'patch_execution')
        _inline_function(graph, instance, self.function)
        arrange_shader_graph(graph, contract)
        return graph


class _Builder:
    def __init__(self, contract: ShaderContract, *, ordered=False):
        self.contract = contract
        self.graph = _RecoveryGraph("material_function", "")
        self.inputs: dict[tuple, tuple[MaterialFunctionPin, _Value]] = {}
        self.values: dict[tuple, _Value] = {}
        self.clips: list[_Value] = []
        self.ordered_nodes: set[str] = set()
        self._next_node = 0
        self.precise_math = False
        self.execution = None
        if ordered:
            self.execute('execution_begin')

    def execute(self, kind, *, properties=None, **arguments):
        if kind in {'emit_vertex','end_primitive'}:
            from file_handlers.sdf.shader_graph_geometry import geometry_shape as execution_shape
        elif kind in {'mesh_output_counts','mesh_indices','mesh_vertex','mesh_primitive'}:
            from file_handlers.sdf.shader_graph_mesh import mesh_shape as execution_shape
        elif kind in {'group_read','group_write','dispatch_mesh'}:
            from file_handlers.sdf.shader_graph_storage import storage_shape as execution_shape
        else:
            from file_handlers.sdf.shader_graph_execution import execution_shape
        if kind != 'execution_begin':
            if self.execution is None:
                raise VanillaFunctionError('This effect requires an explicit execution chain.')
            arguments = {'exec': self.execution, **arguments}
        node = self._node(kind, properties=properties or {})
        inputs, outputs = execution_shape(node)
        if dict(inputs) != {pin: value.value_type for pin, value in arguments.items()}:
            raise VanillaFunctionError('Execution operands disagree with the declared operation types.')
        for pin, value in arguments.items():
            self.graph.connect(value.node, value.pin, node.node_id, pin)
        self.ordered_nodes.add(node.node_id)
        result = {pin: _Value(node.node_id, pin, domain) for pin, domain in outputs}
        self.execution = result['next']
        return result

    def capture(self, value):
        if self.execution is None:
            return value
        return self.execute('capture_value', properties={'value_type': value.value_type}, value=value)['value']

    def memory(self, kind, resource, address, *, record_offset=None, operation='add', value=None, compare=None, words=1):
        if resource is not None and resource.input_type == 'rw-typed' and kind == 'memory_atomic':
            original = self.image('image_atomic', resource, address, operation=operation, value=value, compare=compare)['original']
            return {'original': self.convert(original, value.value_type, bits=True)}
        if resource is None or resource.input_type not in {'rw-byte-address-buffer', 'rw-structured-buffer'} or resource.bind_count != 1:
            raise VanillaFunctionError('This writable resource needs its own typed coordinate adapter.')
        structured = resource.input_type == 'rw-structured-buffer'
        if structured != (record_offset is not None):
            raise VanillaFunctionError('The write address does not match its raw/structured resource.')
        word_bits = 64 if structured and kind == 'memory_atomic' and value.value_type.endswith('64_t') else 32
        arguments = {'buffer': self.resource_value(resource, word_bits=word_bits), 'byte_offset': address}
        if structured:
            arguments.update(index=address, byte_offset=self.integer(record_offset) if isinstance(record_offset, int) else record_offset)
        if value is not None:
            arguments['value'] = value
        if compare is not None:
            arguments['compare'] = compare
        properties = {'globally_coherent': bool(getattr(resource, 'globally_coherent', False))}
        if words > 1:
            properties['words'] = words
        if structured:
            properties['stride'] = resource.sample_count
        if kind == 'memory_atomic':
            properties.update(operation=operation, value_type=value.value_type)
        return self.execute(kind, properties=properties, **arguments)

    def image(self, kind, resource, coordinates, *, operation='add', value=None, compare=None):
        if resource is None or resource.input_type != 'rw-typed' or not resource.texture_value_type:
            raise VanillaFunctionError('A writable texture operation requires an identified typed image.')
        element = resource.texture_value_type
        arguments = dict(texture=self.resource_value(resource), coordinates=coordinates)
        domain = value.value_type if kind == 'image_atomic' and value is not None else element
        if value is not None:
            arguments['value'] = self.convert(value, domain, bits=True)
        if compare is not None:
            arguments['compare'] = self.convert(compare, domain, bits=True)
        return self.execute(kind, properties=dict(dimension=resource.dimension.replace('-', ''), element_type=element,
            **({'value_type':domain} if kind == 'image_atomic' else {}), operation=operation,
            globally_coherent=bool(getattr(resource, 'globally_coherent', False))), **arguments)

    def loop_execution_state(self, frame):
        frame['execution_state'] = self.execution is not None
        if self.execution is not None:
            initial, index = self.execution, len(frame['states'])
            node = self._node('function_input', properties=dict(pin_id=f'state_{index}', display_name='Execution', value_type='execution'))
            self.execution = _Value(node.node_id, 'value', 'execution')
            frame['states'].append(('__execution__', initial, self.execution))

    def invocation_input(self, meaning, component=0):
        from file_handlers.sdf.shader_graph_invocation import INVOCATION_INPUTS, invocation_stages
        if meaning not in INVOCATION_INPUTS or self.contract.stage not in invocation_stages(meaning):
            raise VanillaFunctionError('Invocation inputs require their declared renderer stage.')
        label, domain, _ = INVOCATION_INPUTS[meaning]
        if not 0 <= component < (1 if domain == 'uint' else 3):
            raise VanillaFunctionError('A thread invocation read has an invalid component.')
        return self.scalar(self.input(('invocation', meaning), label, domain), component)

    def domain_coordinate(self,component):
        from file_handlers.sdf.shader_graph_tessellation import domain_coordinate_type
        domain = domain_coordinate_type(self.contract)
        if not 0<=component<int(domain[-1]):
            raise VanillaFunctionError('The domain coordinate exceeds its declared dimension.')
        return self.scalar(self.input(('domain_coordinates',),'Domain Coordinates',domain),component)

    def patch_constant(self,field,component):
        if self.contract.stage!='domain' or field not in self.contract.patch_constants:
            raise VanillaFunctionError('A patch constant requires its per-patch domain declaration.')
        return self.scalar(self.input(('patch_constant',*field.key),field.display_name,field.value_type),component)

    def _node(self, kind, *, properties=None):
        from file_handlers.sdf.shader_graph_precision import PRECISION_OPERATIONS
        if self.precise_math and kind in PRECISION_OPERATIONS:
            properties = {**(properties or {}), 'precise': True}
        index = self._next_node
        self._next_node += 1
        return self.graph.add_node(kind, node_id=f"value_{index}",
            x=280.0 * (1 + index // 12), y=100.0 * (index % 12), properties=properties or {})

    def input(self, key, label, value_type):
        if key not in self.inputs:
            identity = "in_" + hashlib.sha256(repr(key).encode()).hexdigest()[:20]
            pin = MaterialFunctionPin(identity, label, value_type)
            node = self._node("function_input", properties={
                "pin_id": identity, "display_name": label, "value_type": value_type,
            })
            self.inputs[key] = pin, _Value(node.node_id, "value", value_type)
        return self.inputs[key][1]

    def merge_branch(self, condition, true_nodes, false_nodes, true_values, false_values, *, clips=((), ()), executions=()):
        from .material_branch import branch_properties
        if len(true_values) != len(false_values):
            raise VanillaFunctionError("Conditional results have different shapes.")
        regions = [set(true_nodes), set(false_nodes)]
        values = [list(true_values), list(false_values)]
        if executions:
            for results, execution in zip(values, executions):
                results.append(execution)
        has_clip = any(clips)
        if has_clip:
            for region, results, masks in zip(regions, values, clips):
                before = len(self.graph.nodes)
                results.append(self.clip_value(masks))
                region.update(self.graph.nodes_since(before))
        for index, (left, right) in enumerate(zip(*values)):
            if left.value_type != right.value_type:
                for region, outputs in zip(regions, values):
                    before = len(self.graph.nodes)
                    outputs[index] = self.convert(outputs[index], "uint", bits=True, reuse=False)
                    region.update(self.graph.nodes_since(before))
        nodes = self.graph._nodes_by_id
        global_inputs = {identity for identity in regions[0] | regions[1]
                         if identity in nodes and nodes[identity].kind in {"function_input", "constant", 'unassigned_value'}}
        regions = [region - global_inputs for region in regions]
        removed = regions[0] | regions[1]
        aliases, unique, pairs = [], {}, []
        for left, right in zip(*values):
            if left == right and left.node not in removed:
                aliases.append(left)
            else:
                pair = left, right
                if pair not in unique:
                    unique[pair] = len(pairs)
                    pairs.append(pair)
                aliases.append(unique[pair])
        values = [[pair[arm] for pair in pairs] for arm in (0,1)]

        def results(joined):
            expanded = [joined[alias] if isinstance(alias,int) else alias for alias in aliases]
            if has_clip:
                self.clips.append(expanded.pop())
            if executions:
                self.execution = expanded.pop()
            return expanded

        if not values[0]:
            if removed & self.ordered_nodes:
                raise VanillaFunctionError('A conditional execution region cannot be discarded as unused calculations.')
            self.graph.discard_nodes(removed)
            self.graph.discard_links(removed)
            self.values = {key: value for key, value in self.values.items() if value.node not in removed}
            return results([])
        links = self.graph.incoming_links()
        captures = {}
        ports = {}
        shared_nodes = regions[0] & regions[1]

        def capture(value):
            if value not in captures:
                captures[value] = MaterialFunctionPin(f"capture_{len(captures)}",
                    nodes[value.node].properties.get("display_name") or f"Input {len(captures) + 1}", value.value_type)
            return captures[value]

        graphs = []
        output_pins = tuple(MaterialFunctionPin(f"result_{index}", f"Result {index + 1}", value.value_type, False)
                            for index, value in enumerate(values[0]))
        for region, outputs in zip(regions, values):
            graph = _RecoveryGraph("material_function", "")
            copied = set()

            def visit(value):
                def external(value):
                    pin = capture(value)
                    return f"branch_{pin.pin_id}", "value"
                return _copy_region_value(value, graph, nodes, links, region, copied, external, ports, shared_nodes)

            for index, (pin, value) in enumerate(zip(output_pins, outputs)):
                source = visit(value)
                output = graph.add_node("function_output", node_id=f"branch_output_{index}", properties=dict(
                    pin_id=pin.pin_id, display_name=pin.name, value_type=pin.value_type))
                graph.connect(*source, output.node_id, "value")
            graphs.append(graph)
        retained = {node.node_id for graph in graphs for node in graph.nodes}
        if (removed & self.ordered_nodes) - retained:
            raise VanillaFunctionError('A conditional region needs explicit ownership of its unused executions.')
        arms = []
        for label, graph in zip(("True", "False"), graphs):
            for value, pin in captures.items():
                graph.add_node("function_input", node_id=f"branch_{pin.pin_id}", properties=dict(
                    pin_id=pin.pin_id, display_name=pin.name, value_type=pin.value_type))
            arrange_shader_graph(graph)
            digest = _recovery_graph_digest(graph)
            arms.append(_compiler_function_asset("region." + digest, label, "Control Flow", "Editable conditional calculations.",
                                              tuple(captures.values()), output_pins, graph.publish()))
        self.graph.discard_nodes(removed)
        self.graph.discard_links(removed)
        self.values = {key: value for key, value in self.values.items() if value.node not in removed}
        branch = self._node("branch", properties=branch_properties(*arms))
        if removed & self.ordered_nodes:
            self.ordered_nodes.difference_update(removed)
            self.ordered_nodes.add(branch.node_id)
        self.graph.connect(condition.node, condition.pin, branch.node_id, "condition")
        for value, pin in captures.items():
            self.graph.connect(value.node, value.pin, branch.node_id, pin.pin_id)
        joined = [_Value(branch.node_id, pin.pin_id, pin.value_type) for pin in output_pins]
        return results(joined)

    def constant(self, value):
        try:
            number = struct.unpack("<f", struct.pack("<f", float(value)))[0]
        except (OverflowError, ValueError) as exc:
            raise VanillaFunctionError("A native literal is outside the finite float domain.") from exc
        if not math.isfinite(number):
            raise VanillaFunctionError("A non-finite native constant needs bit-exact graph representation.")
        # Keep negative zero distinct.
        key = ("constant", struct.pack("<f", number))
        if key not in self.values:
            node = self._node("constant", properties={"value_type": "float", "values": [number]})
            self.values[key] = _Value(node.node_id)
        return self.values[key]

    def merge_loop(self, frame, continuation, updates, exports):
        from .material_loop import loop_properties
        if frame.get('execution_state'):
            updates = [*updates, self.execution]
            exports = {**exports, '__execution__': self.execution}
        region = self.graph.nodes_since(frame['node_start'])
        placeholders = {state.node for _, _, state in frame['states']}
        nodes = self.graph._nodes_by_id
        region = {identity for identity in region if nodes[identity].kind != 'unassigned_value'
                  and (nodes[identity].kind != 'function_input' or identity in placeholders)}
        links = self.graph.incoming_links()
        graph = _RecoveryGraph('material_function', '')
        ports = {}
        inputs, external, state_pairs = [], {}, []
        for i, (name, initial, placeholder) in enumerate(frame['states']):
            pin = MaterialFunctionPin(f'state_{i}', f'State {i + 1}', initial.value_type)
            inputs.append(pin)
            graph.append_node(deepcopy(nodes[placeholder.node]))
            external[pin.pin_id] = initial
            state_pairs.append((pin.pin_id, f'update_{i}'))
        copied = {n.node_id for n in graph.nodes}
        captures = {}
        def capture(value):
            if value not in captures:
                pin = MaterialFunctionPin(f'capture_{len(captures)}', f'Input {len(captures) + 1}', value.value_type)
                inputs.append(pin)
                node = graph.add_node('function_input', properties=dict(pin_id=pin.pin_id, display_name=pin.name, value_type=pin.value_type))
                captures[value] = (node.node_id, 'value')
                external[pin.pin_id] = value
            return captures[value]
        first_update = {}
        for i, value in enumerate(updates):
            first_update.setdefault(value, i)
        shared = {name: first_update.get(value) for name, value in exports.items()}
        results = [('continue_loop', 'Continue', continuation),
                   *((f'update_{i}', f'Updated State {i + 1}', value) for i, value in enumerate(updates)),
                   *((f'result_{i}', f'Result {i + 1}', value) for i, (name, value) in enumerate(exports.items())
                     if shared[name] is None)]
        outputs = []
        for identity, label, value in results:
            outputs.append(MaterialFunctionPin(identity, label, value.value_type, False))
            source = _copy_region_value(value, graph, nodes, links, region, copied, capture, ports)
            output = graph.add_node('function_output', properties=dict(pin_id=identity, display_name=label, value_type=value.value_type))
            graph.connect(*source, output.node_id, 'value')
        arrange_shader_graph(graph)
        if (region & self.ordered_nodes) - copied:
            raise VanillaFunctionError('A repeat region needs explicit ownership of its unused executions.')
        digest = _recovery_graph_digest(graph)
        body = _compiler_function_asset('repeat.' + digest, 'Repeat Step', 'Control Flow', 'Editable calculations for one iteration.', tuple(inputs), tuple(outputs), graph.publish())
        self.graph.discard_nodes(region)
        self.graph.discard_links(region)
        self.values = dict(frame['cache'])
        loop = self._node('repeat', properties=loop_properties(body, state_pairs, 'continue_loop'))
        self.ordered_nodes.difference_update(region)
        self.ordered_nodes.add(loop.node_id)
        for identity, value in external.items():
            self.graph.connect(value.node, value.pin, loop.node_id, identity)
        joined = {name: _Value(loop.node_id, f'result_{i}' if shared[name] is None else f'update_{shared[name]}', value.value_type)
                  for i, (name, value) in enumerate(exports.items())}
        if frame.get('execution_state'):
            self.execution = joined.pop('__execution__')
        return joined

    def float_literal(self, text):
        text = text.strip()
        if re.fullmatch(r"0x[0-9a-fA-F]{16}", text):
            bits = int(text, 16)
            if (bits >> 52) & 0x7ff == 0x7ff:
                mantissa = bits & ((1 << 52) - 1)
                # LLVM spells floats with their double-width encoding; converting through a host double would quiet sNaNs.
                if mantissa & ((1 << 29) - 1):
                    raise VanillaFunctionError("The LLVM special-value literal is not an exact 32-bit float encoding.")
                word = ((bits >> 63) << 31) | 0x7f800000 | (mantissa >> 29)
                return self.convert(self.integer(word), "float", bits=True)
        return self.constant(_float_literal(text))

    def integer(self, value, value_type='uint'):
        bits = 64 if value_type.endswith('64_t') else 32
        number = int(value) & ((1 << bits)-1)
        if value_type in {'int', 'int64_t'} and number >= 1 << (bits-1):
            number -= 1 << bits
        key = ('integer', number, value_type)
        if key not in self.values:
            node = self._node('constant', properties={'value_type': value_type, 'values': [number]})
            self.values[key] = _Value(node.node_id, 'value', value_type)
        return self.values[key]

    def convert(self, value, value_type, *, bits=False, reuse=True):
        if value.value_type == value_type:
            return value
        source = self.graph.node(value.node)
        if bits and source.kind == 'constant' and value.value_type in {'float', 'int', 'uint'} and value_type in {'float', 'int', 'uint'}:
            number = source.properties['values'][0]
            word = struct.unpack('<I', struct.pack('<f', number))[0] if value.value_type == 'float' else int(number) & 0xffffffff
            if value_type != 'float':
                return self.integer(word, value_type)
            number = struct.unpack('<f', struct.pack('<I', word))[0]
            if math.isfinite(number):
                return self.constant(number)
        if bits and source.kind == 'reinterpret' and source.properties['source_type'] == value_type:
            link = next(link for link in self.graph.links if link.target_node == source.node_id)
            return _Value(link.source_node, link.source_pin, value_type)
        kind = 'reinterpret' if bits else 'convert'
        key = (kind, value, value_type)
        if not reuse or key not in self.values:
            properties = {'source_type': value.value_type, 'value_type': value_type}
            typed_math_signature(kind, properties)
            node = self._node(kind, properties=properties)
            self.graph.connect(value.node, value.pin, node.node_id, 'value')
            result = _Value(node.node_id, 'result', value_type)
            if not reuse:
                return result
            self.values[key] = result
        return self.values[key]

    def scalar(self, value, component):
        width = value_width(value.value_type)
        if not 0 <= component < width:
            raise VanillaFunctionError("A native component exceeds its declared logical value.")
        if width == 1:
            return value
        key = ("component", value, component)
        if key not in self.values:
            node = self._node("swizzle", properties={"source_type": value.value_type, "mask": "xyzw"[component]})
            self.graph.connect(value.node, value.pin, node.node_id, "value")
            self.values[key] = _Value(node.node_id, "result", value.value_type.rstrip('234'))
        return self.values[key]

    def boolean(self, value):
        key = ("boolean", bool(value))
        if key not in self.values:
            node = self._node("constant", properties={"value_type": "bool", "values": [bool(value)]})
            self.values[key] = _Value(node.node_id, "value", "bool")
        return self.values[key]

    def unassigned(self, domain):
        key = ('unassigned', domain)
        if key not in self.values:
            node = self._node('unassigned_value', properties={'value_type': domain})
            self.values[key] = _Value(node.node_id, 'value', domain)
        return self.values[key]

    def precise(self, value):
        from file_handlers.sdf.shader_graph_precision import PRECISION_OPERATIONS
        node = self.graph.node(value.node)
        if node is not None and node.kind == 'capture_value':
            link = next(link for link in self.graph.links if link.target_node == node.node_id and link.target_pin == 'value')
            node = self.graph.node(link.source_node)
        if node is not None and node.kind in PRECISION_OPERATIONS:
            node.properties['precise'] = True
        return value

    def operation(self, kind, *, _value_type=None, _properties=None, **inputs):
        types = {value.value_type for value in inputs.values()}
        value_type = _value_type or (next(iter(types)) if len(types) == 1 else None)
        key = (kind, value_type, tuple(inputs.items()), self.precise_math, tuple(sorted((_properties or {}).items())))
        reusable = kind not in COLLECTIVE_NODES
        if not reusable and self.clips:
            raise VanillaFunctionError('Collective lane operations after discard need an ordered execution-effect region.')
        if reusable and key in self.values:
            return self.values[key]
        if value_type is None:
            raise VanillaFunctionError('A native operation mixes incompatible operand types.')
        output_type = value_type
        if kind in TYPED_MATH_NODES:
            expected, output_type = typed_math_signature(kind, {'value_type': value_type})
            if dict(expected) != {name: value.value_type for name, value in inputs.items()}:
                raise VanillaFunctionError('A native operation disagrees with its typed input contract.')
        node = self._node(kind, properties={"value_type": value_type, **(_properties or {})})
        for name, value in inputs.items():
            self.graph.connect(value.node, value.pin, node.node_id, name)
        output = "value" if kind in {"add", "subtract", "multiply", "divide", "power", "minimum", "maximum", "lerp"} else "result"
        result = _Value(node.node_id, output, output_type)
        if not reusable or kind.startswith('derivative_'):
            result = self.capture(result)
        if reusable:
            self.values[key] = result
        return result

    def vector(self, parts):
        if len(parts) == 1:
            return parts[0]
        if not 2 <= len(parts) <= 4:
            raise VanillaFunctionError("A graph value needs one to four components.")
        key = ("compose", tuple(parts))
        if key not in self.values:
            if len({part.value_type for part in parts}) != 1 or parts[0].value_type not in NUMERIC_BASE_TYPES:
                raise VanillaFunctionError('A vector must have components with the same scalar type.')
            value_type = parts[0].value_type + str(len(parts))
            node = self._node("compose", properties={"value_type": value_type})
            for component, value in zip("xyzw", parts):
                self.graph.connect(value.node, value.pin, node.node_id, component)
            self.values[key] = _Value(node.node_id, "value", value_type)
        return self.values[key]

    def dot(self, first, second):
        a, b = self.vector(first), self.vector(second)
        node = self._node("dot", properties={"value_type": a.value_type})
        self.graph.connect(a.node, a.pin, node.node_id, "a")
        self.graph.connect(b.node, b.pin, node.node_id, "b")
        return _Value(node.node_id, value_type=a.value_type.rstrip('234'))

    def _mask_condition(self, value):
        node = self.graph.node(value.node)
        if node is None or node.kind != 'select' or value.value_type not in {'uint', 'int'}:
            return None
        table = self.graph.incoming_links()
        links = {pin: table.get((node.node_id, pin)) for pin in ('condition', 'true', 'false')}
        def literal(pin):
            link = links.get(pin)
            source = self.graph.node(link.source_node) if link else None
            values = source.properties.get('values') if source is not None and source.kind == 'constant' else None
            return int(values[0]) & 0xFFFFFFFF if values and len(values) == 1 else None
        condition = links.get('condition')
        if condition is None or literal('true') != 0xFFFFFFFF or literal('false') != 0:
            return None
        return _Value(condition.source_node, condition.source_pin, 'bool')

    def compare(self, relation, a, b, *, nan_result=False):
        if a.value_type != b.value_type or a.value_type not in NUMERIC_BASE_TYPES:
            raise VanillaFunctionError('A comparison needs matching scalar operand types.')
        if relation in {'equal', 'not_equal'}:
            # Native comparisons produce all-ones/zero masks.
            for mask, other in ((a, b), (b, a)):
                condition = self._mask_condition(mask)
                other_node = self.graph.node(other.node)
                if (condition is not None and other_node is not None and other_node.kind == 'constant'
                        and list(other_node.properties.get('values', ())) == [0]):
                    if relation == 'not_equal':
                        return condition
                    return self.compare('equal', condition, self.boolean(False))
        node = self._node("compare", properties={"relation": relation, "nan_result": nan_result, 'value_type': a.value_type})
        self.graph.connect(a.node, a.pin, node.node_id, "a")
        self.graph.connect(b.node, b.pin, node.node_id, "b")
        return _Value(node.node_id, "result", "bool")

    def select(self, condition, yes, no):
        if condition.value_type != "bool" or yes.value_type != no.value_type:
            raise VanillaFunctionError("This native selection needs a typed mask/value graph.")
        node = self._node("select", properties={"value_type": yes.value_type})
        for pin, value in (("condition", condition), ("true", yes), ("false", no)):
            self.graph.connect(value.node, value.pin, node.node_id, pin)
        return _Value(node.node_id, "result", yes.value_type)

    def leading_bit(self, operand):
        # Native FirstbitHi counts from the MSB; HLSL firstbithigh returns the index from the LSB.
        index = self.operation('first_bit_high', value=operand)
        absent = self.compare('equal', index, self.integer(0xffffffff))
        most_significant_bit = 63 if operand.value_type.endswith('64_t') else 31
        count = self.operation('subtract', a=self.integer(most_significant_bit), b=index)
        return self.select(absent, self.integer(0xffffffff), count)

    def unsigned_division(self, numerator, denominator):
        zero = self.compare('equal', denominator, self.integer(0))
        divisor = self.select(zero, self.integer(1), denominator)
        return tuple(self.select(zero,self.integer(0xffffffff),self.operation(kind,a=numerator,b=divisor))
                     for kind in ('divide','integer_remainder'))

    def word_carry(self, a, b, *, subtract=False):
        result = self.operation('subtract' if subtract else 'add',a=a,b=b)
        carry = self.compare('less',a if subtract else result,b if subtract else a)
        return result,carry

    def discard(self, condition, *, when_true=True):
        if self.contract.stage != "pixel":
            raise VanillaFunctionError("Discard is only a pixel behavior.")
        if self.execution is not None:
            if not when_true:
                condition = self.compare('equal', condition, self.boolean(False))
            self.execute('discard_pixel', condition=condition)
            return
        self.clips.append(self.select(condition, self.constant(-1 if when_true else 1), self.constant(1 if when_true else -1)))

    def clip_value(self, masks):
        if not masks:
            return self.constant(1)
        result = masks[0]
        for value in masks[1:]:
            result = self.operation('minimum', a=result, b=value)
        return result

    def texture_operation(self, kind, texture, coordinates, *, sampler=None, mode='implicit', offset=(), channel=0, **operands):
        if texture is None or not texture.texture_value_type or texture.bind_count != 1:
            raise VanillaFunctionError('This texture needs an indexed or otherwise typed resource dependency.')
        properties = {'dimension': texture.dimension.replace('-', ''), 'element_type': texture.texture_value_type,
            'mode': mode, 'offset': list(offset), 'minimum_lod': 'minimum_lod' in operands}
        if mode in {'gather','gather_compare'}:
            properties.update(channel=channel, dynamic_offset='texel_offset' in operands)
        if 'ms' in properties['dimension']:
            properties['sample_count'] = texture.texture_samples
        try:
            operation = TextureOperation.from_properties(kind, properties)
            operation.validate_stage(self.contract.stage)
        except ValueError as exc:
            raise VanillaFunctionError(str(exc)) from exc
        operands = {'coordinates': self.vector(coordinates), **operands}
        operands['texture'] = self.resource_value(texture)
        if kind == 'sample_texture':
            if sampler is None or sampler.input_type != 'sampler' or sampler.bind_count != 1:
                raise VanillaFunctionError('This sample needs an explicit sampler dependency.')
            operands['sampler'] = self.resource_value(sampler)
        if {name: operand.value_type for name, operand in operands.items()} != dict(operation.inputs):
            raise VanillaFunctionError('Texture operands do not match their declared typed sampling contract.')
        key = (kind, operation, tuple(sorted(operands.items())))
        if key not in self.values or self.execution is not None and _writable(texture):
            node = self._node(kind, properties=properties)
            for name, operand in operands.items():
                self.graph.connect(operand.node, operand.pin, node.node_id, name)
            self.values[key] = self.capture(_Value(node.node_id, operation.result_pin, operation.result_type))
        return self.values[key]

    def stage_input(self, field, component, *, vertex=None, output_point=False):
        if field.component_type not in {'float32', 'uint32', 'sint32'} or field.array_length != 1:
            raise VanillaFunctionError("This input component type or primitive array needs a typed behavior lift.")
        key = ("input", field.semantic_name.casefold(), field.semantic_index)
        count = (self.contract.primitive_execution.output_control_points if output_point else self.contract.primitive_execution.primitive_vertices) if self.contract.primitive_execution else 0
        if output_point and (self.contract.stage != 'hull' or field not in self.contract.outputs):
            raise VanillaFunctionError('Output control points need their declared hull interface.')
        if isinstance(vertex,_Value):
            if vertex.value_type != 'uint' or self.contract.primitive_execution is None:
                raise VanillaFunctionError('A dynamic primitive vertex requires a uint index and a declared input extent.')
            key = ('primitive_selection',field.key,vertex,output_point)
            if key not in self.values:
                elements = [self.vector([self.stage_input(field,lane,vertex=i,output_point=output_point) for lane in range(field.mask.bit_count())])
                            for i in range(count)]
                self.values[key] = self.array_element(vertex,elements)
            return self.scalar(self.values[key],component)
        if vertex is not None:
            if not 0 <= vertex < count:
                raise VanillaFunctionError('A primitive vertex is outside its declared input extent.')
            key = ('output_control_point' if output_point else 'primitive_input',field.semantic_name.casefold(),field.semantic_index,vertex)
        value = self.input(key, field.display_name + (f' (vertex {vertex})' if vertex is not None else ''), field.value_type)
        return self.scalar(value, component)

    def resource_value(self, handle, *, word_bits=32):
        from file_handlers.sdf.shader_graph_resource import resource_value_type, resource_array_parts
        selection = handle if isinstance(handle, _ResourceHandle) else _ResourceHandle(handle)
        resource = selection.resource
        if selection.heap:
            source = None
            element = resource_value_type(resource, globally_coherent=selection.globally_coherent, word_bits=word_bits)
            kind = 'descriptor_heap'
            properties = {'element_type': element, 'non_uniform': selection.non_uniform}
        else:
            object_type = resource_value_type(resource, globally_coherent=selection.globally_coherent, word_bits=word_bits)
            source = self.input(('resource', resource.name, resource.register_space, resource.bind_point, 'object', selection.globally_coherent,
                                 *((word_bits,) if word_bits != 32 else ())),
                                resource.name, object_type)
            if selection.index is None:
                return source
            parts = resource_array_parts(object_type)
            if parts is None:
                raise VanillaFunctionError('A scalar resource cannot supply a descriptor table.')
            element, count = parts
            kind = 'resource_index'
            properties = {'element_type': element, 'count': count, 'non_uniform': selection.non_uniform}
        key = (kind, source, selection.index, tuple(properties.items()))
        if key not in self.values:
            node = self._node(kind, properties=properties)
            self.graph.connect(selection.index.node, selection.index.pin, node.node_id, 'index')
            if source is not None:
                self.graph.connect(source.node, source.pin, node.node_id, 'table')
            self.values[key] = _Value(node.node_id, 'value', element)
        return self.values[key]

    def dimensions(self, resource, mip=None):
        from file_handlers.sdf.shader_graph_dimensions import ResourceDimensions
        if resource is None:
            raise VanillaFunctionError('A dimension query has no identified resource.')
        source = self.resource_value(resource)
        shape = ResourceDimensions.from_type(source.value_type)
        if shape.mipmapped != (mip is not None) or mip is not None and mip.value_type != 'uint':
            raise VanillaFunctionError('The resource dimension query has an inconsistent mip operand.')
        key = ('dimensions', source, mip)
        if key not in self.values:
            node = self._node('resource_dimensions', properties={'resource_type': source.value_type})
            self.graph.connect(source.node, source.pin, node.node_id, 'resource')
            if mip is not None:
                self.graph.connect(mip.node, mip.pin, node.node_id, 'mip')
            self.values[key] = _Value(node.node_id, shape.outputs[0], 'uint')
        return {pin: _Value(self.values[key].node, pin, 'uint') for pin in shape.outputs}, shape

    def buffer_word(self, resource, address, *, record_offset=None):
        if resource is not None and resource.input_type in {'rw-byte-address-buffer', 'rw-structured-buffer'} and resource.bind_count == 1:
            return self.memory('memory_load', resource, address, record_offset=record_offset)['value']
        if resource is None or resource.input_type not in {"byte-address-buffer", "structured-buffer"} or resource.bind_count != 1:
            raise VanillaFunctionError("This buffer read needs an indexed/structured resource dependency.")
        if (resource.input_type == "structured-buffer") != (record_offset is not None):
            raise VanillaFunctionError("The buffer address does not match its declared raw/structured resource kind.")
        try:
            operation = (BufferRead(resource.sample_count, 0, True) if isinstance(record_offset, _Value)
                         else BufferRead(resource.sample_count, record_offset) if record_offset is not None else BufferRead())
        except ValueError as exc:
            raise VanillaFunctionError(str(exc)) from exc
        source = self.resource_value(resource)
        key = ("buffer_word", source, address, operation, record_offset)
        if key not in self.values or self.execution is not None and _writable(resource):
            properties = dict(stride=operation.stride, byte_offset=operation.byte_offset) if operation.stride else {}
            if operation.dynamic_offset:
                properties['dynamic_offset'] = True
            node = self._node("buffer_load", properties=properties)
            self.graph.connect(source.node, source.pin, node.node_id, "buffer")
            self.graph.connect(address.node, address.pin, node.node_id, "index" if operation.stride else "byte_offset")
            if operation.dynamic_offset:
                self.graph.connect(record_offset.node, record_offset.pin, node.node_id, 'record_byte_offset')
            self.values[key] = self.capture(_Value(node.node_id, "value", "uint"))
        return self.values[key]

    def buffer_words(self, resource, address, lanes):
        width = lanes.bit_length()
        if (resource is None or resource.input_type not in {'byte-address-buffer', 'rw-byte-address-buffer'}
                or resource.bind_count != 1 or lanes.bit_count() < 2 or width > 4):
            return None
        if resource.input_type == 'rw-byte-address-buffer':
            vector = self.memory('memory_load', resource, address, words=width)['value']
            return tuple(self.scalar(vector, lane) if lanes & (1 << lane) else None for lane in range(4))
        source = self.resource_value(resource)
        key = ('buffer_words', source, address, width)
        if key not in self.values or self.execution is not None and _writable(resource):
            node = self._node('buffer_load', properties={'words': width})
            self.graph.connect(source.node, source.pin, node.node_id, 'buffer')
            self.graph.connect(address.node, address.pin, node.node_id, 'byte_offset')
            self.values[key] = self.capture(_Value(node.node_id, 'value', f'uint{width}'))
        vector = self.values[key]
        return tuple(self.scalar(vector, lane) if lanes & (1 << lane) else None for lane in range(4))

    def record_slot(self, resource, index, slot):
        if resource is None or resource.input_type != 'structured-buffer' or resource.bind_count != 1:
            return None
        width = min(4, (resource.sample_count - slot * 16) // 4)
        if width < 2:
            return None
        try:
            operation = BufferRead(resource.sample_count, slot * 16, False, width)
        except ValueError:
            return None
        source = self.resource_value(resource)
        key = ('record_slot', source, index, slot)
        if key not in self.values or self.execution is not None and _writable(resource):
            node = self._node('buffer_load', properties=dict(stride=operation.stride, byte_offset=operation.byte_offset,
                                                            words=width))
            self.graph.connect(source.node, source.pin, node.node_id, 'buffer')
            self.graph.connect(index.node, index.pin, node.node_id, 'index')
            self.values[key] = self.capture(_Value(node.node_id, 'value', f'uint{width}'))
        return self.values[key]

    def word_offset(self, offset, component):
        if offset is None:
            return None
        if isinstance(offset, int):
            return offset + component * 4
        node = self.graph.node(offset.node)
        if node.kind == 'constant':
            return int(node.properties['values'][0]) + component * 4
        return self.operation('add', a=offset, b=self.integer(component * 4)) if component else offset

    def constant_word(self, buffer, offset):
        if buffer is None:
            raise VanillaFunctionError("A native constant read has no reflected buffer.")
        matches = [(variable, field, field.offsets.index(offset))
            for variable in buffer.variables for field in constant_value_fields(variable)
            if offset in field.offsets]
        if len(matches) != 1:
            raise VanillaFunctionError(f"{buffer.name} byte {offset} is not a uniquely declared value.")
        variable, field, component = matches[0]
        value = self.input(("constant", buffer.name, buffer.register_space, variable.name, field.path),
            f"{buffer.name}.{variable.name}{field.path}", field.value_type)
        word = self.scalar(value, component)
        return self.convert(word, 'uint') if word.value_type == 'bool' else word

    def constant_array_element(self, buffer, index, base_row, component, count=None):
        from file_handlers.sdf.shader_graph_constant_buffer import array_element_shape
        variable = next((item for item in (buffer.variables if buffer is not None else ())
                         if item.byte_offset == base_row * 16 and item.array_length > 1), None)
        shape = array_element_shape(buffer, variable.name) if variable is not None else None
        if (shape is None or not -1 <= component < shape[1] or index.value_type != 'uint'
                or count is not None and variable.array_length != count):
            return None
        value_type = shape[0] if component >= 0 or shape[1] == 1 else f'{shape[0]}{shape[1]}'
        key = ('array_element', buffer.name, variable.name, index.node, index.pin, component)
        if key not in self.values:
            node = self._node('parameter_element', properties={'buffer': buffer.name, 'variable': variable.name,
                                                               'component': component, 'value_type': value_type})
            self.graph.connect(index.node, index.pin, node.node_id, 'index')
            self.values[key] = _Value(node.node_id, 'value', value_type)
        return self.values[key]

    def constant_array_word(self, buffer, index, base_row, component, count=None, *, whole=False):
        if whole:
            element = self.constant_array_element(buffer, index, base_row, -1, count)
            if element is not None and element.value_type[-1:] in '234' and component < int(element.value_type[-1]):
                return self.scalar(element, component)
        return self.constant_array_element(buffer, index, base_row, component, count)

    def indexed_constant_word(self, buffer, row, component):
        if buffer is None or row.value_type != 'uint' or not 0 <= component < 4:
            raise VanillaFunctionError('An indexed constant read needs a reflected buffer and a uint register index.')
        source = self.graph.node(row.node)
        if source.kind == 'constant':
            return self.constant_word(buffer, int(source.properties['values'][0]) * 16 + component * 4)
        resource = self.contract.resource(buffer.name)
        if (self.contract.model_major >= 6 and self.contract.vulkan_execution is None and resource is not None
                and self.contract.stage in {'pixel', 'vertex', 'compute'}
                and resource.input_type == 'constant-buffer' and not resource.sample_count and resource.bind_count == 1):
            # SM6 reads the computed register row like cbufferLoadLegacy: zero outside the binding.
            key = ('constant_row', buffer.name, row.node, row.pin, component)
            if key not in self.values:
                from file_handlers.sdf.shader_graph_constant_buffer import constant_buffer_type, row_view_size
                size = row_view_size(buffer.byte_size)
                view = self.input(('constant_view', buffer.name, buffer.register_space, buffer.bind_point),
                                  buffer.name, constant_buffer_type(size))
                node = self._node('constant_buffer_row', properties={'byte_size': size, 'component': component})
                self.graph.connect(view.node, view.pin, node.node_id, 'buffer')
                self.graph.connect(row.node, row.pin, node.node_id, 'row')
                self.values[key] = _Value(node.node_id, 'value', 'uint')
            return self.values[key]
        from file_handlers.sdf.shader_graph_constant_buffer import array_element_shape
        for variable in sorted(buffer.variables, key=lambda item: item.byte_offset):
            shape = array_element_shape(buffer, variable.name) if variable.array_length > 1 else None
            if shape is None or component >= shape[1]:
                continue
            base = variable.byte_offset // 16
            element = self.constant_array_element(
                buffer, self.operation('subtract', a=row, b=self.integer(base)) if base else row, base, component)
            if element is not None:
                return element
        words = {offset: (variable, field, lane)
                 for variable in buffer.variables for field in constant_value_fields(variable)
                 for lane, offset in enumerate(field.offsets) if offset % 16 == component * 4}
        rows = sorted(offset // 16 for offset in words)
        if not rows or rows != list(range(rows[-1] + 1)) or rows[-1] * 16 + 16 < buffer.byte_size:
            raise VanillaFunctionError('An indexed constant read can reach undeclared padding; its address range needs proof.')
        values = [self.constant_word(buffer, index * 16 + component * 4) for index in rows]
        domain = values[0].value_type if len({v.value_type for v in values}) == 1 else 'uint'
        values = [self.convert(v, domain, bits=True) for v in values]
        node = self._node('indexed_select', properties=dict(value_type=domain, count=len(values)))
        self.graph.connect(row.node, row.pin, node.node_id, 'index')
        for index, selected in enumerate(values):
            self.graph.connect(selected.node, selected.pin, node.node_id, f'item{index}')
        # Native constant-buffer reads outside the bound buffer return zero.
        fallback = self.convert(self.integer(0), domain, bits=True)
        self.graph.connect(fallback.node, fallback.pin, node.node_id, 'fallback')
        return _Value(node.node_id, 'value', domain)

    def _constant_words(self, values):
        words = []
        for value in values:
            node = self.graph.node(value.node)
            literal = node.properties.get('values') if node.kind == 'constant' else None
            if (value.value_type not in {'float', 'int', 'uint'} or node.properties.get('value_type') != value.value_type
                    or type(literal) is not list or len(literal) != 1):
                return None
            if value.value_type == 'float':
                word = struct.unpack('<I', struct.pack('<f', float(literal[0])))[0]
                if struct.unpack('<f', struct.pack('<I', word))[0] != literal[0] and literal[0] == literal[0]:
                    return None
            else:
                word = int(literal[0]) & 0xFFFFFFFF
            words.append(word)
        return words

    def array_element(self, index, values, *, cases=False, fallback=None):
        if not values or len({v.value_type for v in values}) != 1:
            raise VanillaFunctionError('A value array must have one declared element type.')
        domain = values[0].value_type
        words = self._constant_words(values)
        if words is not None and (fallback is None or fallback.value_type == domain):
            node = self._node('constant_table', properties=dict(value_type=domain, words=words, has_default=fallback is not None))
            self.graph.connect(index.node, index.pin, node.node_id, 'index')
            if fallback is not None:
                self.graph.connect(fallback.node, fallback.pin, node.node_id, 'fallback')
            return _Value(node.node_id, 'value', domain)
        node = self._node('indexed_select' if cases or fallback is not None else 'array_read',
            properties=dict(value_type=domain, count=len(values), **({'has_default': fallback is not None} if cases or fallback is not None else {})))
        for pin, item in [('index', index), *((f'item{i}', item) for i,item in enumerate(values))]:
            self.graph.connect(item.node, item.pin, node.node_id, pin)
        if fallback is not None:
            if fallback.value_type != domain:
                raise VanillaFunctionError('An indexed default has a different value type.')
            self.graph.connect(fallback.node, fallback.pin, node.node_id, 'fallback')
        return _Value(node.node_id, 'value', domain)

    def finish(self, name, outputs, *, extra_outputs=()):
        pins = []
        for index, (field, parts) in enumerate(outputs):
            if field.component_type not in {'float32', 'uint32', 'sint32'} or field.stream or field.array_length != 1:
                raise VanillaFunctionError("This output needs a typed primitive/stream behavior lift.")
            value = self.vector(parts)
            pin = MaterialFunctionPin(f"out_{index}", field.display_name, value.value_type, False)
            pins.append(pin)
            node = self._node("function_output", properties={
                "pin_id": pin.pin_id, "display_name": pin.name, "value_type": pin.value_type,
            })
            self.graph.connect(value.node, value.pin, node.node_id, "value")
        for pin,value in extra_outputs:
            if value.value_type != pin.value_type:
                raise VanillaFunctionError('An explicit function result has an inconsistent type.')
            pins.append(pin)
            node = self._node('function_output',properties=dict(pin_id=pin.pin_id,display_name=pin.name,value_type=pin.value_type))
            self.graph.connect(value.node,value.pin,node.node_id,'value')
        clip_pin = ""
        if not outputs and not self.clips and self.execution is None and self.contract.stage == 'pixel':
            self.clips.append(self.constant(1.0))
        if self.clips:
            clip_pin = "out_clip"
            pin = MaterialFunctionPin(clip_pin, "Opacity clip (negative discards)", "float", False)
            pins.append(pin)
            clipped = self.clip_value(self.clips)
            node = self._node("function_output", properties={"pin_id": pin.pin_id, "display_name": pin.name, "value_type": "float"})
            self.graph.connect(clipped.node, clipped.pin, node.node_id, "value")
        execution_pin = ''
        if self.execution is not None:
            execution_pin = 'out_execution'
            pins.append(MaterialFunctionPin(execution_pin, 'Execution', 'execution', False))
            node = self._node('function_output', properties=dict(pin_id=execution_pin, display_name='Execution', value_type='execution'))
            self.graph.connect(self.execution.node, self.execution.pin, node.node_id, 'value')
        if not pins:
            raise VanillaFunctionError("This program has no value outputs; its side effects need their own behavior model.")
        reverse = {}
        for link in self.graph.links:
            reverse.setdefault(link.target_node, []).append(link.source_node)
        reachable = {node.node_id for node in self.graph.nodes if node.kind == "function_output"}
        pending = list(reachable)
        while pending:
            for identity in reverse.get(pending.pop(), ()):
                if identity not in reachable:
                    reachable.add(identity)
                    pending.append(identity)
        if self.ordered_nodes - reachable:
            raise VanillaFunctionError('Unused execution regions need explicit execution ownership; they cannot be pruned as values.')
        self.graph.keep_nodes(reachable)
        self.graph.links[:] = [link for link in self.graph.links if link.source_node in reachable and link.target_node in reachable]
        self.graph._incoming_links = None
        if any(node.kind in {'branch', 'repeat'} for node in self.graph.nodes):
            from .region_dependencies import specialize_regions
            self.graph = specialize_regions(self.graph, keep_inputs=False)
            reachable = {node.node_id for node in self.graph.nodes}
            if self.ordered_nodes - reachable:
                raise VanillaFunctionError('Region specialization discarded an ordered execution node.')
        inputs = tuple(pin for pin, value in self.inputs.values() if value.node in reachable)
        arrange_shader_graph(self.graph)
        digest = _recovery_graph_digest(self.graph)
        function = MaterialFunctionAsset(
            "vanilla." + digest, name, "Recovered Vanilla Behaviors",
            "Editable native calculations. Inputs are explicit value dependencies; output semantic names do not imply surface-channel meanings.",
            inputs, tuple(pins), self.graph.publish() if isinstance(self.graph, _RecoveryGraph) else self.graph,
        )
        return RecoveredBehavior(function, tuple((pin.pin_id, key)
            for key, (pin, value) in self.inputs.items() if value.node in reachable), clip_pin, execution_pin)


def _float_literal(text):
    from file_handlers.sdf.dxil_metadata import native_float_literal
    try:
        return native_float_literal(text)
    except ValueError as exc:
        raise VanillaFunctionError(f"Native value {text!r} is not a float literal.") from exc


def recover_dxbc_function(ir: DxbcSemanticIr, contract: ShaderContract, *, name="Vanilla Behavior", _hull_phase=None):
    if ir.stage == 'hs' and _hull_phase is None:
        from .legacy_hull_flow import normalized_hull_phases, passthrough_hull_behavior
        from .hull_behavior import combine_hull_behaviors
        control, patch = normalized_hull_phases(ir)
        control_behavior = (recover_dxbc_function(control,contract,name=name+' Control Points',_hull_phase='control_point')
                            if control is not None else passthrough_hull_behavior(contract,name+' Control Points'))
        return combine_hull_behaviors(control_behavior,
            recover_dxbc_function(patch,contract,name=name+' Patch Constants',_hull_phase='patch'),contract,name)
    if _hull_phase is not None and (ir.stage != 'hs' or _hull_phase not in {'control_point','patch'}):
        raise VanillaFunctionError('Invalid hull invocation scope.')
    phase_outputs = contract.patch_constants if _hull_phase == 'patch' else contract.outputs
    if ir.stage not in {"ps", "vs", "cs", "gs", "ds", "hs"}:
        raise VanillaFunctionError("This program needs indexed/control-flow behavior recovery.")
    strict = 'refactoringAllowed' not in ir.global_flags
    if strict and any(instruction.precise_mask for instruction in ir.instructions):
        raise VanillaFunctionError('Per-instruction precise masks require the native refactoring-allowed flag.')
    from .legacy_loop_flow import normalized_loop_exits, joined_complementary_branches, lowered_switches
    from .legacy_register_flow import lower_indexable_temps, register_liveness, destination_count, destination_register, pure_register_calculation
    from .profiles.dxbc_recovery import _output_register
    source_ir = joined_complementary_branches(lowered_switches(lower_indexable_temps(ir)))
    loop_origins = {}
    ir = normalized_loop_exits(source_ir, loop_origins=loop_origins)
    if ir.stage == "ps":
        from .profiles.dxbc_stages import pixel_signature_interpolation
        modes = pixel_signature_interpolation(contract.inputs, ir.input_interpolation)
        for field in contract.inputs:
            if modes.get(field.key, "") != field.interpolation:
                raise VanillaFunctionError("Complete the renderer input interpolation contract before recovering this behavior.")
    if ir.stage == 'cs' and contract.thread_group_size != ir.thread_group_size:
        raise VanillaFunctionError('Native thread-group dimensions disagree with the renderer contract.')
    if ir.stage in {'gs','ds','hs'} and contract.primitive_execution != ir.execution:
        raise VanillaFunctionError('Complete native primitive execution before recovering its behavior.')
    builder = _Builder(contract, ordered=ir.stage in {'cs','gs'} or any(instruction.opcode == 'loop' for instruction in ir.instructions) or any(resource.register_class == 'u' for resource in contract.resources)
                       or any(instruction.opcode in {'discard_nz', 'discard_z'} for instruction in ir.instructions))
    registers = {}
    def lane_fields(fields):
        return {(field.register, lane): (field, (field.mask & ((1 << lane) - 1)).bit_count())
                for field in fields for lane in range(4) if field.mask & (1 << lane)}
    inputs, patch_inputs, control_outputs = (lane_fields(fields) for fields in
                                             (contract.inputs, contract.patch_constants, contract.outputs))
    buffers = {buffer.bind_point: buffer for buffer in contract.constant_buffers if buffer.register_space == 0}
    from .native_dxbc_precision import operand_precision, word_value
    output_lanes = {(_output_register(field), lane)
        for field in phase_outputs for lane in range(4) if field.mask & (1 << lane)}
    stream_lanes = {stream:{(_output_register(field),lane) for field in contract.outputs if field.stream == stream
                           for lane in range(4) if field.mask & (1 << lane)}
                    for stream in ir.execution.output_streams} if ir.stage == 'gs' else None
    live_before, live_after = register_liveness(ir, output_lanes, stream_lanes=stream_lanes)
    source_before = register_liveness(source_ir, output_lanes, stream_lanes=stream_lanes)[0] if loop_origins else live_before
    def bookkeeping_lanes(lanes):
        return {key for key in lanes if key[0].startswith('r') and int(key[0][1:]) >= source_ir.temp_count}
    written_registers = {(match[1],'xyzw'.index(axis)) for record in ir.instructions
                         for operand in record.operands[:destination_count(record)]
                         if (match := destination_register(operand)) is not None for axis in match[2] or 'xyzw'}

    def read_operand(resolve, operand, lane, value_type=None):
        operand, stored_type, used_type = operand_precision(operand)
        domain = used_type or value_type
        # Operand modifiers follow the numeric type: IADD -r is integer negation.
        modifier, inner = None, operand
        if operand.startswith('-'):
            modifier, inner = 'negate', operand[1:]
        elif operand.startswith('abs(') and operand.endswith(')'):
            modifier, inner = 'absolute', operand[4:-1]
        elif operand.startswith('|') and operand.endswith('|'):
            modifier, inner = 'absolute', operand[1:-1]
        if modifier:
            result = builder.operation(modifier, value=resolve(resolve, inner, lane, stored_type or domain or 'float'))
        else:
            result = read_value(resolve, operand, lane)
            if stored_type:
                result = word_value(builder, result, stored_type)
        return word_value(builder, result, domain) if domain else result

    def destination_value(operand, value):
        _, stored, used = operand_precision(operand)
        if stored != used:
            raise VanillaFunctionError('A native destination cannot have a source conversion annotation.')
        return word_value(builder, value, stored) if stored else value

    def write_values(updates):
        destinations = {}
        for operand in instruction.operands[:destination_count(instruction)]:
            match = destination_register(operand)
            if match is not None:
                destinations.update(((match[1], 'xyzw'.index(axis)), operand) for axis in match[2] or 'xyzw')
        for key, result in updates.items():
            if key not in destinations:
                raise VanillaFunctionError('A native result is outside its declared destination lanes.')
            result = destination_value(destinations[key], result)
            if strict or 'xyzw'[key[1]] in instruction.precise_mask:
                result = builder.precise(result)
            registers[key] = result

    def read_as(operand, lane, domain):
        return word_value(builder, read(operand, lane, domain), domain)

    def read_value(resolve, operand, lane):
        operand = operand.strip()
        domain = re.fullmatch(r'vDomain(?:\.([xyzw]{1,4}))?',operand)
        if domain:
            mask = domain[1] or 'xyzw'
            if ir.stage!='ds' or len(mask) not in {1,4}:
                raise VanillaFunctionError('Invalid domain coordinate operand.')
            return builder.domain_coordinate('xyzw'.index(mask[0 if len(mask)==1 else lane]))
        patch = re.fullmatch(r'vpc(\d+)(?:\.([xyzw]{1,4}))?',operand)
        if patch:
            mask = patch[2] or 'xyzw'
            if ir.stage!='ds' or len(mask) not in {1,4}:
                raise VanillaFunctionError('Invalid patch constant operand.')
            field = patch_inputs.get((int(patch[1]),'xyzw'.index(mask[0 if len(mask)==1 else lane])))
            if field is None:
                raise VanillaFunctionError('A patch read is outside its declared signature.')
            return builder.patch_constant(*field)
        system = re.fullmatch(r'(vPrim|vGSInstanceID|vOutputControlPointID)(?:\.(x+))?',operand,re.I)
        if system:
            meaning = {'vprim':'primitive_id','vgsinstanceid':'geometry_instance_id','voutputcontrolpointid':'output_control_point_id'}[system[1].casefold()]
            allowed = {'primitive_id':ir.stage in {'gs','ds','hs'},'geometry_instance_id':ir.stage=='gs',
                       'output_control_point_id':_hull_phase=='control_point'}
            if not allowed[meaning] or system[1].casefold() not in ir.primitive_system_inputs:
                raise VanillaFunctionError('The primitive invocation input was not declared in this scope.')
            return builder.invocation_input(meaning)
        primitive = re.fullmatch(r'(v|vicp|vocp)\[(\d+|(?:r\d+\.[xyzw]|vOutputControlPointID(?:\.x)?)(?:\s*\+\s*\d+)?)\]\[(\d+)\](?:\.([xyzw]{1,4}))?',operand)
        if primitive:
            mask = primitive[4] or 'xyzw'
            allowed = {'v'} if ir.stage=='gs' or _hull_phase=='control_point' else {'vicp','vocp'} if _hull_phase=='patch' else {'vicp'} if ir.stage=='ds' else set()
            if primitive[1] not in allowed or len(mask) not in {1,4}:
                raise VanillaFunctionError('Invalid primitive input read.')
            output_point = primitive[1]=='vocp'
            field = (control_outputs if output_point else inputs).get((int(primitive[3]),'xyzw'.index(mask[0 if len(mask)==1 else lane])))
            if field is None:
                raise VanillaFunctionError('A primitive attribute read is outside its signature.')
            if primitive[2].isdigit():
                vertex = int(primitive[2])
            else:
                terms = re.split(r'\s*\+\s*',primitive[2])
                vertex = resolve(resolve,terms[0],0,'uint')
                if len(terms)==2:
                    vertex = builder.operation('add',a=vertex,b=builder.integer(int(terms[1])))
            return builder.stage_input(*field,vertex=vertex,output_point=output_point)
        builtin = re.fullmatch(r'(vThreadID|vThreadGroupID|vThreadIDInGroup|vThreadIDInGroupFlattened)(?:\.([xyz]{1,4}))?', operand, re.I)
        if builtin:
            meaning = {'vthreadid':'dispatch_thread_id', 'vthreadgroupid':'group_id', 'vthreadidingroup':'group_thread_id',
                       'vthreadidingroupflattened':'group_index'}[builtin[1].casefold()]
            if builtin[1].casefold() not in ir.compute_inputs:
                raise VanillaFunctionError('The native thread input was not declared.')
            mask = builtin[2] or ('x' if meaning == 'group_index' else 'xyz')
            if len(mask) > 1 and lane >= len(mask):
                raise VanillaFunctionError('A thread-input swizzle does not cover the destination.')
            return builder.invocation_input(meaning, 'xyz'.index(mask[0 if len(mask) == 1 else lane]))
        literal = re.fullmatch(r"l\((.+)\)", operand)
        if literal:
            values = literal[1].split(",")
            if len(values) not in {1, 4}:
                raise VanillaFunctionError("A native immediate vector has an invalid component count.")
            literal_value = values[0 if len(values) == 1 else lane].strip()
            return builder.integer(int(literal_value, 16)) if re.fullmatch(r'0x[0-9a-fA-F]{8}', literal_value) else builder.constant(_float_literal(literal_value))
        immediate = re.fullmatch(r'icb\[(\d+|r\d+\.[xyzw](?:\s*\+\s*\d+)?)\](?:\.([xyzw]{1,4}))?', operand)
        if immediate:
            if not ir.immediate_constants:
                raise VanillaFunctionError('The native immediate constant buffer is not declared.')
            mask = immediate[2] or 'xyzw'
            if len(mask) != 1 and lane >= len(mask):
                raise VanillaFunctionError('An immediate buffer swizzle does not cover the destination lane.')
            component = 'xyzw'.index(mask[0 if len(mask) == 1 else lane])
            address = immediate[1]
            column = [builder.integer(int(row[component], 16)) for row in ir.immediate_constants]
            # D3D11 spec 7.5/7.5.1: immediate constant buffers read zero out of range, like bound constant buffers.
            if address.isdigit():
                return column[int(address)] if int(address) < len(column) else builder.integer(0)
            register, _, displacement = address.partition('+')
            index = resolve(resolve,register.strip(), 0, 'uint')
            if displacement:
                index = builder.operation('add', a=index, b=builder.integer(int(displacement)))
            return builder.array_element(index, column, fallback=builder.integer(0))
        value = re.fullmatch(r"(?:(?P<register>[rvo]\d+)|cb(?P<buffer>\d+)\[(?P<row>\d+|r\d+\.[xyzw](?:\s*\+\s*\d+)?)\])(?:\.(?P<mask>[xyzw]{1,4}))?", operand)
        if value is None:
            raise VanillaFunctionError(f"Operand {operand!r} needs an additional semantic graph operation.")
        mask = value["mask"] or "xyzw"
        if len(mask) != 1 and lane >= len(mask):
            raise VanillaFunctionError("A native operand swizzle does not cover the destination lane.")
        component = "xyzw".index(mask[0 if len(mask) == 1 else lane])
        if value["buffer"] is not None:
            buffer, address = buffers.get(int(value['buffer'])), value['row']
            if address.isdigit():
                return builder.constant_word(buffer, int(address) * 16 + component * 4)
            register, _, displacement = address.partition('+')
            row = resolve(resolve,register.strip(), 0, 'uint')
            element = builder.constant_array_element(buffer, row, int(displacement or 0), component)
            if element is not None:
                return element
            if displacement:
                row = builder.operation('add', a=row, b=builder.integer(int(displacement)))
            return builder.indexed_constant_word(buffer, row, component)
        register = value["register"]
        if register.startswith("v"):
            field = inputs.get((int(register[1:]), component))
            if field is None:
                raise VanillaFunctionError("A native input read is outside the declared signature.")
            return builder.stage_input(*field)
        try:
            return registers[register, component]
        except KeyError as exc:
            if not register.startswith('r'):
                raise VanillaFunctionError('A native calculation reads an undefined output register.') from exc
            result = builder.unassigned('uint')
            registers[register, component] = result
            return result

    read = partial(read_operand, read_operand)

    binary = {"add": "add", "mul": "multiply", "div": "divide", "min": "minimum", "max": "maximum"}
    unary = {"frc": "fraction", "sqrt": "square_root", "round_ni": "floor", "round_pi": "ceiling",
             "rsq": "reciprocal_square_root", "rcp": "reciprocal", "exp": "exponent2", "log": "logarithm2",
             "round_ne": "round_even", "round_z": "truncate", "deriv_rtx_coarse": "derivative_x", "deriv_rty_coarse": "derivative_y",
             "deriv_rtx_fine": "derivative_x_fine", "deriv_rty_fine": "derivative_y_fine"}
    comparisons = {"lt": "less", "ge": "greater_equal", "eq": "equal", "ne": "not_equal"}
    integer_ops = {'iadd': ('add', 'int'), 'imul': ('multiply', 'int'), 'and': ('bit_and', 'uint'),
        'or': ('bit_or', 'uint'), 'xor': ('bit_xor', 'uint'), 'ishl': ('shift_left', 'uint'),
        'ishr': ('shift_right', 'int'), 'ushr': ('shift_right', 'uint'), 'imin': ('minimum', 'int'),
        'imax': ('maximum', 'int'), 'umin': ('minimum', 'uint'), 'umax': ('maximum', 'uint')}
    integer_unary = {'not': ('bit_not', 'uint'), 'ineg': ('negate', 'int'), 'countbits': ('count_bits', 'uint'),
        'f32tof16': ('pack_half', 'float'), 'f16tof32': ('unpack_half', 'uint'),
        'bfrev': ('reverse_bits', 'uint'), 'firstbit_lo': ('first_bit_low', 'uint'),
        'firstbit_hi': ('first_bit_high', 'uint'), 'firstbit_shi': ('first_bit_high', 'int')}
    integer_compare = {'ieq': ('equal', 'int'), 'ine': ('not_equal', 'int'), 'ilt': ('less', 'int'),
        'ige': ('greater_equal', 'int'), 'ult': ('less', 'uint'), 'uge': ('greater_equal', 'uint')}
    conversions = {'ftoi': ('float', 'int'), 'ftou': ('float', 'uint'), 'itof': ('int', 'float'), 'utof': ('uint', 'float')}

    def condition(operand, lane):
        return builder.compare('not_equal', read_as(operand, lane, 'uint'), builder.integer(0))

    def comparison_mask(operand):
        return builder.select(operand, builder.integer(0xffffffff), builder.integer(0))

    def resource_at(register_class, binding):
        resource = next((item for item in contract.resources if item.register_class == register_class and item.register_space == 0
            and item.bind_count > 0 and item.bind_point <= binding < item.bind_point + item.bind_count), None)
        if resource is None:
            return None
        index = builder.integer(binding - resource.bind_point) if resource.bind_count != 1 else None
        return _ResourceHandle(resource, index, globally_coherent=register_class == 'u' and binding in ir.globally_coherent_uavs)

    ended = False
    branches = []
    loops = []

    for instruction_index, instruction in enumerate(ir.instructions):
        builder.precise_math = strict or bool(instruction.precise_mask)
        if pure_register_calculation(instruction):
            destinations = {(match[1], 'xyzw'.index(axis))
                for operand in instruction.operands[:destination_count(instruction)]
                if (match := destination_register(operand)) is not None for axis in match[2] or 'xyzw'}
            if destinations and not destinations & live_after[instruction_index]:
                continue
        if instruction.opcode == "ret" and not instruction.operands:
            if ended or branches:
                raise VanillaFunctionError("Early/multiple returns need execution-flow recovery.")
            ended = True
            continue
        if ended:
            raise VanillaFunctionError("Instructions after a return need control-flow recovery.")
        saturate = instruction.opcode.endswith("_sat")
        opcode = instruction.opcode.removesuffix("_sat")
        operands = instruction.operands
        if opcode in {'emit','cut','emitthencut','emit_stream','cut_stream','emitthencut_stream'}:
            from file_handlers.sdf.shader_graph_geometry import vertex_fields
            match = re.fullmatch(r'm([0-3])',operands[0]) if len(operands)==1 else None
            if ir.stage != 'gs' or saturate or (opcode.endswith('_stream') and match is None) or (not opcode.endswith('_stream') and operands):
                raise VanillaFunctionError('Invalid geometry emission stream operand.')
            stream = int(match[1]) if match else 0
            if stream not in ir.execution.output_streams:
                raise VanillaFunctionError('Geometry emission targets an undeclared stream.')
            if opcode.startswith('emit'):
                arguments = {}
                for i,field in enumerate(field for field in contract.outputs if field.stream==stream):
                    parts = []
                    for lane in range(4):
                        if field.mask & (1 << lane):
                            value = registers.get((_output_register(field),lane))
                            if value is None:
                                value = builder.unassigned(field.value_type.rstrip('234'))
                            parts.append(word_value(builder,value,field.value_type.rstrip('234')))
                    arguments[f'field{i}'] = builder.vector(parts)
                builder.execute('emit_vertex',properties={'stream':stream,'fields':vertex_fields(contract,stream)},**arguments)
                # emit_stream invalidates all output registers, including other streams.
                registers = {key:value for key,value in registers.items() if not key[0].startswith('o')}
            if 'cut' in opcode:
                builder.execute('end_primitive',properties={'stream':stream})
            continue
        closing_loop = opcode == 'endloop'
        if opcode == 'loop' and not operands:
            if builder.clips:
                raise VanillaFunctionError('Repeat after discard needs ordered execution ownership.')
            frame = dict(node_start=len(builder.graph.nodes), cache=dict(builder.values),
                         states=[], branch_depth=len(branches), continuation=None, header_index=instruction_index)
            origin = loop_origins.get(id(instruction))
            frame['header_live'] = ((source_before[origin[0]] | bookkeeping_lanes(live_before[instruction_index]))
                                    if origin else live_before[instruction_index])
            frame['exit_live'] = ((source_before[origin[1] + 1] if origin[1] + 1 < len(source_before) else set())
                                 | (output_lanes if origin[2] else set())) if origin else None
            registers = {key: value for key, value in registers.items() if key in frame['header_live']}
            for key in sorted(frame['header_live'] - registers.keys()):
                if key[0].startswith('r'):
                    registers[key] = builder.unassigned('uint')
            for key, initial in registers.items():
                if key not in written_registers:
                    continue
                index = len(frame['states'])
                placeholder = builder._node('function_input', properties=dict(pin_id=f'state_{index}',
                    display_name=f'State {index + 1}', value_type=initial.value_type))
                state = _Value(placeholder.node_id, 'value', initial.value_type)
                frame['states'].append((key, initial, state))
                registers[key] = state
            builder.loop_execution_state(frame)
            loops.append(frame)
            continue
        if opcode in {'breakc_nz', 'breakc_z'} and len(operands) == 1 and not saturate:
            if not loops or loops[-1]['continuation'] is not None or len(branches) != loops[-1]['branch_depth']:
                raise VanillaFunctionError('Multiple/conditional loop exits need ordered loop-exit regions.')
            opcode = 'if_z' if opcode == 'breakc_nz' else 'if_nz'
        if closing_loop:
            if not loops or operands or loops[-1]['continuation'] is None or len(branches) != loops[-1]['branch_depth'] + 1:
                raise VanillaFunctionError('The legacy loop has no complete single-exit region.')
            opcode = 'endif'
        if opcode in {"if_nz", "if_z"} and len(operands) == 1 and not saturate:
            predicate = condition(operands[0], 0)
            if opcode == "if_z":
                predicate = builder.compare("equal", predicate, builder.boolean(False))
            if instruction.opcode in {'breakc_nz', 'breakc_z'}:
                loops[-1]['continuation'] = predicate
            branches.append(dict(condition=predicate, registers=dict(registers), values=dict(builder.values),
                                 node_start=len(builder.graph.nodes), true_registers=None, clips=tuple(builder.clips), execution=builder.execution))
            continue
        if opcode == "else" and not operands:
            if not branches or branches[-1]["true_registers"] is not None:
                raise VanillaFunctionError("A native else has no matching conditional arm.")
            frame = branches[-1]
            frame["true_registers"] = dict(registers)
            frame['true_execution'], builder.execution = builder.execution, frame['execution']
            frame['true_clips'] = tuple(builder.clips[len(frame['clips']):])
            builder.clips = list(frame['clips'])
            frame["true_nodes"] = builder.graph.nodes_since(frame["node_start"])
            registers = dict(frame["registers"])
            builder.values = dict(frame["values"])
            continue
        if opcode == "endif" and not operands:
            if not branches:
                raise VanillaFunctionError("A native endif has no matching branch.")
            frame = branches.pop()
            new_nodes = builder.graph.nodes_since(frame["node_start"])
            if frame["true_registers"] is None:
                true_registers, false_registers = registers, frame["registers"]
                true_nodes, false_nodes = new_nodes, set()
                clips = (tuple(builder.clips[len(frame['clips']):]), ())
                executions = (builder.execution, frame['execution']) if builder.execution is not None else ()
            else:
                true_registers, false_registers = frame["true_registers"], registers
                true_nodes = frame["true_nodes"]
                false_nodes = new_nodes - true_nodes
                clips = (frame['true_clips'], tuple(builder.clips[len(frame['clips']):]))
                executions = (frame['true_execution'], builder.execution) if builder.execution is not None else ()
            builder.clips = list(frame['clips'])
            live = live_after[instruction_index]
            if closing_loop:
                exit_live = (loops[-1]['exit_live'] | bookkeeping_lanes(live_before[instruction_index + 1])
                             if loops[-1]['exit_live'] is not None else live_before[instruction_index + 1])
                live = exit_live | loops[-1]['header_live']
            common = sorted((true_registers.keys() | false_registers.keys()) & live)
            for key in common:
                for values in (true_registers, false_registers):
                    if key not in values:
                        values[key] = builder.unassigned('uint')
            changed = [key for key in common if true_registers[key] != false_registers[key]]
            joined = builder.merge_branch(frame["condition"], true_nodes, false_nodes,
                [true_registers[key] for key in changed], [false_registers[key] for key in changed], clips=clips, executions=executions)
            registers = {key: true_registers[key] for key in common if key not in changed}
            registers.update(zip(changed, joined))
            if closing_loop:
                loop = loops.pop()
                if any(key not in registers for key, _, _ in loop['states'] if key != '__execution__'):
                    raise VanillaFunctionError('A carried register is not defined on every loop exit.')
                updates = [builder.convert(registers[key], initial.value_type, bits=True)
                           for key, initial, _ in loop['states'] if key != '__execution__']
                exports = {key: value for key, value in registers.items() if key in exit_live and key in written_registers}
                invariants = {key:value for key,value in registers.items() if key in exit_live and key not in written_registers}
                registers = {**invariants,**builder.merge_loop(loop, loop['continuation'], updates, exports)}
            continue
        if opcode in {"discard_nz", "discard_z"} and len(operands) == 1 and not saturate:
            if loops and builder.execution is None:
                raise VanillaFunctionError('Discard inside repeat needs ordered execution ownership.')
            builder.discard(condition(operands[0], 0), when_true=opcode == "discard_nz")
            continue
        if (opcode.startswith('resinfo') or opcode in {'bufinfo_indexable', 'bufinfo'}) and not saturate:
            buffer_query = opcode.startswith('bufinfo')
            if len(operands) != (2 if buffer_query else 3):
                raise VanillaFunctionError('The resource dimension instruction has an inconsistent operand count.')
            target = re.fullmatch(r'([tu])(\d+)(?:\.([xyzw]{1,4}))?', operands[-1])
            destination = destination_register(operands[0])
            if target is None or destination is None or len(target[3] or 'xyzw') not in {1,4}:
                raise VanillaFunctionError('The resource dimension instruction has an invalid binding or swizzle.')
            resource = resource_at(target[1], int(target[2]))
            mipmapped = resource is not None and resource.input_type == 'texture' and 'ms' not in resource.dimension
            queried, shape = builder.dimensions(resource, read(operands[1], 0, 'uint') if mipmapped else None)
            if buffer_query != bool(shape.stride is not None or 'bytes' in shape.outputs):
                raise VanillaFunctionError('The dimension opcode does not match the resource kind.')
            mode = 'uint' if buffer_query else opcode.removeprefix('resinfo').removeprefix('_indexable').removeprefix('_')
            if mode not in {'', 'uint', 'rcpfloat'}:
                raise VanillaFunctionError('The dimension query has an unsupported result modifier.')
            swizzle = target[3] or 'xyzw'
            for axis in destination[2] or 'xyzw':
                lane = 'xyzw'.index(swizzle[0 if len(swizzle) == 1 else 'xyzw'.index(axis)])
                if buffer_query and lane:
                    raise VanillaFunctionError('A buffer dimension query reads an undeclared result lane.')
                pin = shape.native_lanes[lane]
                # SM4 resinfo returns zero for unused dimensions.
                result = queried[pin] if pin is not None else builder.integer(0)
                if mode != 'uint':
                    result = builder.convert(result, 'float')
                    if mode == 'rcpfloat' and pin in {'width','height','depth'}:
                        result = builder.operation('divide', a=builder.constant(1.0), b=result)
                write_values({(destination[1], 'xyzw'.index(axis)): result})
            continue
        if opcode.startswith(('imm_atomic_', 'atomic_')) and not saturate:
            immediate = opcode.startswith('imm_atomic_')
            native = opcode.removeprefix('imm_').removeprefix('atomic_')
            operations = {'iadd': 'add', 'and': 'and', 'or': 'or', 'xor': 'xor', 'imin': 'minimum',
                          'imax': 'maximum', 'umin': 'minimum', 'umax': 'maximum', 'exch': 'exchange',
                          'cmp_exch': 'compare_exchange', 'cmp_store': 'compare_exchange'}
            operation = operations.get(native)
            if operation is None or len(operands) != 3 + immediate + (operation == 'compare_exchange'):
                raise VanillaFunctionError('The native atomic operation has an unsupported declaration.')
            target, coordinate, *arguments = operands[int(immediate):]
            binding = re.fullmatch(r'u(\d+)(?:\.[xyzw]{1,4})?', target)
            if binding is None:
                raise VanillaFunctionError('This atomic target needs a different writable resource adapter.')
            domain = 'int' if native in {'imin', 'imax'} else 'uint'
            words = [read(argument, 0, domain) for argument in arguments]
            resource = resource_at('u', int(binding[1]))
            dimensions = TEXTURE_LOAD_COORDINATES.get(resource.dimension.replace('-', '')) if resource and resource.input_type == 'rw-typed' else 'uint'
            if dimensions is None:
                raise VanillaFunctionError('This atomic image dimension requires a different coordinate adapter.')
            width = int(dimensions[-1]) if dimensions[-1].isdigit() else 1
            address = builder.vector([read(coordinate, lane, 'uint') for lane in range(width)])
            result = builder.memory('memory_atomic', resource, address,
                record_offset=read(coordinate, 1, 'uint') if resource and resource.input_type == 'rw-structured-buffer' else None,
                operation=operation, value=words[-1], compare=words[0] if operation == 'compare_exchange' else None)['original']
            if immediate and operands[0] != 'null':
                destination = re.fullmatch(r'([ro]\d+)\.([xyzw])', operands[0])
                if destination is None:
                    raise VanillaFunctionError('An atomic original value needs a scalar destination.')
                write_values({(destination[1], 'xyzw'.index(destination[2])): result})
            continue
        if opcode in {'store_uav_typed', 'ld_uav_typed', 'ld_uav_typed_indexable'} and len(operands) == 3 and not saturate:
            store = opcode == 'store_uav_typed'
            target = re.fullmatch(r'u(\d+)(?:\.([xyzw]{1,4}))?', operands[0 if store else 2])
            resource = resource_at('u', int(target[1])) if target else None
            dimension = resource.dimension.replace('-', '') if resource else ''
            coordinate_type = TEXTURE_LOAD_COORDINATES.get(dimension)
            if coordinate_type is None or resource.input_type != 'rw-typed' or not resource.texture_value_type:
                raise VanillaFunctionError('This writable image needs a different typed coordinate adapter.')
            width = int(coordinate_type[-1]) if coordinate_type[-1].isdigit() else 1
            element = resource.texture_value_type
            channels = int(element[-1]) if element[-1].isdigit() else 1
            coordinates = builder.vector([read(operands[1], lane, 'uint') for lane in range(width)])
            if store:
                if not set('xyzw'[:channels]) <= set(target[2] or 'xyzw'):
                    raise VanillaFunctionError('A partial-channel typed store needs an explicit image write mask.')
                data = builder.vector([read(operands[2], lane, element.rstrip('234')) for lane in range(channels)])
                builder.image('image_store', resource, coordinates, value=data)
            else:
                destination = re.fullmatch(r'([ro]\d+)(?:\.([xyzw]{1,4}))?', operands[0])
                mask = target[2] or 'xyzw'
                if destination is None or len(mask) not in {1,4}:
                    raise VanillaFunctionError('An image read has an invalid destination or result swizzle.')
                loaded = builder.image('image_load', resource, coordinates)['value']
                write_values({(destination[1], 'xyzw'.index(axis)): builder.scalar(loaded, 'xyzw'.index(mask[0 if len(mask) == 1 else 'xyzw'.index(axis)]))
                    for axis in destination[2] or 'xyzw'})
            continue
        if opcode in {'store_raw', 'store_structured'} and not saturate and len(operands) == (4 if opcode == 'store_structured' else 3):
            target = re.fullmatch(r'u(\d+)(?:\.([xyzw]{1,4}))?', operands[0])
            if target is None:
                raise VanillaFunctionError('This raw store needs an explicit UAV binding.')
            resource, address = resource_at('u', int(target[1])), read(operands[1], 0, 'uint')
            record_offset = read(operands[2], 0, 'uint') if opcode == 'store_structured' else None
            for component in target[2] or 'xyzw':
                lane = 'xyzw'.index(component)
                pointer = builder.operation('add', a=address, b=builder.integer(lane * 4)) if lane and record_offset is None else address
                builder.memory('memory_store', resource, pointer, record_offset=builder.word_offset(record_offset, lane), value=read(operands[-1], lane, 'uint'))
            continue
        if opcode.startswith('sync_') and not operands:
            modes = {'sync_g': ('group', False), 'sync_g_t': ('group', True), 'sync_uglobal': ('device', False),
                     'sync_uglobal_t': ('device', True), 'sync_uglobal_g': ('all', False), 'sync_uglobal_g_t': ('all', True)}
            if opcode not in modes:
                raise VanillaFunctionError('This barrier requires an additional exact execution-scope adapter.')
            scope, synchronize = modes[opcode]
            builder.execute('memory_barrier', properties=dict(scope=scope, synchronize=synchronize))
            continue
        if opcode in {"ld_raw", "ld_raw_indexable", "ld_structured", "ld_structured_indexable"} and not saturate:
            structured = opcode.startswith("ld_structured")
            if len(operands) != (4 if structured else 3):
                raise VanillaFunctionError("The buffer load has an invalid operand count.")
            destination = re.fullmatch(r"([ro]\d+)(?:\.([xyzw]{1,4}))?", operands[0])
            source = re.fullmatch(r"([tu])(\d+)(?:\.([xyzw]{1,4}))?", operands[3 if structured else 2])
            if destination is None or source is None:
                raise VanillaFunctionError("A raw buffer read needs an indexed resource/output adapter.")
            resource = resource_at(source[1], int(source[2]))
            address = read(operands[1], 0, "uint")
            record_offset = read(operands[2], 0, 'uint') if structured else None
            mask = source[3] or "xyzw"
            if len(mask) not in {1, 4}:
                raise VanillaFunctionError("A native buffer result swizzle is invalid.")
            updates = {}
            for axis in destination[2] or "xyzw":
                lane = "xyzw".index(axis)
                offset = "xyzw".index(mask[0 if len(mask) == 1 else lane]) * 4
                pointer = builder.operation("add", a=address, b=builder.integer(offset)) if offset and not structured else address
                updates[destination[1], lane] = builder.buffer_word(resource, pointer,
                    record_offset=builder.word_offset(record_offset, offset // 4))
            write_values(updates)
            continue
        if opcode == 'udiv' and not saturate:
            if len(operands) != 4:
                raise VanillaFunctionError('Unsigned division needs quotient/remainder destinations and two operands.')
            updates = {}
            for destination_text, operation in zip(operands[:2], ('divide', 'integer_remainder')):
                if destination_text == 'null':
                    continue
                destination = re.fullmatch(r'([ro]\d+)(?:\.([xyzw]{1,4}))?', destination_text)
                if destination is None:
                    raise VanillaFunctionError('Unsigned division needs an indexed destination adapter.')
                for axis in destination[2] or 'xyzw':
                    lane = 'xyzw'.index(axis)
                    numerator, denominator = (read(operand, lane, 'uint') for operand in operands[2:])
                    updates[destination[1], lane] = builder.unsigned_division(numerator,denominator)[operation=='integer_remainder']
            write_values(updates)
            continue
        if opcode == 'swapc' and not saturate:
            if len(operands) != 5:
                raise VanillaFunctionError('Conditional swap needs two destinations, a condition and two values.')
            updates, destinations = {}, []
            for index, destination_text in enumerate(operands[:2]):
                if destination_text == 'null':
                    continue
                destination = destination_register(destination_text)
                if destination is None or destination[1] in destinations:
                    raise VanillaFunctionError('Conditional swap needs two distinct destination registers.')
                destinations.append(destination[1])
                domain = operand_precision(destination_text)[1] or 'uint'
                for axis in destination[2] or 'xyzw':
                    lane = 'xyzw'.index(axis)
                    first, second = (read_as(text, lane, domain) for text in operands[3:])
                    yes, no = (first, second) if index else (second, first)
                    updates[destination[1], lane] = builder.select(condition(operands[2], lane), yes, no)
            write_values(updates)
            continue
        if opcode in {'imul','umul','uaddc','usubb'} and len(operands) == 4 and not saturate:
            updates = {}
            domain = 'int' if opcode == 'imul' else 'uint'
            for word,text in enumerate(operands[:2]):
                if text == 'null':
                    continue
                destination = destination_register(text)
                if destination is None:
                    raise VanillaFunctionError('A full-width product needs register destinations.')
                for axis in destination[2] or 'xyzw':
                    lane = 'xyzw'.index(axis)
                    a,b = (read_as(operand,lane,domain) for operand in operands[2:])
                    if opcode in {'uaddc','usubb'}:
                        value = builder.word_carry(a,b,subtract=opcode=='usubb')[word]
                        if word:
                            value = builder.convert(value,'uint')
                    else:
                        value = builder.operation(('multiply_high','multiply')[word],a=a,b=b)
                    updates[destination[1],lane] = value
            write_values(updates)
            continue
        if opcode in integer_ops or opcode in integer_unary or opcode in integer_compare or opcode in conversions or opcode in {'ubfe', 'bfi', 'imad'}:
            if saturate:
                raise VanillaFunctionError('An integer operation cannot saturate a float result.')
            destination = destination_register(operands[0])
            if destination is None:
                raise VanillaFunctionError('This integer destination needs indexed value recovery.')
            updates = {}
            for axis in destination[2] or 'xyzw':
                lane = 'xyzw'.index(axis)
                if opcode in conversions:
                    source_type, target_type = conversions[opcode]
                    target_type = operand_precision(operands[0])[1] or target_type
                    result = builder.convert(read(operands[1], lane, source_type), target_type)
                elif opcode in integer_compare:
                    relation, value_type = integer_compare[opcode]
                    result = comparison_mask(builder.compare(relation, read_as(operands[1], lane, value_type), read_as(operands[2], lane, value_type)))
                elif opcode in integer_unary:
                    kind, value_type = integer_unary[opcode]
                    operand = read(operands[1], lane, value_type)
                    result = builder.leading_bit(operand) if opcode in {'firstbit_hi', 'firstbit_shi'} else builder.operation(kind, value=operand)
                elif opcode == 'imad':
                    result = builder.operation('multiply_add', a=read(operands[1], lane, 'int'),
                        b=read(operands[2], lane, 'int'), c=read(operands[3], lane, 'int'))
                elif opcode in {'ubfe', 'bfi'}:
                    parts = {'value': read(operands[3], lane, 'uint'),
                             'width': read(operands[1], lane, 'uint'), 'offset': read(operands[2], lane, 'uint')}
                    if opcode == 'bfi': parts['base'] = read(operands[4], lane, 'uint')
                    result = builder.operation('extract_bits_dynamic' if opcode == 'ubfe' else 'insert_bits_dynamic', **parts)
                else:
                    kind, value_type = integer_ops[opcode]
                    value_type = operand_precision(operands[0])[1] or value_type
                    result = builder.operation(kind, a=read_as(operands[1], lane, value_type), b=read_as(operands[2], lane, value_type))
                updates[destination[1], lane] = result
            write_values(updates)
            continue
        special = None
        texture_opcode = opcode.replace('_aoffimmi', '').removesuffix('_indexable')
        clamped = texture_opcode.endswith('_cl_s')
        if clamped:
            if len(operands) < 2 or operands[1] != 'null':
                raise VanillaFunctionError('A live texture residency result needs an explicit status output.')
            texture_opcode = texture_opcode.removesuffix('_cl_s')
            operands = (operands[0], *operands[2:])
        sampling_modes = {'sample': ('implicit', 4), 'sample_l': ('level', 5), 'sample_b': ('bias', 5),
            'sample_d': ('gradients', 6), 'sample_c': ('compare', 5), 'sample_c_lz': ('compare_level_zero', 5),
            'gather4': ('gather', 4), 'gather4_c': ('gather_compare', 5)}
        gather_offset = None
        if texture_opcode in {'gather4_po','gather4_po_c'}:
            if len(operands) != (6 if texture_opcode.endswith('_c') else 5) or any(instruction.texel_offset):
                raise VanillaFunctionError('The programmable gather has an invalid offset declaration.')
            gather_offset = builder.vector([read(operands[2],lane,'int') for lane in range(2)])
            operands = (*operands[:2], *operands[3:])
            texture_opcode = texture_opcode.replace('_po','')
        if texture_opcode in sampling_modes or texture_opcode in {'ld', 'ldms'}:
            is_load = texture_opcode in {'ld', 'ldms'}
            mode, count = ('implicit', 4 if texture_opcode == 'ldms' else 3) if is_load else sampling_modes[texture_opcode]
            if len(operands) != count + bool(clamped) or clamped and mode not in {'implicit', 'bias', 'gradients', 'compare'}:
                raise VanillaFunctionError('The native texture operation has an invalid operand count or clamp mode.')
            texture = re.fullmatch(r"t(\d+)(?:\.([xyzw]{1,4}))?", operands[2])
            sampler = re.fullmatch(r"s(\d+)(?:\.([xyzw]))?", operands[3]) if not is_load else None
            if texture is None or not is_load and sampler is None:
                raise VanillaFunctionError("This sample needs an indexed resource dependency.")
            if sampler and sampler[2] and mode not in {'gather','gather_compare'}:
                raise VanillaFunctionError('Only gathers select a texture channel on their sampler operand.')
            resource = resource_at('t', int(texture[1]))
            state = resource_at('s', int(sampler[1])) if sampler else None
            if resource is None:
                raise VanillaFunctionError("The native sample has no texture declaration.")
            dimension = resource.dimension.replace('-', '')
            coordinate_type = TEXTURE_LOAD_COORDINATES.get(dimension) if is_load else resource.coordinate_type
            if coordinate_type is None or dimension not in TEXTURE_SPATIAL_WIDTHS:
                raise VanillaFunctionError('This texture dimension needs a different graph operation.')
            width = value_width(coordinate_type)
            spatial = TEXTURE_SPATIAL_WIDTHS[dimension]
            offset_width = 0 if 'cube' in dimension else spatial
            if any(instruction.texel_offset[offset_width:]):
                raise VanillaFunctionError('A native texture offset exceeds its image dimension.')
            offset = instruction.texel_offset[:offset_width]
            extras = {}
            if is_load:
                if (texture_opcode == 'ldms') != ('ms' in dimension):
                    raise VanillaFunctionError('The native multisample operation disagrees with its texture type.')
                extras['sample_index' if texture_opcode == 'ldms' else 'mip_level'] = read(operands[3], 0, 'int') if texture_opcode == 'ldms' else read(operands[1], 3, 'int')
            elif mode in {'level', 'bias', 'compare', 'compare_level_zero', 'gather_compare'}:
                extras[{'level': 'mip_level', 'bias': 'mip_bias', 'compare': 'comparison', 'compare_level_zero': 'comparison', 'gather_compare':'comparison'}[mode]] = read(operands[4], 0, 'float')
            elif mode == 'gradients':
                extras.update((name, builder.vector([read(operands[index], lane, 'float') for lane in range(spatial)]))
                              for name, index in (('gradient_x', 4), ('gradient_y', 5)))
            if clamped:
                extras['minimum_lod'] = read(operands[-1], 0, 'float')
            if gather_offset is not None:
                extras['texel_offset'] = gather_offset
                offset = ()
            sampled = builder.texture_operation('texture_load' if is_load else 'sample_texture', resource,
                [read(operands[1], lane, 'int' if is_load else 'float') for lane in range(width)],
                sampler=state, mode=mode, offset=offset,
                channel='xyzw'.index(sampler[2] or 'x') if sampler else 0, **extras)
            mask = texture[2] or "xyzw"
            if len(mask) not in {1, 4}:
                raise VanillaFunctionError("The native sample has an unsupported result swizzle.")
            destination = re.fullmatch(r'([ro]\d+)(?:\.([xyzw]{1,4}))?', operands[0])
            if destination is None:
                raise VanillaFunctionError('The native texture result needs indexed output recovery.')
            special = {'xyzw'.index(axis): builder.scalar(sampled, 'xyzw'.index(mask[0 if len(mask) == 1 else 'xyzw'.index(axis)]))
                       for axis in destination[2] or 'xyzw'}
        elif opcode in {"dp2", "dp3", "dp4"} and len(operands) == 3:
            width = int(opcode[-1])
            dotted = builder.dot([read(operands[1], lane, 'float') for lane in range(width)], [read(operands[2], lane, 'float') for lane in range(width)])
            special = [dotted] * 4
        elif opcode == "sincos" and len(operands) == 3:
            # Two masked destinations read the same pre-instruction state.
            updates = {}
            for destination, kind in zip(operands[:2], ("sine", "cosine")):
                if destination == "null":
                    continue
                match = re.fullmatch(r"([ro]\d+)(?:\.([xyzw]{1,4}))?", destination)
                if match is None or saturate:
                    raise VanillaFunctionError("This sincos destination needs additional recovery.")
                for axis in match[2] or "xyzw":
                    lane = "xyzw".index(axis)
                    updates[match[1], lane] = builder.operation(kind, value=read(operands[2], lane, 'float'))
            write_values(updates)
            continue
        elif opcode not in {*binary, *unary, *comparisons, "mov", "mad", "movc"}:
            raise VanillaFunctionError(f"{instruction.opcode} needs an additional editable behavior operation.")
        count = 4 if opcode in {"mad", "movc"} else 3 if opcode in {*binary, *comparisons} else 2
        if special is None and len(instruction.operands) != count:
            raise VanillaFunctionError("A native operation has an invalid operand count.")
        destination = destination_register(instruction.operands[0])
        if destination is None:
            raise VanillaFunctionError("This destination needs indexed/side-effect behavior recovery.")
        updates = {}
        for axis in destination[2] or "xyzw":
            lane = "xyzw".index(axis)
            first = read(instruction.operands[1], lane, None if opcode in {'mov', 'movc'} else 'float') if special is None else None
            if special is not None:
                value = special[lane]
            elif opcode in comparisons:
                value = comparison_mask(builder.compare(comparisons[opcode], word_value(builder, first, 'float'), read_as(operands[2], lane, 'float'), nan_result=opcode == "ne"))
            elif opcode == "movc":
                domain = operand_precision(operands[0])[1] or 'uint'

                def selected(operand):
                    # MOVC source modifiers are float modifiers: -r is a sign flip.
                    if operand_precision(operand)[0].startswith(('-', '|', 'abs(')):
                        return word_value(builder, read_as(operand, lane, 'float'), domain)
                    return read_as(operand, lane, domain)
                value = builder.select(condition(operands[1], lane), selected(operands[2]), selected(operands[3]))
            elif opcode == "mad":
                value = builder.operation("multiply_add", a=first, b=read(operands[2], lane, 'float'), c=read(operands[3], lane, 'float'))
            elif opcode in binary:
                value = builder.operation(binary[opcode], a=first, b=read(instruction.operands[2], lane, 'float'))
            elif opcode in unary:
                value = builder.operation(unary[opcode], value=first)
            else:
                value = first
            if saturate:
                value = builder.operation('saturate', value=value if is_float_type(value.value_type) else word_value(builder, value, 'float'))
            updates[destination[1], lane] = value
        # Masked reads use the register state before the instruction writes it.
        write_values(updates)
    if not ended or branches or loops:
        raise VanillaFunctionError("The native program has no terminating return.")
    if ir.stage == 'gs':
        return builder.finish(name, ())
    outputs = []
    for field in phase_outputs:
        parts = []
        for lane in range(4):
            if not field.mask & (1 << lane):
                continue
            value = registers.get((_output_register(field), lane))
            domain = field.value_type.rstrip('234')
            if value is None:
                # Output signature read/write bits mean never written, unlike the input signature's read bits.
                if not field.read_write_mask & (1 << lane):
                    raise VanillaFunctionError('A native output component has no defined calculation.')
                value = builder.unassigned(domain)
            parts.append(word_value(builder, value, domain))
        outputs.append((field, parts))
    return builder.finish(name, outputs)


def recover_dxil_function(ir: ReEngineDxilSemanticIr, contract: ShaderContract, *, name="Vanilla Behavior", _hull_phase=None):
    from .native_local_memory import LocalValueState
    function_name = name
    metadata = DxilEntryMetadata.parse(ir.to_assembly().splitlines())
    if metadata.stage == 'hull' and _hull_phase is None:
        from .hull_behavior import combine_hull_behaviors
        return combine_hull_behaviors(
            recover_dxil_function(ir,contract,name=name+' Control Points',_hull_phase='control_point'),
            recover_dxil_function(ir,contract,name=name+' Patch Constants',_hull_phase='patch'),contract,name)
    if _hull_phase is not None and (metadata.stage != 'hull' or _hull_phase not in {'control_point','patch'}):
        raise VanillaFunctionError('Invalid hull invocation scope.')
    phase_outputs = contract.patch_constants if _hull_phase == 'patch' else contract.outputs
    # NativeLowPrecision is a shader flag; ordinary DXIL half means minimum precision.
    half_type = 'half' if metadata.shader_flags() & (1 << 23) else 'min16float'
    from .native_resource import DXIL_RESOURCE_CLASSES, coherent_uav_bindings, native_constant_buffer_extents
    coherent_bindings = coherent_uav_bindings(metadata)
    cbuffer_extents = native_constant_buffer_extents(metadata)
    if metadata.stage not in {"pixel", "vertex", "compute", "geometry", "domain", "hull", "mesh", "amplification"}:
        raise VanillaFunctionError("This stage needs an execution/side-effect behavior graph.")
    if metadata.stage in {'compute','mesh','amplification'} and metadata.thread_group_size() != contract.thread_group_size:
        raise VanillaFunctionError('Native thread-group dimensions disagree with the renderer contract.')
    if metadata.stage in {'geometry','domain','hull'} and metadata.primitive_execution() != contract.primitive_execution:
        raise VanillaFunctionError('Native geometry execution disagrees with the renderer contract.')
    if metadata.stage in {'mesh','amplification'}:
        if metadata.mesh_execution() != contract.mesh_execution or contract.mesh_execution is None:
            raise VanillaFunctionError('Native mesh execution disagrees with the renderer contract.')
        if contract.mesh_execution.payload_bytes and contract.mesh_execution.payload_layout is None:
            raise VanillaFunctionError('This mesh entry requires a typed amplification-payload adapter.')
    from .vanilla_control_flow import structured_dxil_records
    from .native_scalar_functions import inline_scalar_calls
    entry_name = metadata.hull_patch_function_name() if _hull_phase == 'patch' else metadata.function_name
    events = tuple(inline_scalar_calls(structured_dxil_records(ir, entry_name), ir))
    read_lanes = {}
    for event,record in events:
        if event == 'instruction' and record.opcode == 'extractvalue':
            extraction = re.search(r'extractvalue %dx\.types\.ResRet\.(?:f32|i32) (%[-\w.$]+), ([0-3])(?=\s*(?:, !|$))',record.text)
            if extraction:
                read_lanes[extraction[1]] = read_lanes.get(extraction[1],0) | (1 << int(extraction[2]))
    builder = _Builder(contract, ordered=metadata.stage in {'compute','geometry','mesh','amplification'} or any(event == 'loop_begin' for event,_ in events) or any(resource.register_class == 'u' for resource in contract.resources)
                       or any(record.opcode == 'call' and ('@dx.op.discard(' in record.text or '@dx.op.wave' in record.text
                                                           or '@dx.op.quad' in record.text) for record in ir.records))
    def signature_rows(fields, rows, *, streams=False):
        declared = {(field.stream_key if streams else field.key): field for field in fields}
        if set(rows) != set(declared) or len(set(rows.values())) != len(rows):
            raise VanillaFunctionError("Native signature metadata disagrees with reflection.")
        return {location: declared[key] for key, location in rows.items()}

    input_rows = signature_rows(contract.inputs, metadata.input_rows())
    output_rows = signature_rows(contract.outputs, metadata.output_rows(streams=True),streams=True)
    patch_rows = signature_rows(contract.patch_constants,metadata.patch_rows())
    if metadata.stage == "pixel":
        modes = metadata.input_interpolation()
        if any(modes.get(field.key, "") != field.interpolation for field in contract.inputs):
            raise VanillaFunctionError("Complete the renderer input interpolation contract before recovering this behavior.")
    by_id = {(resource.register_class, resource.resource_id): resource for resource in contract.resources}
    by_binding = {(resource.register_class, resource.register_space, resource.bind_point): resource for resource in contract.resources}
    handles, loads, samples, outputs = {}, {}, {}, {}
    values = LocalValueState()
    written_outputs = set()
    ended = False
    branches = []
    loops = []

    def resolve_value(resolve, text, value_type='float'):
        text = text.strip()
        if value_type == 'native_half':
            value_type = half_type
        if text == 'undef':
            return builder.unassigned(value_type)
        arithmetic = re.fullmatch(r'(fadd|fsub|fmul|fdiv|frem) \((.*)\)', text)
        if arithmetic:
            operands = split(arithmetic[2])
            native_type = 'half' if value_type in {'half', 'min16float'} else 'float'
            if value_type not in {'float', 'half', 'min16float'} or len(operands) != 2 or any(not operand.startswith(native_type + ' ') for operand in operands):
                raise VanillaFunctionError('A floating constant expression has inconsistent operand types.')
            kind = {'fadd': 'add', 'fsub': 'subtract', 'fmul': 'multiply', 'fdiv': 'divide', 'frem': 'float_remainder'}[arithmetic[1]]
            return builder.operation(kind, **{pin: resolve(resolve, operand[len(native_type)+1:], value_type) for pin, operand in zip(('a','b'), operands)})
        if text in {"true", "false"}:
            return builder.boolean(text == "true")
        if text.startswith("%"):
            if text not in values:
                raise VanillaFunctionError(f"{text} has no recovered value expression.")
            result = values[text]
            if result.value_type != value_type:
                if {result.value_type, value_type} <= {'int64_t', 'uint64_t'}:
                    return builder.convert(result, value_type)
                if {result.value_type, value_type} <= {'int', 'uint'}:
                    return builder.convert(result, value_type, bits=True)
                raise VanillaFunctionError(f'{text} has {result.value_type} type, not {value_type}.')
            return result
        if value_type in {'int', 'uint', 'int64_t', 'uint64_t'}:
            return builder.integer(int(text), value_type)
        if value_type in {'half', 'min16float'}:
            if not re.fullmatch(r'0xH[0-9A-Fa-f]{4}', text):
                raise VanillaFunctionError('A half literal must have an exact native binary16 encoding.')
            decoded = builder.operation('unpack_half', value=builder.integer(int(text[3:], 16)))
            return builder.convert(decoded, value_type)
        return builder.float_literal(text)

    value = partial(resolve_value, resolve_value)

    def integer(argument, prefix="i32 "):
        if not argument.startswith(prefix) or not re.fullmatch(r"-?\d+", argument[len(prefix):]):
            raise VanillaFunctionError("A dynamic native index needs an indexed behavior graph.")
        return int(argument[len(prefix):])

    def integer_value(text, bits, *, signed=False):
        result = value(text, ('int64_t' if signed else 'uint64_t') if bits == 64 else 'int' if signed and bits == 32 else 'uint')
        if bits < 32:
            result = builder.operation('bit_and', a=builder.convert(result, 'uint', bits=True), b=builder.integer((1 << bits) - 1))
            if signed:
                shifted = builder.operation('shift_left', a=builder.convert(result, 'int', bits=True), b=builder.integer(32 - bits, 'int'))
                result = builder.operation('shift_right', a=shifted, b=builder.integer(32 - bits, 'int'))
        return result

    def buffer_word(handle, row, component):
        resource = handles.get(handle)
        if isinstance(resource,_ResourceHandle) and resource.heap and resource.input_type == 'constant-buffer':
            address = builder.operation('add',a=builder.operation('multiply',a=row,b=builder.integer(16)),b=builder.integer(component*4))
            node = builder._node('constant_buffer_load',properties={'byte_size':resource.sample_count})
            source = builder.resource_value(resource)
            builder.graph.connect(source.node,source.pin,node.node_id,'buffer')
            builder.graph.connect(address.node,address.pin,node.node_id,'byte_offset')
            return _Value(node.node_id,'value','uint')
        if isinstance(resource, _ResourceHandle) and (resource.index is not None or resource.heap):
            raise VanillaFunctionError('Indexed constant buffers need an explicit typed constant-buffer load.')
        buffer = next((item for item in contract.constant_buffers if resource is not None
            and item.bind_point == resource.bind_point and item.register_space == resource.register_space), None)
        return builder.indexed_constant_word(buffer, row, component)

    def bound_handle(resource, index_argument, uniform_argument):
        if resource is None or uniform_argument not in {'i1 false', 'i1 true'}:
            raise VanillaFunctionError('The descriptor binding or nonuniform declaration is invalid.')
        if resource.bind_count == 1:
            if integer(index_argument) != resource.bind_point:
                raise VanillaFunctionError('A scalar descriptor index differs from its declared binding.')
            return _ResourceHandle(resource, globally_coherent=resource.register_class == 'u' and (resource.register_space, resource.bind_point) in coherent_bindings)
        index = value(index_argument.removeprefix('i32 '), 'uint')
        if resource.bind_point:
            index = builder.operation('subtract', a=index, b=builder.integer(resource.bind_point))
        return _ResourceHandle(resource, index, uniform_argument == 'i1 true',
            globally_coherent=resource.register_class == 'u' and (resource.register_space, resource.bind_point) in coherent_bindings)

    from .native_local_memory import LocalArrays
    local_arrays = LocalArrays(ir, builder, value, half_type)
    values.update(local_arrays.initial_cells)
    from .native_shared_memory import SharedMemory
    shared_memory = SharedMemory(ir, metadata, builder, value)
    pending_precise = None

    def finish_precise_result():
        if pending_precise is None:
            return
        result = values.get(pending_precise)
        if result is None:
            raise VanillaFunctionError('A precise instruction must produce an identified numeric value.')
        values[pending_precise] = builder.precise(result)

    for event, record in events:
        finish_precise_result()
        pending_precise = None
        builder.precise_math = False
        if event == 'case_values':
            selector = value(record['selector'], record['domain'])
            count = len(record['results'][0][2])
            if record['domain'] == 'float':
                valid = builder.select(builder.compare('greater_equal', selector, builder.constant(0)),
                    builder.compare('less', selector, builder.constant(count)), builder.boolean(False))
                valid = builder.select(valid, builder.compare('equal', selector, builder.operation('floor', value=selector)), builder.boolean(False))
                # Sanitize before fptoui: invalid selectors must not create poison.
                selector = builder.select(valid, selector, builder.constant(count))
            index = builder.convert(selector, 'uint')
            selected = {row[0]: builder.array_element(index, [value(item, row[1]) for item in row[2]], cases=True,
                                                      fallback=value(row[3], row[1]) if len(row) > 3 and row[3] is not None
                                                      else None)
                        for row in record['results']}
            values.update(selected)
            continue
        if event == 'loop_begin':
            if builder.clips:
                raise VanillaFunctionError('Repeat after discard needs ordered execution ownership.')
            initial_values = [(name, kind, value(operand, kind)) for name, kind, operand in record]
            initial_values.extend((name, item.value_type, item) for name,item in values.local_items())
            frame = dict(node_start=len(builder.graph.nodes), cache=dict(builder.values), values=values.copy(),
                         handles=dict(handles), loads=dict(loads), samples=dict(samples), pointers=dict(local_arrays.pointers), states=[])
            for i, (name, kind, initial) in enumerate(initial_values):
                kind = initial.value_type
                placeholder = builder._node('function_input', properties=dict(pin_id=f'state_{i}', display_name=f'State {i + 1}', value_type=kind))
                state = _Value(placeholder.node_id, 'value', kind)
                frame['states'].append((name, initial, state))
                values[name] = state
            builder.loop_execution_state(frame)
            loops.append(frame)
            continue
        if event == 'loop_end':
            frame = loops.pop()
            continuation = value(record['continuation'], 'bool')
            if not record['when_true']:
                continuation = builder.compare('equal', continuation, builder.boolean(False))
            load_exports = {name: loads[name] for name in record['exports'] if name in loads}
            handle_names = set(record['exports']) | {handle for handle, _ in load_exports.values()}
            handle_exports = {name: handles[name] for name in handle_names if name in handles}
            if any(name not in values and name not in handle_exports and name not in load_exports for name in record['exports']):
                missing = ', '.join(name for name in record['exports'] if name not in values and name not in handle_exports and name not in load_exports)
                raise VanillaFunctionError(f'A loop exports objects without recovered value expressions: {missing}.')
            if not record['exports'] and builder.execution is None:
                raise VanillaFunctionError('A loop without escaping values requires ordered execution ownership.')
            updates = [value(operand, kind) for name, kind, operand in record['states']]
            local_states = [(name, initial) for name,initial,_ in frame['states'] if local_arrays.is_cell(name)]
            updates.extend(values[name] if name in values else builder.unassigned(initial.value_type)
                           for name,initial in local_states)
            local_exports = {name:item for name,item in values.local_items()}
            exports = {name: values[name] for name in record['exports'] if name in values}
            for name, handle in handle_exports.items():
                if not isinstance(handle, _ResourceHandle):
                    raise VanillaFunctionError('A loop resource export needs a typed descriptor handle.')
                if handle.index is not None:
                    exports['resource_index', name] = handle.index
            literal_rows = {name: int(builder.graph.node(row.node).properties['values'][0])
                            for name, (_, row) in load_exports.items()
                            if builder.graph.node(row.node).kind == 'constant'}
            exports.update({('constant_row', name): row for name, (_, row) in load_exports.items() if name not in literal_rows})
            joined = builder.merge_loop(frame, continuation, updates, {**exports, **local_exports})
            handle_exports = {name: replace(handle, index=joined.pop(('resource_index', name))) if handle.index is not None else handle
                              for name, handle in handle_exports.items()}
            load_exports = {name: (handle, builder.integer(literal_rows[name]) if name in literal_rows
                                   else joined.pop(('constant_row', name))) for name, (handle, _) in load_exports.items()}
            values = frame['values'].nonlocal_copy()
            values.update(joined)
            handles, loads, samples = {**frame['handles'], **handle_exports}, {**frame['loads'], **load_exports}, frame['samples']
            local_arrays.pointers = frame['pointers']
            continue
        if event == "aliases":
            # PHIs read predecessor state simultaneously.
            aliases = {name: value(operand, kind) for name, kind, operand in record}
            values.update(aliases)
            continue
        if event == "begin":
            predicate = (builder.compare('equal' if record[0] == 'case' else 'less',
                                         value(record[1], 'uint'), builder.integer(record[2]))
                         if isinstance(record, tuple) and record[0] in {'case', 'case_less'} else value(record, "bool"))
            branches.append(dict(condition=predicate, node_start=len(builder.graph.nodes),
                values=values.copy(), cache=dict(builder.values), outputs=dict(outputs),
                handles=dict(handles), loads=dict(loads), samples=dict(samples), pointers=dict(local_arrays.pointers), clips=tuple(builder.clips), execution=builder.execution))
            continue
        if event == "else":
            frame = branches[-1]
            frame["true_phis"] = tuple((name, value(operand, kind)) for name, kind, operand in record)
            frame['true_cells'] = {name:item for name,item in values.local_items()}
            frame["true_outputs"] = dict(outputs)
            frame['true_execution'], builder.execution = builder.execution, frame['execution']
            frame['true_clips'] = tuple(builder.clips[len(frame['clips']):])
            builder.clips = list(frame['clips'])
            frame["true_nodes"] = builder.graph.nodes_since(frame["node_start"])
            values, outputs = frame["values"].copy(), dict(frame["outputs"])
            handles, loads, samples = dict(frame["handles"]), dict(frame["loads"]), dict(frame["samples"])
            local_arrays.pointers = dict(frame['pointers'])
            builder.values = dict(frame["cache"])
            continue
        if event == "end":
            frame = branches.pop()
            false_phis = tuple((name, value(operand, kind)) for name, kind, operand in record)
            if tuple(name for name, _ in frame["true_phis"]) != tuple(name for name, _ in false_phis):
                raise VanillaFunctionError("The branch arms do not reach the same SSA join.")
            cells = sorted(frame['true_cells'].keys() & values.keys())
            frame['true_phis'] += tuple((name,frame['true_cells'][name]) for name in cells)
            false_phis += tuple((name,values[name]) for name in cells)
            common = sorted(frame["true_outputs"].keys() | outputs.keys())
            for key in common:
                if key not in frame['true_outputs']:
                    missing = builder.unassigned(outputs[key].value_type)
                    frame['true_outputs'][key] = missing
                    frame['true_nodes'].add(missing.node)
                if key not in outputs:
                    outputs[key] = builder.unassigned(frame['true_outputs'][key].value_type)
            changed = [key for key in common if frame["true_outputs"][key] != outputs[key]]
            true_values = [value for _, value in frame["true_phis"]] + [frame["true_outputs"][key] for key in changed]
            false_values = [value for _, value in false_phis] + [outputs[key] for key in changed]
            false_nodes = builder.graph.nodes_since(frame["node_start"]) - frame["true_nodes"]
            clips = (frame['true_clips'], tuple(builder.clips[len(frame['clips']):]))
            builder.clips = list(frame['clips'])
            joined = builder.merge_branch(frame["condition"], frame["true_nodes"], false_nodes, true_values, false_values, clips=clips,
                executions=(frame['true_execution'], builder.execution) if builder.execution is not None else ())
            values = frame["values"].nonlocal_copy()
            values.update((name, item) for (name, _), item in zip(false_phis, joined))
            outputs = {key: outputs[key] for key in common if key not in changed}
            outputs.update(zip(changed, joined[len(false_phis):]))
            handles, loads, samples = frame["handles"], frame["loads"], frame["samples"]
            local_arrays.pointers = frame['pointers']
            continue
        body = record.text.split("=", 1)[1].strip() if record.result else record.text
        annotation = re.search(r', !dx\.precise (!\d+)(?=,|$)', body)
        if annotation:
            operands = metadata.definitions.get(annotation[1])
            if operands not in {('i32 0',), ('i32 1',)} or not record.result:
                raise VanillaFunctionError('The native calculation-order annotation is invalid.')
            if operands == ('i32 1',):
                pending_precise = record.result
                builder.precise_math = True
            body = body[:annotation.start()] + body[annotation.end():]
            body = re.sub(r' #\d+$', '', body)
        if body == "ret void":
            if ended:
                raise VanillaFunctionError("Multiple returns need control-flow behavior recovery.")
            ended = True
            continue
        if ended:
            raise VanillaFunctionError("Instructions after a return need control-flow recovery.")
        if shared_memory.apply(record, body, values) or local_arrays.apply(record, body, values):
            continue
        arithmetic = re.fullmatch(r"(fadd|fsub|fmul|fdiv|frem)(?: (?:fast|nnan|ninf|nsz|arcp|contract|afn|reassoc))* (float|half) ([^,]+), (.+)", body)
        if arithmetic:
            kind = {"fadd": "add", "fsub": "subtract", "fmul": "multiply", "fdiv": "divide", 'frem': 'float_remainder'}[arithmetic[1]]
            domain = half_type if arithmetic[2] == 'half' else 'float'
            precise = not re.match(r'\w+ fast ', body)
            builder.precise_math |= precise
            values[record.result] = builder.operation(kind, a=value(arithmetic[3], domain), b=value(arithmetic[4], domain))
            if precise:
                pending_precise = record.result
            continue
        arithmetic = re.fullmatch(r'(add|sub|mul|udiv|sdiv|urem|srem|and|or|xor|shl|lshr|ashr)(?: (?:nuw|nsw|exact))* (i64|i32|i16|i8|i1) ([^,]+), (.+)', body)
        if arithmetic:
            opcode, native_type, a, b = arithmetic.groups()
            if native_type == 'i1':
                a, b = value(a, 'bool'), value(b, 'bool')
                if opcode == 'and': result = builder.select(a, b, builder.boolean(False))
                elif opcode == 'or': result = builder.select(a, builder.boolean(True), b)
                elif opcode == 'xor': result = builder.compare('not_equal', a, b)
                else: raise VanillaFunctionError('This one-bit arithmetic needs explicit native semantics.')
            else:
                kind = {'add':'add', 'sub':'subtract', 'mul':'multiply', 'udiv':'divide', 'sdiv':'divide',
                    'urem':'integer_remainder', 'srem':'integer_remainder', 'and':'bit_and', 'or':'bit_or',
                    'xor':'bit_xor', 'shl':'shift_left', 'lshr':'shift_right', 'ashr':'shift_right'}[opcode]
                bits, signed = int(native_type[1:]), opcode in {'sdiv', 'srem', 'ashr'}
                result = builder.operation(kind, a=integer_value(a, bits, signed=signed), b=integer_value(b, bits, signed=signed))
                if bits < 32:
                    result = builder.operation('bit_and', a=builder.convert(result, 'uint', bits=True), b=builder.integer((1 << bits) - 1))
            values[record.result] = result
            continue
        integer_comparison = re.fullmatch(r'icmp (eq|ne|[su](?:lt|le|gt|ge)) (i64|i32|i16|i8|i1) ([^,]+), (.+)', body)
        if integer_comparison:
            predicate, native_type, a, b = integer_comparison.groups()
            value_type = 'bool' if native_type == 'i1' else 'int' if predicate.startswith('s') else 'uint'
            relation = {'eq':'equal', 'ne':'not_equal', 'lt':'less', 'le':'less_equal', 'gt':'greater', 'ge':'greater_equal'}[predicate[-2:]]
            operands = (value(a, 'bool'), value(b, 'bool')) if native_type == 'i1' else tuple(
                integer_value(text, int(native_type[1:]), signed=value_type == 'int') for text in (a, b))
            values[record.result] = builder.compare(relation, *operands)
            continue
        float_conversion = re.fullmatch(r'(fptrunc|fpext) (float|half) (.+) to (float|half)', body)
        if float_conversion:
            opcode, source, argument, target = float_conversion.groups()
            if (opcode, source, target) not in {('fptrunc', 'float', 'half'), ('fpext', 'half', 'float')}:
                raise VanillaFunctionError('A floating-point conversion has inconsistent precision domains.')
            values[record.result] = builder.convert(value(argument, half_type if source == 'half' else 'float'),
                                                    half_type if target == 'half' else 'float')
            continue
        conversion = re.fullmatch(r'(uitofp|sitofp|fptoui|fptosi|bitcast|zext|sext|trunc) (float|i64|i32|i16|i8|i1) (.+) to (float|i64|i32|i16|i8|i1)', body)
        if conversion:
            opcode, source_type, argument, target_type = conversion.groups()
            source_bits = int(source_type[1:]) if source_type.startswith('i') else 32
            target_bits = int(target_type[1:]) if target_type.startswith('i') else 32
            if opcode == 'bitcast' and source_bits != target_bits:
                raise VanillaFunctionError('A bitcast cannot change the declared bit width.')
            source_type = 'float' if source_type == 'float' else 'bool' if source_type == 'i1' else ('int64_t' if opcode == 'sitofp' else 'uint64_t') if source_bits == 64 else 'int' if opcode == 'sitofp' else 'uint'
            target_type = 'float' if target_type == 'float' else 'bool' if target_type == 'i1' else ('int64_t' if opcode == 'fptosi' else 'uint64_t') if target_bits == 64 else 'int' if opcode == 'fptosi' else 'uint'
            operand = (integer_value(argument, source_bits, signed=opcode in {'sext', 'sitofp'})
                       if source_bits in {8, 16} or opcode == 'sext' and source_type != 'bool' else value(argument, source_type))
            if opcode == 'sext' and source_type == 'bool':
                result = builder.select(operand, builder.integer((1 << target_bits)-1, target_type), builder.integer(0, target_type))
            elif opcode == 'trunc' and target_type == 'bool':
                low_bit = builder.operation('bit_and', a=operand, b=builder.integer(1))
                result = builder.compare('not_equal', low_bit, builder.integer(0))
            else:
                result = builder.convert(operand, target_type, bits=opcode == 'bitcast')
            if target_bits in {8, 16}:
                result = builder.operation('bit_and', a=builder.convert(result, 'uint', bits=True), b=builder.integer((1 << target_bits) - 1))
            values[record.result] = result
            continue
        comparison = re.fullmatch(r"fcmp(?: (?:fast|nnan|ninf|nsz|arcp|contract|afn|reassoc))* ([ou])(eq|ne|lt|le|gt|ge) (float|half) ([^,]+), (.+)", body)
        if comparison:
            relation = {"eq": "equal", "ne": "not_equal", "lt": "less", "le": "less_equal", "gt": "greater", "ge": "greater_equal"}[comparison[2]]
            domain = half_type if comparison[3] == 'half' else 'float'
            precise = not re.match(r'fcmp fast ', body)
            builder.precise_math |= precise
            values[record.result] = builder.compare(relation, value(comparison[4], domain), value(comparison[5], domain), nan_result=comparison[1] == "u")
            if precise:
                pending_precise = record.result
            continue
        selection = re.fullmatch(r"select i1 ([^,]+), (float|half|i64|i32|i16|i8|i1) ([^,]+), \2 (.+)", body)
        if selection:
            value_type = {'float':'float', 'half':half_type, 'i64':'uint64_t', 'i32':'uint', 'i16':'uint', 'i8':'uint', 'i1':'bool'}[selection[2]]
            operands = tuple(integer_value(text, int(selection[2][1:])) if selection[2] in {'i8', 'i16'} else value(text, value_type)
                             for text in (selection[3], selection[4]))
            values[record.result] = builder.select(value(selection[1], 'bool'), *operands)
            continue
        extraction = re.fullmatch(r'extractvalue %dx\.types\.(twoi32|i32c) (%[-\w.$]+), ([01])',body)
        if extraction:
            pair = samples.get(extraction[2])
            if not isinstance(pair,_LoadedWords) or len(pair.words)!=2:
                raise VanillaFunctionError('A paired integer result has no recovered calculation.')
            domain = 'bool' if extraction[1]=='i32c' and extraction[3]=='1' else 'uint'
            values[record.result] = builder.convert(pair.words[int(extraction[3])],domain,bits=True)
            continue
        extraction = re.fullmatch(r"extractvalue %dx\.types\.CBufRet\.(f32|i32) (%[-\w.$]+), ([0-3])", body)
        if extraction:
            if extraction[2] not in loads:
                raise VanillaFunctionError("A native constant result has no declared load.")
            handle, row = loads[extraction[2]]
            values[record.result] = builder.convert(buffer_word(handle, row, int(extraction[3])),
                'float' if extraction[1] == 'f32' else 'uint', bits=True)
            continue
        extraction = re.fullmatch(r'extractvalue %dx\.types\.Dimensions (%[-\w.$]+), ([0-3])', body)
        if extraction:
            queried = samples.get(extraction[1])
            if not isinstance(queried, _LoadedWords) or queried.words[int(extraction[2])] is None:
                raise VanillaFunctionError('A dimension query reads an undeclared result lane.')
            values[record.result] = queried.words[int(extraction[2])]
            continue
        extraction = re.fullmatch(r'extractvalue %dx\.types\.fouri32 (%[-\w.$]+), ([0-3])', body)
        if extraction:
            values[record.result] = builder.scalar(value(extraction[1], 'uint4'), int(extraction[2]))
            continue
        extraction = re.fullmatch(r"extractvalue %dx\.types\.ResRet\.(f32|i32) (%[-\w.$]+), ([0-3])", body)
        if extraction:
            if extraction[2] not in samples:
                raise VanillaFunctionError("The resource result has no recovered sample.")
            sampled = samples[extraction[2]]
            lane = int(extraction[3])
            if isinstance(sampled, _LoadedWords):
                component = sampled.words[lane]
                if component is None:
                    raise VanillaFunctionError('The shader reads a word outside its native load mask.')
            elif isinstance(sampled, _BufferLoad):
                if not sampled.mask & (1 << lane):
                    raise VanillaFunctionError("The shader reads a component absent from the native resource load mask.")
                grouped = (builder.buffer_words(sampled.resource, sampled.address, sampled.mask & read_lanes.get(extraction[2], 0))
                           if sampled.record_offset is None else None)
                if grouped and grouped[lane] is not None:
                    component = grouped[lane]
                else:
                    pointer = builder.operation("add", a=sampled.address, b=builder.integer(lane * 4)) if lane and sampled.record_offset is None else sampled.address
                    component = builder.buffer_word(sampled.resource, pointer,
                        record_offset=builder.word_offset(sampled.record_offset, lane))
            else:
                component = builder.scalar(sampled, lane)
            values[record.result] = builder.convert(component, 'float' if extraction[1] == 'f32' else 'uint', bits=True)
            continue
        call = re.fullmatch(r"call .+ @dx\.op\.([\w.]+)\((.*)\)", body)
        if call is None:
            raise VanillaFunctionError(f"{record.opcode} needs an additional editable behavior operation.")
        operation, arguments = call[1], split(call[2])
        from .native_collective import collective_call
        collective = collective_call(operation, arguments)
        if collective is not None:
            kind, domain, operands = collective
            values[record.result] = builder.operation(kind, _value_type=domain,
                **{pin: value(operand, value_type) for pin, value_type, operand in operands})
            continue
        if operation=='domainLocation.f32' and len(arguments)==2 and integer(arguments[0])==105:
            values[record.result] = builder.domain_coordinate(integer(arguments[1],'i8 '))
        elif operation in {'loadPatchConstant.f32','loadPatchConstant.i32'} and len(arguments)==4 and integer(arguments[0])==104:
            field = patch_rows.get((integer(arguments[1]),integer(arguments[2])))
            if field is None:
                raise VanillaFunctionError('A patch read is outside its declared signature.')
            values[record.result] = builder.convert(builder.patch_constant(field,integer(arguments[3],'i8 ')),
                'float' if operation.endswith('f32') else 'uint',bits=True)
        elif operation in {'gsInstanceID.i32','primitiveID.i32','outputControlPointID.i32'}:
            opcode, meaning = {'gsInstanceID.i32':(100,'geometry_instance_id'),'primitiveID.i32':(108,'primitive_id'),
                               'outputControlPointID.i32':(107,'output_control_point_id')}[operation]
            if meaning == 'output_control_point_id' and _hull_phase != 'control_point':
                raise VanillaFunctionError('Control Point ID belongs only to the control-point invocation.')
            if arguments != (f'i32 {opcode}',):
                raise VanillaFunctionError('Invalid native geometry invocation query.')
            values[record.result] = builder.invocation_input(meaning)
        elif operation in {'setMeshOutputCounts','emitIndices'} or operation.startswith(('storeVertexOutput.', 'storePrimitiveOutput.')):
            from file_handlers.sdf.shader_graph_mesh import attribute_properties
            if metadata.stage != 'mesh':
                raise VanillaFunctionError('Mesh output operation outside a mesh entry.')
            execution = contract.mesh_execution
            if operation == 'setMeshOutputCounts':
                if len(arguments) != 3 or integer(arguments[0]) != 168 or any(not a.startswith('i32 ') for a in arguments[1:]):
                    raise VanillaFunctionError('Invalid native mesh output counts.')
                builder.execute('mesh_output_counts', vertices=value(arguments[1][4:], 'uint'), primitives=value(arguments[2][4:], 'uint'))
            elif operation == 'emitIndices':
                if len(arguments) != 5 or integer(arguments[0]) != 169 or any(not a.startswith('i32 ') for a in arguments[1:]):
                    raise VanillaFunctionError('Invalid native mesh primitive indices.')
                count = 2 if execution.output_topology == 'line' else 3
                # DXC emits a zero placeholder for the unused line endpoint.
                if count == 2 and arguments[4] not in {'i32 undef', 'i32 0'}:
                    raise VanillaFunctionError('A line mesh writes an unexpected third vertex index.')
                builder.execute('mesh_indices', properties={'topology': execution.output_topology},
                    index=value(arguments[1][4:], 'uint'), vertices=builder.vector([value(a[4:], 'uint') for a in arguments[2:2+count]]))
            else:
                primitive = operation.startswith('storePrimitiveOutput.')
                native_type = operation.rsplit('.', 1)[1]
                prefix, domain = {'f32':('float ', 'float'), 'i32':('i32 ', 'uint')}.get(native_type, ('', ''))
                if (len(arguments) != 6 or integer(arguments[0]) != (172 if primitive else 171) or not prefix
                        or not arguments[4].startswith(prefix) or not arguments[5].startswith('i32 ')):
                    raise VanillaFunctionError('Invalid native mesh output store.')
                location = integer(arguments[1]), integer(arguments[2])
                field = patch_rows.get(location) if primitive else output_rows.get(location)
                column = integer(arguments[3], 'i8 ')
                if field is None or not 0 <= column < field.mask.bit_count():
                    raise VanillaFunctionError('Mesh output store has no declared semantic component.')
                source_value = value(arguments[4][len(prefix):], domain)
                output_value = (builder.compare('not_equal',source_value,builder.integer(0)) if field.value_type == 'bool'
                                else builder.convert(source_value, field.value_type.rstrip('234'), bits=True))
                builder.execute('mesh_primitive' if primitive else 'mesh_vertex', properties=attribute_properties(field, column),
                    index=value(arguments[5][4:], 'uint'), value=output_value)
        elif operation in {'emitStream','cutStream','emitThenCutStream'}:
            opcode = {'emitStream':97,'cutStream':98,'emitThenCutStream':99}[operation]
            if metadata.stage != 'geometry' or len(arguments)!=2 or integer(arguments[0])!=opcode:
                raise VanillaFunctionError('Invalid native geometry emission operation.')
            stream = integer(arguments[1],'i8 ')
            if stream not in contract.primitive_execution.output_streams:
                raise VanillaFunctionError('Geometry emission targets an undeclared stream.')
            if operation != 'cutStream':
                from file_handlers.sdf.shader_graph_geometry import vertex_fields
                emitted = {}
                for i,field in enumerate(field for field in contract.outputs if field.stream==stream):
                    parts = []
                    for lane in range(field.mask.bit_count()):
                        part = outputs.get((field.stream_key,lane))
                        parts.append(part if part is not None else builder.unassigned(field.value_type.rstrip('234')))
                    emitted[f'field{i}'] = builder.vector(parts)
                builder.execute('emit_vertex',properties={'stream':stream,'fields':vertex_fields(contract,stream)},**emitted)
                # Each emit invalidates all output slots, across every stream.
                outputs = {}
            if operation != 'emitStream':
                builder.execute('end_primitive',properties={'stream':stream})
        elif operation in {'threadId.i32', 'groupId.i32', 'threadIdInGroup.i32', 'flattenedThreadIdInGroup.i32'}:
            opcode, meaning = {'threadId.i32':(93,'dispatch_thread_id'), 'groupId.i32':(94,'group_id'),
                'threadIdInGroup.i32':(95,'group_thread_id'), 'flattenedThreadIdInGroup.i32':(96,'group_index')}[operation]
            if len(arguments) != (1 if opcode == 96 else 2) or integer(arguments[0]) != opcode:
                raise VanillaFunctionError('Invalid native invocation query.')
            values[record.result] = builder.invocation_input(meaning, 0 if opcode == 96 else integer(arguments[1]))
        elif operation == "createHandle" and len(arguments) == 5 and integer(arguments[0]) == 57:
            kind = DXIL_RESOURCE_CLASSES.get(integer(arguments[1], "i8 "))
            resource = by_id.get((kind, integer(arguments[2])))
            handles[record.result] = bound_handle(resource, arguments[3], arguments[4])
        elif operation == "createHandleFromBinding" and len(arguments) == 4 and integer(arguments[0]) == 217:
            declaration = arguments[1]
            if declaration == "%dx.types.ResBind zeroinitializer":
                declaration = "%dx.types.ResBind { i32 0, i32 0, i32 0, i8 0 }"
            binding = re.fullmatch(r"%dx.types.ResBind \{ i32 (\d+), i32 (\d+|-1), i32 (\d+), i8 ([0-3]) \}", declaration)
            if binding is None:
                raise VanillaFunctionError('The descriptor range declaration is invalid.')
            resource = by_binding.get((DXIL_RESOURCE_CLASSES[int(binding[4])], int(binding[3]), int(binding[1])))
            expected_end = -1 if resource and resource.bind_count in {0, 0xffffffff} else resource.bind_point + resource.bind_count - 1 if resource else None
            if expected_end != int(binding[2]):
                raise VanillaFunctionError('The native descriptor range disagrees with reflection.')
            handles[record.result] = bound_handle(resource, arguments[2], arguments[3])
        elif operation == 'createHandleFromHeap' and len(arguments) == 4 and integer(arguments[0]) == 218:
            if arguments[2] not in {'i1 false', 'i1 true'} or arguments[3] not in {'i1 false', 'i1 true'} or not arguments[1].startswith('i32 '):
                raise VanillaFunctionError('The descriptor heap index/selection is invalid.')
            handles[record.result] = _HeapIndex(value(arguments[1][4:], 'uint'), arguments[2] == 'i1 true', arguments[3] == 'i1 true')
        elif operation == "annotateHandle" and len(arguments) == 3 and integer(arguments[0]) == 216:
            handle = arguments[1].removeprefix("%dx.types.Handle ")
            if handle not in handles:
                raise VanillaFunctionError("A dynamic descriptor needs its own behavior dependency.")
            resource = handles[handle]
            annotation = re.fullmatch(r"%dx.types.ResourceProperties \{ i32 (\d+), i32 (\d+) \}", arguments[2])
            if annotation is None:
                raise VanillaFunctionError('The descriptor property declaration is invalid.')
            from .native_resource import heap_resource, validate_resource_properties
            kind, layout = map(int, annotation.groups())
            if isinstance(resource, _HeapIndex):
                coherent = bool(kind & 16384)
                decoded = heap_resource(kind & ~16384, layout, sampler_heap=resource.sampler_heap)
                if coherent and decoded.register_class != 'u':
                    raise VanillaFunctionError('Only UAV handles can declare global coherence.')
                resource = _ResourceHandle(decoded, resource.index, resource.non_uniform, True, coherent)
            else:
                validate_resource_properties(resource, kind, layout, contract,
                                             native_cbuffer_extents=cbuffer_extents)
            handles[record.result] = resource
        elif operation in {'atomicBinOp.i32', 'atomicCompareExchange.i32', 'atomicBinOp.i64', 'atomicCompareExchange.i64'}:
            exchange = operation.startswith('atomicCompareExchange.')
            wide = operation.endswith('i64')
            if len(arguments) != 7 or integer(arguments[0]) != (79 if exchange else 78):
                raise VanillaFunctionError('The native atomic operation has an inconsistent declaration.')
            resource = handles.get(arguments[1].removeprefix('%dx.types.Handle '))
            selector = None if exchange else integer(arguments[2])
            mode = 'compare_exchange' if exchange else {0:'add', 1:'and', 2:'or', 3:'xor', 4:'minimum', 5:'maximum', 6:'minimum', 7:'maximum', 8:'exchange'}.get(selector)
            offset = 2 if exchange else 3
            structured = resource is not None and resource.input_type == 'rw-structured-buffer'
            coordinates_type = TEXTURE_LOAD_COORDINATES.get(resource.dimension.replace('-', '')) if resource and resource.input_type == 'rw-typed' else 'uint2' if structured else 'uint'
            coordinate_count = int(coordinates_type[-1]) if coordinates_type and coordinates_type[-1].isdigit() else 1
            if mode is None or coordinates_type is None or any(argument != 'i32 undef' for argument in arguments[offset+coordinate_count:offset+3]):
                raise VanillaFunctionError('This atomic operation needs a different operation/coordinate adapter.')
            prefix = 'i64 ' if wide else 'i32 '
            if not arguments[offset].startswith('i32 ') or any(not arguments[index].startswith(prefix) for index in (6, 5) if index != 5 or exchange):
                raise VanillaFunctionError('Atomic coordinates and values must be native integer words.')
            domain = ('int64_t' if selector in {4, 5} else 'uint64_t') if wide else 'int' if selector in {4, 5} else 'uint'
            coordinates = builder.vector([value(argument.removeprefix('i32 '), 'uint') for argument in arguments[offset:offset+coordinate_count]])
            values[record.result] = builder.memory('memory_atomic', resource, builder.scalar(coordinates, 0) if structured else coordinates,
                record_offset=builder.scalar(coordinates, 1) if structured else None, operation=mode,
                value=value(arguments[6][4:], domain), compare=value(arguments[5][4:], domain) if exchange else None)['original']
        elif operation == 'getDimensions' and len(arguments) == 3 and integer(arguments[0]) == 72:
            resource = handles.get(arguments[1].removeprefix('%dx.types.Handle '))
            mipmapped = resource is not None and resource.input_type == 'texture' and 'ms' not in resource.dimension
            if not mipmapped and arguments[2] not in {'i32 undef', 'i32 0'}:
                raise VanillaFunctionError('This resource dimension query cannot select a mip.')
            queried, shape = builder.dimensions(resource, value(arguments[2].removeprefix('i32 '), 'uint') if mipmapped else None)
            samples[record.result] = _LoadedWords(tuple(queried.get(pin) for pin in shape.native_lanes))
        elif operation in {'rawBufferStore.i32', 'rawBufferStore.f32', 'bufferStore.i32', 'bufferStore.f32'}:
            raw = operation.startswith('rawBufferStore')
            if len(arguments) != (10 if raw else 9) or integer(arguments[0]) != (140 if raw else 69):
                raise VanillaFunctionError('This store needs a different typed buffer coordinate adapter.')
            resource = handles.get(arguments[1].removeprefix('%dx.types.Handle '))
            mask = integer(arguments[8], 'i8 ')
            if not 1 <= mask <= 15 or raw and integer(arguments[9]) not in {4, 8, 16}:
                raise VanillaFunctionError('The buffer store has an invalid word mask or alignment.')
            address = value(arguments[2].removeprefix('i32 '), 'uint')
            record_offset = None if arguments[3] == 'i32 undef' else value(arguments[3].removeprefix('i32 '), 'uint')
            domain, prefix = ('float', 'float ') if operation.endswith('f32') else ('uint', 'i32 ')
            width = mask.bit_length()
            if (raw and record_offset is None and mask == (1 << width) - 1 and width > 1
                    and resource is not None and resource.input_type == 'rw-byte-address-buffer'
                    and all(arguments[4 + lane].startswith(prefix) for lane in range(width))):
                # Kept as one StoreN to match the native program.
                words = [builder.convert(value(arguments[4 + lane][len(prefix):], domain), 'uint', bits=True) for lane in range(width)]
                builder.memory('memory_store', resource, address, value=builder.vector(words), words=width)
            else:
                for lane in range(4):
                    if mask & (1 << lane):
                        if not arguments[4+lane].startswith(prefix):
                            raise VanillaFunctionError('An active store word has the wrong native type.')
                        pointer = builder.operation('add', a=address, b=builder.integer(lane * 4)) if lane and record_offset is None else address
                        builder.memory('memory_store', resource, pointer, record_offset=builder.word_offset(record_offset, lane), value=builder.convert(value(arguments[4+lane][len(prefix):], domain), 'uint', bits=True))
        elif operation == 'barrier' and len(arguments) == 2 and integer(arguments[0]) == 80:
            mode = integer(arguments[1])
            scope = {2:'device', 3:'device', 8:'group', 9:'group', 10:'all', 11:'all'}.get(mode)
            if scope is None:
                raise VanillaFunctionError('This barrier requires an additional exact execution-scope adapter.')
            builder.execute('memory_barrier', properties=dict(scope=scope, synchronize=bool(mode & 1)))
        elif operation in {"rawBufferLoad.i32", "rawBufferLoad.f32", "bufferLoad.i32", "bufferLoad.f32"}:
            raw = operation.startswith("rawBufferLoad")
            if len(arguments) != (6 if raw else 4) or integer(arguments[0]) != (139 if raw else 68):
                raise VanillaFunctionError("The native buffer load declaration is inconsistent.")
            resource = handles.get(arguments[1].removeprefix("%dx.types.Handle "))
            mask, alignment = (integer(arguments[4], "i8 "), integer(arguments[5])) if raw else (15, 4)
            if not 1 <= mask <= 15 or alignment not in {4, 8, 16}:
                raise VanillaFunctionError("This buffer read needs a structured/precision/alignment adapter.")
            address = value(arguments[2].removeprefix("i32 "), "uint")
            offset = None if arguments[3] == "i32 undef" else value(arguments[3].removeprefix('i32 '), 'uint')
            samples[record.result] = _BufferLoad(resource, address, offset, mask)
            if builder.execution is not None:
                consumed_mask = mask & read_lanes.get(record.result,0)
                grouped = builder.buffer_words(resource, address, consumed_mask) if offset is None else None
                samples[record.result] = _LoadedWords(grouped) if grouped else _LoadedWords(tuple(builder.buffer_word(resource,
                    builder.operation('add', a=address, b=builder.integer(lane * 4)) if lane and offset is None else address,
                    record_offset=builder.word_offset(offset, lane)) if consumed_mask & (1 << lane) else None for lane in range(4)))
        elif operation in {"cbufferLoadLegacy.f32", 'cbufferLoadLegacy.i32'} and len(arguments) == 3 and integer(arguments[0]) == 59:
            handle = arguments[1].removeprefix("%dx.types.Handle ")
            if handle not in handles or handles[handle].register_class != "b":
                raise VanillaFunctionError("A constant load has no exact native binding.")
            if not arguments[2].startswith('i32 '):
                raise VanillaFunctionError('The constant-register index must be a native 32-bit integer.')
            loads[record.result] = handle, value(arguments[2][4:], 'uint')
        elif operation in {'sample.f32', 'sampleLevel.f32', 'sampleBias.f32', 'sampleGrad.f32', 'sampleCmp.f32', 'sampleCmpLevelZero.f32'}:
            expected_opcode, count, mode = {'sample.f32': (60, 11, 'implicit'), 'sampleBias.f32': (61, 12, 'bias'),
                'sampleLevel.f32': (62, 11, 'level'), 'sampleGrad.f32': (63, 17, 'gradients'),
                'sampleCmp.f32': (64, 12, 'compare'), 'sampleCmpLevelZero.f32': (65, 11, 'compare_level_zero')}[operation]
            if len(arguments) != count or integer(arguments[0]) != expected_opcode:
                raise VanillaFunctionError('The native sample declaration is inconsistent.')
            texture = handles.get(arguments[1].removeprefix("%dx.types.Handle "))
            sampler = handles.get(arguments[2].removeprefix("%dx.types.Handle "))
            if texture is None:
                raise VanillaFunctionError("This sample has no explicit texture dependency.")
            width = value_width(texture.coordinate_type)
            dimension = texture.dimension.replace('-', '')
            spatial = TEXTURE_SPATIAL_WIDTHS[dimension]
            offset_width = 0 if 'cube' in dimension else spatial
            if any(arg != 'float undef' for arg in arguments[3+width:7]) or any(arg not in {'i32 0', 'i32 undef'} for arg in arguments[7+offset_width:10]):
                raise VanillaFunctionError('The native sample has active coordinates outside its texture dimension.')
            offsets = tuple(0 if arg == 'i32 undef' else integer(arg) for arg in arguments[7:7+offset_width])
            if any(not arg.startswith("float ") for arg in arguments[3:3+width]):
                raise VanillaFunctionError("Sample coordinates are not float values.")
            base_level = None
            if mode == 'implicit' and contract.stage != 'pixel':
                # Zero derivatives select the base level, as D3D sampling does outside the pixel stage.
                if arguments[-1] != 'float undef':
                    raise VanillaFunctionError('Derivative-free implicit sampling needs its minimum-LOD clamp as an explicit level.')
                mode, base_level = 'level', builder.constant(0)
            extras = {}
            if base_level is not None:
                extras['mip_level'] = base_level
            elif mode in {'level', 'bias', 'compare', 'compare_level_zero'}:
                extras[{'level': 'mip_level', 'bias': 'mip_bias', 'compare': 'comparison', 'compare_level_zero': 'comparison'}[mode]] = value(arguments[10].removeprefix('float '))
            if mode == 'gradients':
                for name, start in (('gradient_x', 10), ('gradient_y', 13)):
                    if any(arg != 'float undef' for arg in arguments[start+spatial:start+3]):
                        raise VanillaFunctionError('The native gradient has active components outside the texture dimension.')
                    extras[name] = builder.vector([value(arg.removeprefix('float ')) for arg in arguments[start:start+spatial]])
            if mode in {'implicit', 'bias', 'gradients', 'compare'} and arguments[-1] != 'float undef':
                extras['minimum_lod'] = value(arguments[-1].removeprefix('float '))
            samples[record.result] = builder.texture_operation('sample_texture', texture, [value(arg[6:]) for arg in arguments[3:3+width]],
                sampler=sampler, mode=mode, offset=offsets if any(offsets) else (), **extras)
        elif operation in {'textureGather.f32', 'textureGather.i32', 'textureGatherCmp.f32'}:
            comparison = operation.startswith('textureGatherCmp')
            if len(arguments) != (11 if comparison else 10) or integer(arguments[0]) != (74 if comparison else 73):
                raise VanillaFunctionError('The native gather declaration is inconsistent.')
            texture = handles.get(arguments[1].removeprefix('%dx.types.Handle '))
            sampler = handles.get(arguments[2].removeprefix('%dx.types.Handle '))
            from file_handlers.sdf.shader_graph_texture import GATHER_DIMENSIONS
            dimension = texture.dimension.replace('-', '') if texture else ''
            if dimension not in GATHER_DIMENSIONS:
                raise VanillaFunctionError('A gather requires its declared 2D or cube texture.')
            width = int(texture.coordinate_type[-1])
            if any(arg != 'float undef' for arg in arguments[3+width:7]) or any(not arg.startswith('float ') for arg in arguments[3:3+width]):
                raise VanillaFunctionError('Gather coordinates disagree with the texture dimension.')
            extras = {'comparison': value(arguments[10].removeprefix('float '))} if comparison else {}
            if 'cube' in dimension:
                if any(arg not in {'i32 undef','i32 0'} for arg in arguments[7:9]):
                    raise VanillaFunctionError('Cube gathers do not have texel offsets.')
            else:
                extras['texel_offset'] = builder.vector([value('0' if arg == 'i32 undef' else arg.removeprefix('i32 '),'int') for arg in arguments[7:9]])
            samples[record.result] = builder.texture_operation('sample_texture',texture,
                [value(arg[6:]) for arg in arguments[3:3+width]],sampler=sampler,
                mode='gather_compare' if comparison else 'gather',channel=integer(arguments[9]),**extras)
        elif operation in {'textureStore.f32', 'textureStore.i32'} and len(arguments) == 10 and integer(arguments[0]) == 67:
            texture = handles.get(arguments[1].removeprefix('%dx.types.Handle '))
            coordinates_type = TEXTURE_LOAD_COORDINATES.get(texture.dimension.replace('-', '')) if texture else None
            if coordinates_type is None or texture.input_type != 'rw-typed' or not texture.texture_value_type:
                raise VanillaFunctionError('A typed texture store needs an identified writable image.')
            width = int(coordinates_type[-1]) if coordinates_type[-1].isdigit() else 1
            if any(argument != 'i32 undef' for argument in arguments[2+width:5]):
                raise VanillaFunctionError('A typed texture store has excess coordinates.')
            element = texture.texture_value_type
            channels = int(element[-1]) if element[-1].isdigit() else 1
            mask = integer(arguments[9], 'i8 ')
            if not 1 <= mask <= 15 or mask & ((1 << channels)-1) != (1 << channels)-1:
                raise VanillaFunctionError('A partial-channel typed store needs an explicit image write mask.')
            domain, prefix = ('float','float ') if operation.endswith('f32') else ('uint','i32 ')
            if any(not argument.startswith(prefix) for argument in arguments[5:5+channels]):
                raise VanillaFunctionError('A typed store value has the wrong native domain.')
            data = builder.vector([value(argument[len(prefix):], domain) for argument in arguments[5:5+channels]])
            coordinates = builder.vector([value(argument.removeprefix('i32 '), 'uint') for argument in arguments[2:2+width]])
            builder.image('image_store', texture, coordinates, value=data)
        elif operation in {'textureLoad.f32', 'textureLoad.i32'} and len(arguments) == 9 and integer(arguments[0]) == 66:
            texture = handles.get(arguments[1].removeprefix('%dx.types.Handle '))
            dimension = texture.dimension.replace('-', '') if texture else ''
            coordinate_type = TEXTURE_LOAD_COORDINATES.get(dimension)
            if coordinate_type is None:
                raise VanillaFunctionError('This load needs a different typed texture dependency.')
            width = value_width(coordinate_type)
            spatial = TEXTURE_SPATIAL_WIDTHS[dimension]
            if any(arg != 'i32 undef' for arg in arguments[3+width:6]) or any(arg not in {'i32 0', 'i32 undef'} for arg in arguments[6+spatial:9]):
                raise VanillaFunctionError('Texture load coordinates exceed the declared image dimension.')
            offsets = tuple(0 if arg == 'i32 undef' else integer(arg) for arg in arguments[6:6+spatial])
            if texture.input_type == 'rw-typed':
                if arguments[2] not in {'i32 undef', 'i32 0'} or any(offsets):
                    raise VanillaFunctionError('A writable image read cannot select a mip or instruction offset.')
                coordinates = builder.vector([value(argument.removeprefix('i32 '), 'uint') for argument in arguments[3:3+width]])
                samples[record.result] = builder.image('image_load', texture, coordinates)['value']
                continue
            extras = {'sample_index' if 'ms' in dimension else 'mip_level': value(arguments[2].removeprefix('i32 '), 'int')}
            samples[record.result] = builder.texture_operation('texture_load', texture,
                [value(arg.removeprefix('i32 '), 'int') for arg in arguments[3:3+width]], offset=offsets if any(offsets) else (), **extras)
        elif operation == 'sampleIndex.i32' and len(arguments) == 1 and integer(arguments[0]) == 90:
            field = contract.input('SV_SampleIndex', 0)
            if contract.stage != 'pixel' or field is None or field.value_type != 'uint' or field.system_value != 'sample-index':
                raise VanillaFunctionError('Sample Index requires its exact declared pixel system input.')
            values[record.result] = builder.stage_input(field, 0)
        elif operation in {"loadInput.f32", 'loadInput.i32', 'loadOutputControlPoint.f32', 'loadOutputControlPoint.i32'} and len(arguments) == 5:
            output_point = operation.startswith('loadOutputControlPoint.')
            if integer(arguments[0]) != (103 if output_point else 4) or output_point and _hull_phase != 'patch':
                raise VanillaFunctionError('Invalid control-point input operation or invocation scope.')
            field = (output_rows if output_point else input_rows).get((integer(arguments[1]), integer(arguments[2])))
            vertex = None
            if metadata.stage in {'geometry','domain','hull'}:
                vertex = integer(arguments[4]) if re.fullmatch(r'i32 \d+',arguments[4]) else value(arguments[4].removeprefix('i32 '),'uint')
            if field is None or vertex is None and arguments[4] != "i32 undef":
                raise VanillaFunctionError("This input needs an indexed behavior dependency.")
            values[record.result] = builder.convert(builder.stage_input(field, integer(arguments[3], "i8 "),vertex=vertex,output_point=output_point),
                'float' if operation.endswith('f32') else 'uint', bits=True)
        elif operation in {"storeOutput.f32", 'storeOutput.i32', 'storePatchConstant.f32', 'storePatchConstant.i32'} and len(arguments) == 5:
            patch = operation.startswith('storePatchConstant.')
            if integer(arguments[0]) != (106 if patch else 5) or patch != (_hull_phase == 'patch'):
                raise VanillaFunctionError('An output store belongs to a different invocation scope.')
            field = (patch_rows if patch else output_rows).get((integer(arguments[1]), integer(arguments[2])))
            lane = integer(arguments[3], "i8 ")
            scalar_type = 'float' if operation.endswith('f32') else 'i32'
            if field is None or not 0 <= lane < field.mask.bit_count() or not arguments[4].startswith(scalar_type + ' '):
                raise VanillaFunctionError("This output has no declared scalar value slot.")
            key = field.stream_key, lane
            written_outputs.add(key)
            outputs[key] = value(arguments[4][len(scalar_type)+1:], field.value_type.rstrip('234'))
        elif operation in {'unary.f32', 'unary.f16'} and len(arguments) == 2 and arguments[1].startswith('half ' if operation.endswith('f16') else 'float '):
            kind = {6: "absolute", 7: "saturate", 12: "cosine", 13: "sine", 24: "square_root",
                    27: "floor", 28: "ceiling", 22: "fraction", 21: "exponent2", 23: "logarithm2",
                    25: "reciprocal_square_root", 26: "round_even", 29: "truncate", 83: "derivative_x", 84: "derivative_y",
                    85: "derivative_x_fine", 86: "derivative_y_fine",
                    14: "tangent", 15: "arc_cosine", 16: "arc_sine", 17: "arc_tangent",
                    18: "hyperbolic_cosine", 19: "hyperbolic_sine", 20: "hyperbolic_tangent"}.get(integer(arguments[0]))
            if kind is None:
                raise VanillaFunctionError(f"DXIL unary operation {arguments[0]} needs a behavior node.")
            prefix, domain = ('half ', half_type) if operation.endswith('f16') else ('float ', 'float')
            values[record.result] = builder.operation(kind, value=value(arguments[1][len(prefix):], domain))
        elif operation in {'legacyF16ToF32', 'legacyF32ToF16'}:
            opcode, prefix, value_type, kind = (131, 'i32 ', 'uint', 'unpack_half') if operation == 'legacyF16ToF32' else (130, 'float ', 'float', 'pack_half')
            if len(arguments) != 2 or integer(arguments[0]) != opcode or not arguments[1].startswith(prefix):
                raise VanillaFunctionError('The packed half-float conversion declaration is inconsistent.')
            values[record.result] = builder.operation(kind, value=value(arguments[1][len(prefix):], value_type))
        elif operation == 'isSpecialFloat.f32' and len(arguments) == 2 and arguments[1].startswith('float '):
            kind = {8: 'is_nan', 9: 'is_infinite', 10: 'is_finite'}.get(integer(arguments[0]))
            if kind is None:
                raise VanillaFunctionError('The floating-point predicate has an unsupported operation ID.')
            values[record.result] = builder.operation(kind, value=value(arguments[1][6:]))
        elif operation in {'binaryWithTwoOuts.i32','binaryWithCarryOrBorrow.i32'} and len(arguments)==3 and all(arg.startswith('i32 ') for arg in arguments):
            opcode = integer(arguments[0])
            if (operation=='binaryWithTwoOuts.i32' and opcode not in {41,42,43}
                    or operation=='binaryWithCarryOrBorrow.i32' and opcode not in {44,45}):
                raise VanillaFunctionError('A paired integer operation has an invalid operation ID.')
            domain = 'int' if opcode==41 else 'uint'
            a,b = (value(arg[4:],domain) for arg in arguments[1:])
            if opcode in {41,42}:
                pair = tuple(builder.operation(kind,a=a,b=b) for kind in ('multiply_high','multiply'))
            elif opcode==43:
                pair = builder.unsigned_division(a,b)
            else:
                pair = builder.word_carry(a,b,subtract=opcode==45)
            samples[record.result] = _LoadedWords(pair)
        elif operation in {'unary.i32', 'unaryBits.i32', 'unary.i64', 'unaryBits.i64'} and len(arguments) == 2 and arguments[1].startswith(operation.split('.')[1]+' '):
            opcode = integer(arguments[0])
            if (operation.split('.')[0], opcode) not in {('unary', 30), ('unaryBits', 31), ('unaryBits', 32),
                                                       ('unaryBits', 33), ('unaryBits', 34)}:
                raise VanillaFunctionError(f'DXIL integer unary operation {operation}/{opcode} needs a behavior node.')
            domain = ('int64_t' if opcode == 34 else 'uint64_t') if operation.endswith('i64') else 'int' if opcode == 34 else 'uint'
            operand = value(arguments[1][4:], domain)
            values[record.result] = (builder.leading_bit(operand) if opcode in {33, 34} else
                builder.operation({30: 'reverse_bits', 31: 'count_bits', 32: 'first_bit_low'}[opcode], value=operand))
        elif operation in {'binary.i32','binary.i64'} and len(arguments) == 3 and integer(arguments[0]) in {37, 38, 39, 40} and all(arg.startswith(operation[7:]+' ') for arg in arguments[1:]):
            opcode = integer(arguments[0])
            value_type = ('int64_t' if opcode in {37, 38} else 'uint64_t') if operation.endswith('i64') else 'int' if opcode in {37, 38} else 'uint'
            values[record.result] = builder.operation('maximum' if opcode in {37, 39} else 'minimum',
                a=value(arguments[1][4:], value_type), b=value(arguments[2][4:], value_type))
        elif operation in {'binary.f32', 'binary.f16'} and len(arguments) == 3 and integer(arguments[0]) in {35, 36} and all(arg.startswith('half ' if operation.endswith('f16') else 'float ') for arg in arguments[1:]):
            prefix, domain = ('half ', half_type) if operation.endswith('f16') else ('float ', 'float')
            values[record.result] = builder.operation('maximum' if integer(arguments[0]) == 35 else 'minimum',
                **{pin: value(argument[len(prefix):], domain) for pin, argument in zip(('a', 'b'), arguments[1:])})
        elif operation in {'tertiary.f32', 'tertiary.f16'} and len(arguments) == 4 and integer(arguments[0]) == 46 and all(arg.startswith('half ' if operation.endswith('f16') else 'float ') for arg in arguments[1:]):
            prefix, domain = ('half ', half_type) if operation.endswith('f16') else ('float ', 'float')
            values[record.result] = builder.operation('multiply_add',
                **{pin: value(argument[len(prefix):], domain) for pin, argument in zip(('a', 'b', 'c'), arguments[1:])})
        elif operation == 'tertiary.i32' and len(arguments) == 4 and integer(arguments[0]) in {48, 49} and all(arg.startswith('i32 ') for arg in arguments[1:]):
            domain = 'int' if integer(arguments[0]) == 48 else 'uint'
            values[record.result] = builder.operation('multiply_add',
                **{pin: value(argument[4:], domain) for pin, argument in zip(('a', 'b', 'c'), arguments[1:])})
        elif operation == "discard" and len(arguments) == 2 and integer(arguments[0]) == 82 and arguments[1].startswith("i1 "):
            if loops and builder.execution is None:
                raise VanillaFunctionError('Discard inside repeat needs ordered execution ownership.')
            builder.discard(value(arguments[1][3:], 'bool'))
        elif operation in {'dot2.f16','dot3.f16','dot4.f16','dot2.f32','dot3.f32','dot4.f32'}:
            width = int(operation[3])
            prefix, domain = ('half ',half_type) if operation.endswith('f16') else ('float ','float')
            if len(arguments) != width * 2 + 1 or integer(arguments[0]) != width + 52 or any(not arg.startswith(prefix) for arg in arguments[1:]):
                raise VanillaFunctionError("The dot-product declaration is inconsistent.")
            parts = [value(arg[len(prefix):],domain) for arg in arguments[1:]]
            values[record.result] = builder.dot(parts[:width], parts[width:])
        else:
            raise VanillaFunctionError(f"{operation} needs an additional editable behavior operation.")
    if not ended:
        raise VanillaFunctionError("The native entry has no terminating return.")
    if metadata.stage in {'geometry','mesh','amplification'}:
        return builder.finish(function_name, ())
    result = []
    for field in phase_outputs:
        parts = []
        for lane in range(field.mask.bit_count()):
            key = field.stream_key, lane
            part = outputs.get(key)
            if part is None:
                # DXIL signature usage can include channels with no storeOutput (e.g. MHR blend-depth MRTs).
                if key in written_outputs:
                    raise VanillaFunctionError("A native output component has no defined calculation.")
                part = builder.unassigned(field.value_type.rstrip('234'))
            parts.append(part)
        result.append((field, parts))
    return builder.finish(function_name, result)


def _recover_behavior(ir, contract, name):
    try:
        if isinstance(ir, DxbcSemanticIr):
            return recover_dxbc_function(ir, contract, name=name)
        if isinstance(ir, ReEngineDxilSemanticIr):
            return recover_dxil_function(ir, contract, name=name)
        from file_handlers.sdf.spirv_ir import SpirvSemanticIr
        from .spirv_behavior import recover_spirv_function
        if not isinstance(ir, SpirvSemanticIr):
            raise VanillaFunctionError("This native backend needs an editable behavior graph lifter.")
        return recover_spirv_function(ir, contract, name=name)
    except ShaderGraphError as exc:
        if isinstance(exc, VanillaFunctionError):
            raise
        raise VanillaFunctionError(str(exc)) from exc


def recover_vanilla_behavior(ir, contract, *, name="Vanilla Behavior"):
    from .material_region_read import region_read_session
    from file_handlers.sdf.graph_snapshot import thaw_graph
    with _recovery_allocation_batch(ir):
        with region_read_session(reuse_content=False):
            result = _recover_behavior(ir, contract, name)
        thaw_graph(result.function.graph)
        return result


@contextmanager
def _recover_vanilla_graph_snapshot(ir, contract, *, name="Vanilla Behavior"):
    from .material_region_read import region_read_session
    from file_handlers.sdf.graph_snapshot import thaw_graph
    with _recovery_allocation_batch(ir):
        graph = None
        try:
            with region_read_session(reuse_content=False):
                result = _recover_behavior(ir, contract, name)
                graph = result._bind(contract)
                del result
                yield graph
        finally:
            if graph is not None:
                thaw_graph(graph)


__all__ = ["VanillaFunctionError", "RecoveredBehavior", "recover_vanilla_behavior"]
