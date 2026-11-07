from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
from dataclasses import dataclass, replace
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import fastgraph

from file_handlers.sdf.shader_graph_math import NUMERIC_VALUE_TYPES, is_float_type, value_width
from file_handlers.sdf.shader_graph_texture import TEXTURE_OBJECT_TYPES, WRITABLE_TEXTURE_OBJECT_TYPES

from file_handlers.sdf.shader_graph import (
    MAX_GRAPH_LINKS,
    MAX_GRAPH_NODES,
    MAX_PROJECT_LENGTH,
    ShaderContract,
    ShaderGraph,
    ShaderGraphDiagnostic,
    ShaderGraphError,
    ShaderGraphLink,
    ShaderGraphNode,
    connection_compatible,
    node_pin,
    node_pins,
    node_property_diagnostics,
    node_title,
    shader_value_literal,
)
from file_handlers.sdf.graph_snapshot import (IMMUTABLE_JSON, checked_graph_snapshot, compiler_graph_copy,
                                              freeze_properties, immutable_property_validation, thaw_graph)
from file_handlers.sdf.shader_graph_execution import EXECUTION_KINDS, EXECUTION_TYPE
from file_handlers.sdf.shader_graph_resource import is_resource_type, resource_array_parts

from .material_region_read import (REGION_ASSET_FIELDS, read_region, region_content_hash, region_derived_cache,
                                   region_read_session)


MATERIAL_FUNCTION_SCHEMA = "reasy.material-function"
MATERIAL_FUNCTION_SCHEMA_VERSION = 1
MAX_MATERIAL_FUNCTION_BYTES = MAX_PROJECT_LENGTH
# Pragmata shaders contain 129 nested conditional regions before calls.
MAX_FUNCTION_DEPTH = 256
FUNCTION_VALUE_TYPES = frozenset(
    {
        *NUMERIC_VALUE_TYPES,
        *TEXTURE_OBJECT_TYPES,
        *WRITABLE_TEXTURE_OBJECT_TYPES,
        "texture",
        "sampler",
        "comparison_sampler",
        "byte_address_buffer",
        'rw_byte_address_buffer',
        'execution',
    }
)
_PIN_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class MaterialFunctionError(ShaderGraphError):
    pass


_known_pins = ContextVar("material_function_known_pins", default=None)


@dataclass(frozen=True, slots=True)
class MaterialFunctionPin:
    pin_id: str
    name: str
    value_type: str
    required: bool = True
    default: tuple[float | int | bool, ...] | None = None

    def __post_init__(self) -> None:
        if not _PIN_ID.fullmatch(str(self.pin_id)):
            raise MaterialFunctionError(f"invalid material-function pin ID {self.pin_id!r}")
        if not str(self.name).strip() or len(str(self.name)) > 128:
            raise MaterialFunctionError("material-function pin names are invalid")
        try:
            supported = self.value_type in FUNCTION_VALUE_TYPES or is_resource_type(self.value_type) or resource_array_parts(self.value_type) is not None
        except ValueError as exc:
            raise MaterialFunctionError(str(exc)) from exc
        if not supported:
            raise MaterialFunctionError(
                f"unsupported material-function pin type {self.value_type!r}"
            )
        if self.default is not None:
            if self.value_type not in NUMERIC_VALUE_TYPES:
                raise MaterialFunctionError("object inputs cannot have numeric defaults")
            if len(self.default) != value_width(self.value_type):
                raise MaterialFunctionError(
                    f"{self.name} has an invalid default value"
                )
            try:
                shader_value_literal(self.value_type, self.default)
            except (ValueError, OverflowError) as exc:
                raise MaterialFunctionError(f'{self.name} has an invalid default value: {exc}') from exc
            scalar = float if is_float_type(self.value_type) else bool if self.value_type.startswith('bool') else int
            values = tuple(scalar(value) for value in self.default)
            object.__setattr__(self, "default", values)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.pin_id,
            "name": self.name,
            "type": self.value_type,
            "required": bool(self.required),
        }
        if self.default is not None:
            result["default"] = list(self.default)
        return result

    @classmethod
    def from_dict(cls, value: object) -> "MaterialFunctionPin":
        if not isinstance(value, dict):
            raise MaterialFunctionError("material-function pins must be objects")
        default = value.get("default")
        return cls(
            str(value.get("id", "")),
            str(value.get("name", "")),
            str(value.get("type", "")),
            bool(value.get("required", True)),
            None if default is None else tuple(default),
        )


@dataclass(frozen=True, slots=True)
class MaterialFunctionAsset:
    function_id: str
    name: str
    category: str
    description: str
    inputs: tuple[MaterialFunctionPin, ...]
    outputs: tuple[MaterialFunctionPin, ...]
    graph: ShaderGraph
    source_profile: str = ""

    def __post_init__(self) -> None:
        if (
            not str(self.function_id).strip()
            or len(str(self.function_id)) > 160
            or not str(self.name).strip()
            or len(str(self.name)) > 160
            or len(str(self.category)) > 128
            or len(str(self.description)) > 2048
            or len(str(self.source_profile)) > 64
        ):
            raise MaterialFunctionError("material-function metadata is invalid")
        input_ids = [pin.pin_id for pin in self.inputs]
        output_ids = [pin.pin_id for pin in self.outputs]
        if (
            not self.outputs
            or len(input_ids) != len(set(input_ids))
            or len(output_ids) != len(set(output_ids))
            or set(input_ids) & set(output_ids)
        ):
            raise MaterialFunctionError("material-function pin IDs must be unique")
        errors = [
            issue.message
            for issue in validate_material_function(self)
            if issue.severity == "error"
        ]
        if errors:
            raise MaterialFunctionError("\n".join(errors))

    def to_dict(self) -> dict[str, Any]:
        return _function_document(self.function_id, self.name, self.category,
            self.description, self.inputs, self.outputs, self.graph, self.source_profile)

    @property
    def content_hash(self) -> str:
        return region_content_hash(self, self.to_dict)

    def to_json(self) -> str:
        payload = json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)
        byte_size = len(payload.encode("utf-8"))
        if byte_size > MAX_MATERIAL_FUNCTION_BYTES:
            raise MaterialFunctionError(f"material-function asset is {byte_size} bytes, exceeding the {MAX_MATERIAL_FUNCTION_BYTES}-byte authoring budget")
        return payload

    @classmethod
    def from_dict(cls, value: object) -> "MaterialFunctionAsset":
        with region_read_session():
            if (
                not isinstance(value, dict)
                or value.get("schema") != MATERIAL_FUNCTION_SCHEMA
                or int(value.get("version", -1)) != MATERIAL_FUNCTION_SCHEMA_VERSION
            ):
                raise MaterialFunctionError("file is not a supported REasy material function")
            inputs = value.get("inputs", [])
            outputs = value.get("outputs", [])
            if (
                not isinstance(inputs, list)
                or not isinstance(outputs, list)
                or len(inputs) + len(outputs) > MAX_GRAPH_NODES
            ):
                raise MaterialFunctionError("material-function interface is too large")
            return cls(
                str(value.get("id", "")),
                str(value.get("name", "")),
                str(value.get("category", "User Functions")),
                str(value.get("description", "")),
                tuple(MaterialFunctionPin.from_dict(pin) for pin in inputs),
                tuple(MaterialFunctionPin.from_dict(pin) for pin in outputs),
                ShaderGraph.from_dict(value.get("graph")),
                str(value.get("source_profile", "")),
            )

    @classmethod
    def from_json(cls, text: str) -> "MaterialFunctionAsset":
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MATERIAL_FUNCTION_BYTES:
            raise MaterialFunctionError("material-function asset is invalid or too large")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MaterialFunctionError(f"invalid material-function JSON: {exc}") from exc
        return cls.from_dict(value)


def _function_document(function_id, name, category, description, inputs, outputs,
                       graph, source_profile, *, graph_document=None):
    return {
        "schema": MATERIAL_FUNCTION_SCHEMA,
        "version": MATERIAL_FUNCTION_SCHEMA_VERSION,
        "id": function_id,
        "name": name,
        "category": category,
        "description": description,
        "source_profile": source_profile,
        "inputs": [pin.to_dict() for pin in inputs],
        "outputs": [pin.to_dict() for pin in outputs],
        "graph": graph.to_dict() if graph_document is None else graph_document,
    }


@dataclass(frozen=True, slots=True)
class _CompilerFunctionPublication:
    document: dict


def _matches_compiler_graph(graph, document):
    return fastgraph.matches_graph(graph, document,
                                   (ShaderGraph, ShaderGraphNode, ShaderGraphLink, (dict, *IMMUTABLE_JSON)))


def _compiler_function_asset(function_id, name, category, description, inputs,
                             outputs, graph, source_profile="", *, reuse=None, pins=None):
    cache = region_derived_cache('compiler_function_snapshots')
    if cache is None:
        return MaterialFunctionAsset(function_id, name, category, description,
            inputs, outputs, graph, source_profile)
    canonical = (all(type(value) is str for value in
        (function_id, name, category, description, source_profile))
        and type(inputs) is tuple and type(outputs) is tuple
        and all(type(pin) is MaterialFunctionPin
                and all(type(value) is str for value in (pin.pin_id, pin.name, pin.value_type))
                and type(pin.required) is bool
                and (pin.default is None or type(pin.default) is tuple)
                for pin in (*inputs, *outputs)))
    previous = cache.get(id(reuse)) if reuse is not None else None
    if (canonical and previous is not None and previous[0] is reuse
            and len(inputs) + len(outputs) <= MAX_GRAPH_NODES):
        asset = previous[1]
        if ((function_id, name, category, description, source_profile)
                == (asset.function_id, asset.name, asset.category, asset.description, asset.source_profile)
                and len(inputs) == len(asset.inputs) and len(outputs) == len(asset.outputs)
                and all(left is right for left, right in zip((*inputs, *outputs), (*asset.inputs, *asset.outputs)))
                and _matches_compiler_graph(asset.graph, reuse['graph'])
                and (graph is asset.graph or _matches_compiler_graph(graph, reuse['graph']))):
            return _CompilerFunctionPublication(reuse)
    checked = checked_graph_snapshot(graph) if canonical else None
    with immutable_property_validation():
        document = freeze_properties(_function_document(function_id, name, category,
            description, inputs, outputs, graph, source_profile,
            graph_document=None if checked is None else checked.document))
    if checked is None:
        asset = MaterialFunctionAsset.from_dict(document)
    else:
        if len(inputs) + len(outputs) > MAX_GRAPH_NODES:
            raise MaterialFunctionError("material-function interface is too large")
        pin_cache = region_derived_cache('compiler_function_pins')

        def checked_pin(pin, declaration):
            defaults = (None if pin.default is None else tuple(
                (type(value), value.hex() if type(value) is float else value)
                for value in pin.default))
            key = pin.pin_id, pin.name, pin.value_type, pin.required, defaults
            result = pin_cache.get(key)
            if result is None:
                result = MaterialFunctionPin.from_dict(declaration)
                pin_cache[key] = result
            return result

        recorded = region_derived_cache('compiler_function_graph_pins')
        if pins is None:
            published = previous[1].graph if previous is not None and previous[0] is reuse else None
            kept = recorded.get(id(published)) if published is not None else None
            owned = ({node.node_id: node for node in published.nodes}
                     if kept is not None and kept[0] is published else {})
            pins = {node.node_id: kept[1][node.node_id] for node in graph.nodes
                    if owned.get(node.node_id) is node and node.node_id in kept[1]}
        token = _known_pins.set((checked.graph, pins))
        try:
            asset = MaterialFunctionAsset(function_id, name, category, description,
                tuple(checked_pin(pin, declaration) for pin, declaration in zip(inputs, document['inputs'])),
                tuple(checked_pin(pin, declaration) for pin, declaration in zip(outputs, document['outputs'])),
                checked.graph_for(document['graph']), source_profile)
        finally:
            _known_pins.reset(token)
        recorded[id(asset.graph)] = asset.graph, pins
    cache[id(document)] = document, asset
    return _CompilerFunctionPublication(document)


def _compiler_function_document(asset):
    if isinstance(asset, _CompilerFunctionPublication):
        return asset.document
    return asset.to_dict()


def _matches_compiler_function(asset, document):
    metadata = (('id', 'function_id'), ('name', 'name'), ('category', 'category'),
                ('description', 'description'), ('source_profile', 'source_profile'))
    if (type(asset) is not MaterialFunctionAsset or not isinstance(document, dict)
            or not isinstance(document, IMMUTABLE_JSON)
            or set(document) != {'schema', 'version', 'id', 'name', 'category',
                                  'description', 'source_profile', 'inputs', 'outputs', 'graph'}
            or type(document['schema']) is not str or document['schema'] != MATERIAL_FUNCTION_SCHEMA
            or type(document['version']) is not int or document['version'] != MATERIAL_FUNCTION_SCHEMA_VERSION
            or any(document[key] is not getattr(asset, field) for key, field in metadata)):
        return False
    for name, pins in (('inputs', asset.inputs), ('outputs', asset.outputs)):
        if len(pins) != len(document[name]):
            return False
        for pin, declaration in zip(pins, document[name]):
            keys = {'id', 'name', 'type', 'required'} | ({'default'} if pin.default is not None else set())
            if (set(declaration) != keys or any(declaration[key] is not value for key, value in
                    zip(('id', 'name', 'type', 'required'),
                        (pin.pin_id, pin.name, pin.value_type, pin.required)))):
                return False
            if pin.default is not None and (len(pin.default) != len(declaration['default'])
                    or any(left is not right for left, right in zip(pin.default, declaration['default']))):
                return False
    return _matches_compiler_graph(asset.graph, document['graph'])


def _read_compiler_function(document):
    cache = region_derived_cache('compiler_function_snapshots')
    snapshot = cache.get(id(document)) if cache is not None else None
    if snapshot is not None and snapshot[0] is document:
        return snapshot[1]
    asset = MaterialFunctionAsset.from_dict(document)
    if cache is not None and _matches_compiler_function(asset, document):
        cache[id(document)] = document, asset
    return asset


def material_function_node_properties(
    asset: MaterialFunctionAsset,
) -> dict[str, Any]:
    return {
        "display_name": asset.name,
        "function": asset.to_dict(),
        "function_hash": asset.content_hash,
        "authoring_origin": "user_function",
    }


def load_material_function(path: str | Path) -> MaterialFunctionAsset:
    source = Path(path)
    try:
        return MaterialFunctionAsset.from_json(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise MaterialFunctionError(f"cannot read material function {source}: {exc}") from exc


def discover_material_functions(
    roots: Iterable[str | Path],
) -> tuple[MaterialFunctionAsset, ...]:
    by_id: dict[str, MaterialFunctionAsset] = {}
    for root in roots:
        path = Path(root)
        candidates = (
            (path,)
            if path.is_file()
            else tuple(sorted(path.rglob("*.reasymaterialfunction.json")))
            if path.is_dir()
            else ()
        )
        for candidate in candidates:
            asset = load_material_function(candidate)
            by_id.setdefault(asset.function_id, asset)
    return tuple(
        sorted(by_id.values(), key=lambda asset: (asset.category.casefold(), asset.name.casefold()))
    )


def material_function_from_node(node: ShaderGraphNode) -> MaterialFunctionAsset:
    if node.kind != "material_function":
        raise MaterialFunctionError("node is not a material function")
    return read_region('material_function', node.properties)


def _parse_function_snapshot(properties):
    asset = MaterialFunctionAsset.from_dict(properties.get("function"))
    stored_hash = str(properties.get("function_hash", ""))
    if stored_hash and stored_hash != asset.content_hash:
        raise MaterialFunctionError(
            f"{asset.name} has a modified or corrupt embedded snapshot"
        )
    return asset


def validate_material_function(
    asset: MaterialFunctionAsset,
) -> tuple[ShaderGraphDiagnostic, ...]:
    with region_read_session():
        return _validate_material_function(asset)


_OUTSIDE_FUNCTION_KINDS = frozenset({
    "input", "output", "parameter", "structured_parameter", "material_parameter", "material_texture",
    "material_surface", "texture_coordinates", "texture_sample", "shader_resource",
})


def _outside_function(node) -> bool:
    return node.kind in _OUTSIDE_FUNCTION_KINDS or bool(node.properties.get("semantic_reconstruction_boundary", False))


def _validate_material_function(asset: MaterialFunctionAsset) -> tuple[ShaderGraphDiagnostic, ...]:
    graph = asset.graph
    issues: list[ShaderGraphDiagnostic] = []
    if graph.stage != "material_function":
        issues.append(
            ShaderGraphDiagnostic(
                "error", "function-domain", "A material function needs a function graph."
            )
        )
    if len(graph.nodes) > MAX_GRAPH_NODES or len(graph.links) > MAX_GRAPH_LINKS:
        issues.append(
            ShaderGraphDiagnostic(
                "error", "function-size", "The material function is too large."
            )
        )
    known = _known_pins.get()
    issues.extend(fastgraph.function_diagnostics(graph.nodes, graph.links, asset.inputs, asset.outputs,
        known[1] if known is not None and known[0] is graph else {}, (
        _OUTSIDE_FUNCTION_KINDS, node_title, node_property_diagnostics, connection_compatible, ShaderGraphDiagnostic,
        node_pins, (EXECUTION_KINDS, EXECUTION_TYPE, ShaderGraphDiagnostic))))
    return tuple(issues)


def replace_material_function(graph: ShaderGraph, node_id: str, asset: MaterialFunctionAsset) -> None:
    node = graph.node(node_id)
    if node is None or node.kind != "material_function":
        raise MaterialFunctionError("Select a material function to edit.")
    candidate = deepcopy(graph)
    replacement = candidate.node(node_id)
    replacement.properties = material_function_node_properties(asset)
    incoming = set()
    for link in candidate.links:
        if node_id not in (link.source_node, link.target_node):
            continue
        output = node_pin(candidate.node(link.source_node), None, link.source_pin, "output")
        input_pin = node_pin(candidate.node(link.target_node), None, link.target_pin, "input")
        if output is None or input_pin is None or not connection_compatible(output.value_type, input_pin.value_type):
            raise MaterialFunctionError("This interface change would break an existing connection. Disconnect that port in the material first.")
        if link.target_node == node_id:
            incoming.add(link.target_pin)
    if any(link.source_node == node_id for link in candidate.links):
        missing = [p.name for p in asset.inputs if p.required and p.default is None and p.pin_id not in incoming]
        if missing:
            raise MaterialFunctionError("New inputs need defaults before applying to a connected function: " + ", ".join(missing))
    expand_material_functions(asset.graph, live_only=True)
    node.properties = replacement.properties


def validate_material_function_nodes(
    graph: ShaderGraph,
) -> tuple[ShaderGraphDiagnostic, ...]:
    issues: list[ShaderGraphDiagnostic] = []
    for node in graph.nodes:
        if node.kind != "material_function":
            continue
        try:
            asset = material_function_from_node(node)
        except MaterialFunctionError as exc:
            issues.append(
                ShaderGraphDiagnostic("error", "material-function", str(exc), node.node_id)
            )
            continue
        issues.extend(
            ShaderGraphDiagnostic(
                issue.severity,
                issue.code,
                f"{asset.name}: {issue.message}",
                node.node_id,
            )
            for issue in validate_material_function(asset)
        )
    return tuple(issues)


def _mapped_node_id(instance_id: str, internal_id: str, occupied: set[str]) -> str:
    digest = hashlib.sha256(
        f"{instance_id}\0{internal_id}".encode("utf-8")
    ).hexdigest()
    for length in range(20, 65, 4):
        candidate = f"fn_{digest[:length]}"
        if candidate not in occupied:
            occupied.add(candidate)
            return candidate
    raise MaterialFunctionError("material-function node IDs collide")


def _splice_function(
    graph: ShaderGraph,
    instance: ShaderGraphNode,
    asset: MaterialFunctionAsset,
    function_graph: ShaderGraph,
) -> None:
    input_nodes = {
        str(node.properties.get("pin_id", "")): node
        for node in function_graph.nodes
        if node.kind == "function_input"
    }
    output_nodes = {
        node.node_id: str(node.properties.get("pin_id", ""))
        for node in function_graph.nodes
        if node.kind == "function_output"
    }
    incoming = {
        link.target_pin: (link.source_node, link.source_pin)
        for link in graph.links
        if link.target_node == instance.node_id
    }
    outgoing = [link for link in graph.links if link.source_node == instance.node_id]
    occupied = {node.node_id for node in graph.nodes}
    mapping: dict[str, str] = {}
    ordinary = [
        node
        for node in function_graph.nodes
        if node.kind not in {"function_input", "function_output"}
    ]
    center_x = sum((node.x for node in ordinary), 0.0) / max(1, len(ordinary))
    center_y = sum((node.y for node in ordinary), 0.0) / max(1, len(ordinary))
    for node in ordinary:
        mapped = _mapped_node_id(instance.node_id, node.node_id, occupied)
        mapping[node.node_id] = mapped
        graph.nodes.append(
            replace(node, node_id=mapped,
                x=instance.x + node.x - center_x, y=instance.y + node.y - center_y)
        )

    input_sources: dict[str, tuple[str, str]] = {}
    for pin in asset.inputs:
        source = incoming.get(pin.pin_id)
        if source is None and pin.default is not None:
            node_id = _mapped_node_id(instance.node_id, f"default:{pin.pin_id}", occupied)
            graph.nodes.append(
                ShaderGraphNode(
                    node_id,
                    "constant",
                    instance.x - 360.0,
                    instance.y,
                    {"value_type": pin.value_type, "values": list(pin.default)},
                )
            )
            source = (node_id, "value")
        if source is None and pin.required:
            raise MaterialFunctionError(f"{asset.name} needs {pin.name}")
        if source is not None:
            input_sources[input_nodes[pin.pin_id].node_id] = source

    new_links: list[ShaderGraphLink] = [
        link
        for link in graph.links
        if link.source_node != instance.node_id and link.target_node != instance.node_id
    ]
    output_sources: dict[str, tuple[str, str]] = {}
    for link in function_graph.links:
        if link.source_node in input_sources:
            source = input_sources[link.source_node]
        elif link.source_node in mapping:
            source = (mapping[link.source_node], link.source_pin)
        else:
            raise MaterialFunctionError(f"{asset.name} has an unresolved function input")
        if link.target_node in output_nodes:
            output_sources[output_nodes[link.target_node]] = source
        elif link.target_node in mapping:
            new_links.append(
                ShaderGraphLink(source[0], source[1], mapping[link.target_node], link.target_pin)
            )
        else:
            raise MaterialFunctionError(f"{asset.name} has an unresolved function output")
    for link in outgoing:
        source = output_sources.get(link.source_pin)
        if source is None:
            raise MaterialFunctionError(
                f"{asset.name} does not produce {link.source_pin!r}"
            )
        new_links.append(
            ShaderGraphLink(source[0], source[1], link.target_node, link.target_pin)
        )
    graph.nodes[:] = [node for node in graph.nodes if node.node_id != instance.node_id]
    graph.links[:] = new_links
    if len(graph.nodes) > MAX_GRAPH_NODES or len(graph.links) > MAX_GRAPH_LINKS:
        raise MaterialFunctionError("expanded material graph exceeds its safety limit")
    return mapping


def _has_function_calls(graph, stack, source_profile):
    pending = [(graph.to_dict(), stack)]
    while pending:
        document, ancestors = pending.pop()
        for node in document['nodes']:
            kind = node['kind']
            if kind == 'material_function':
                return True
            for name in REGION_ASSET_FIELDS.get(kind, ()):
                asset = node['properties'][name]
                identity = asset['id']
                if identity in ancestors or len(ancestors) >= MAX_FUNCTION_DEPTH:
                    raise MaterialFunctionError('Recursive or excessively nested control-flow function: ' + identity)
                _check_profile(asset['name'], asset.get('source_profile', ''), source_profile)
                pending.append((asset['graph'], (*ancestors, identity)))
    return False


def _check_profile(name, profile, source_profile):
    if source_profile is not None and profile and profile.casefold() != source_profile.casefold():
        raise MaterialFunctionError(f"{name} targets {profile}, not {source_profile}.")


def _expand_graph(
    graph: ShaderGraph,
    stack: tuple[str, ...],
    source_profile: str | None = None,
    live_only: bool = False,
) -> ShaderGraph:
    from .material_branch import branch_arms, branch_properties
    from .material_loop import loop_body, loop_properties
    result = compiler_graph_copy(graph)
    if live_only:
        live = result.upstream_nodes(n.node_id for n in result.nodes
            if n.kind in {"material_surface", "function_output", "output"})
        for node in tuple(result.nodes):
            if node.kind == "material_function" and node.node_id not in live:
                result.remove_node(node.node_id)
    if not _has_function_calls(result, stack, source_profile):
        for node in result.nodes:
            if node.kind == 'branch':
                branch_arms(node)
            elif node.kind == 'repeat':
                loop_body(node)
        return result
    while True:
        instance = next(
            (node for node in result.nodes if node.kind == "material_function"),
            None,
        )
        if instance is None:
            for node in result.nodes:
                if node.kind in {"branch", "repeat"}:
                    if node.kind == 'repeat':
                        body, states, continuation = loop_body(node)
                        regions = (body,)
                    else:
                        regions = branch_arms(node)
                    arms = []
                    for arm in regions:
                        _check_profile(arm.name, arm.source_profile, source_profile)
                        if arm.function_id in stack:
                            raise MaterialFunctionError('Recursive control-flow function: ' + arm.function_id)
                        if len(stack) >= MAX_FUNCTION_DEPTH:
                            raise MaterialFunctionError(f'Control-flow function exceeds the {MAX_FUNCTION_DEPTH}-region safety budget.')
                        arms.append(replace(arm, graph=_expand_graph(arm.graph, (*stack, arm.function_id), source_profile, live_only)))
                    node.properties = (branch_properties(*arms, name=node.properties.get("display_name", "Branch")) if node.kind == 'branch'
                                       else loop_properties(arms[0], states, continuation))
            return result
        asset = material_function_from_node(instance)
        _inline_function(result, instance, asset, stack, source_profile, live_only)


def _inline_function(graph, instance, asset, stack=(), source_profile=None, live_only=False):
    _check_profile(asset.name, asset.source_profile, source_profile)
    if asset.function_id in stack or len(stack) >= MAX_FUNCTION_DEPTH:
        raise MaterialFunctionError(f"recursive or excessively nested material function: {asset.name}")
    expanded = _expand_graph(asset.graph, (*stack, asset.function_id), source_profile, live_only)
    expanded = replace(asset, graph=ShaderGraph.from_dict(expanded.to_dict())).graph
    return _splice_function(graph, instance, asset, expanded)


def _expand_material_functions_for_compilation(
    graph: ShaderGraph, *, source_profile: str | None = None, live_only=False,
) -> ShaderGraph:
    with region_read_session():
        return _expand_graph(graph, (), source_profile, live_only)


def expand_material_functions(graph: ShaderGraph, *, source_profile: str | None = None, live_only=False) -> ShaderGraph:
    return thaw_graph(_expand_material_functions_for_compilation(
        graph, source_profile=source_profile, live_only=live_only))


def compilation_graph(graph: ShaderGraph, *, source_profile: str | None = None) -> ShaderGraph:
    if not _has_function_calls(graph, (), source_profile):
        return graph
    return expand_material_functions(graph, source_profile=source_profile)


def expand_material_function_node(
    graph: ShaderGraph,
    node_id: str,
) -> tuple[str, ...]:
    candidate = deepcopy(graph)
    instance = candidate.node(str(node_id))
    if instance is None or instance.kind != "material_function":
        raise MaterialFunctionError("Select a material-function call first")
    asset = material_function_from_node(instance)
    before = {node.node_id for node in candidate.nodes}
    _splice_function(candidate, instance, asset, deepcopy(asset.graph))
    expand_material_functions(candidate, live_only=True)
    graph.nodes[:] = candidate.nodes
    graph.links[:] = candidate.links
    return tuple(
        node.node_id
        for node in graph.nodes
        if node.node_id not in before or node.node_id == str(node_id)
    )


def extract_material_function(
    graph: ShaderGraph,
    selected_node_ids: Iterable[str],
    *,
    name: str,
    category: str = "User Functions",
    description: str = "",
    source_profile: str = "",
    contract: ShaderContract | None = None,
) -> tuple[MaterialFunctionAsset, ShaderGraphNode]:
    selected = {str(node_id) for node_id in selected_node_ids}
    nodes = {node.node_id: node for node in graph.nodes}
    if not selected or not selected <= set(nodes):
        raise MaterialFunctionError("Select one or more calculation nodes first")
    invalid = next(
        (
            node
            for node_id, node in nodes.items()
            if node_id in selected
            and (_outside_function(node) or node.kind in {"function_input", "function_output"})
        ),
        None,
    )
    if invalid is not None:
        raise MaterialFunctionError(
            f"{node_title(invalid)} must remain outside the reusable function"
        )
    incoming = [
        link
        for link in graph.links
        if link.target_node in selected and link.source_node not in selected
    ]
    outgoing = [
        link
        for link in graph.links
        if link.source_node in selected and link.target_node not in selected
    ]
    if not outgoing:
        raise MaterialFunctionError("The selection needs at least one connected output")

    function_graph = ShaderGraph("material_function", "")
    chosen = [nodes[node_id] for node_id in selected]
    minimum_x = min(node.x for node in chosen)
    minimum_y = min(node.y for node in chosen)
    maximum_x = max(node.x for node in chosen)
    center_x = sum(node.x for node in chosen) / len(chosen)
    center_y = sum(node.y for node in chosen) / len(chosen)
    for node in chosen:
        function_graph.add_node(
            node.kind,
            node_id=node.node_id,
            x=node.x - minimum_x + 260.0,
            y=node.y - minimum_y,
            properties=deepcopy(node).properties,
        )
    for link in graph.links:
        if link.source_node in selected and link.target_node in selected:
            function_graph.links.append(link)

    input_pins: list[MaterialFunctionPin] = []
    input_ids: dict[tuple[str, str], str] = {}
    input_nodes: dict[tuple[str, str], ShaderGraphNode] = {}
    for link in incoming:
        endpoint = (link.source_node, link.source_pin)
        target = nodes[link.target_node]
        target_pin = node_pin(target, contract, link.target_pin, "input")
        source = nodes[link.source_node]
        source_pin = node_pin(source, contract, link.source_pin, "output")
        if target_pin is None or source_pin is None:
            raise MaterialFunctionError("The selection contains an unavailable input pin")
        boundary = input_nodes.get(endpoint)
        if boundary is None:
            pin_id = (
                f"in_{len(input_pins)}_"
                f"{hashlib.sha1(f'{link.source_node}:{link.source_pin}'.encode()).hexdigest()[:10]}"
            )
            definition = MaterialFunctionPin(
                pin_id,
                f"{node_title(source)} · {source_pin.label}",
                source_pin.value_type,
                True,
            )
            input_pins.append(definition)
            input_ids[endpoint] = pin_id
            boundary = function_graph.add_node(
                "function_input",
                x=0.0,
                y=(len(input_pins) - 1) * 105.0,
                properties={
                    "pin_id": pin_id,
                    "display_name": definition.name,
                    "value_type": definition.value_type,
                },
            )
            input_nodes[endpoint] = boundary
        function_graph.connect(boundary.node_id, "value", link.target_node, link.target_pin)

    unique_outputs: dict[tuple[str, str], MaterialFunctionPin] = {}
    for link in outgoing:
        key = (link.source_node, link.source_pin)
        if key in unique_outputs:
            continue
        source = nodes[link.source_node]
        pin = node_pin(source, contract, link.source_pin, "output")
        if pin is None:
            raise MaterialFunctionError("The selection contains an unavailable output pin")
        pin_id = f"out_{len(unique_outputs)}_{hashlib.sha1(f'{link.source_node}:{link.source_pin}'.encode()).hexdigest()[:10]}"
        definition = MaterialFunctionPin(pin_id, pin.label, pin.value_type, False)
        unique_outputs[key] = definition
        boundary = function_graph.add_node(
            "function_output",
            x=maximum_x - minimum_x + 620.0,
            y=len(unique_outputs) * 105.0,
            properties={
                "pin_id": pin_id,
                "display_name": definition.name,
                "value_type": definition.value_type,
            },
        )
        function_graph.connect(link.source_node, link.source_pin, boundary.node_id, "value")

    asset = MaterialFunctionAsset(
        uuid4().hex,
        str(name).strip() or "Material Function",
        str(category).strip() or "User Functions",
        str(description),
        tuple(input_pins),
        tuple(unique_outputs.values()),
        function_graph,
        str(source_profile),
    )

    candidate = deepcopy(graph)
    candidate.nodes[:] = [node for node in candidate.nodes if node.node_id not in selected]
    candidate.links[:] = [
        link
        for link in candidate.links
        if link.source_node not in selected and link.target_node not in selected
    ]
    instance = candidate.add_node(
        "material_function",
        x=center_x,
        y=center_y,
        properties=material_function_node_properties(asset),
    )
    for (source_node, source_pin), pin_id in input_ids.items():
        candidate.connect(source_node, source_pin, instance.node_id, pin_id)
    for link in outgoing:
        candidate.connect(
            instance.node_id,
            unique_outputs[link.source_node, link.source_pin].pin_id,
            link.target_node,
            link.target_pin,
        )
    expand_material_functions(candidate, live_only=True)
    graph.nodes[:] = candidate.nodes
    graph.links[:] = candidate.links
    return asset, graph.node(instance.node_id)


__all__ = [
    "FUNCTION_VALUE_TYPES",
    "MATERIAL_FUNCTION_SCHEMA",
    "MATERIAL_FUNCTION_SCHEMA_VERSION",
    "MaterialFunctionAsset",
    "MaterialFunctionError",
    "MaterialFunctionPin",
    "expand_material_functions",
    "expand_material_function_node",
    "discover_material_functions",
    "extract_material_function",
    "material_function_from_node",
    "material_function_node_properties",
    "replace_material_function",
    "load_material_function",
    "validate_material_function",
    "validate_material_function_nodes",
]
