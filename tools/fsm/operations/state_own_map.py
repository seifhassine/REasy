"""Give an existing state an independent map ID, reusing its transition data."""
from file_handlers.motfsm.map_ownership import detach_state_maps
from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.validation import assert_uvar_stable, variable_snapshot
from tools.fsm.common import resolve_node


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--node', required=True, help='node owning the state')
    parser.add_argument('--target', required=True, help='node the state derives to (identifies the state)')


def run(args):
    source = args.source.resolve().read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    node_index = resolve_node(doc, args.node)
    node = doc.bhvt.nodes[node_index]
    target = doc.bhvt.nodes[resolve_node(doc, args.target)]
    states = [i for i, state in enumerate(node.states)
              if (state.mTransitions, state.mTransitionAttributes) == (target.id_hash, target.ex_id)]
    if len(states) != 1:
        raise ValueError(f'{node.name} has {len(states)} states pointing at {target.name} (ex={target.ex_id})')
    output = detach_state_maps(source, [(node_index, states[0])], doc.type_registry)
    verified = MotfsmFile()
    verified.set_rsz_type_info_path(doc.type_registry.json_path)
    verified.read(output)
    assert_uvar_stable(variable_snapshot(doc), variable_snapshot(verified), args.allow_uvar_drift)
    return output
