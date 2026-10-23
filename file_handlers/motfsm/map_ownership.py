"""Assign independent map identities while retaining native transition records."""
import struct

from .motfsm_file import MotfsmFile
from .serialization import serialize_nodes, splice_document
from .transitions import TransitionTables


def detach_state_maps(raw, locations, registry=None):
    """Return verified FSM bytes with a fresh map ID for each (node, state).

    Locations are zero-based table positions. Data records remain shared and
    byte-identical. ``registry`` is an optional TypeRegistry, as used by the
    caller's native model. No files are written and the input is not mutated.
    """
    locations = list(locations)
    if not locations:
        return raw
    if len(set(locations)) != len(locations):
        raise ValueError('A state location may only be selected once')
    file = MotfsmFile()
    if registry is not None:
        file.set_rsz_type_info_path(registry.json_path)
    file.read(raw)
    tables = TransitionTables(file)
    if list(tables.maps) != sorted(tables.maps):
        raise ValueError('Transition-map table is not sorted')
    cursor = max(tables.maps, default=0) + 1
    if cursor + len(locations) > 0xFFFFFFFF:
        raise ValueError('Transition-map identity space exhausted')
    entries = []
    for node_index, state_index in locations:
        if not 0 <= node_index < len(file.bhvt.nodes):
            raise ValueError(f'Node index out of range: {node_index}')
        states = file.bhvt.nodes[node_index].states
        if not 0 <= state_index < len(states):
            raise ValueError(f'State index out of range: {state_index}')
        state = states[state_index]
        target = tables.resolve(state.TransitionMaps)
        if target is None:
            raise ValueError(f'State {node_index}:{state_index} has no transition map')
        entries.append(struct.pack('<Ii', cursor, target))
        state.TransitionMaps = cursor
        cursor += 1
    start = file.bhvt.offsets['nodes']
    payload = serialize_nodes(file)
    assert len(payload) == file.bhvt.node_data_end - start
    insert = file.transition_map_tbl_offset + file.transition_map_count * 8
    added = b''.join(entries)
    gap = (-(insert + len(added))) % 16
    output = splice_document(file, [(start, file.bhvt.node_data_end, payload),
                                   (insert, file.transition_data_tbl_offset, added + bytes(gap))])
    struct.pack_into('<i', output, 48, file.transition_map_count + len(entries))
    output = bytes(output)
    rebuilt = MotfsmFile()
    rebuilt.set_rsz_type_info_path(file.type_registry.json_path)
    rebuilt.read(output)
    assert rebuilt.bhvt.nodes == file.bhvt.nodes
    assert rebuilt.build_validated() == output
    result = TransitionTables(rebuilt)
    assert rebuilt.transition_map_count == file.transition_map_count + len(entries)
    assert rebuilt.transition_data_count == file.transition_data_count
    assert all(result.maps[key] == row for key, row in tables.maps.items())
    assert all(result.record(i) == tables.record(i) for i in range(file.transition_data_count))
    return output
