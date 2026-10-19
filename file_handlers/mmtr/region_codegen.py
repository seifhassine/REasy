import hashlib
import json

from .material_region_read import region_derived_cache


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def region_assets(node):
    if node.kind == 'branch':
        from .material_branch import branch_arms
        return branch_arms(node)
    if node.kind == 'repeat':
        from .material_loop import loop_body
        return (loop_body(node)[0],)
    if node.kind == 'material_function':
        from .material_function import material_function_from_node
        return (material_function_from_node(node),)
    return ()


def _ports(asset):
    return {pin.pin_id: (direction, index) for direction, pins in
            (('input', asset.inputs), ('output', asset.outputs)) for index, pin in enumerate(pins)}


def _region(node, assets, keys):
    properties = {key: value for key, value in node.properties.items() if key != 'display_name'}
    if node.kind == 'branch':
        properties.update(when_true=keys[id(assets[0])][1], when_false=keys[id(assets[1])][1])
    elif node.kind == 'repeat':
        ports = _ports(assets[0])
        properties.update(body=keys[id(assets[0])][1],
            states=[dict(input=ports[row['input']], output=ports[row['output']]) for row in properties['states']],
            continuation=ports[properties['continuation']])
    else:
        properties.update(function=keys[id(assets[0])][1])
        properties.pop('function_hash', None)
    return properties


def function_codegen_key(asset):
    shared = region_derived_cache('function_codegen_keys')
    keys = shared if shared is not None else {}
    pending = [(asset, False)]
    children = {}
    while pending:
        current, ready = pending.pop()
        if id(current) in keys:
            continue
        if not ready:
            children[id(current)] = {node.node_id: arms for node in current.graph.nodes
                                     if (arms := region_assets(node))}
            pending.append((current, True))
            pending.extend((arm, False) for arms in children[id(current)].values() for arm in arms)
            continue
        graph = current.graph
        identities = {node.node_id: index for index, node in enumerate(graph.nodes)}
        ports = _ports(current)
        node_ports = {identity: _ports(arms[0]) for identity, arms in children[id(current)].items()}
        nodes = []
        for node in graph.nodes:
            arms = children[id(current)].get(node.node_id)
            properties = (_region(node, arms, keys) if arms else
                          {key: value for key, value in node.properties.items() if key != 'display_name'})
            if node.kind in {'function_input', 'function_output'}:
                properties['pin_id'] = ports[properties['pin_id']]
            nodes.append((node.kind, properties))
        def endpoint(identity, pin):
            return identities[identity], node_ports.get(identity, {}).get(pin, pin)
        signature = lambda pins: [(pin.value_type, pin.required, pin.default) for pin in pins]
        value = (current.source_profile, signature(current.inputs), signature(current.outputs),
                 graph.stage, graph.contract_fingerprint, nodes,
                 [(endpoint(link.source_node, link.source_pin), endpoint(link.target_node, link.target_pin)) for link in graph.links])
        keys[id(current)] = current, _digest(value)
    return keys[id(asset)][1]


def region_codegen_key(node):
    assets = region_assets(node)
    keys = {id(asset): (asset, function_codegen_key(asset)) for asset in assets}
    return _digest((node.kind, _region(node, assets, keys)))
