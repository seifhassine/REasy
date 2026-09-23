"""Give one existing state its own transition-map id (repair a shared `TransitionMaps`).

Native BHVT data keeps `TransitionMaps` strictly 1:1 with states (LongSword: 10684 states /
10684 ids / 0 shared).  When two states share a map id the engine resolves the shared transition
to the older binding, so a derivation can silently go to the wrong node.  This tool appends a new
map-table entry that points at the **same data record** the state already used and rebinds only
that one state - the data table, every other state and every block stay byte-identical.

"""
import struct


from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.validation import assert_uvar_stable, variable_snapshot

HEADER_MAP_COUNT, HEADER_DATA_COUNT = 48, 52
TRANSITION_DATA_SIZE = 36
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from tools.fsm.common import resolve_node


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--node', required=True, help='node owning the state')
    parser.add_argument('--target', required=True, help='node the state derives to (identifies the state)')


def run(args):
    TARGET_FILE = args.source.resolve()
    source = TARGET_FILE.read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    nodes = doc.bhvt.nodes

    def node_index(text):
        return resolve_node(doc, text)

    node = nodes[node_index(args.node)]
    target = nodes[node_index(args.target)]
    states = [i for i, state in enumerate(node.states) if state.mTransitions == target.id_hash]
    if len(states) != 1:
        raise SystemExit(f'{node.name} has {len(states)} states pointing at {target.name}')
    position = states[0]
    state = node.states[position]

    map_offset, map_count = doc.transition_map_tbl_offset, doc.transition_map_count
    data_offset, data_count = doc.transition_data_tbl_offset, doc.transition_data_count
    map_entries = [struct.unpack_from('<Ii', source, map_offset + i * 8) for i in range(map_count)]
    assert all(map_entries[i][0] <= map_entries[i + 1][0] for i in range(map_count - 1)), 'map table unsorted'
    assert map_offset % 16 == 0 and data_offset % 16 == 0, 'tail tables are not 16-byte aligned'
    old_gap = data_offset - (map_offset + map_count * 8)
    assert old_gap in (0, 8), f'unexpected map/data gap {old_gap}'
    entry = next((e for e in map_entries if e[0] == state.TransitionMaps), None)
    if entry is None:
        raise SystemExit(f'{node.name}[{position}] map id {state.TransitionMaps} is not in the map table')
    data_index = entry[1]
    owners = [(n.name, i) for n in nodes for i, s in enumerate(n.states) if s.TransitionMaps == state.TransitionMaps]
    print(f'  {node.name}[{position}] -> {target.name}: map {state.TransitionMaps} -> data#{data_index}; '
          f'shared by {owners}')
    new_map_id = max({e[0] for e in map_entries} | {s.TransitionMaps for n in nodes for s in n.states if s.TransitionMaps}) + 1
    assert new_map_id != 0xFFFFFFFF
    new_map_bytes = struct.pack('<Ii', new_map_id, data_index)
    state.TransitionMaps = new_map_id
    print(f'  new map id {new_map_id} -> data#{data_index} (data record reused, data table untouched)')

    nodes_start = doc.bhvt.offsets['nodes']
    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)
    state.TransitionMaps = entry[0]
    assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], 'node writer does not reproduce the source'
    state.TransitionMaps = new_map_id
    nodes_data = serialize_nodes(doc)
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
    map_insert = map_offset + map_count * 8
    new_gap = (16 - (map_insert + len(new_map_bytes)) % 16) % 16
    assert new_gap in (0, 8), f'unexpected new gap {new_gap}'
    splices = sorted([(nodes_start, node_tail, nodes_data),
                      (map_insert, data_offset, new_map_bytes + bytes(new_gap))])
    map_delta = (len(new_map_bytes) + new_gap) - old_gap
    output = splice_document(doc, splices)
    struct.pack_into('<i', output, HEADER_MAP_COUNT, map_count + 1)
    assert struct.unpack_from('<i', output, HEADER_DATA_COUNT)[0] == data_count
    tree_growth = len(output) - len(source) - map_delta
    assert tree_growth == 0, 'a map-id repair must not resize the BHVT tree'
    assert len(output) >= len(source), 'the candidate shrank'
    output = bytes(output)

    # ---------------------------------------------------------------- verification
    verified, original = MotfsmFile(), MotfsmFile()
    verified.read(output)
    original.read(source)
    assert verified.bhvt.nodes == doc.bhvt.nodes, 'reopened node table differs from the authored model'
    assert verified.bhvt.action_ex_ids == original.bhvt.action_ex_ids
    assert verified.bhvt.static_action_ex_ids == original.bhvt.static_action_ex_ids
    for name in BLOCK_NAMES:
        old, new = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
        assert source[old.offset:old.end] == output[new.offset:new.end], f'{name} changed unexpectedly'
    assert verified.transition_map_count == original.transition_map_count + 1
    assert verified.transition_data_count == original.transition_data_count
    assert output[verified.transition_map_tbl_offset:
                  verified.transition_map_tbl_offset + original.transition_map_count * 8] == \
        source[original.transition_map_tbl_offset:
               original.transition_map_tbl_offset + original.transition_map_count * 8], 'map table changed'
    assert output[verified.transition_data_tbl_offset:
                  verified.transition_data_tbl_offset + verified.transition_data_count * TRANSITION_DATA_SIZE] == \
        source[original.transition_data_tbl_offset:
               original.transition_data_tbl_offset + original.transition_data_count * TRANSITION_DATA_SIZE], \
        'transition data table changed'
    assert_uvar_stable(variable_snapshot(original), variable_snapshot(verified), args.allow_uvar_drift)
    assert verified.rebuild() == output, 'REasy rebuild is not stable on the candidate'

    verified_node_index = node_index(args.node)
    for index, previous in enumerate(original.bhvt.nodes):
        current = verified.bhvt.nodes[index]
        if index == verified_node_index:
            assert len(current.states) == len(previous.states)
            for i, (a, b) in enumerate(zip(previous.states, current.states)):
                if i == position:
                    assert b.TransitionMaps == new_map_id and b.mTransitions == previous.states[i].mTransitions
                    assert (b.TransitionConditions, b.mStates.values, b.mTransitionAttributes, b.mStatesEx) == \
                        (a.TransitionConditions, a.mStates.values, a.mTransitionAttributes, a.mStatesEx)
                else:
                    assert a == b, f'{args.node} state[{i}] changed'
        else:
            assert current == previous, f'node {index} {previous.name} changed'
    # no state may share a map id with a state that has a different target
    seen = {}
    for n in verified.bhvt.nodes:
        for i, s in enumerate(n.states):
            if not s.TransitionMaps:
                continue
            if s.TransitionMaps in seen and seen[s.TransitionMaps][0] != s.mTransitions:
                if node.name in (n.name, seen[s.TransitionMaps][1]):
                    raise AssertionError(f'transition map {s.TransitionMaps} still conflicts: '
                                     f'{seen[s.TransitionMaps]} vs {(s.mTransitions, n.name, i)}')
            seen[s.TransitionMaps] = (s.mTransitions, n.name, i)
    shared = len({s.TransitionMaps for n in verified.bhvt.nodes for s in n.states if s.TransitionMaps})
    total = sum(1 for n in verified.bhvt.nodes for s in n.states if s.TransitionMaps)
    assert shared <= verified.transition_map_count, 'more referenced map ids than table entries'
    print(f'  Verified {total} states / {shared} map IDs; no target conflicts')

    return bytes(output)
