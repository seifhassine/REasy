"""Dump one BHVT node completely, with its Action instances and shared-reference counts.

usage: python fsm_node_dump.py <file.motfsm2.43> <NODE_NAME> [--actions-of NAME ...]
"""
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

REASY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REASY))
sys.setrecursionlimit(100000)
from file_handlers.motfsm.motfsm_file import MotfsmFile      # noqa: E402
from file_handlers.motfsm.graph_model import MotfsmGraph     # noqa: E402


def open_fsm(path):
    data = Path(path).read_bytes()
    fsm = MotfsmFile()
    fsm.read(data)
    graph = MotfsmGraph(SimpleNamespace(bhvt=fsm.bhvt, references=fsm.references))
    return data, fsm, graph


def action_users(fsm, graph):
    """(id_hash, ex_id) -> node indexes referencing that Action identity."""
    users = {}
    for index, node in enumerate(graph.nodes):
        for position, reference in enumerate(node.actions):
            users.setdefault((reference.id_hash, reference.ex_id), []).append((index, position))
    return users


def dump_node(data, fsm, graph, named):
    nodes = graph.nodes
    matches = [i for i, n in enumerate(nodes) if n.name == named]
    if not matches:
        raise SystemExit(f'node not found: {named}')
    users = action_users(fsm, graph)
    for index in matches:
        node = nodes[index]
        print(f'==== {node.name}  index={index}  identity={graph.identity(index)} ====')
        print(f'  parent={graph.parents[index]} '
              f'({nodes[graph.parents[index]].name if graph.parents[index] is not None else None}) '
              f'raw_parent=0x{node.parent:08X}:{node.parent_ex}  path={graph.path(index)}')
        print(f'  name_index={node.name_index} name_hash=0x{node.name_hash:08X} fullname_hash=0x{node.fullname_hash:08X}')
        print(f'  priority={node.priority} node_attribute=0x{node.node_attribute:X} work_flags=0x{node.work_flags:X} '
              f'is_fsm={node.is_fsm} is_branch={node.is_branch} is_end={node.is_end} reference_tree_index={node.reference_tree_index}')
        print(f'  tags={node.tags} selector_id={node.selector_id} selector_callers={node.selector_callers} '
              f'selector_caller_condition_id={node.selector_caller_condition_id}')
        print(f'  children({len(node.children)}):')
        for position, child in enumerate(node.children):
            target = None
            try:
                target = fsm.references.node_index(child.id_hash, child.ex_id)
            except (ValueError, IndexError) as exc:
                target = f'ERR {exc}'
            label = nodes[target].name if isinstance(target, int) else target
            print(f'    child[{position}] 0x{child.id_hash:08X}:{child.ex_id} condition_id={child.condition_id} -> {label}')
        print(f'  actions({len(node.actions)}):')
        for position, reference in enumerate(node.actions):
            instance = fsm.references.action(reference.id_hash, reference.ex_id)
            shared = users[(reference.id_hash, reference.ex_id)]
            fields = {f.name: f.value for f in instance.fields} if instance else {}
            print(f'    action[{position}] identity=0x{reference.id_hash:08X}:{reference.ex_id} '
                  f'class={instance.class_name if instance else None} '
                  f'shared_by={[(nodes[i].name, p) for i, p in shared]}')
            for name, value in fields.items():
                print(f'        {name} = {value!r}')
        print(f'  states({len(node.states)}):')
        for position, state in enumerate(node.states):
            print(f'    state[{position}] mStates={state.mStates.values} mTransitions={state.mTransitions} '
                  f'TransitionConditions={state.TransitionConditions} TransitionMaps={state.TransitionMaps} '
                  f'mTransitionAttributes={state.mTransitionAttributes} mStatesEx={state.mStatesEx}')
        print(f'  transitions({len(node.transitions)}):')
        for position, transition in enumerate(node.transitions):
            target = None
            try:
                target = fsm.references.node_index(transition.mStartState, transition.mStartStateEx)
            except (ValueError, IndexError) as exc:
                target = f'ERR {exc}'
            label = nodes[target].name if isinstance(target, int) else target
            print(f'    transition[{position}] events={transition.mStartTransitionEvent.values} '
                  f'mStartState=0x{transition.mStartState:08X}:{transition.mStartStateEx} '
                  f'mStartStateTransition={transition.mStartStateTransition} -> {label}')
        print(f'  all_states({len(node.all_states)}):')
        for position, state in enumerate(node.all_states):
            target = None
            try:
                target = fsm.references.node_index(state.mAllState, state.mAllStateEx)
            except (ValueError, IndexError) as exc:
                target = f'ERR {exc}'
            label = nodes[target].name if isinstance(target, int) else target
            print(f'    all[{position}] mAllState=0x{state.mAllState:08X}:{state.mAllStateEx} '
                  f'mAllTransition={state.mAllTransition} mAllTransitionID={state.mAllTransitionID} '
                  f'mAllTransitionAttributes={state.mAllTransitionAttributes} -> {label}')


if __name__ == '__main__':
    path, name = sys.argv[1], sys.argv[2]
    data, fsm, graph = open_fsm(path)
    for target in [name, *sys.argv[3:]]:
        dump_node(data, fsm, graph, target)
