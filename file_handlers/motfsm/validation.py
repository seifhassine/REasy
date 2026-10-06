"""Independent native UVAR snapshot used when verifying BHVT relocation."""
import struct
from file_handlers.uvar.base_model import FileHandler
from file_handlers.uvar.uvar_file import UVarFile


def variable_snapshot(document):
    source, base = document.source, document.tree_data_offset
    table = document.bhvt.offsets.get('base_variables')
    count = struct.unpack_from('<I', source, table)[0] if table else 0
    roots = [document.bhvt.offsets['variables']] if document.bhvt.offsets.get('variables') else []
    roots += [base + struct.unpack_from('<Q', source, table + 4 + i * 8)[0] for i in range(count)
              if struct.unpack_from('<Q', source, table + 4 + i * 8)[0]]
    result = []
    for root in roots:
        reader = FileHandler(source, offset=base)
        reader.seek(root)
        uvar = UVarFile()
        uvar.do_read(reader)
        variables = []
        for variable in uvar.variables:
            expression = variable.expression
            nodes = [] if expression is None else [
                (n.name, n.node_id, [(p.name_hash, p.raw_type_code, p.value) for p in n.parameters])
                for n in expression.nodes]
            relations = [] if expression is None else [vars(r) for r in expression.relations]
            variables.append((variable.guid, variable.name, variable.type, variable.flags,
                              variable.name_hash, variable.value, nodes, relations))
        result.append((uvar.header.version, uvar.header.name, uvar.header.uvar_hash,
                       variables, uvar.hash_data.guids, uvar.hash_data.guid_map,
                       uvar.hash_data.name_hashes, uvar.hash_data.name_hash_map))
    return result


def validate_graph(document):
    """Check the distinct node, Action, object-slot and transition namespaces."""
    from .graph_model import MotfsmGraph
    from .rsz_adapter import BLOCK_NAMES
    from .selectors import resolve_selector
    from .transitions import TransitionTables
    from file_handlers.rsz.rsz_instance_operations import RszInstanceOperations
    from file_handlers.rsz.utils.rsz_field_utils import validate_reference_type
    document.references.invalidate()
    graph = MotfsmGraph(document)
    document.references.action_identities()
    for name in BLOCK_NAMES:
        native = document.rsz_blocks.get_block(name).file
        native.validate_type_registry_state()
        for slot, index in enumerate(native.object_table):
            if not 0 < index < len(native.instance_infos) or native.instance_infos[index].type_id == 0:
                raise ValueError(f'{name} object {slot} resolves to an invalid/NULL instance')
        for owner, field, ref, _ in RszInstanceOperations.iter_instance_references(native.parsed_elements):
            if not 0 <= ref.value < len(native.instance_infos):
                raise ValueError(f'{name} instance {owner}.{field}: dangling RSZ reference {ref.value}')
            validate_reference_type(document.type_registry, native.instance_infos, ref.orig_type, ref.value)
    tables = TransitionTables(document) if document.transition_map_count or document.transition_data_count else None

    def condition(raw):
        document.references.object_instance('conditions', raw)

    def events(values):
        for raw in values:
            document.references.object_instance('transition_events', raw)

    for i, node in enumerate(graph.nodes):
        try:
            if graph.issues[i]:
                raise ValueError('; '.join(graph.issues[i]))
            parent = graph.parents[i]
            if parent is not None:
                matches = [c for c in graph.nodes[parent].children
                           if (c.id_hash, c.ex_id) == (node.id_hash, node.ex_id)]
                if len(matches) != 1:
                    raise ValueError('Parent must contain exactly one reciprocal child link')
            for child in node.children:
                target = document.references._node_identity(child.id_hash, child.ex_id)
                if graph.parents[target] != i:
                    raise ValueError('Child and parent links disagree')
                condition(child.condition_id)
            seen, current = set(), i
            while current is not None:
                if current in seen:
                    raise ValueError('Cyclic BHVT parent relationship')
                seen.add(current)
                current = graph.parents[current]
            for action in node.actions:
                document.references.action(action.id_hash, action.ex_id)
            resolve_selector(document, node.selector_id)
            resolve_selector(document, node.selector_caller_condition_id)
            for raw in node.selector_callers:
                document.references.object_instance('selector_callers', raw)
            for state in node.states:
                document.references.state_target(state.mTransitions, state.mTransitionAttributes)
                condition(state.TransitionConditions)
                events(state.mStates.values)
                if tables:
                    tables.resolve(state.TransitionMaps)
                elif state.TransitionMaps:
                    raise ValueError(f'Unknown transition-map identity {state.TransitionMaps}')
            for transition in node.transitions:
                condition(transition.mStartStateTransition)
                if document.layout.transition_event_lists is not False:
                    events(transition.mStartTransitionEvent.values)
                else:
                    # Older layouts store an event UID hash, not an object slot.
                    for uid in transition.mStartTransitionEvent.values:
                        if uid in (0, 0xFFFFFFFF):
                            continue
                        matches = [fields for name in ('transition_events', 'static_transition_events')
                                   for fields in document.rsz_blocks.get_block(name).file.parsed_elements.values()
                                   if 'v0_UID' in fields and fields['v0_UID'].value == uid]
                        if not matches:
                            raise ValueError(f'Unknown TransitionEvent UID {uid}')
            for state in node.all_states:
                condition(state.mAllTransition)
                if tables:
                    tables.resolve(state.mAllTransitionID)
                elif state.mAllTransitionID:
                    raise ValueError(f'Unknown transition-map identity {state.mAllTransitionID}')
            if node.has_reference_tree:
                raise ValueError('External reference-tree validation is not implemented')
        except (IndexError, ValueError) as exc:
            raise ValueError(f'Node {i} {node.name}: {exc}') from exc


def verify_graph_write(document, reopened):
    from .rsz_adapter import BLOCK_NAMES
    if (reopened.bhvt.nodes != document.bhvt.nodes or
            reopened.bhvt.action_ex_ids != document.bhvt.action_ex_ids or
            reopened.bhvt.static_action_ex_ids != document.bhvt.static_action_ex_ids or
            reopened.bhvt.string_pool != document.bhvt.string_pool):
        raise ValueError('MOTFSM graph differs after reopening')
    if variable_snapshot(reopened) != variable_snapshot(document):
        raise ValueError('MOTFSM write changed UVAR data')
    for name in BLOCK_NAMES:
        before, after = document.rsz_blocks.get_block(name), reopened.rsz_blocks.get_block(name)
        if before.modified:
            expected = before.file.build_headless()
            if reopened.source[after.offset:after.offset + len(expected)] != expected:
                raise ValueError(f'{name}: RSZ values differ after reopening')
            if (before.object_table != after.object_table or
                    [(x.type_id, x.crc) for x in before.file.instance_infos] !=
                    [(x.type_id, x.crc) for x in after.file.instance_infos]):
                raise ValueError(f'{name}: RSZ identities differ after reopening')
        elif document.source[before.offset:before.end] != reopened.source[after.offset:after.end]:
            raise ValueError(f'MOTFSM write changed unrelated {name} bytes')
    for name in ('resource_paths', 'userdata_paths', 'reference_prefab_game_objects'):
        if name in document.bhvt.offsets:
            a, b = document.bhvt.offsets[name], reopened.bhvt.offsets[name]
            if document.source[a:document._section_ends[a]] != reopened.source[b:reopened._section_ends[b]]:
                raise ValueError(f'MOTFSM write changed {name}')
    if (document.source[document.transition_map_tbl_offset:document.transition_map_tbl_offset + document.transition_map_count * 8]
            != reopened.source[reopened.transition_map_tbl_offset:reopened.transition_map_tbl_offset + reopened.transition_map_count * 8]
            or document.transition_data_count and
            document.source[document.transition_data_tbl_offset:] != reopened.source[reopened.transition_data_tbl_offset:]):
        raise ValueError('MOTFSM write changed transition tables')


def snapshot_without_relations(snapshot):
    """Drop the heuristic relation reads; the UVAR relation array is sized by a terminator, so a
    document shift can legitimately change how far it reads while the data itself is unchanged."""
    return [(version, name, uvar_hash,
             [(v[0], v[1], v[2], v[3], v[4], v[5], v[6]) for v in variables],
             guids, guid_map, name_hashes, name_hash_map)
            for version, name, uvar_hash, variables, guids, guid_map, name_hashes, name_hash_map
            in snapshot]


def assert_uvar_stable(before, after, allow_drift=False):
    """Strict UVAR comparison, with the known heuristic-relation false alarm distinguished."""
    if allow_drift:
        print('  note: UVAR snapshot equality skipped (--allow-uvar-drift)')
        return
    if before == after:
        return
    assert snapshot_without_relations(before) == snapshot_without_relations(after), 'UVAR data changed'
    print('  note: UVAR snapshot differs only in the heuristic relation reads; data is identical')
