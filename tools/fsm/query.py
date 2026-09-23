"""Structured inspection with native identities, references, and field types."""
from dataclasses import asdict

from file_handlers.motfsm.graph_model import MotfsmGraph
from .common import identity, resolve_node
from .editing import open_document


def instance_record(instance):
    if instance is None:
        return None
    return {
        'instance': instance.index, 'class': instance.class_name,
        'fields': {field.name: field.value for field in instance.fields},
        'field_types': {field.name: field.type_name for field in instance.fields},
        'native_types': {field.name: getattr(field.data, 'orig_type', '') for field in instance.fields},
        'editable_fields': [field.name for field in instance.fields if field.binding is not None],
    }


def inspect(args):
    if args.limit is not None and args.limit < 0:
        raise ValueError('--limit must be nonnegative')
    doc = open_document(args.source)
    graph = MotfsmGraph(doc)
    detail = args.command == 'dump'
    selected = {resolve_node(doc, s) for s in args.node} if args.node else None
    users = {}
    for index, node in enumerate(graph.nodes):
        for position, ref in enumerate(node.actions):
            users.setdefault((ref.id_hash, ref.ex_id), []).append({'node': index, 'position': position})
    matches = []
    for index, node in enumerate(graph.nodes):
        if selected is not None and index not in selected:
            continue
        if args.name and args.name.casefold() not in node.name.casefold():
            continue
        actions = [(ref, doc.references.action(ref.id_hash, ref.ex_id)) for ref in node.actions]
        motion_ids = [field.value for _, instance in actions if instance is not None
                      and instance.class_name == 'snow.PlayerPlayMotion2'
                      for field in instance.fields if field.name == 'v4_MotionID']
        if args.motion is not None and args.motion not in motion_ids:
            continue
        if args.klass and not any(instance is not None and args.klass.casefold() in instance.class_name.casefold()
                                  for _, instance in actions):
            continue
        edges = graph.outgoing[index]
        if args.derives_to and not any(edge.target is not None and
                args.derives_to.casefold() in graph.nodes[edge.target].name.casefold() for edge in edges):
            continue
        record = {'index': index, 'name': node.name, 'path': graph.path(index), 'identity': identity(node),
                  'motion_ids': motion_ids, 'selector': node.selector_id,
                  'counts': {name: len(getattr(node, name)) for name in
                             ('actions', 'states', 'children', 'transitions', 'all_states')},
                  'issues': graph.issues[index]}
        if args.actions or detail:
            record['actions'] = []
            for position, (ref, instance) in enumerate(actions):
                row = {'position': position, 'identity': identity(ref),
                       'users': users[(ref.id_hash, ref.ex_id)], 'instance': instance_record(instance)}
                record['actions'].append(row)
        if args.states or detail:
            record['relations'] = []
            for edge in edges:
                row = asdict(edge)
                row['target_identity'] = identity(graph.nodes[edge.target]) if edge.target is not None else None
                row['target_path'] = graph.path(edge.target) if edge.target is not None else None
                row['condition_instance'] = instance_record(doc.references.object_instance('conditions', edge.condition))
                record['relations'].append(row)
        if detail:
            record['node'] = asdict(node)
            record['node'].pop('_action_span')
            record['state_events'] = [
                {'state': position, 'events': [
                    {'reference': raw, 'instance': instance_record(doc.references.object_instance('transition_events', raw))}
                    for raw in state.mStates.values]}
                for position, state in enumerate(node.states)]
            record['start_events'] = [
                {'transition': position, 'events': [
                    {'reference': raw, 'instance': instance_record(doc.references.object_instance('transition_events', raw))}
                    for raw in transition.mStartTransitionEvent.values]}
                for position, transition in enumerate(node.transitions)]
            record['incoming'] = [asdict(edge) for edge in graph.incoming[index]]
        matches.append(record)
    count = len(matches)
    if args.limit is not None:
        matches = matches[:args.limit]
    return {'source': str(args.source.resolve()), 'total_nodes': len(graph.nodes),
            'matched': count, 'returned': len(matches), 'nodes': matches}


def render(result):
    print(f'{result["matched"]} match(es) of {result["total_nodes"]} nodes')
    for node in result['nodes']:
        print(f'[{node["index"]}] {node["path"]}  {node["identity"]}  motion={node["motion_ids"]}')
        for action in node.get('actions', []):
            instance = action['instance']
            print(f'  action[{action["position"]}] {action["identity"]} {instance["class"] if instance else "None"}')
            if instance:
                for name, value in instance['fields'].items():
                    print(f'    {name} = {value}')
        for edge in node.get('relations', []):
            condition = edge['condition_instance']
            print(f'  {edge["kind"]}[{edge["position"]}] -> {edge["target_path"]} '
                  f'condition={condition["class"] if condition else "always"}')
        for issue in node['issues']:
            print(f'  issue: {issue}')
