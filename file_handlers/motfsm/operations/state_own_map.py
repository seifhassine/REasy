"""Give an existing state an independent map ID, reusing its transition data."""
from dataclasses import dataclass
from file_handlers.motfsm.map_ownership import detach_state_maps
from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.validation import assert_uvar_stable, variable_snapshot
from file_handlers.motfsm.selection import resolve_node


@dataclass(frozen=True, kw_only=True)
class StateOwnMapPlan:
    allow_uvar_drift: bool = False
    node: str
    target: str


def build(source: bytes, plan: StateOwnMapPlan) -> bytes:
    doc = MotfsmFile()
    doc.read(source)
    node_index = resolve_node(doc, plan.node)
    node = doc.bhvt.nodes[node_index]
    target = doc.bhvt.nodes[resolve_node(doc, plan.target)]
    states = [i for i, state in enumerate(node.states)
              if (state.mTransitions, state.mTransitionAttributes) == (target.id_hash, target.ex_id)]
    if len(states) != 1:
        raise ValueError(f'{node.name} has {len(states)} states pointing at {target.name} (ex={target.ex_id})')
    output = detach_state_maps(source, [(node_index, states[0])], doc.type_registry)
    verified = MotfsmFile()
    verified.set_rsz_type_info_path(doc.type_registry.json_path)
    verified.read(output)
    assert_uvar_stable(variable_snapshot(doc), variable_snapshot(verified), plan.allow_uvar_drift)
    return output
