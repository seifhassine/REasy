"""Structured FSM node inspection with native identities, references and field metadata."""
from dataclasses import asdict

from file_handlers.motfsm.graph_model import MotfsmGraph
from file_handlers.motfsm.selection import identity, resolve_node
from file_handlers.rsz.inspection import field_record

# The Rise motion-playing Action; its v4_MotionID is reported as the node's motion IDs.
MOTION_ACTION_CLASS = 'snow.PlayerPlayMotion2'


def instance_record(instance, enums):
    if instance is None:
        return None
    return {
        'instance': instance.index, 'class': instance.class_name,
        'fields': {field.name: field.value for field in instance.fields},
        'field_types': {field.name: field.type_name for field in instance.fields},
        'native_types': {field.name: getattr(field.data, 'orig_type', '') for field in instance.fields},
        'field_details': {field.name: field_record(
            field.data, instance.block.document.type_registry, enums, native_type=field.native_type)
            for field in instance.fields},
        'editable_fields': [field.name for field in instance.fields if field.binding is not None],
    }


def query_nodes(doc, *, selectors=(), name_contains=None, motion_id=None, action_class=None, derives_to=None,
                include_actions=False, include_states=False, limit=None, detail=False) -> dict:
    """Select nodes by name/motion/action class/target and describe them with native identities.

    ``detail`` adds full node records, events and incoming edges (the CLI ``dump`` command).
    """
    if limit is not None and limit < 0:
        raise ValueError('--limit must be nonnegative')
    graph = MotfsmGraph(doc)
    selected = {resolve_node(doc, s) for s in selectors} if selectors else None
    users, enums = {}, {}
    for index, node in enumerate(graph.nodes):
        for position, ref in enumerate(node.actions):
            users.setdefault((ref.id_hash, ref.ex_id), []).append({'node': index, 'position': position})
    matches = []
    for index, node in enumerate(graph.nodes):
        if selected is not None and index not in selected:
            continue
        if name_contains and name_contains.casefold() not in node.name.casefold():
            continue
        actions = [(ref, doc.references.action(ref.id_hash, ref.ex_id)) for ref in node.actions]
        motion_ids = [field.value for _, instance in actions if instance is not None
                      and instance.class_name == MOTION_ACTION_CLASS
                      for field in instance.fields if field.name == 'v4_MotionID']
        if motion_id is not None and motion_id not in motion_ids:
            continue
        if action_class and not any(instance is not None and action_class.casefold() in instance.class_name.casefold()
                                  for _, instance in actions):
            continue
        edges = graph.outgoing[index]
        if derives_to and not any(edge.target is not None and
                derives_to.casefold() in graph.nodes[edge.target].name.casefold() for edge in edges):
            continue
        record = {'index': index, 'name': node.name, 'path': graph.path(index), 'identity': identity(node),
                  'motion_ids': motion_ids, 'selector': node.selector_id,
                  'counts': {name: len(getattr(node, name)) for name in
                             ('actions', 'states', 'children', 'transitions', 'all_states')},
                  'issues': graph.issues[index]}
        if include_actions or detail:
            record['actions'] = []
            for position, (ref, instance) in enumerate(actions):
                row = {'position': position, 'identity': identity(ref),
                       'users': users[(ref.id_hash, ref.ex_id)], 'instance': instance_record(instance, enums)}
                record['actions'].append(row)
        if include_states or detail:
            record['relations'] = []
            for edge in edges:
                row = asdict(edge)
                row['target_identity'] = identity(graph.nodes[edge.target]) if edge.target is not None else None
                row['target_path'] = graph.path(edge.target) if edge.target is not None else None
                row['condition_instance'] = instance_record(doc.references.object_instance('conditions', edge.condition), enums)
                record['relations'].append(row)
        if detail:
            record['node'] = asdict(node)
            record['node'].pop('_action_span')
            record['state_events'] = [
                {'state': position, 'events': [
                    {'reference': raw, 'instance': instance_record(doc.references.object_instance('transition_events', raw), enums)}
                    for raw in state.mStates.values]}
                for position, state in enumerate(node.states)]
            record['start_events'] = [
                {'transition': position, 'events': [
                    {'reference': raw, 'instance': instance_record(doc.references.object_instance('transition_events', raw), enums)}
                    for raw in transition.mStartTransitionEvent.values]}
                for position, transition in enumerate(node.transitions)]
            record['incoming'] = [asdict(edge) for edge in graph.incoming[index]]
        matches.append(record)
    count = len(matches)
    if limit is not None:
        matches = matches[:limit]
    return {'total_nodes': len(graph.nodes),
            'matched': count, 'returned': len(matches), 'nodes': matches, 'enums': enums}
