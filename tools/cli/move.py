"""Cross-resource validation and staged retiming of an explicitly selected move."""
import json
import struct
from pathlib import Path
import tempfile

from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from file_handlers.motion.mhr_editing import validate_slot_order
from file_handlers.motion.mhr_retime import retime_motion, round_frame, properties
from file_handlers.rcol.rcol_handler import RcolHandler
from tools.fsm.common import resolve_node
from tools.fsm.editing import open_document, save_edits
from .runtime import MHR_REGISTRY, qt_application


def load(path):
    spec = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(spec, dict) or set(spec) != {'motion', 'fsm', 'rcol'}:
        raise ValueError('Move plan must contain motion, fsm, and rcol objects')
    for key in ('motion', 'fsm', 'rcol'):
        spec[key]['path'] = (path.parent / spec[key]['path']).resolve()
    if not isinstance(spec['fsm']['nodes'], list) or not spec['fsm']['nodes']:
        raise ValueError('fsm.nodes must be a nonempty list of exact node selectors')
    if not isinstance(spec['rcol']['field0'], list):
        raise ValueError('rcol.field0 must be a list of hit references')
    motion = CODEC.parse(spec['motion']['path'].read_bytes())
    validate_slot_order(motion)
    slots = [slot for slot in motion.slots if slot.motion_id == spec['motion']['id'] and slot.payload]
    if len(slots) != 1:
        raise ValueError('motion.id does not resolve to one embedded payload')
    fsm = open_document(spec['fsm']['path'])
    nodes = {resolve_node(fsm, selector) for selector in spec['fsm']['nodes']}
    qt_application()
    rcol = RcolHandler()
    rcol.filepath = str(spec['rcol']['path'])
    rcol.init_type_registry(str(MHR_REGISTRY))
    rcol.read(spec['rcol']['path'].read_bytes())
    requests = {}
    for field0 in spec['rcol']['field0']:
        matches = [i for i, request in enumerate(rcol.rcol.request_sets) if request.info.field0 == field0]
        if len(matches) != 1 or field0 in requests:
            raise ValueError(f'field0 {field0} must occur exactly once in the plan and RCOL')
        requests[field0] = matches[0]
    return spec, motion, slots[0], fsm, nodes, rcol, requests


def fields(instance):
    return {field.name: field.value for field in instance.fields}


def audit(loaded):
    spec, document, slot, fsm, selected, handler, requests = loaded
    errors, warnings, playback, hits, windows = [], [], [], [], []
    duration = slot.payload.value.end_frame
    maps = {}
    table = dict(struct.unpack_from('<Ii', fsm.source, fsm.transition_map_tbl_offset+i*8)
                 for i in range(fsm.transition_map_count))
    for n, node in enumerate(fsm.bhvt.nodes):
        for s, state in enumerate(node.states):
            maps.setdefault(state.TransitionMaps, []).append((n, s))
    for index in sorted(selected):
        node = fsm.bhvt.nodes[index]
        for reference in node.actions:
            action = fsm.references.action(reference.id_hash, reference.ex_id)
            if action is None:
                errors.append(f'{node.name}: unresolved action')
                continue
            values = fields(action)
            if action.class_name == 'snow.PlayerPlayMotion2':
                row = {'node': index, 'bank': values['v3_BankID'], 'motion': values['v4_MotionID']}
                playback.append(row)
                if row['motion'] != slot.motion_id or row['bank'] != spec['motion']['bank']:
                    errors.append(f'{node.name}: playback {row} differs from the move plan')
            hit_field = ('_hitIndex' if action.class_name.endswith('.PlayerHitAction2') else
                         '_HitId' if action.class_name.endswith('.PlayerFsm2ActionChargeAxeChainsawHit') else None)
            if hit_field:
                value = values[hit_field]
                hits.append({'node': index, 'field0': value})
                if value not in requests:
                    errors.append(f'{node.name}: hit field0 {value} is absent from the move plan')
        for position, state in enumerate(node.states):
            data_index = table.get(state.TransitionMaps)
            if data_index is None or not 0 <= data_index < fsm.transition_data_count:
                errors.append(f'{node.name}.state[{position}]: transition map has no valid data record')
            if len(maps[state.TransitionMaps]) > 1:
                errors.append(f'{node.name}.state[{position}]: shared TransitionMap {state.TransitionMaps}')
            condition = fsm.references.object_instance('conditions', state.TransitionConditions)
            if condition and condition.class_name == 'snow.player.fsm.PlayerFsm2Command':
                value = fields(condition)
                row = {'node': index, 'state': position, **{k: value[k] for k in ('StartFrame', 'PreFrame', 'EndFrame')}}
                row['unbounded'] = value['EndFrame'] == 0
                windows.append(row)
                if value['EndFrame'] > 0 and value['StartFrame'] > value['EndFrame']:
                    errors.append(f'{node.name}.state[{position}]: reversed derivation window')
                if value['StartFrame'] > duration:
                    warnings.append(f'{node.name}.state[{position}]: starts after the motion ends')
    if not playback:
        errors.append('No authored PlayerPlayMotion2 action found in the selected nodes')
    request_rows = []
    for field0, index in requests.items():
        request = handler.rcol.request_sets[index]
        values = handler.rcol.rsz.parsed_elements[handler.rcol.rsz.object_table[index]]
        row = {'index': index, 'id': request.info.id, 'field0': field0, 'name': request.info.name}
        for prefix in ('Hit', 'Just'):
            start, span = (values[f'_{prefix}{part}Delay'].value for part in ('Start', 'End'))
            row[prefix.lower()] = {'start': start, 'duration': span, 'end': start+span if span >= 0 else None}
            if start > duration:
                warnings.append(f'RCOL field0 {field0}: {prefix} window starts after the motion ends')
        request_rows.append(row)
    clips = []
    for sequence in [*slot.payload.value.sequences, *slot.overrides]:
        if sequence.clip:
            for prop in properties(sequence.clip.root):
                clips.append({'category': sequence.category.name, 'property': prop.name,
                              'start': prop.start_frame, 'end': prop.end_frame,
                              'keys': [key.frame for key in prop.keys]})
    return {'valid': not errors, 'errors': errors, 'warnings': warnings,
            'motion': {'id': slot.motion_id, 'frames': duration},
            'playback': playback, 'hits': hits, 'derivation_windows': windows,
            'requests': request_rows, 'clips': clips}


def check(args):
    return audit(load(args.source))


def retime_fsm(fsm, selected, factor):
    wanted, owners = {}, {}
    for n, node in enumerate(fsm.bhvt.nodes):
        for kind, rows, attribute in (('state', node.states, 'TransitionConditions'),
                                      ('child', node.children, 'condition_id'),
                                      ('start', node.transitions, 'mStartStateTransition'),
                                      ('all', node.all_states, 'mAllTransition')):
            for s, row in enumerate(rows):
                condition = fsm.references.object_instance('conditions', getattr(row, attribute))
                if condition is None or condition.class_name != 'snow.player.fsm.PlayerFsm2Command':
                    continue
                owners.setdefault(condition.start_offset, set()).add((n, kind, s))
                if n in selected and kind == 'state':
                    wanted[condition.start_offset] = condition
    edits = {}
    for offset, condition in wanted.items():
        if any(n not in selected or kind != 'state' for n, kind, s in owners[offset]):
            raise ValueError('A selected frame condition is shared outside the move; clone it before retiming')
        for field in condition.fields:
            if field.name in ('StartFrame', 'PreFrame', 'EndFrame') and field.value > 0:
                edits[field.offset] = (field.binding, field.value*factor)
    effect_fields, effect_owners = {}, {}
    for n, node in enumerate(fsm.bhvt.nodes):
        for reference in node.actions:
            action = fsm.references.action(reference.id_hash, reference.ex_id)
            if action and action.class_name == 'snow.player.fsm.PlayerFsm2ActionSetEffect':
                field = next(f for f in action.fields if f.name == '_Frame')
                effect_owners.setdefault(field.offset, set()).add(n)
                if n in selected and field.value > 0:
                    effect_fields[field.offset] = field
    for offset, field in effect_fields.items():
        if effect_owners[offset] - selected:
            raise ValueError('A selected effect frame is shared outside the move; clone the action before retiming')
        edits[offset] = (field.binding, field.value*factor)
    return save_edits(fsm, edits.values())


def retime_rcol(handler, requests, factor):
    from .formats.rcol.request_add import resolve_requests
    for index in requests.values():
        values = handler.rcol.rsz.parsed_elements[handler.rcol.rsz.object_table[index]]
        for prefix in ('Hit', 'Just'):
            start_field, span_field = (values[f'_{prefix}{part}Delay'] for part in ('Start', 'End'))
            start, span = start_field.value, span_field.value
            new_start = round_frame(start*factor) if start >= 0 else start
            if span >= 0:
                span_field.value = round_frame((start+span)*factor)-new_start if start >= 0 else round_frame(span*factor)
            start_field.value = new_start
    expected = resolve_requests(handler.rcol)
    output = handler.rebuild()
    reopened = RcolHandler()
    reopened.filepath = handler.filepath
    reopened.init_type_registry(str(MHR_REGISTRY))
    reopened.read(output)
    if reopened.rebuild() != output:
        raise ValueError('Retimed RCOL did not roundtrip')
    if resolve_requests(reopened.rcol) != expected:
        raise ValueError('Retimed RCOL changed the request or shape userdata graph')
    return output


def retime(args):
    loaded = load(args.source)
    report = audit(loaded)
    if not report['valid']:
        raise ValueError('; '.join(report['errors']))
    spec, motion, slot, fsm, selected, handler, requests = loaded
    # A shared hit request also changes the timing of every other consumer.
    for n, node in enumerate(fsm.bhvt.nodes):
        if n in selected:
            continue
        for ref in node.actions:
            action = fsm.references.action(ref.id_hash, ref.ex_id)
            if action:
                value = fields(action)
                if (action.class_name == 'snow.PlayerPlayMotion2' and
                        value['v3_BankID'] == spec['motion']['bank'] and value['v4_MotionID'] == slot.motion_id):
                    raise ValueError(f'Motion {slot.motion_id} is also used by {node.name}; include it or duplicate the motion')
                key = ('_hitIndex' if action.class_name.endswith('.PlayerHitAction2') else
                       '_HitId' if action.class_name.endswith('.PlayerFsm2ActionChargeAxeChainsawHit') else None)
                if key and value[key] in requests:
                    raise ValueError(f'Hit request {value[key]} is also used by {node.name}; clone it before retiming')
    candidate, factor = retime_motion(motion, slot.motion_id, args.frames)
    data = {'motion': CODEC.write(candidate), 'fsm': retime_fsm(fsm, selected, factor),
            'rcol': retime_rcol(handler, requests, factor)}
    output = args.output.resolve()
    if output.exists():
        raise ValueError('Bundle output directory must be new')
    if any(path == output or output in path.parents for path in [args.source.resolve(), *(spec[k]['path'] for k in data)]):
        raise ValueError('Bundle output must not contain any input')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.reasy-move-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'candidate'
        stage.mkdir()
        for key, blob in data.items():
            path = Path(key) / spec[key]['path'].name
            (stage / key).mkdir()
            (stage / path).write_bytes(blob)
            spec[key]['path'] = path.as_posix()
        manifest = stage / 'move.json'
        manifest.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding='utf-8')
        after = audit(load(manifest))
        if not after['valid']:
            raise ValueError('; '.join(after['errors']))
        if not args.dry_run:
            stage.rename(output)
    return {'output': str(output), 'dry_run': args.dry_run, 'factor': factor, 'before': report, 'after': after}
