"""See docs/internals/surface-splicing.md, Dataflow and channels."""
from __future__ import annotations

from dataclasses import dataclass, field

lazy from file_handlers.sdf.shader_graph import ShaderGraphError

lazy from .surface_flow_dxbc import dxbc_flow
lazy from .surface_flow_dxil import dxil_flow
lazy from .surface_flow_spirv import spirv_flow
lazy import fastsurface


MAX_CHANNEL_WIDTH = 4


def _constructed(self):
    return type(self), tuple(getattr(self, name) for name in self.__slots__)


@dataclass(frozen=True, slots=True)
class FlowValue:
    kind: str
    operands: tuple[int, ...] = ()
    controls: tuple[int, ...] = ()
    # Operation identity: opcode and constants for arithmetic; only the read kind for a source, since one vector may be
    # gathered from several inputs.
    signature: tuple = ()
    scalar: str = ""
    origin: object = None
    __reduce__ = _constructed


@dataclass(frozen=True, slots=True)
class FlowStore:
    semantic: str
    semantic_index: int
    lane: int
    value: int | None
    constant: str = ""
    site: object = None
    scope: str = "output"
    __reduce__ = _constructed


@dataclass(frozen=True, slots=True)
class NativeFlow:
    stage: str
    values: tuple[FlowValue, ...]
    stores: tuple[FlowStore, ...]
    discards: tuple[FlowStore, ...] = ()
    component_types: dict = field(default_factory=dict)
    # DXBC: the value each (instruction, register, component) read resolves to.
    reads: dict = field(default_factory=dict)
    # Return sites, in the store site format.
    exits: tuple = ()

    def __post_init__(self):
        fastsurface.check_flow(self.values, self.stores, self.discards, FlowValue, FlowStore)

    def __reduce__(self):
        state = [getattr(self, name) for name in self.__slots__]
        columns = fastsurface.value_columns(state[1], FlowValue)
        if columns is not None:
            state[1] = None
        return _loaded_flow, (columns, *state)


def _loaded_flow(columns, *state):
    """A pickled ``NativeFlow``, set as its dataclass state was set: without its checks."""
    flow = object.__new__(NativeFlow)
    for name, value in zip(NativeFlow.__slots__, state):
        object.__setattr__(flow, name, value)
    if columns is not None:
        object.__setattr__(flow, "values", fastsurface.flow_values(columns, FlowValue))
    return flow


@dataclass(frozen=True, slots=True)
class SurfaceChannel:
    stage: str
    scope: str
    semantic: str
    semantic_index: int
    lanes: tuple[int, ...]
    value_type: str
    stored_type: str
    packed: bool = False
    constant: bool = False
    __reduce__ = _constructed

    @property
    def key(self) -> str:
        return (f"{self.stage}:{self.scope}:{self.semantic}{self.semantic_index}."
                f"{''.join('xyzw'[lane] for lane in self.lanes)}")


@dataclass(frozen=True, slots=True)
class ChannelSplit:
    channel: SurfaceChannel
    stores: tuple[FlowStore, ...]
    # Values before packing, in definition order; empty when the stored lanes are edited directly.
    frontier: tuple[int, ...] = ()
    tail: frozenset[int] = frozenset()
    __reduce__ = _constructed


def parallel_signatures(flow: NativeFlow) -> list[int]:
    return fastsurface.Graph(flow.values, FlowValue).signatures()


def constant_field(contract, binding: int, space: int, offset: int) -> str:
    buffer = next((item for item in contract.constant_buffers
                   if (item.bind_point, item.register_space) == (binding, space)), None)
    if buffer is None:
        raise ShaderGraphError(f"The program reads constant buffer {binding} (space {space}), which it does not declare.")
    variable = next((item for item in buffer.variables
                     if item.byte_offset <= offset < item.byte_offset + item.byte_size), None)
    if variable is None:
        return f"{buffer.name}@{offset}"
    inner = offset - variable.byte_offset
    if variable.byte_size == 4:
        return variable.name
    if variable.byte_size <= 16 and variable.array_length == 1 and not variable.matrix and not variable.members:
        return f"{variable.name}.{'xyzw'[inner // 4]}"
    return f"{variable.name}@{inner}"


def buffer_element(resource: str, fields, byte_offset: int) -> tuple[str, int]:
    if byte_offset % 4:
        raise ShaderGraphError(f"A store into {resource} is not aligned to a word.")
    return "+".join((resource, *sorted(fields))), byte_offset // 4


def structured_element(resource: str, stride: int, index_fields, index: int, fields,
                       byte_offset: int) -> tuple[str, int]:
    return buffer_element(resource, [*fields, *(f"{field}*{stride}" for field in index_fields)],
                          byte_offset + index * stride)


# Texture return type -> component type; other return types read as float32.
TEXEL_TYPES = {"uint": "uint32", "sint": "sint32"}


def declared_component_types(contract) -> dict:
    return {(scope, field.semantic_name.casefold(), field.semantic_index): field.component_type
            for scope, fields in (("output", contract.outputs), ("patch", contract.patch_constants))
            for field in fields}


def buffer_component_type(scalars) -> str:
    kinds = set(scalars) - {""}
    return "float32" if kinds == {"float"} else "sint32" if kinds == {"int"} else "uint32"


def recovered_surface_flow(ir_kind: str, ir, contract) -> NativeFlow:
    if ir_kind == "dxbc-sm5":
        return dxbc_flow(ir, contract)
    if ir_kind == "dxil":
        return dxil_flow(ir.to_assembly(), contract)
    if ir_kind == "spirv":
        return spirv_flow(ir, contract)
    raise ShaderGraphError(f"Recovered program kind {ir_kind!r} has no surface dataflow reader.")


SCALAR_TYPES = {"float32": "float", "uint32": "uint", "sint32": "int"}


def _scalar_type(component_type: str) -> str:
    try:
        return SCALAR_TYPES[component_type]
    except KeyError as exc:
        raise ShaderGraphError(f"Output component type {component_type} has no editable scalar type.") from exc


def _vector(base: str, width: int) -> str:
    return base if width == 1 else f"{base}{width}"


def surface_channels(flow: NativeFlow) -> tuple[ChannelSplit, ...]:
    graph = fastsurface.Graph(flow.values, FlowValue)
    signatures = graph.signatures()
    elements: dict[tuple[str, str, int], dict[int, list[FlowStore]]] = {}
    textures: dict[str, list[FlowStore]] = {}
    for store in flow.stores:
        if store.scope == "read":
            textures.setdefault(store.semantic, []).append(store)
            continue
        elements.setdefault((store.scope, store.semantic, store.semantic_index), {}).setdefault(store.lane, []).append(store)
    result = []
    for name, reads in sorted(textures.items()):
        # A read returns four components regardless of use.
        base = _scalar_type(flow.component_types[("read", name, 0)])
        channel = SurfaceChannel(flow.stage, "read", name, 0, (0, 1, 2, 3), _vector(base, 4), _vector(base, 4))
        result.append(ChannelSplit(channel, tuple(reads)))
    for (scope, semantic, semantic_index), lanes in sorted(elements.items()):
        component = flow.component_types.get((scope, semantic, semantic_index))
        if component is None:
            raise ShaderGraphError(f"{semantic}{semantic_index} has no declared output component type.")
        stored_base = _scalar_type(component)
        # A lane stored at several sites keeps its packing; only the stored value is editable.
        single = {lane: stores[0].value for lane, stores in lanes.items()
                  if len(stores) == 1 and stores[0].value is not None}
        packing = {lane: graph.packing((value,), MAX_CHANNEL_WIDTH) for lane, value in sorted(single.items())}
        packing = {lane: item for lane, item in packing.items() if item[0]}

        def shared(lane, other):
            return bool(packing[lane][0] & packing[other][0] or set(packing[lane][1]) & set(packing[other][1]))

        # Lanes whose packing shares a value pack from one vector.
        groups: list[set[int]] = []
        for lane in packing:
            joined = [group for group in groups if any(shared(lane, other) for other in group)]
            groups = [group for group in groups if group not in joined] + [{lane}.union(*joined)]
        joint = []
        for group in groups:
            members = tuple(sorted(group))
            tail, frontier = graph.packing(tuple(single[lane] for lane in members), MAX_CHANNEL_WIDTH)
            if tail:
                joint.append((members, frontier, tail))
        # Separate packings with the same steps re-encode one vector lane by lane.
        alike: dict[tuple, list] = {}
        for members, frontier, tail in joint:
            shape = graph.tail_shape(frontier, tuple(single[lane] for lane in members))
            alike.setdefault((shape, len(frontier)), []).append((members, frontier, tail))
        splits = []
        for parts in alike.values():
            members = tuple(lane for part in parts for lane in part[0])
            frontier = tuple(item for part in parts for item in part[1])
            if len(parts) > 1 and len(frontier) > MAX_CHANNEL_WIDTH:
                candidates = parts
            else:
                candidates = [(members, frontier, frozenset().union(*(part[2] for part in parts)))]
            for candidate in candidates:
                scalars = {flow.values[item].scalar for item in candidate[1]}
                if len(scalars) == 1 and "" not in scalars:
                    splits.append(candidate)
        packed_lanes = {*group for group, _frontier, _tail in splits}
        raw: dict[object, list[int]] = {}
        for lane in sorted(set(lanes) - packed_lanes):
            stores = lanes[lane]
            if len(stores) > 1:
                identity = ("sites", lane)
            elif stores[0].value is None:
                identity = ("constant", stores[0].constant)
            else:
                identity = ("value", signatures[stores[0].value])
            raw.setdefault(identity, []).append(lane)
        for group, frontier, tail in splits:
            scalar = flow.values[frontier[0]].scalar
            if scalar == "integer":
                scalar = stored_base if stored_base != "float" else "uint"
            channel = SurfaceChannel(flow.stage, scope, semantic, semantic_index, group,
                                     _vector(scalar, len(frontier)), _vector(stored_base, len(group)), packed=True)
            result.append(ChannelSplit(channel, tuple(lanes[lane][0] for lane in group), frontier, tail))
        for identity, group in raw.items():
            stores = tuple(store for lane in group for store in lanes[lane])
            channel = SurfaceChannel(flow.stage, scope, semantic, semantic_index, tuple(group),
                                     _vector(stored_base, len(group)), _vector(stored_base, len(group)),
                                     constant=identity[0] == "constant")
            result.append(ChannelSplit(channel, stores))
    if flow.stage == "pixel":
        channel = SurfaceChannel("pixel", "discard", "discard", 0, (0,), "bool", "bool", constant=not flow.discards)
        result.append(ChannelSplit(channel, flow.discards))
    return tuple(sorted(result, key=lambda item: (item.channel.scope, item.channel.semantic,
                                                    item.channel.semantic_index, item.channel.lanes)))


__all__ = [
    "ChannelSplit",
    "FlowStore",
    "FlowValue",
    "NativeFlow",
    "SurfaceChannel",
    "TEXEL_TYPES",
    "buffer_component_type",
    "declared_component_types",
    "structured_element",
    "buffer_element",
    "constant_field",
    "parallel_signatures",
    "recovered_surface_flow",
    "surface_channels",
]
