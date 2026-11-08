r"""Set TransitionData fields (EndType / exitFrame / startFrame / ...) of one state.

A state row *is* the BHVT transition (`State.mTransitions` holds the target), and
`State.TransitionMaps` is that transition's map -> TransitionData record.  The record
carries the timing:

    EndType = 1  -> motion end (exitFrame stays 0)
    EndType = 2  -> exit frame (`exitFrame` = the frame the transition fires)
    startFrame / interpolationFrame  -> window start and blend length

So "fire the transition at frame N" is `EndType=2, exitFrame=N`.

Editing goes through the editor's own `edit_state_transition`, which detaches a shared
map or data record into a private copy first, so unrelated states keep their behaviour.
Values are accepted as enum member names or raw numbers ("EndType=2", "exitFrame=54").

Verification mirrors the GUI path: only the addressed record may change, UVAR data must
stay identical, and REasy's rebuild() must be stable on the candidate.
"""
from dataclasses import dataclass
from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.validation import variable_snapshot
from file_handlers.motfsm.transitions import TransitionTables, patch_record, edit_state_transition
from file_handlers.motfsm.selection import resolve_node


@dataclass(frozen=True, kw_only=True)
class StateTransitionPlan:
    node: str
    state: int
    set: tuple[str, ...]


def build(source: bytes, plan: StateTransitionPlan) -> bytes:
    document = MotfsmFile()
    document.read(source)
    node = document.bhvt.nodes[resolve_node(document, plan.node)]

    def resolve_on(doc):
        row = doc.bhvt.nodes[resolve_node(doc, plan.node)]
        binding = doc.bindings.get(row.states[plan.state], 'TransitionMaps')
        tables = TransitionTables(doc)
        index = tables.resolve(binding.value)
        if index is None:
            raise ValueError(f'state {plan.state} on {row.name} has no transition-data record')
        return binding, tables, index

    if not 0 <= plan.state < len(node.states):
        raise ValueError(f'{node.name} has {len(node.states)} states; --state {plan.state} is out of range')
    _, _, data_index = resolve_on(document)
    before_fields = {field.name: field.value for field in TransitionTables(document).fields(data_index)}
    print(f'  {node.name} state[{plan.state}] transition-data #{data_index}')

    after = source
    for item in plan.set:
        name, _, text = item.partition('=')
        name, text = name.strip(), text.strip()
        if not name:
            raise ValueError(f'bad assignment {item!r}')
        doc = MotfsmFile()
        doc.read(after)
        binding, tables, index = resolve_on(doc)
        expected = patch_record(tables, index, name, text)
        updated = edit_state_transition(doc, binding, name, text)
        prepared = MotfsmFile()
        prepared.read(updated)
        saved_tables = TransitionTables(prepared)
        saved_index = saved_tables.resolve(resolve_on(prepared)[0].value)
        if saved_tables.record(saved_index)[4:] != expected[4:]:
            raise ValueError(f'{name}: value did not survive serialization')
        for i in range(doc.transition_data_count):
            if i != saved_index and saved_tables.record(i) != tables.record(i):
                raise ValueError(f'{name}: an unrelated transition-data record changed')
        if variable_snapshot(prepared) != variable_snapshot(doc):
            raise ValueError(f'{name}: UVAR data changed')
        if prepared.rebuild() != updated:
            raise ValueError(f'{name}: rebuild is not stable')
        after = updated
        now = saved_tables.fields(saved_index)
        print(f'  {name}: {before_fields.get(name)} -> '
              f'{[f.value for f in now if f.name == name]}')

    verified = MotfsmFile()
    verified.read(after)
    if verified.rebuild() != after:
        raise ValueError('rebuild is not stable on the candidate')
    return after
