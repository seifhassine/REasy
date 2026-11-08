"""Command adapters for `fsm query` and `fsm dump`."""
from file_handlers.motfsm.query import query_nodes
from file_handlers.motfsm.scalar_editing import open_document


def inspect(args):
    result = query_nodes(open_document(args.source), selectors=args.node or (), name_contains=args.name,
                         motion_id=args.motion, action_class=args.klass, derives_to=args.derives_to,
                         include_actions=args.actions, include_states=args.states, limit=args.limit,
                         detail=args.command == 'dump')
    return {'source': str(args.source.resolve()), **result}


def render(result):
    print(f'{result["matched"]} match(es) of {result["total_nodes"]} nodes')
    for node in result['nodes']:
        print(f'[{node["index"]}] {node["path"]}  {node["identity"]}  motion={node["motion_ids"]}')
        for action in node.get('actions', []):
            instance = action['instance']
            print(f'  action[{action["position"]}] {action["identity"]} {instance["class"] if instance else "None"}')
            if instance:
                for name, value in instance['fields'].items():
                    detail = instance['field_details'][name]
                    label = detail.get('enum_name')
                    print(f'    {name} = {label} ({value})' if label is not None else f'    {name} = {value}')
        for edge in node.get('relations', []):
            condition = edge['condition_instance']
            print(f'  {edge["kind"]}[{edge["position"]}] -> {edge["target_path"]} '
                  f'condition={condition["class"] if condition else "always"}')
        for issue in node['issues']:
            print(f'  issue: {issue}')
