from dataclasses import replace
import hashlib
import json

import fastgraph

from file_handlers.sdf.shader_graph import ShaderGraphLink

from .material_branch import branch_arms, branch_properties
from .material_loop import loop_body, loop_properties
from .material_function import _compiler_function_asset
from .material_region_read import REGION_ASSET_FIELDS, read_region, region_derived_cache, region_identity


def _graph_key(graph):
    document = graph.to_dict()
    for node in document['nodes']:
        if node['kind'] in {'branch', 'repeat'}:
            node['properties'] = region_identity(node['kind'], node['properties'])
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':')).encode()).digest()


def _cache_published_bodies(kind, properties, graphs):
    cache = region_derived_cache('specialized_graphs')
    if cache is None:
        return
    result = read_region(kind, properties)
    assets = result if kind == 'branch' else (result[0],)
    for asset, graph in zip(assets, graphs):
        cache[_graph_key(asset.graph)] = graph


def _region_properties(region, unassigned, trimmed, cache_results):
    node, assets, states, continuation, inputs, outputs, children = region
    arms, graphs = [], []
    for asset, child, declaration in zip(assets, children, REGION_ASSET_FIELDS[node.kind]):
        graph = trimmed[child]
        if unassigned:
            graph = replace(graph, nodes=[
                replace(item, kind='unassigned_value', properties=dict(value_type=item.properties['value_type']))
                if item.kind == 'function_input' and item.properties['pin_id'] in unassigned else item
                for item in graph.nodes])
        graphs.append(graph)
        arms.append(_compiler_function_asset(asset.function_id, asset.name,
            asset.category, asset.description,
            tuple(pin for pin in asset.inputs if pin.pin_id in inputs and pin.pin_id not in unassigned),
            tuple(pin for pin in asset.outputs if pin.pin_id in outputs),
            graph, asset.source_profile, reuse=node.properties[declaration]))
    if node.kind == 'repeat':
        properties = loop_properties(arms[0], [(source, target) for source, target in states if source in inputs],
                                     continuation)
    else:
        properties = branch_properties(*arms, name=node.properties.get('display_name', 'Branch'))
    if cache_results and not unassigned:
        _cache_published_bodies(node.kind, properties, graphs)
    return properties


def _trimmed(plan, trimmed, cache_results):
    graph, kept, links, regions, replaced, failure = plan
    properties = {identity: _region_properties(region, unassigned, trimmed, cache_results)
                  for identity, region, unassigned, _ in regions}
    if failure is not None:
        raise failure
    if (len(kept) == len(graph.nodes) and links == graph.links
            and all(all(properties[identity][name] is node.properties[name] for name in REGION_ASSET_FIELDS[node.kind])
                    and properties[identity] == node.properties for identity, _, _, node in regions)):
        result = graph
    else:
        for position, identity in replaced:
            kept[position] = replace(kept[position], properties=properties[identity])
        result = replace(graph, links=links, nodes=kept)
    cache = region_derived_cache('specialized_graphs') if cache_results else None
    if cache is not None:
        cache[_graph_key(result)] = result
    return result


def specialize_regions(graph, *, keep_inputs=True):
    cache = region_derived_cache('specialized_graphs')
    key = _graph_key(graph) if cache is not None and keep_inputs else None
    if key is not None and key in cache:
        return cache[key]
    keep = [node.properties['pin_id'] for node in graph.nodes if node.kind == 'function_input'] if keep_inputs else ()
    trimmed = []
    for plan in fastgraph.region_plan(graph, keep, branch_arms, loop_body, ShaderGraphLink, replace):
        trimmed.append(_trimmed(plan, trimmed, keep_inputs))
    result = trimmed[-1]
    if key is not None:
        cache[key] = result
    return result
