"""See docs/internals/effect-lifting.md."""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property, reduce
import hashlib
from itertools import islice
import struct
from types import SimpleNamespace
from uuid import uuid4

lazy import fastsurface

from file_handlers.native_math import f32
from file_handlers.sdf.shader_graph import (
    ShaderContract, ShaderGraph, ShaderGraphError, ShaderGraphLink, ShaderGraphNode, node_pins,
)
lazy from file_handlers.sdf.graph_snapshot import IMMUTABLE_JSON
lazy from file_handlers.sdf.shader_graph_layout import arrange_shader_graph
lazy from file_handlers.sdf.shader_graph_math import value_width
lazy from .engine_inputs import engine_input_catalog_for
lazy from .gbuffer_order import gbuffer_order_for
lazy from .material_branch import branch_arms
lazy from .material_function import MaterialFunctionAsset, MaterialFunctionPin, material_function_node_properties
lazy from .material_implementation import implementation_contract
lazy from .material_region_read import region_read_session
lazy from .material_project import (
    _TEXTURE_CONTRACT_DIMENSIONS, MaterialParameterDefinition, MaterialTextureDefinition,
    ensure_material_interface_nodes, material_program_contract,
)
lazy from .pass_purpose import DRAWING_FOR_KIND, PURPOSES, pass_purpose, purpose_title
lazy from .profiles.re_engine_dxil_recovery import recover_re_engine_dxil_ir
lazy from .profiles.re_engine_shader_graph import _reads_instance_copy
lazy from .profiles.renderer_inputs import (
    RENDERER_RESOURCE_KIND, RENDERER_VALUE_KIND, _renderer_pin, _supplies, add_engine_buffers,
    engine_resource_declaration, engine_resource_pin, engine_resource_value_type, implicit_sampler,
)
lazy from .surface_data import SURFACE_DATA_KIND, program_surface_data, program_vertex_inputs
lazy from .surface_flow import FlowValue
lazy from .surface_meaning import pixel_shader_digest, surface_output_catalog_for
lazy from .surface_outputs import (
    _catalog_target, _recover_ir, _shader_bytes, channel_label, channel_meaning, channel_pin_name,
    material_texture_names, named_texture_reads, native_program_surface, program_flow,
)
lazy from .surface_reach import parameter_sources
lazy from .vanilla_function import _recover_vanilla_graph_snapshot, _recovery_allocation_batch

_TEXTURE_READS = frozenset({"sample_texture", "texture_load"})
_COMMUTATIVE = frozenset({"add", "multiply", "minimum", "maximum", "dot"})
_GATE_VALUES = (0.0, 1.0)
_TRIALS = 256
_STATE_KINDS = frozenset({"function_input", "output", "discard_pixel", "execution_begin", "memory_load",
                          "memory_store", "memory_atomic", "memory_barrier", "image_load", "image_store",
                          "image_atomic"})
_LOADS = frozenset({"buffer_load", "texture_load", "sample_texture", "memory_load", "image_load"})
# Recoveries kept for passes sharing a shader: a lift meets one again up to ~16 passes later.
_RECOVERIES = 32


class _Unmapped(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Leaf:
    kind: str
    key: tuple
    value_type: str


@dataclass(slots=True)
class EffectLift:
    function: MaterialFunctionAsset | None
    leaves: dict
    outputs: dict
    parameters: dict
    textures: dict
    engine: dict
    labels: dict
    left_out: list = field(default_factory=list)


def _bits(value, source: str, target: str):
    codes = {"float": "f", "uint": "I", "int": "i"}
    return struct.unpack("<" + codes[target], struct.pack("<" + codes[source], value))[0]


def _round_trip(properties, inner) -> bool:
    return inner.kind == "reinterpret" and (
        inner.properties.get("source_type", "float") == properties.get("value_type", "float"))


def _same(left, right) -> bool:
    if isinstance(left, tuple) or isinstance(right, tuple):
        return isinstance(left, tuple) and isinstance(right, tuple) and len(left) == len(right) and all(
            _same(a, b) for a, b in zip(left, right))
    if isinstance(left, float) or isinstance(right, float):
        return struct.pack("<f", float(left)) == struct.pack("<f", float(right))
    return left == right


class _Flat:
    def __init__(self, graph):
        self.nodes: dict[str, ShaderGraphNode] = {}
        self.inputs: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
        self.users: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        self.where: dict[str, list[str]] = defaultdict(list)
        self.selects: dict[tuple[str, str], tuple[str, str]] = {}
        self.discards: list[tuple[str, tuple]] = []
        self._next = 0
        self._add(graph, "", {}, ())

    def _add(self, graph, prefix, captures, guards):
        by_id = {node.node_id: node for node in graph.nodes}
        links = {(link.target_node, link.target_pin): (link.source_node, link.source_pin) for link in graph.links}
        joined = {}

        def resolve(source, resolve):
            node = by_id[source[0]]
            if node.kind == "function_input":
                return captures[node.properties["pin_id"]]
            if node.kind == "branch":
                if node.node_id not in joined:
                    joined[node.node_id] = self._join(node, prefix, {
                        pin: resolve(value, resolve) for (target, pin), value in links.items()
                        if target == node.node_id}, guards)
                return joined[node.node_id][source[1]]
            return prefix + source[0], source[1]

        results = {}
        for node in graph.nodes:
            if node.kind == "function_output":
                if (node.node_id, "value") in links:
                    results[node.properties["pin_id"]] = resolve(links[node.node_id, "value"], resolve)
            elif node.kind not in {"function_input", "branch"}:
                self.nodes[prefix + node.node_id] = node
                self.where[node.node_id].append(prefix + node.node_id)
                if node.kind == "discard_pixel":
                    self.discards.append((prefix + node.node_id, guards))
        for (target, pin), source in links.items():
            if by_id[target].kind not in {"function_output", "branch"}:
                self.link(resolve(source, resolve), prefix + target, pin)
        return results

    def _join(self, node, prefix, sources, guards):
        flat = prefix + node.node_id
        condition = sources.pop("condition")
        arms = branch_arms(node)
        results = [self._add(arm.graph, f"{flat}/{label}/", sources, (*guards, (condition, label == "T")))
                   for label, arm in zip("TF", arms)]
        self.where[node.node_id].append(flat)
        for pin in arms[0].outputs:
            self.selects[flat, pin.pin_id] = self.add("select", {"value_type": pin.value_type}, condition=condition,
                                                      true=results[0][pin.pin_id], false=results[1][pin.pin_id])
        return {pin.pin_id: self.selects[flat, pin.pin_id] for pin in arms[0].outputs}

    def link(self, source, target, pin):
        self.inputs[target][pin] = source
        self.users[source[0]].append((target, pin, source[1]))

    def add(self, kind, properties, **inputs):
        identity = f"#{self._next}"
        self._next += 1
        self.nodes[identity] = ShaderGraphNode(identity, kind, 0.0, 0.0, properties)
        for pin, source in inputs.items():
            self.link(source, identity, pin)
        return identity, "value" if kind in {"constant", "compose"} else "result"

    def refs(self, origins, origin, ir_kind):
        if ir_kind == "dxbc-sm5" and isinstance(origin, tuple) and len(origin) == 3:
            if origin[0] == "join":
                # A value joined from branches lives in its register from the join on.
                origin = (origin[1], f"r{origin[2][1]}" if origin[2][0] == "r" else origin[2][1], origin[2][2])
            if isinstance(origin[0], int):
                origin = (origin[0], str(origin[1]).casefold(), origin[2])
        value, lane = origins.get(origin), None
        if value is None and ir_kind == "spirv" and isinstance(origin, tuple) and len(origin) == 2:
            value, lane = origins.get(origin[0]), origin[1]
        if value is None or not hasattr(value, "node"):
            return []
        refs = [self.selects.get((node, value.pin), (node, value.pin)) for node in self.where.get(value.node, ())]
        if lane is not None and value_width(value.value_type) > 1:
            refs = [self.add("swizzle", {"source_type": value.value_type, "mask": "xyzw"[lane]}, value=ref)
                    for ref in refs]
        return refs

    @cached_property
    def decision(self):
        parts = []
        for site, guards in self.discards:
            value = self.inputs[site]["condition"]
            for condition, taken in guards:
                false = self.add("constant", {"value_type": "bool", "values": [False]})
                value = self.add("select", {"value_type": "bool"}, condition=condition,
                                 true=value if taken else false, false=false if taken else value)
            parts.append(value)
        if "clip" in self.inputs["output"]:
            zero = self.add("constant", {"value_type": "float", "values": [0.0]})
            parts.append(self.add("compare", {"relation": "less", "nan_result": False, "value_type": "float"},
                                  a=self.inputs["output"]["clip"], b=zero))
        decision = parts[0] if parts else None
        for part in parts[1:]:
            true = self.add("constant", {"value_type": "bool", "values": [True]})
            decision = self.add("select", {"value_type": "bool"}, condition=part, true=true, false=decision)
        return decision

    def forward(self, roots, stop=frozenset()) -> set[str]:
        found, pending = set(), [node for node, _pin in roots]
        while pending:
            node = pending.pop()
            if node not in found:
                found.add(node)
                pending.extend(user for user, pin, source_pin in self.users[node] if node not in stop
                               and pin != "exec" and source_pin != "next" and user != "output")
        return found


def _evaluate(kind, properties, values):
    value_type = str(properties.get("value_type", "float"))
    value = values.get("value")
    if kind == "reinterpret":
        return None if isinstance(value, tuple) else _bits(value, str(properties.get("source_type", "uint")).rstrip(
            "234"), value_type.rstrip("234"))
    if kind == "convert":
        return None if isinstance(value, tuple) else f32(value) if value_type == "float" else int(value)
    if kind == "swizzle":
        parts = value if isinstance(value, tuple) else (value,)
        picked = tuple(parts["xyzw".index(item)] for item in str(properties.get("mask", "")))
        return picked[0] if len(picked) == 1 else picked
    if kind == "compose":
        return tuple(values[name] for name in "xyzw"[:value_width(value_type)])
    if kind == "select":
        return values["true"] if values["condition"] else values["false"]
    if any(isinstance(item, tuple) for item in values.values()):
        return None
    a, b, c = values.get("a"), values.get("b"), values.get("c")
    operation = {
        "add": lambda: a + b, "subtract": lambda: a - b, "multiply": lambda: a * b,
        "divide": lambda: a / b if b else None, "multiply_add": lambda: a * b + c,
        "minimum": lambda: min(a, b), "maximum": lambda: max(a, b),
        "saturate": lambda: min(max(value, 0.0), 1.0), "absolute": lambda: abs(value), "negate": lambda: -value,
        "compare": lambda: {"equal": a == b, "not_equal": a != b, "less": a < b, "less_equal": a <= b,
                            "greater": a > b, "greater_equal": a >= b}[str(properties.get("relation"))],
    }.get(kind)
    result = operation() if operation is not None else None
    return f32(result) if value_type == "float" and isinstance(result, float) else result


class _Fold:
    def __init__(self, source, forward, gate, assigned=None):
        self.source, self.forward, self.gate, self.assigned = source, forward, gate, assigned or {}
        self.memo = {}

    def __call__(self, ref):
        if ref not in self.memo:
            self.memo[ref] = None
            self.memo[ref] = self._fold(ref, self.source.flat.nodes[ref[0]])
        return self.memo[ref]

    def _fold(self, ref, node):
        if ref in self.assigned:
            return ("const", self.assigned[ref])
        if node.kind == "constant":
            values = node.properties.get("values", [0.0])
            return ("const", values[0] if len(values) == 1 else tuple(values))
        read = self.source.reads.get(ref)
        if read is not None and read[0] in self.source.effect:
            if read[0] not in self.gate:
                return None
            value_type = _value_type(self.source, ref)
            value = self.gate[read[0]]
            value = value if value_type.startswith("float") else _bits(value, "float", value_type.rstrip("234"))
            return ("const", value if value_width(value_type) == 1 else (value,) * value_width(value_type))
        if ref[0] not in self.forward:
            return ("leaf", ref)
        pins = {pin: self(source) for pin, source in self.source.flat.inputs[ref[0]].items() if pin != "exec"}
        if node.kind == "capture_value":
            return pins.get("value")
        if node.kind == "reinterpret" and pins["value"] is not None and pins["value"][0] == "leaf" and _round_trip(
                node.properties, self.source.flat.nodes[pins["value"][1][0]]):
            return "leaf", self.source.flat.inputs[pins["value"][1][0]]["value"]
        constants = {pin: value[1] for pin, value in pins.items() if value is not None and value[0] == "const"}
        if pins and len(constants) == len(pins):
            value = _evaluate(node.kind, node.properties, constants)
            return None if value is None else ("const", value)
        zero = {pin for pin, value in constants.items() if not isinstance(value, tuple) and value == 0}
        one = {pin for pin, value in constants.items() if not isinstance(value, tuple) and value == 1}
        if node.kind == "multiply":
            return ("const", 0.0) if zero else pins["b"] if "a" in one else pins["a"] if "b" in one else None
        if node.kind == "add":
            return pins["b"] if "a" in zero else pins["a"] if "b" in zero else None
        if node.kind == "subtract":
            return pins["a"] if "b" in zero else None
        if node.kind == "multiply_add" and zero & {"a", "b"}:
            return pins["c"]
        if node.kind == "divide" and "a" in zero:
            return ("const", 0.0)
        if node.kind == "select":
            if "condition" in constants:
                return pins["true" if constants["condition"] else "false"]
            return pins["true"] if pins["true"] is not None and pins["true"] == pins["false"] else None
        return None


def _value_type(source, ref) -> str:
    node = source.flat.nodes[ref[0]]
    if node.kind == "constant":
        return str(node.properties.get("value_type", "float"))
    return next(pin.value_type for pin in node_pins(node, source.contract)
                if pin.direction == "output" and pin.name == ref[1])


@dataclass(slots=True)
class _Position:
    split: object
    form: str
    refs: tuple


def _lane(channel, index, meaning) -> tuple[str, str, str]:
    return (f"{channel.stage}:{channel.scope}:{channel.semantic}{channel.semantic_index}",
            "*" if channel.packed else "xyzw"[channel.lanes[index]], meaning)


class _Pass:
    def __init__(self, document, route, shader, kind, contract, effect, own):
        profile = document.profile.shader_graph_profile
        target, index, role = route
        program = document.sdf.targets[target].programs[index]
        self.document, self.route, self.program, self.role, self.effect = document, route, program.name, role, effect
        self.purpose = pass_purpose(document, profile, target, index)
        pixel = program.shader("pixel")
        self.digest = pixel_shader_digest(pixel) if pixel is not None and pixel.present else ""
        self.contract, self.kind = contract, kind
        self.flat, origins, ir, (flow, channels) = shader
        interface = document.material_interface
        if kind == "dxil":
            channels = named_texture_reads(channels, material_texture_names(
                interface.descriptor_layouts, interface.shader_lod, profile.material_record_prefix_size))
        self.flow, self.channels = flow, channels

        def refs(value):
            return self.flat.refs(origins, flow.values[value].origin, kind) if value is not None else []

        self.reads = {}
        for value, offset in parameter_sources(kind, ir, contract, flow, profile.user_buffer_name,
                                               profile.material_record_prefix_size,
                                               profile.instanced_material_buffer).items():
            found = next((item for item in interface.parameters
                          if item.byte_offset <= offset < item.byte_offset + item.byte_size), None)
            for ref in refs(value) if found is not None else ():
                self.reads[ref] = (found.name, (offset - found.byte_offset) // 4)
        buffers = {name.casefold() for name in document.profile.user_material_buffer_names}
        names = {item.name.casefold(): item.name for item in interface.parameters}
        for node_id, node in self.flat.nodes.items():
            if (node.kind == "parameter" and str(node.properties.get("buffer", "")).casefold() in buffers
                    and str(node.properties.get("variable", "")).casefold() in names):
                self.reads[node_id, "value"] = (names[str(node.properties["variable"]).casefold()], -1)
        self.textures = {}
        texture_names = {item.name for item in profile.editable_textures(document)}
        for split in channels:
            if split.channel.scope == "read" and split.channel.semantic in texture_names:
                for ref in (ref for store in split.stores for ref in refs(store.value)):
                    sample = ref[0]
                    while self.flat.nodes[sample].kind not in _TEXTURE_READS and "value" in self.flat.inputs[sample]:
                        sample = self.flat.inputs[sample]["value"][0]
                    if self.flat.nodes[sample].kind in _TEXTURE_READS:
                        self.textures[self.flat.inputs[sample]["texture"][0]] = split.channel.semantic
        self.textures.update((node_id, node.properties["resource"]) for node_id, node in self.flat.nodes.items()
                             if node.kind == "shader_resource" and node.properties.get("resource") in texture_names)
        self.positions, self.outputs = [], []
        for split in channels:
            channel = split.channel
            if channel.scope in {"read", "discard"} or role != "pixel" and not (
                    channel.scope == "position" or channel.semantic.startswith("sv_")):
                continue
            self.outputs.append(channel)
            frontier = [refs(item)[:1] for item in split.frontier]
            if channel.packed and all(frontier):
                self.positions.append(_Position(split, "value", tuple(item[0] for item in frontier)))
            sites = defaultdict(list)
            for store in split.stores:
                sites[store.lane].extend(refs(store.value))
            if all(len(sites[lane]) == 1 for lane in channel.lanes):
                self.positions.append(_Position(split, "stored" if channel.packed else "value",
                                                tuple(sites[lane][0] for lane in channel.lanes)))
        self.effect_free = {}
        for position in self.positions:
            if position.form == "value":
                pin = self.pin(position.split, position.form)
                for lane, ref in enumerate(position.refs):
                    self.effect_free.setdefault(ref, (pin, lane))
        self.texels = {}
        for split in channels:
            if split.channel.scope == "read":
                for store in split.stores:
                    self.texels.update((ref, (self.pin(split, "value"), split.channel.lanes.index(store.lane)))
                                       for ref in refs(store.value))
        self.decision = self.flat.decision if role == "pixel" else None
        cut_out = next((split.channel for split in channels if split.channel.scope == "discard"), None)
        self.cut_out = None if cut_out is None else _lane(cut_out, 0, self.meaning(cut_out))
        self.surface = defaultdict(list)
        for datum in sorted((program_surface_data if role == "pixel" else program_vertex_inputs)(
                document, profile, target, index), key=lambda item: item.key == "coordinates"):
            for position, (semantic, semantic_index, lane) in enumerate(datum.lanes):
                self.surface[semantic.casefold(), int(semantic_index), lane].append((datum, position))
        self.own, self.own_contract, self.own_tables = own

    def meaning(self, channel) -> str:
        catalog = surface_output_catalog_for(self.document.profile)
        return str(channel_meaning(channel, self.purpose, _catalog_target(catalog, self.purpose, channel, self.digest)))

    def pin(self, split, form: str) -> str:
        return channel_pin_name(split.channel, form, self.meaning(split.channel))

    def fed(self, nodes) -> set:
        found = {self.cut_out} if self.decision is not None and self.decision[0] in nodes else set()
        for item in self.positions:
            meaning = self.meaning(item.split.channel)
            found.update(_lane(item.split.channel, index, meaning) for index, ref in enumerate(item.refs)
                         if ref[0] in nodes)
        return found

    def tail(self, split):
        graph = fastsurface.Graph(self.flow.values, FlowValue)
        return graph.tail_shape(split.frontier, tuple(store.value for store in split.stores))

    def sampled(self, ref) -> dict:
        found, pending, seen = {}, [ref], set()
        while pending:
            item = pending.pop()
            if item not in seen:
                seen.add(item)
                if item in self.texels:
                    key = self.texels[item]
                    found[key] = item if found.get(key, item) == item else None
                pending.extend(source for pin, source in self.flat.inputs[item[0]].items() if pin != "exec")
        return {key: item for key, item in found.items() if item is not None}


def _target_program(document, target: int, name: str) -> int | None:
    programs = document.sdf.targets[target].programs if target < len(document.sdf.targets) else ()
    return next((index for index, program in enumerate(programs) if program.name == name), None)


def _layout(buffer):
    return buffer and (buffer.byte_size, buffer.variables)


def _own_pass(document, target: int, name: str, role: str) -> tuple[tuple[dict, dict], ShaderContract | None,
                                                                    set | None]:
    index = _target_program(document, target, name)
    if index is None:
        return ({}, {}), None, None
    profile = document.profile.shader_graph_profile
    handed = {datum.key: datum for datum in (program_surface_data if role == "pixel" else program_vertex_inputs)(
        document, profile, target, index)}
    carried = {datum.key: datum for datum in program_vertex_inputs(document, profile, target, index)
               if role == "pixel" and datum.key not in handed}
    program = document.sdf.targets[target].programs[index]
    contract = profile.shader_contract(document, program, role)
    # Below Shader Model 6.6 a bindless pass samples a material texture only through a table of its kind.
    tables = None if (_shader_bytes(document, profile, program, role)[0] != "dxil" or (
        contract.model_major, contract.model_minor) >= (6, 6) or _reads_instance_copy(profile, contract, "dxil")) \
        else {item.dimension for item in contract.resources
              if item.register_class == "t" and item.register_space and item.bind_count != 1}
    return (handed, carried), contract, tables


@contextmanager
def _recovered(document, route, effect=frozenset(), own=None, cache=None):
    profile = document.profile.shader_graph_profile
    target, index, role = route
    program = document.sdf.targets[target].programs[index]
    kind, data, _contract, digest = _shader_bytes(document, profile, program, role)
    contract = implementation_contract(material_program_contract(
        document, target, index, role, allow_unreflected_material_fields=True), data)
    key = (id(document), digest, contract.fingerprint)
    if cache is not None and key in cache:
        cache.move_to_end(key)
        shader = cache[key]
    else:
        ir = recover_re_engine_dxil_ir(data) if kind == "dxil" else _recover_ir(kind, data)
        origins = {}
        with _recover_vanilla_graph_snapshot(ir, contract, name=program.name, origins=origins, thaw=False) as graph:
            shader = _Flat(graph), origins, ir, program_flow(kind, ir, contract)
        if cache is not None:
            cache[key] = shader
            while len(cache) > _RECOVERIES:
                cache.popitem(last=False)
    with region_read_session(reuse_content=False):
        yield _Pass(document, route, shader, kind, contract, effect, own or _own_pass(document, target, program.name,
                                                                                        role))


def _hash(*parts) -> str:
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()[:20]


class _Fragment:
    def __init__(self, lift, source, forward, substitutions, fold=None):
        self.lift, self.source, self.forward, self.substitutions, self.fold = lift, source, forward, substitutions, fold
        self.memo = {}

    def value(self, ref):
        if ref not in self.memo:
            self.memo[ref] = self._value(ref)
        return self.memo[ref]

    def _loaded(self, ref) -> bool:
        pending, seen = [ref[0]], set()
        while pending:
            node = pending.pop()
            if node not in seen:
                seen.add(node)
                if self.source.flat.nodes[node].kind in _LOADS:
                    return True
                pending.extend(item[0] for pin, item in self.source.flat.inputs[node].items() if pin != "exec")
        return False

    def _value(self, ref):
        lift, source = self.lift, self.source
        if self.fold is not None:
            folded = self.fold(ref)
            if folded is not None and folded[0] == "const":
                return lift.constant(folded[1], _value_type(source, ref))
            if folded is not None and folded[1] != ref:
                return self.value(folded[1])
        if ref in self.substitutions:
            kind, *item = self.substitutions[ref]
            return (lift.channel(*item) if kind == "channel" else item[0] if kind == "lifted"
                    else lift.constant(item[0], _value_type(source, ref)))
        node, inputs = source.flat.nodes[ref[0]], source.flat.inputs[ref[0]]
        if node.kind == "capture_value":
            return self.value(inputs["value"])
        if ref in source.reads:
            value = lift.parameter(source, *source.reads[ref])
            value_type = _value_type(source, ref)
            return value if value_type.startswith("float") else lift.node(
                "reinterpret", {"source_type": "float", "value_type": value_type}, value=value)
        if node.kind == "reinterpret" and inputs.get("value") in source.reads and str(
                node.properties.get("value_type")) == "float":
            return lift.parameter(source, *source.reads[inputs["value"]])
        if ref[0] in source.textures:
            return lift.texture(source, source.textures[ref[0]], _value_type(source, ref))
        if ref[0] not in self.forward and ref in source.effect_free and source.effect_free[ref][0] in lift.target_pins:
            return lift.channel(*source.effect_free[ref])
        if node.kind == "constant":
            return lift.node("constant", dict(node.properties))
        if node.kind == "swizzle" and source.flat.nodes[inputs["value"][0]].kind == "input":
            return lift.stage_input(source, source.flat.nodes[inputs["value"][0]], str(node.properties["mask"]))
        if node.kind == "input":
            return lift.stage_input(source, node)
        if node.kind == "parameter":
            return lift.engine_value(source, node)
        if node.kind == "shader_resource":
            return lift.resource(source, node)
        if node.kind in _STATE_KINDS or node.kind == "repeat" and ref[0] in self.forward:
            raise _Unmapped(node.kind.replace("_", " "))
        if node.kind in {"descriptor_heap", "resource_index"} and self._loaded(inputs["index"]):
            raise _Unmapped("a resource it picks from the descriptor heap by data it reads while drawing")
        values = {pin: self.value(item) for pin, item in inputs.items() if pin != "exec"}
        return lift.node(node.kind, dict(node.properties), ref[1], **values)


class _Lift:
    def __init__(self, target_project, target_document, source_document, source_material, effect):
        self.target_project, self.target_document = target_project, target_document
        self.source_document, self.source_material, self.effect = source_document, source_material, effect
        self.master = next(node for node in target_project.graph.nodes
                           if node.kind == "re_engine_recovered_master_material")
        self.target_pins = {str(record["pin"]): record for record in self.master.properties.get("output_pins", ())}
        self.lanes = {lane: (pin, position) for pin, record in self.target_pins.items()
                      if record["form"] == "value" and not record.get("packed")
                      for position, lane in enumerate(self.lanes_of(pin))}
        self.nodes: dict[str, ShaderGraphNode] = {}
        self.links: dict[tuple[str, str], tuple[str, str]] = {}
        self.leaves: dict[str, Leaf] = {}
        self.engine, self.labels, self.parameters, self.textures = {}, {}, {}, {}
        self.left_out: list[tuple[str, str, str]] = []
        self._renderer_pins = {}
        self.recoveries = OrderedDict()
        self.identities = {}

    def node(self, kind, properties, pin=None, **inputs):
        properties = {key: value for key, value in properties.items() if key != "display_name"}
        if kind == "reinterpret" and inputs["value"][0] in self.nodes and _round_trip(properties,
                                                                                        self.nodes[inputs["value"][0]]):
            return self.links[inputs["value"][0], "value"]
        shape = sorted(inputs.values()) if kind in _COMMUTATIVE else sorted(inputs.items())
        # Frozen values are keyed by identity; the entry keeps them alive.
        signature = kind, tuple(shape), tuple((key, id(value) if isinstance(value, IMMUTABLE_JSON) else repr(value))
                                              for key, value in sorted(properties.items()))
        if signature not in self.identities:
            self.identities[signature] = "n_" + _hash(kind, sorted((key, repr(value)) for key, value in
                                                                   properties.items()), shape), properties
        identity = self.identities[signature][0]
        if identity not in self.nodes:
            self.nodes[identity] = ShaderGraphNode(identity, kind, 0.0, 0.0, properties)
            self.links.update(((identity, name), source) for name, source in inputs.items())
        if pin is None:
            pin = next(item.name for item in node_pins(self.nodes[identity], None) if item.direction == "output")
        return identity, pin

    def constant(self, value, value_type):
        values = [bool(item) if value_type.startswith("bool") else item
                  for item in (value if isinstance(value, tuple) else (value,))]
        return self.node("constant", {"value_type": value_type, "values": values})

    def leaf(self, leaf: Leaf):
        identity = "in_" + _hash(leaf.kind, leaf.key, leaf.value_type)
        if identity not in self.leaves:
            self.leaves[identity] = leaf
            label = (str(self.target_pins[leaf.key[0]]["label"]) if leaf.kind == "channel"
                     else self.labels.get(leaf.key[0], leaf.key[0]) if leaf.kind == "surface"
                     else ".".join(str(item) for item in leaf.key if item))
            self.nodes[identity] = ShaderGraphNode(identity, "function_input", 0.0, 0.0, {
                "pin_id": identity, "display_name": label[:128], "value_type": leaf.value_type})
        return identity, "value"

    def lane(self, value, value_type, lane):
        return value if lane < 0 or value_width(value_type) == 1 else self.node(
            "swizzle", {"source_type": value_type, "mask": "xyzw"[lane]}, value=value)

    def parameter(self, source, name, lane):
        parameter = next(item for item in source.document.material_interface.parameters if item.name == name)
        width = parameter.byte_size // 4
        value_type = "float" if width == 1 else f"float{width}"
        own = next((item for item in self.target_project.parameters if item.name == name), None)
        if own is not None and own.value_type != value_type:
            raise _Unmapped(f"{name}, which this material has as a {own.value_type}, not a {value_type}")
        if own is None:
            self.parameters[name] = (value_type, next((tuple(item.parameter[:width])
                                                       for item in self.source_material.parameters
                                                       if item.name == name), (0.0,) * width))
        return self.lane(self.leaf(Leaf("parameter", (name,), value_type)), value_type, lane)

    def texture(self, source, name, value_type):
        own = next((item for item in self.target_project.textures if item.name == name), None)
        if own is not None and own.dimension != value_type:
            raise _Unmapped(f"{name}, which this material has as a {own.dimension}, not a {value_type}")
        if source.own_tables is not None and _TEXTURE_CONTRACT_DIMENSIONS[value_type] not in source.own_tables:
            raise _Unmapped(f"{name}, a {value_type}, which this material's pass has no descriptor table to sample")
        if own is None:
            self.textures[name] = (value_type, next((item.tex_path for item in self.source_material.textures
                                                     if item.tex_type == name), ""))
        return self.leaf(Leaf("texture", (name,), value_type))

    def channel(self, pin, lane):
        value_type = str(self.target_pins[pin]["value_type"])
        return self.lane(self.leaf(Leaf("channel", (pin,), value_type)), value_type, lane)

    def stage_input(self, source, node, mask=None):
        semantic, index = str(node.properties["semantic"]), int(node.properties.get("semantic_index", 0))
        declared = source.contract.input(semantic, index)
        lanes = [lane for lane in range(4) if declared.mask & (1 << lane)]
        found = []
        for component in ("xyzw".index(item) for item in mask or "xyzw"[:len(lanes)]):
            own = next(((data[datum.key], position) for data in source.own for datum, position in source.surface[
                semantic.casefold(), index, lanes[component]] if datum.key in data
                and position < value_width(data[datum.key].value_type)), None)
            if own is None:
                raise _Unmapped(f"{semantic.upper()}{index}.{'xyzw'[lanes[component]]}, which this material's pass "
                                "does not have")
            self.labels[own[0].key] = own[0].label
            found.append(own)
        own = found[0][0]
        if found == [(own, position) for position in range(value_width(own.value_type))]:
            return self.leaf(Leaf("surface", (own.key,), own.value_type))
        parts = [self.lane(self.leaf(Leaf("surface", (datum.key,), datum.value_type)), datum.value_type, position)
                 for datum, position in found]
        scalar = declared.value_type.rstrip("234")
        return parts[0] if len(parts) == 1 else self.node("compose", {"value_type": f"{scalar}{len(parts)}"},
                                                          **dict(zip("xyzw", parts)))

    def engine_value(self, source, node):
        buffer_name, name = str(node.properties["buffer"]), str(node.properties["variable"])
        buffer = source.contract.buffer(buffer_name)
        variable = next((item for item in buffer.variables if item.name == name), None) if buffer else None
        if variable is None:
            raise _Unmapped(f"{buffer_name}.{name}")
        _key, pin = _renderer_pin(buffer, variable, self._renderer_pins)
        path = str(node.properties.get("field_path") or "")
        value_type = next((str(item["value_type"]) for item in pin.get("value_layout", ()) if item["path"] == path),
                          str(pin["value_type"]))
        self.engine[buffer_name, name, path] = pin
        return self.leaf(Leaf("engine", (buffer_name, name, path), value_type))

    def resource(self, source, node):
        name = str(node.properties["resource"])
        declared, buffer, own = source.contract.resource(name), source.contract.buffer(name), source.own_contract
        catalog = engine_input_catalog_for(self.target_document.profile)
        declaration = engine_resource_declaration(catalog, name) if catalog is not None else None
        if own is not None and buffer is not None and own.buffer(name) is None:
            own = add_engine_buffers(self.target_document, own, SimpleNamespace(
                target_index=source.route[0], shader_role=source.role,
                program_index=_target_program(self.target_document, source.route[0], source.program)), (name,))
        if declaration is None and (own is not None and own.resource(name) is not None and _supplies(
                own.resource(name), declared) and _layout(own.buffer(name)) == _layout(buffer)
                or declared == implicit_sampler(self.target_document, source.role, declared)):
            declaration = declared
        if declaration is None or buffer is None and engine_resource_value_type(declaration) is None:
            raise _Unmapped(f"{name}, which neither the game's engine inputs nor this material's pass declare")
        pin = engine_resource_pin(declaration, buffer)
        self.engine[(name,)] = pin
        return self.leaf(Leaf("resource", (name,), str(pin["value_type"])))

    def label(self, pin) -> str:
        return str(self.target_pins[pin]["label"]) if pin in self.target_pins else pin

    def lanes_of(self, pin) -> list:
        record = self.target_pins[pin]
        base, _dot, lanes = str(record["channel"]).rpartition(".")
        return [(base, "*" if record.get("packed") else lane, str(record.get("meaning", ""))) for lane in lanes]

    def places(self, pass_, item) -> dict:
        channel = item.split.channel
        if channel.packed:
            pin = pass_.pin(item.split, item.form)
            return {pin: dict(enumerate(item.refs))} if pin in self.target_pins else {}
        found = defaultdict(dict)
        for index, ref in enumerate(item.refs):
            place = self.lanes.get(_lane(channel, index, pass_.meaning(channel)))
            if place is None:
                return {}
            found[place[0]][place[1]] = ref
        return found

    def every(self, conditions):
        false = self.constant(False, "bool")
        return reduce(lambda a, b: self.node("select", {"value_type": "bool"}, condition=a, true=b, false=false),
                      conditions)

    def added_pin(self, split, meaning) -> str | None:
        prefix = f"pixel:added:{split.channel.semantic}{split.channel.semantic_index}."
        return next((pin for pin, record in self.target_pins.items()
                     if str(record["channel"]).startswith(prefix) and record.get("meaning") == meaning), None)


def _inverses(node, known, pins, target) -> list[list[tuple[str, object]]]:
    kind, open_ = node.kind, [pin for pin in pins if pin not in known]
    if kind == "capture_value":
        return [[("value", target)]]
    if kind == "compose":
        return [list(zip("xyzw", target))] if isinstance(target, tuple) else []
    if kind == "select":
        if "condition" in known:
            return [[("true" if known["condition"] else "false", target)]]
        return [[("condition", True), ("true", target)], [("condition", False), ("false", target)],
                [("true", target), ("false", target)]]
    if isinstance(target, tuple) or any(isinstance(value, tuple) for value in known.values()):
        return []
    if kind in {"minimum", "maximum"}:
        bound = all(value >= target if kind == "minimum" else value <= target for value in known.values())
        return [[(pin, target) for pin in open_]] if bound else []
    if kind == "saturate":
        return [[("value", target)]] if 0.0 <= target <= 1.0 else []
    if kind in {"multiply", "multiply_add"} and target == 0 and known.get("c", 0) == 0:
        return [[(pin, 0.0)] for pin in open_ if pin != "c"]
    if kind == "divide" and target == 0 and "a" in open_:
        return [[("a", 0.0)]]
    if len(open_) != 1:
        return []
    pin, = open_
    if kind == "compare":
        inclusive = node.properties.get("relation") in {"equal", "less_equal", "greater_equal"}
        return [[(pin, known["b" if pin == "a" else "a"])]] if inclusive == bool(target) else []
    if kind == "reinterpret":
        return [[(pin, _bits(target, str(node.properties.get("value_type", "float")).rstrip("234"),
                             str(node.properties.get("source_type", "uint")).rstrip("234")))]]
    if kind == "convert":
        return [[(pin, float(target) if isinstance(target, int) else int(target))]]
    if kind in {"negate", "absolute"}:
        return [[(pin, -target if kind == "negate" else target)]] if kind == "negate" or target >= 0 else []
    if kind == "add":
        return [[(pin, f32(target - known["b" if pin == "a" else "a"]))]]
    if kind == "subtract":
        return [[(pin, f32(target + known["b"]) if pin == "a" else f32(known["a"] - target))]]
    if kind == "multiply":
        other = known["b" if pin == "a" else "a"]
        return [[(pin, f32(target / other))]] if other else []
    if kind == "divide":
        return ([[("a", f32(target * known["b"]))]] if pin == "a"
                else [[("b", f32(known["a"] / target))]] if target else [])
    if kind == "multiply_add" and pin == "c":
        return [[("c", f32(target - f32(known["a"] * known["b"])))]]
    return []


def _solutions(source, fold, goals, unknowns, tainted):
    stack = [(tuple(goals), {})]
    while stack:
        goals, assigned = stack.pop()
        if not goals:
            yield assigned
            continue
        (ref, target), rest = goals[0], goals[1:]
        folded = fold(ref)
        if folded is not None and folded[0] == "const":
            options = [()] if _same(folded[1], target) else []
        elif folded is not None and folded[1] != ref:
            options = [((folded[1], target),)]
        elif ref in unknowns:
            if ref not in assigned or _same(assigned[ref], target):
                stack.append((rest, {**assigned, ref: target}))
            continue
        elif ref[0] in tainted:
            pins = {pin: item for pin, item in source.flat.inputs[ref[0]].items() if pin != "exec"}
            known = {pin: value[1] for pin, item in pins.items() if (value := fold(item)) and value[0] == "const"}
            options = [tuple((pins[pin], value) for pin, value in option)
                       for option in _inverses(source.flat.nodes[ref[0]], known, pins, target)]
        else:
            options = []
        stack.extend((option + rest, assigned) for option in reversed(options))


def _added_target(source, forward, fold, lanes, clear):
    derived = {}

    def material(ref, material):
        if ref not in derived:
            derived[ref] = False
            derived[ref] = ref in source.reads or ref[0] in source.textures or any(
                material(item, material) for pin, item in source.flat.inputs[ref[0]].items() if pin != "exec")
        return derived[ref]

    unknowns, pending, seen = set(), list(lanes.values()), set()
    while pending:
        ref = pending.pop()
        folded = fold(ref)
        if ref in seen or folded is not None and folded[0] == "const":
            continue
        seen.add(ref)
        if folded is not None and folded[1] != ref:
            pending.append(folded[1])
        elif ref[0] not in forward:
            unknowns.update((ref,) if material(ref, material) else ())
        else:
            pending.extend(item for pin, item in source.flat.inputs[ref[0]].items() if pin != "exec")
    goals = [(ref, clear[lane]) for lane, ref in lanes.items()]
    for assigned in islice(_solutions(source, fold, goals, unknowns, source.flat.forward(unknowns)), _TRIALS):
        check = _Fold(source, forward, fold.gate, assigned)
        if all((value := check(ref)) is not None and value[0] == "const" and _same(value[1], target)
               for ref, target in goals):
            return {ref: ("constant", value) for ref, value in assigned.items()}
    raise _Unmapped("with the effect off, no value of the material's own inputs makes its code write what the game "
                    "clears the target to")


def _cuts(source, forward, fold, roots) -> tuple[dict, set, set]:
    cuts, through, live = {}, set(), set()
    for root in roots:
        found, pending, seen, reached = {}, [(root, False)], set(), False
        while pending:
            ref, settled = pending.pop()
            if (ref, settled) in seen or ref[0] not in forward:
                continue
            seen.add((ref, settled))
            folded = fold(ref)
            if folded is not None and folded[0] == "leaf" and folded[1] != ref:
                found[ref] = folded[1]
            elif ref in source.reads:
                reached = True
                live.update(() if settled else (source.reads[ref][0],))
            else:
                settled = settled or folded is not None and folded[0] == "const"
                pending.extend((item, settled) for pin, item in source.flat.inputs[ref[0]].items() if pin != "exec")
        if not reached:
            cuts.update(found)
            through.add(root)
    return cuts, through, live


def _turn_off(source, forward, effect, identities, added, clears):
    decided = source.decision is not None and source.decision[0] in forward
    everything = len(identities) + len(added) + decided

    def tried(gate):
        fold = _Fold(source, forward, gate)
        quiet = {pin for pin, refs in identities.items() if all(
            (folded := fold(ref)) is not None and folded[0] == "leaf" for ref in refs.values() if ref[0] in forward)}
        solved = {}
        for pin, lanes in added.items():
            try:
                solved[pin] = _added_target(source, forward, fold, lanes, clears[pin])
            except _Unmapped as exc:
                solved[pin] = exc.with_traceback(None)
        cuts, through, live = _cuts(source, forward, fold, [
            *(ref for pin, refs in identities.items() if pin not in quiet for ref in refs.values()),
            *([source.decision] if decided else [])])
        off = (len(quiet) + sum(not isinstance(item, _Unmapped) for item in solved.values())
               + sum(pin not in quiet and all(ref in through for ref in refs.values() if ref[0] in forward)
                     for pin, refs in identities.items()) + (decided and source.decision in through))
        return (off, -len(live - gate.keys())), (fold, quiet, solved, cuts)

    best = None
    while best is None or best[0][0] < everything:
        gate, step = best[1][0].gate if best else {}, None
        for setting in ({**gate, name: value} for name in sorted(effect - gate.keys()) for value in _GATE_VALUES):
            found = tried(setting)
            step = found if step is None or found[0] > step[0] else step
            if step[0][0] == everything:
                break
        if step is None or best is not None and step[0] <= best[0]:
            break
        best = step
    return best[1]


def _lift_pass(lift, source, reached):
    seeds = [ref for ref, read in source.reads.items() if read[0] in lift.effect]
    forward = source.flat.forward(seeds)

    def leave(pin, reason):
        lift.left_out.append((lift.label(pin), source.program, reason))

    placed = {item.split.channel.key for item in source.positions}
    for channel in source.outputs:
        if channel.key in reached and channel.key not in placed:
            leave(f"{channel_label(channel)} · {purpose_title(source.purpose)}",
                  "REasy cannot follow where this pass writes it")
    if not forward:
        return []
    written = [item for item in source.positions if any(ref[0] in forward for ref in item.refs)]
    owned = {(id(item.split), item.form) for item in written}
    order = gbuffer_order_for(lift.target_document.profile)
    identities, tails, added, clears = defaultdict(dict), {}, defaultdict(dict), {}

    for item in written:
        split, channel = item.split, item.split.channel
        if item.form == "stored" and (id(split), "value") in owned:
            continue
        places = lift.places(source, item)
        for pin, refs in places.items():
            identities[pin].update(refs)
            tails[pin] = source.tail(split) if channel.packed and item.form == "value" else None
        if places:
            continue
        if channel.semantic == "sv_target" and (pin := lift.added_pin(split, source.meaning(channel))) is not None:
            clear = order.cleared.get(channel.semantic_index) if order and source.purpose == "gbuffer" else None
            if clear is None:
                leave(pin, "REasy has no record of what the game leaves in that target where no draw writes it")
            else:
                added[pin].update(zip(channel.lanes, item.refs))
                clears[pin] = clear
        else:
            leave(f"{channel_label(channel)} · {purpose_title(source.purpose)}",
                  "the target master has no such output")
    fold, quiet, solved, cuts = _turn_off(source, forward, lift.effect, identities, added, clears)
    handled = {lane for pin in (*quiet, *(pin for pin, item in solved.items() if not isinstance(item, _Unmapped)))
               for lane in lift.lanes_of(pin)}
    inside, covered, reasons = _changes_inside(lift, source, forward, cuts, handled) if cuts else ([], set(), {})
    reach = source.fed(forward)
    results = [item for item in inside if set(lift.lanes_of(item[0])) & reach <= covered]
    off = "no value of the effect's parameters turns it off there"
    for pin in (pin for pin in identities if pin not in quiet and not set(lift.lanes_of(pin)) & reach <= covered):
        alone = all((folded := fold(ref)) is not None and folded[0] == "const"
                    for ref in identities[pin].values() if ref[0] in forward)
        leave(pin, next((reasons[lane] for lane in lift.lanes_of(pin) if lane in reasons),
                        "it is all the effect: off, its own material writes a constant there, not a value this "
                        "material could take" if alone else off))
    if source.decision is not None and source.decision[0] in forward and source.cut_out not in covered:
        leave("Discard", reasons.get(source.cut_out, off))
    identities = {pin: refs for pin, refs in identities.items() if pin in quiet}
    substitutions = {}
    for pin, refs in identities.items():
        for position, ref in refs.items():
            if ref[0] in forward:
                substitutions.setdefault(fold(ref)[1], ("channel", pin, position))
    fragment = _Fragment(lift, source, forward, substitutions)
    for pin, refs in identities.items():
        try:
            parts = [fragment.value(refs[position]) if position in refs and refs[position][0] in forward
                     else lift.channel(pin, position)
                     for position in range(value_width(str(lift.target_pins[pin]["value_type"])))]
        except _Unmapped as exc:
            leave(pin, f"it reads {exc}")
            continue
        results.append((pin, _whole(lift, pin, parts), tails.get(pin)))
    for pin, lanes in added.items():
        if isinstance(solved[pin], _Unmapped):
            leave(pin, str(solved[pin]))
            continue
        fragment = _Fragment(lift, source, forward, {**substitutions, **solved[pin]})
        try:
            parts = [fragment.value(lanes[lane]) if lane in lanes else lift.constant(clears[pin][lane], "float")
                     for lane in range(value_width(str(lift.target_pins[pin]["value_type"])))]
        except _Unmapped as exc:
            leave(pin, f"it reads {exc}")
            continue
        results.append((pin, _whole(lift, pin, parts), None))
    return results


def _changes_inside(lift, source, forward, cuts, handled):
    def read(pass_):
        return {ref: ("channel", pin, lane) for ref, (pin, lane) in pass_.texels.items() if pin in lift.target_pins}

    sites = defaultdict(list)
    for cut, free in cuts.items():
        bits = []
        while source.flat.nodes[free[0]].kind == "reinterpret":
            bits.append(source.flat.nodes[free[0]].properties)
            free = source.flat.inputs[free[0]]["value"]
        sites[free].append((cut, bits))
    changes, reached, failed, reasons, results = [], set(), set(), {}, []
    route = (source.route[0], _target_program(lift.target_document, source.route[0], source.program), source.role)
    with _recovered(lift.target_document, route, cache=lift.recoveries) as own:
        mine, direct, theirs = (_Fragment(lift, own, set(), read(own)), _Fragment(lift, own, set(), {}),
                                _Fragment(lift, source, set(), read(source)))
        alike = defaultdict(list)
        for ref in dict.fromkeys((*(item for inputs in own.flat.inputs.values() for pin, item in inputs.items()
                                    if pin != "exec"), *filter(None, [own.decision]))):
            try:
                alike[mine.value(ref)].append(ref)
            except _Unmapped:
                pass
        for free, at in sites.items():
            stop = {cut[0] for cut, _bits in at}
            allowed = source.fed(source.flat.forward([cut for cut, _bits in at]))
            try:
                found = alike.get(theirs.value(free), [])
                fed = own.fed(own.flat.forward(found))
                why = ("this material does not compute alike" if not found
                       else "the source material also reads unchanged" if source.fed(source.flat.forward([free], stop)
                                                                                     - stop)
                       else "this material also reads for an output the effect writes outright"
                       if not fed.isdisjoint(handled) else None)
                if why:
                    raise _Unmapped(f"the value the effect changes there, which {why}")
                moved, texels = [], source.sampled(free)
                for ref in found:
                    start, own_texels = direct.value(ref), own.sampled(ref)
                    change = _Fragment(lift, source, forward, {
                        free: ("lifted", start), **{item: ("lifted", direct.value(own_texels[key]))
                                                    for key, item in texels.items() if key in own_texels}})
                    changed = {reduce(lambda value, item: lift.node("reinterpret", {
                        "source_type": item.get("value_type", "float"), "value_type": item.get("source_type", "float")},
                        value=value), bits, change.value(cut)) for cut, bits in at}
                    if len(changed) > 1:
                        raise _Unmapped("a value the effect changes in several ways")
                    moved.append((ref, start, changed.pop()))
            except _Unmapped as exc:
                failed |= allowed
                reasons.update((lane, f"it reads {exc}") for lane in allowed if lane not in reasons)
                continue
            for ref, start, changed in moved:
                value_type = _value_type(own, ref)
                changes.append((ref, changed, own.flat.forward([ref]), lift.every(
                    lift.node("compare", {"relation": "equal", "nan_result": False,
                                          "value_type": value_type.rstrip("234")},
                              a=lift.lane(changed, value_type, lane), b=lift.lane(start, value_type, lane))
                    for lane in range(value_width(value_type)))))
            reached |= fed
            reasons.update((lane, "this material computes it without the value the effect changes there")
                           for lane in allowed - fed if lane not in reasons)
        near = set().union(*(item[2] for item in changes))
        recomputed = _Fragment(lift, own, near, {ref: ("lifted", changed) for ref, changed, _near, _same in changes})
        written = [item for item in own.positions if any(ref[0] in near for ref in item.refs)]
        owned = {(id(item.split), item.form) for item in written}
        outputs, missing = defaultdict(dict), set()
        for item in written:
            if not (item.form == "stored" and (id(item.split), "value") in owned):
                places = lift.places(own, item)
                missing |= set() if places else own.fed({ref[0] for ref in item.refs})
                for pin, refs in places.items():
                    outputs[pin].update(refs)
        if own.decision is not None and own.decision[0] in near:
            if own.cut_out in lift.lanes:
                pin, position = lift.lanes[own.cut_out]
                outputs[pin][position] = own.decision
            else:
                missing.add(own.cut_out)
        failed |= missing
        reasons.update((lane, "the target master has no such output") for lane in missing if lane not in reasons)
        for pin, refs in outputs.items():
            lanes, value_type = lift.lanes_of(pin), str(lift.target_pins[pin]["value_type"])
            if failed.intersection(lanes):
                continue
            try:
                parts = [lift.node("select", {"value_type": value_type.rstrip("234")}, condition=lift.every(
                    same for _ref, _changed, reach, same in changes if refs[position][0] in reach),
                    true=lift.channel(pin, position), false=recomputed.value(refs[position]))
                    if position in refs and refs[position][0] in near else lift.channel(pin, position)
                    for position in range(value_width(value_type))]
            except _Unmapped as exc:
                failed.update(lanes)
                reasons.update((lane, f"it reads {exc}") for lane in lanes if lane not in reasons)
                continue
            results.append((pin, _whole(lift, pin, parts), None))
    return results, reached - failed, reasons


def _whole(lift, pin, parts):
    value_type = str(lift.target_pins[pin]["value_type"])
    return parts[0] if len(parts) == 1 else lift.node("compose", {"value_type": value_type},
                                                      **dict(zip("xyzw", parts)))


def _routes(record) -> list[tuple[int, int, str]]:
    found = []
    for name in record.get("routes", ()):
        route = str(name).partition("@")[0].split(":")
        found.append((int(route[0]), int(route[1]), route[2]))
    return list(dict.fromkeys(found))


@_recovery_allocation_batch()
def lift_effect(target_project, target_document, source_document, source_material, effect, *, name=""):
    effect = frozenset(effect)
    lift = _Lift(target_project, target_document, source_document, source_material, effect)
    profile = source_document.profile.shader_graph_profile
    words = {f"parameter@{offset}" for item in source_document.material_interface.parameters if item.name in effect
             for offset in range(item.byte_offset, item.byte_offset + item.byte_size, 4)}
    variants: dict[str, dict] = defaultdict(dict)
    done = {}
    for route in profile.material_program_routes(source_document):
        if route[2] not in {"pixel", "vertex"}:
            continue
        program = source_document.sdf.targets[route[0]].programs[route[1]]
        try:
            _splits, reach = native_program_surface(source_document, profile, *route)
        except ShaderGraphError:
            continue
        index = _target_program(target_document, route[0], program.name)
        if not words & reach.keys():
            continue
        if index is None:
            lift.left_out.append(("", program.name, "the target master has no such pass"))
            continue
        # Passes sharing a shader lift alike where both hand it the same data and the target's passes of their names
        # have the same shader, data and resources.
        own = _own_pass(target_document, route[0], program.name, route[2])
        data = (program_surface_data if route[2] == "pixel" else program_vertex_inputs)(source_document, profile,
                                                                                         *route[:2])
        key = (route[0], route[2], _shader_bytes(source_document, profile, program, route[2])[3],
               _shader_bytes(target_document, target_document.profile.shader_graph_profile,
                             target_document.sdf.targets[route[0]].programs[index], route[2])[3],
               pass_purpose(source_document, profile, route[0], route[1]), _hash(data, own))
        if key not in done:
            try:
                with _recovered(source_document, route, effect, own, lift.recoveries) as source:
                    done[key] = _lift_pass(lift, source,
                                           {item for word in words & reach.keys() for item in reach[word]})
            except ShaderGraphError as exc:
                lift.left_out.append(("", program.name, str(exc)))
                done[key] = []
        for pin, root, tail in done[key]:
            variants[pin].setdefault(root, [set(), tail])[0].add((route[0], program.name))
    if not variants and not lift.left_out:
        raise ShaderGraphError(
            f"Nothing {source_material.header.mat_name} draws depends on {', '.join(sorted(effect))}.")
    _prove(lift, variants)
    return _assemble(lift, variants, (name or ", ".join(sorted(effect)))[:128])


def _prove(lift, variants):
    seen = {}
    for pin, found in variants.items():
        for programs, tail in found.values():
            for route in _routes(lift.target_pins[pin]) if tail is not None else ():
                name = lift.target_document.sdf.targets[route[0]].programs[route[1]].name
                if (route[0], name) not in programs:
                    continue
                if route not in seen:
                    try:
                        with _recovered(lift.target_document, route, cache=lift.recoveries) as own:
                            seen[route] = {own.pin(split, "value"): own.tail(split) for split in own.channels
                                           if split.channel.packed}
                    except ShaderGraphError:
                        seen[route] = {}
                if seen[route].get(pin) != tail:
                    programs.discard((route[0], name))
                    lift.left_out.append((lift.label(pin), name, "its own code packs it otherwise"))


def _assemble(lift, variants, name) -> EffectLift:
    graph = ShaderGraph("material_function", "")
    outputs, used = {}, set()
    for pin, found in sorted(variants.items()):
        for root, (programs, _tail) in found.items():
            if not programs:
                continue
            identity = "out_" + _hash(pin, root)
            outputs[identity] = (pin, tuple(sorted(programs)), root)
    inputs = defaultdict(list)
    for (target, _name), source in lift.links.items():
        inputs[target].append(source)
    pending = [root for _pin, _programs, root in outputs.values()]
    while pending:
        node, _pin = pending.pop()
        if node not in used:
            used.add(node)
            pending.extend(inputs[node])
    for node in lift.nodes.values():
        if node.node_id in used:
            graph.nodes.append(node)
    graph.links.extend(ShaderGraphLink(source, source_pin, target, pin)
                       for (target, pin), (source, source_pin) in lift.links.items() if target in used)
    for identity, (pin, _programs, (root, root_pin)) in outputs.items():
        record = lift.target_pins[pin]
        graph.add_node("function_output", node_id=identity, properties={
            "pin_id": identity, "display_name": str(record["label"])[:128], "value_type": str(record["value_type"])})
        graph.links.append(ShaderGraphLink(root, root_pin, identity, "value"))
    arrange_shader_graph(graph)
    properties = {node.node_id: node.properties for node in graph.nodes}
    leaves = {identity: leaf for identity, leaf in lift.leaves.items() if identity in used}
    inputs = tuple(MaterialFunctionPin(identity, properties[identity]["display_name"], leaf.value_type)
                   for identity, leaf in leaves.items())
    function = MaterialFunctionAsset(
        "effect." + _hash(name, sorted(outputs), sorted(leaves)), name, "Lifted Effects",
        "", inputs, tuple(MaterialFunctionPin(identity, properties[identity]["display_name"],
                                               properties[identity]["value_type"], False)
                          for identity in outputs), graph, lift.target_document.profile.game) if outputs else None
    read = {(leaf.kind, leaf.key[0]) for leaf in leaves.values()}
    return EffectLift(function, leaves, {identity: item[:2] for identity, item in outputs.items()},
                      {key: value for key, value in lift.parameters.items() if ("parameter", key) in read},
                      {key: value for key, value in lift.textures.items() if ("texture", key) in read},
                      lift.engine, lift.labels, lift.left_out)


def insert_effect(project, lift: EffectLift) -> str:
    graph = project.graph
    for name, (value_type, values) in lift.parameters.items():
        project.parameters.append(MaterialParameterDefinition(uuid4().hex, name, value_type, values,
                                                              group=lift.function.name))
    for name, (dimension, path) in lift.textures.items():
        project.textures.append(MaterialTextureDefinition(uuid4().hex, name, dimension=dimension, default_path=path))
    ensure_material_interface_nodes(project)
    master = next(node for node in graph.nodes if node.kind == "re_engine_recovered_master_material")
    surface = next(node for node in graph.nodes if node.kind == "material_surface")
    current = {link.target_pin: (link.source_node, link.source_pin) for link in graph.links
               if link.target_node == surface.node_id}
    x, y = master.x + 420.0, master.y + 600.0
    function = graph.add_node("material_function", x=x, y=y,
                              properties=material_function_node_properties(lift.function))
    made = {}
    for row, (pin, leaf) in enumerate(lift.leaves.items()):
        if leaf not in made:
            made[leaf] = (current.get(leaf.key[0], (master.node_id, leaf.key[0])) if leaf.kind == "channel"
                          else _leaf_source(graph, project, lift, leaf, x - 420.0, y + 110.0 * row))
        graph.connect(*made[leaf], function.node_id, pin)
    chains = defaultdict(list)
    for output, (pin, programs) in lift.outputs.items():
        chains[pin].append((output, programs))
    for row, (pin, outputs) in enumerate(chains.items()):
        value = current.get(pin, (master.node_id, pin))
        for output, programs in outputs:
            chooser = graph.add_node(DRAWING_FOR_KIND, x=x + 360.0, y=y + 130.0 * row,
                                     properties={"purposes": list(PURPOSES),
                                                 "programs": [list(item) for item in programs]})
            select = graph.add_node("select", x=x + 620.0, y=y + 130.0 * row,
                                    properties={"value_type": str(next(
                                        item.value_type for item in lift.function.outputs if item.pin_id == output))})
            graph.connect(chooser.node_id, "active", select.node_id, "condition")
            graph.connect(function.node_id, output, select.node_id, "true")
            graph.connect(*value, select.node_id, "false")
            value = (select.node_id, "result")
        graph.disconnect_target(surface.node_id, pin)
        graph.connect(*value, surface.node_id, pin)
    master.properties["shown_channels"] = list(dict.fromkeys((*master.properties.get("shown_channels", ()),
                                                              *chains)))
    return function.node_id


def _leaf_source(graph, project, lift, leaf, x, y):
    if leaf.kind in {"parameter", "texture"}:
        definitions = project.parameters if leaf.kind == "parameter" else project.textures
        identity = next(item.parameter_id if leaf.kind == "parameter" else item.texture_id
                        for item in definitions if item.name == leaf.key[0])
        node = next(node for node in graph.nodes if node.kind == f"material_{leaf.kind}"
                    and node.properties.get(f"{leaf.kind}_id") == identity)
        return node.node_id, "value" if leaf.kind == "parameter" else "texture_object"
    if leaf.kind == "surface":
        node = graph.add_node(SURFACE_DATA_KIND, x=x, y=y, properties={
            "datum": leaf.key[0], "label": lift.labels.get(leaf.key[0], leaf.key[0]), "value_type": leaf.value_type})
        return node.node_id, "value"
    pin = lift.engine[leaf.key if leaf.kind == "engine" else leaf.key[:1]]
    node = graph.add_node(RENDERER_VALUE_KIND if leaf.kind == "engine" else RENDERER_RESOURCE_KIND, x=x, y=y,
                          properties={**pin, "display_name": pin["label"]})
    if leaf.kind == "engine" and leaf.key[2]:
        index = next(index for index, item in enumerate(pin["value_layout"]) if item["path"] == leaf.key[2])
        field_ = graph.add_node("aggregate_extract", x=x + 200.0, y=y, properties={
            "value_layout": pin["value_layout"], "field_index": index})
        graph.connect(node.node_id, "value", field_.node_id, "aggregate")
        return field_.node_id, "value"
    return node.node_id, "value"


__all__ = ["EffectLift", "Leaf", "insert_effect", "lift_effect"]
