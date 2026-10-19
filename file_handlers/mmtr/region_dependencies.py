from copy import deepcopy
from dataclasses import replace
import hashlib
import json

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


class _Dependencies:
    def __init__(self, graph):
        self.graph = graph
        self.nodes = {node.node_id: node for node in graph.nodes}
        self.incoming = {}
        for link in graph.links:
            self.incoming.setdefault(link.target_node, {})[link.target_pin] = (link.source_node, link.source_pin)
        self.outputs = {node.properties['pin_id']: node.node_id for node in graph.nodes if node.kind == 'function_output'}
        self.memo = {}
        self.child_memo = {}
        self.source_memo = {}
        self.aliases = {}
        self.regions = {}
        self.prepared = False

    def prepare(self):
        if self.prepared:
            return
        pending = [(self, False)]
        while pending:
            body, ready = pending.pop()
            if body.prepared:
                continue
            if not ready:
                pending.append((body, True))
                for node in body.graph.nodes:
                    if node.kind in {'branch', 'repeat'}:
                        if node.node_id not in body.regions:
                            body.regions[node.node_id] = _Region(node, prepare=False)
                        region = body.regions[node.node_id]
                        pending.extend((child, False) for child in region.bodies)
            else:
                for region in body.regions.values():
                    region.prepare_states()
                body.prepared = True
                for pin in body.outputs:
                    body.output_alias(pin)
                    body.inputs((body.outputs[pin], 'value'))

    def canonical(self, key):
        self.prepare()
        trail = []
        while key not in self.aliases:
            trail.append(key)
            node = self.nodes[key[0]]
            incoming = self.incoming.get(node.node_id, {})
            if node.kind == 'function_output':
                key = incoming['value']
            elif node.kind in {'branch', 'repeat'}:
                source = self.region(node).alias_input(key[1])
                if source is None:
                    break
                key = incoming[source]
            else:
                break
        result = self.aliases.get(key, key)
        self.aliases.update((item, result) for item in trail)
        return result

    def output_alias(self, pin):
        key = self.canonical((self.outputs[pin], 'value'))
        node = self.nodes[key[0]]
        return node.properties['pin_id'] if node.kind == 'function_input' else None

    def region(self, node):
        if node.node_id not in self.regions:
            self.regions[node.node_id] = _Region(node)
        return self.regions[node.node_id]

    def children(self, key):
        result = self.child_memo.get(key)
        if result is None:
            result = self.child_memo[key] = self._children(key)
        return result

    def _children(self, key):
        node = self.nodes[key[0]]
        incoming = self.incoming.get(node.node_id, {})
        if node.kind in {'branch', 'repeat'}:
            inputs = self.region(node).inputs({key[1]})
            if node.kind == 'branch':
                inputs.add('condition')
            return tuple(self.value_source(value) for pin, value in incoming.items() if pin in inputs)
        return tuple(self.value_source(value) for value in incoming.values())

    def value_source(self, key):
        result = self.source_memo.get(key)
        if result is None:
            result = self.source_memo[key] = self._value_source(key)
        return result

    def _value_source(self, key):
        while self.nodes[key[0]].kind == 'branch':
            region = self.region(self.nodes[key[0]])
            if next(pin.value_type for pin in region.assets[0].outputs if pin.pin_id == key[1]) == 'execution':
                break
            source = region.alias_input(key[1])
            if source is None:
                break
            key = self.incoming[key[0]][source]
        return key

    def inputs(self, key):
        self.prepare()
        pending = [(key, False)]
        while pending:
            current, ready = pending.pop()
            if current in self.memo:
                continue
            node = self.nodes[current[0]]
            if node.kind == 'function_input':
                self.memo[current] = frozenset((node.properties['pin_id'],))
                continue
            children = self.children(current)
            if ready:
                self.memo[current] = frozenset().union(*(self.memo[child] for child in children))
            else:
                pending.append((current, True))
                pending.extend((child, False) for child in children if child not in self.memo)
        return self.memo[key]

    def output_inputs(self, outputs):
        return set().union(*(self.inputs((self.outputs[pin], 'value')) for pin in outputs))

    def trim(self, *, keep_inputs=(), cache_results=True):
        self.prepare()
        root = (self, None, frozenset(keep_inputs))
        pending, plans, trimmed = [(root, False)], {}, {}
        while pending:
            request, ready = pending.pop()
            if request in trimmed:
                continue
            body, selected_outputs, inputs = request
            if not ready:
                used = plans[request] = body.live_results(selected_outputs)
                pending.append((request, True))
                for identity, results in used.items():
                    if body.nodes[identity].kind in {'branch', 'repeat'}:
                        region = body.region(body.nodes[identity])
                        child_inputs, child_outputs = region.required(results)
                        pending.extend(((child, frozenset(child_outputs), frozenset(child_inputs)), False) for child in region.bodies)
            else:
                trimmed[request] = body.trim_selected(plans[request], inputs,
                    lambda child, results, child_inputs: trimmed[child, frozenset(results), frozenset(child_inputs)],
                    cache_results=cache_results)
        return trimmed[root]

    def live_results(self, outputs):
        roots = ([(self.outputs[pin], 'value') for pin in outputs] if outputs is not None else
                 [(node.node_id, 'value') for node in self.graph.nodes if node.kind in {'output', 'function_output', 'material_surface'}])
        live = set()
        pending = list(roots)
        while pending:
            key = pending.pop()
            if key in live:
                continue
            live.add(key)
            pending.extend(self.children(key))
        used = {}
        for identity, pin in live:
            used.setdefault(identity, set()).add(pin)
        return used

    def trim_selected(self, used, keep_inputs, trimmed, *, cache_results=True):
        selected = []
        unassigned = {}
        for node in self.graph.nodes:
            if node.node_id not in used and not (node.kind == 'function_input' and node.properties['pin_id'] in keep_inputs):
                continue
            if node.kind in {'branch', 'repeat'}:
                if node.kind == 'branch':
                    unassigned[node.node_id] = {pin for pin, source in self.incoming.get(node.node_id, {}).items()
                        if pin != 'condition' and self.nodes[self.value_source(source)[0]].kind == 'unassigned_value'}
                selected.append(replace(node, properties=self.region(node).properties(used[node.node_id],
                    unassigned.get(node.node_id, ()), trimmed=trimmed, cache_results=cache_results)))
            else:
                selected.append(deepcopy(node))
        identities = {node.node_id for node in selected}
        allowed = {identity: set().union(*(set(self.region(self.nodes[identity]).inputs({pin})) for pin in pins))
                   | ({'condition'} if self.nodes[identity].kind == 'branch' else set())
                   for identity, pins in used.items() if self.nodes[identity].kind in {'branch', 'repeat'}}
        rewired = (link if type(link) is ShaderGraphLink and source == (link.source_node, link.source_pin)
                   else replace(link, source_node=source[0], source_pin=source[1])
                   for link in self.graph.links for source in (self.value_source((link.source_node, link.source_pin)),))
        links = [link for link in rewired if link.source_node in identities and link.target_node in identities
                 and (link.target_node not in allowed or link.target_pin in allowed[link.target_node])
                 and link.target_pin not in unassigned.get(link.target_node, ())]
        result = replace(self.graph, nodes=selected, links=links)
        cache = region_derived_cache('specialized_graphs') if cache_results else None
        if cache is not None:
            cache[_graph_key(result)] = result
        return result


class _Region:
    def __init__(self, node, *, prepare=True):
        self.node = node
        self.continuation = None
        self.states = ()
        if node.kind == 'repeat':
            body, states, self.continuation = loop_body(node)
            self.assets = (body,)
        else:
            self.assets = branch_arms(node)
            states = ()
        self.bodies = tuple(_Dependencies(asset.graph) for asset in self.assets)
        self.declared_states = states
        self._required_memo = {}
        if prepare:
            for body in self.bodies:
                body.prepare()
            self.prepare_states()

    def prepare_states(self):
        if self.declared_states:
            body = self.bodies[0]
            self.states = tuple((source, target) for source, target in self.declared_states if body.output_alias(target) != source)

    def alias_input(self, output):
        sources = [body.output_alias(output) for body in self.bodies]
        source = sources[0]
        if source is None or any(item != source for item in sources):
            return None
        return None if source in dict(self.states) else source

    def required(self, outputs):
        key = (frozenset(outputs), self.states)
        memo = self._required_memo
        if key not in memo:
            inputs, outputs_ = self._required(outputs)
            memo[key] = (frozenset(inputs), frozenset(outputs_))
        inputs, outputs_ = memo[key]
        return set(inputs), set(outputs_)

    def _required(self, outputs):
        outputs = set(outputs)
        if self.continuation is not None:
            outputs.add(self.continuation)
        inputs = set().union(*(body.output_inputs(outputs) for body in self.bodies))
        pending = set(inputs)
        updates = dict(self.states)
        while pending:
            source = pending.pop()
            target = updates.get(source)
            if target is not None and target not in outputs:
                outputs.add(target)
                additions = self.bodies[0].output_inputs({target}) - inputs
                inputs.update(additions)
                pending.update(additions)
        return inputs, outputs

    def inputs(self, outputs):
        return self.required(outputs)[0]

    def properties(self, outputs, unassigned, *, trimmed, cache_results):
        inputs, outputs = self.required(outputs)
        arms, graphs = [], []
        declarations = REGION_ASSET_FIELDS[self.node.kind]
        for asset, body, declaration in zip(self.assets, self.bodies, declarations):
            graph = trimmed(body, outputs, inputs)
            if unassigned:
                graph = replace(graph, nodes=[replace(node, kind='unassigned_value', properties=dict(value_type=node.properties['value_type']))
                                             if node.kind == 'function_input' and node.properties['pin_id'] in unassigned else node
                                             for node in graph.nodes])
            graphs.append(graph)
            arms.append(_compiler_function_asset(asset.function_id, asset.name,
                asset.category, asset.description,
                tuple(pin for pin in asset.inputs if pin.pin_id in inputs and pin.pin_id not in unassigned),
                tuple(pin for pin in asset.outputs if pin.pin_id in outputs),
                graph, asset.source_profile, reuse=self.node.properties[declaration]))
        if self.node.kind == 'repeat':
            properties = loop_properties(arms[0], [(source, target) for source, target in self.states if source in inputs], self.continuation)
        else:
            properties = branch_properties(*arms, name=self.node.properties.get('display_name', 'Branch'))
        if cache_results and not unassigned:
            _cache_published_bodies(self.node.kind, properties, graphs)
        return properties


def specialize_regions(graph, *, keep_inputs=True):
    cache = region_derived_cache('specialized_graphs')
    key = _graph_key(graph) if cache is not None and keep_inputs else None
    if key is not None and key in cache:
        return cache[key]
    result = _Dependencies(graph).trim(
        keep_inputs={node.properties['pin_id'] for node in graph.nodes if node.kind == 'function_input'} if keep_inputs else (),
        cache_results=keep_inputs)
    if key is not None:
        cache[key] = result
    return result
