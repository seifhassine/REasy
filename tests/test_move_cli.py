"""Move-level regressions on dynamically selected native assets."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

from tools.cli.main import main
from tools.cli.runtime import qt_application
from tools.fsm.editing import open_document
from tools.fsm.values import enums
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from file_handlers.motion.mhr_editing import next_motion_id
from file_handlers.motion.mhr_retime import properties, round_frame, retime_motion

CORPUS = Path(__file__).parent / 'TESTFILE'


@unittest.skipUnless(CORPUS.is_dir(), 'Native corpus not installed')
class MoveCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_application()
        cls.fsm_path = next(CORPUS.rglob('LongSword.motfsm2.43'))
        cls.motion_path = next(CORPUS.rglob('plw_LongSword_100.motlist.528'))
        cls.rcol_path = next(CORPUS.rglob('LongSword.rcol.20'))
        cls.fsm = open_document(cls.fsm_path)
        cls.motion = CODEC.parse(cls.motion_path.read_bytes())

    def invoke(self, argv, expected=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([*map(str, argv), '--json'])
        self.assertEqual(code, expected, stderr.getvalue()+stdout.getvalue())
        return json.loads(stdout.getvalue())

    def test_retime_preserves_other_motions_and_native_name_tail(self):
        for slot in self.motion.slots:
            if not slot.payload or not 10 < slot.payload.value.end_frame < 150:
                continue
            if sum(s.payload is slot.payload for s in self.motion.slots) != 1:
                continue
            if any(p.speed_points or any(k.curve or k.offset_frame for k in [*p.keys, *([p.last_key] if p.last_key else [])])
                   for seq in [*slot.payload.value.sequences, *slot.overrides] if seq.clip for p in properties(seq.clip.root)):
                continue
            break
        else:
            self.fail('No retimeable native motion found')
        candidate, factor = retime_motion(self.motion, slot.motion_id, round_frame(slot.payload.value.end_frame*1.5))
        def snapshot(s):
            if not s.payload:
                return None
            m = s.payload.value
            return (m.name, m.end_frame, m.looping, m.raw_start_frame, m.raw_end_frame, m.frames_per_second,
                    [(j.name, j.parent.name if j.parent else None, j.translation, j.rotation) for j in m.skeleton.joints],
                    [(n.joint.name, n.weight, n.translation, n.rotation, n.scale) for n in m.animation_nodes],
                    [asdict(seq) for seq in [*m.sequences, *s.overrides]])
        for old, new in zip(self.motion.slots, candidate.slots, strict=True):
            self.assertEqual((old.motion_id, old.tag_hash), (new.motion_id, new.tag_hash))
            if old.motion_id != slot.motion_id:
                self.assertEqual(snapshot(old), snapshot(new))
        ptrs = struct.unpack_from('<Q', candidate.source, 16)[0]
        index = next(i for i, s in enumerate(candidate.slots) if s.motion_id == slot.motion_id)
        base = struct.unpack_from('<Q', candidate.source, ptrs+index*8)[0]
        size = struct.unpack_from('<I', candidate.source, base+12)[0]
        name = struct.unpack_from('<Q', candidate.source, base+88)[0]
        self.assertGreaterEqual(name, size*0.9)
        self.assertEqual(CODEC.write(CODEC.parse(candidate.source)), candidate.source)

    def test_enum_names_state_order_and_insertion_priority(self):
        for index, node in enumerate(self.fsm.bhvt.nodes):
            if len(node.states) < 2:
                continue
            for position, state in enumerate(node.states):
                instance = self.fsm.references.object_instance('conditions', state.TransitionConditions)
                if instance and instance.class_name == 'snow.player.fsm.PlayerFsm2Command':
                    break
            else:
                continue
            break
        else:
            self.fail('No native command state found')
        value = next(f for f in instance.fields if f.name == 'CmdType')
        expected = next(e['value'] for e in enums()[value.data.orig_type] if e['name'] == 'AtkXR1')
        different = next(i for i, target in enumerate(self.fsm.bhvt.nodes)
                         if target.id_hash not in (0, 0xFFFFFFFF)
                         and all(s.mTransitions != target.id_hash for s in node.states)
                         and self.fsm.references.state_target(target.id_hash) == i)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / self.fsm_path.name
            self.invoke(['fsm', 'state-add', self.fsm_path, '-o', output, '--node', f'index:{index}',
                         '--target', f'index:{different}', '--template-state', position, '--insert-before', 0,
                         '--set', 'CmdType=AtkXR1', '--events', 'drop'])
            saved = open_document(output)
            new_node = saved.bhvt.nodes[index]
            self.assertEqual(new_node.states[1:], node.states)
            condition = saved.references.object_instance('conditions', new_node.states[0].TransitionConditions)
            self.assertEqual(next(f.value for f in condition.fields if f.name == 'CmdType'), expected)
            order = list(reversed(range(len(node.states))))
            self.invoke(['fsm', 'state-order', self.fsm_path, '-o', output, '--node', f'index:{index}',
                         '--order', ','.join(map(str, order))])
            self.assertEqual(open_document(output).bhvt.nodes[index].states, [node.states[i] for i in order])
            snapshot = output.read_bytes()
            self.invoke(['fsm', 'state-order', self.fsm_path, '-o', output, '--node', f'index:{index}',
                         '--order', '0,0'], 2)
            self.assertEqual(output.read_bytes(), snapshot)

    def test_event_edit_reuses_existing_event_and_native_enum(self):
        for index, node in enumerate(self.fsm.bhvt.nodes):
            for position, state in enumerate(node.states):
                for raw in state.mStates.values:
                    event = self.fsm.references.object_instance('transition_events', raw)
                    if event and event.class_name.endswith('.PlayerFsm2EventStateInitOption'):
                        break
                else:
                    continue
                break
            else:
                continue
            break
        else:
            self.fail('No native turn event found')
        field = next(f for f in event.fields if f.name == '_AngleSetType')
        expected = next(e['value'] for e in enums()[field.data.orig_type] if e['name'] == 'NowOppositeToAngle')
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / self.fsm_path.name
            self.invoke(['fsm', 'event-edit', self.fsm_path, '-o', output, '--node', f'index:{index}',
                         '--states', position, '--class', event.class_name, '--allow-shared',
                         '--set', '_AngleSetType=NowOppositeToAngle', '--set', '_LimitAngle=0'])
            saved = open_document(output)
            self.assertEqual(saved.bhvt.nodes, self.fsm.bhvt.nodes)
            actual = saved.references.object_instance('transition_events', raw)
            self.assertEqual(next(f.value for f in actual.fields if f.name == field.name), expected)

    def test_bundle_retime_preserves_sources_sentinels_and_non_timeline_values(self):
        by_id = {s.motion_id: s for s in self.motion.slots}
        for index, node in enumerate(self.fsm.bhvt.nodes):
            actions = [self.fsm.references.action(r.id_hash, r.ex_id) for r in node.actions]
            play = [a for a in actions if a and a.class_name == 'snow.PlayerPlayMotion2']
            hit = [a for a in actions if a and a.class_name.endswith('.PlayerHitAction2')]
            effects = [a for a in actions if a and a.class_name.endswith('.PlayerFsm2ActionSetEffect')]
            if len(play) != 1 or len(hit) != 1 or not node.states or not effects:
                continue
            values = {f.name: f.value for f in play[0].fields}
            slot = by_id.get(values['v4_MotionID'])
            if values['v3_BankID'] != 100 or slot is None or not slot.payload:
                continue
            animation = slot.payload.value
            if not 10 < animation.end_frame < 150:
                continue
            if any(p.speed_points or any(k.curve or k.offset_frame for k in [*p.keys, *([p.last_key] if p.last_key else [])])
                   for seq in [*animation.sequences, *slot.overrides] if seq.clip for p in properties(seq.clip.root)):
                continue
            parent = self.fsm.references.parent_index(node)
            if parent is not None:
                break
        else:
            self.fail('No native move with supported timeline channels found')
        source_hit = next(f.value for f in hit[0].fields if f.name == '_hitIndex')
        template = self.invoke(['rcol', 'query', self.rcol_path, '--field0', source_hit])['requests'][0]
        new_id = next_motion_id(self.motion)
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            motion_path, rcol_path = folder / self.motion_path.name, folder / self.rcol_path.name
            fsm_path, cloned = folder / self.fsm_path.name, folder / 'clone.motfsm2.43'
            self.invoke(['motion', 'duplicate', self.motion_path, '-o', motion_path, '--motion', slot.motion_id, '--new-id', new_id])
            self.invoke(['rcol', 'request-add', self.rcol_path, '-o', rcol_path, '--template-id', template['id'], '--name', 'timing_test'])
            request = self.invoke(['rcol', 'query', rcol_path])['requests'][-1]
            self.invoke(['fsm', 'node-clone', self.fsm_path, '-o', cloned, '--copy-node', f'index:{index}',
                         '--parent', f'index:{parent}', '--motion', new_id, '--name', 'timing_test', '--set-effect', 1])
            self.invoke(['fsm', 'hit-request', cloned, '-o', fsm_path, '--node', 'timing_test',
                         '--rcol', rcol_path, '--field0', request['field0']])
            spec = {'motion': {'path': str(motion_path), 'id': new_id, 'bank': 100},
                    'fsm': {'path': str(fsm_path), 'nodes': ['timing_test']},
                    'rcol': {'path': str(rcol_path), 'field0': [request['field0']]}}
            plan = folder / 'move.json'
            plan.write_text(json.dumps(spec), encoding='utf-8')
            before = self.invoke(['move', 'check', plan])
            sources = {p: p.read_bytes() for p in (motion_path, fsm_path, rcol_path, plan)}
            frames = round_frame(animation.end_frame*1.5)
            output = folder / 'candidate'
            result = self.invoke(['move', 'retime', plan, '-o', output, '--frames', frames, '--dry-run'])
            self.assertFalse(output.exists())
            self.assertTrue(result['after']['valid'])
            result = self.invoke(['move', 'retime', plan, '-o', output, '--frames', frames])
            after = self.invoke(['move', 'check', output / 'move.json'])
            self.assertEqual(after['motion']['frames'], frames)
            factor = frames/animation.end_frame
            for old, new in zip(before['derivation_windows'], after['derivation_windows'], strict=True):
                self.assertAlmostEqual(new['StartFrame'], old['StartFrame']*factor, places=4)
                self.assertEqual(new['unbounded'], old['unbounded'])
            before_fsm, after_fsm = open_document(fsm_path), open_document(output / 'fsm' / fsm_path.name)
            def effect_frames(document):
                node = next(n for n in document.bhvt.nodes if n.name == 'timing_test')
                actions = [document.references.action(r.id_hash, r.ex_id) for r in node.actions]
                return [f.value for a in actions if a.class_name.endswith('.PlayerFsm2ActionSetEffect')
                        for f in a.fields if f.name == '_Frame']
            for old, new in zip(effect_frames(before_fsm), effect_frames(after_fsm), strict=True):
                self.assertAlmostEqual(new, old*factor if old > 0 else old, places=4)
            saved_requests = self.invoke(['rcol', 'query', output / 'rcol' / rcol_path.name])['requests']
            new_request = next(r for r in saved_requests if r['field0'] == request['field0'])
            for prefix in ('Hit', 'Just'):
                start = request['fields'][f'_{prefix}StartDelay']['value']
                span = request['fields'][f'_{prefix}EndDelay']['value']
                self.assertEqual(new_request['fields'][f'_{prefix}StartDelay']['value'],
                                 round_frame(start*factor) if start >= 0 else start)
                if start >= 0 and span >= 0:
                    self.assertEqual(new_request['fields'][f'_{prefix}EndDelay']['value'],
                                     round_frame((start+span)*factor)-round_frame(start*factor))
            timing = {'_HitStartDelay', '_HitEndDelay', '_JustStartDelay', '_JustEndDelay'}
            for name, field in request['fields'].items():
                if name not in timing:
                    self.assertEqual(new_request['fields'][name], field)
            for p, raw in sources.items():
                self.assertEqual(p.read_bytes(), raw)
            self.invoke(['move', 'retime', plan, '-o', output, '--frames', frames], 2)
            spec['motion']['bank'] += 1
            plan.write_text(json.dumps(spec), encoding='utf-8')
            bad = self.invoke(['move', 'check', plan], 2)
            self.assertFalse(bad['valid'])
            failed = folder / 'failed'
            self.invoke(['move', 'retime', plan, '-o', failed, '--frames', frames], 2)
            self.assertFalse(failed.exists())


if __name__ == '__main__':
    unittest.main()
