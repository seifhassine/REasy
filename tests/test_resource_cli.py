"""Resource CLI contracts tested on native corpus data, without fixed motion/element IDs."""
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.cli.main import main, build_parser, EDIT_COMMANDS
from tools.cli.runtime import MHR_REGISTRY, qt_application
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
from file_handlers.motion.mhr_editing import next_motion_id
from file_handlers.rcol.rcol_handler import RcolHandler
from tools.cli.formats.rsz import load, type_name
from utils.type_registry import TypeRegistry

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'tests/TESTFILE'


class CliContractTests(unittest.TestCase):
    def test_all_domains_and_operations_have_help(self):
        parser = build_parser()
        commands = [[domain, command] for domain, entries in EDIT_COMMANDS.items() for command in entries]
        commands += [['motion', name] for name in ('query', 'duplicate', 'import-wilds', 'batch')]
        commands += [['rsz', name] for name in ('query', 'scan', 'references')]
        commands += [['efx', 'query'], ['fsm', 'action-edit'], ['batch']]
        for command in commands:
            with self.subTest(command=command), redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    parser.parse_args([*command, '--help'])
                self.assertEqual(caught.exception.code, 0)

    def test_errors_are_one_json_result(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(['motion', 'duplicate', 'missing.motlist.528', '--json'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())['status'], 'error')


@unittest.skipUnless(CORPUS.is_dir(), 'Native corpus not installed')
class NativeResourceCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = qt_application()
        cls.motion_path = next(CORPUS.rglob('plw_LongSword_100.motlist.528'))
        cls.model = RISE.parse(cls.motion_path.read_bytes())
        cls.pfb_path = next(CORPUS.rglob('epvs-prg*.pfb.17'))
        cls.rcol_path = next(CORPUS.rglob('LongSword.rcol.20'))

    def invoke(self, argv, expected=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main([*map(str, argv), '--json'])
        self.assertEqual(code, expected, stderr.getvalue() + stdout.getvalue())
        return json.loads(stdout.getvalue())

    def test_query_typed_records_and_clean_subprocess_stdout(self):
        result = subprocess.run([sys.executable, '-m', 'tools', 'pfb', 'elements', str(self.pfb_path), '--json'],
                                cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        elements = json.loads(result.stdout)['elements']
        self.assertTrue(elements)
        self.assertIsInstance(elements[0]['id'], int)
        self.assertIsInstance(elements[0]['fields']['Rotation']['value']['x'], float)
        info = self.invoke(['rsz', 'query', self.pfb_path, '--class', 'EPVStandardData.Element', '--limit', 1])
        self.assertGreater(info['matched'], 0)
        index = info['instances'][0]['instance']
        info = self.invoke(['rsz', 'scan', self.pfb_path, '--instance', index, '--field', 'ID'])
        self.assertEqual(info['matched'], 1)
        self.invoke(['rsz', 'references', self.pfb_path])

    def test_pfb_element_edit_and_clone_preserve_existing_elements(self):
        before = self.invoke(['pfb', 'elements', self.pfb_path])['elements']
        counts = Counter(row['id'] for row in before)
        selected = next(row for row in before if counts[row['id']] == 1)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / self.pfb_path.name
            self.invoke(['pfb', 'element-edit', self.pfb_path, '-o', output,
                         '--id', selected['id'], '--rotate-delta', '0,0,10'])
            after = self.invoke(['pfb', 'elements', output])['elements']
            for old, new in zip(before, after):
                if old['id'] != selected['id']:
                    self.assertEqual(old, new)
            new_id = max(counts) + 1
            self.invoke(['pfb', 'element-add', self.pfb_path, '-o', output,
                         '--clone', selected['id'], '--new-id', new_id, '--rotate-delta', '0,0,0'])
            after = self.invoke(['pfb', 'elements', output])['elements']
            self.assertEqual(after[:-1], before)
            self.assertEqual(after[-1]['id'], new_id)

    def test_rcol_query_edit_and_clone(self):
        before = self.invoke(['rcol', 'query', self.rcol_path])['requests']
        template = next(row for row in before if row['shapes'] and
                        {shape['joint'] for shape in row['shapes']} == {'L_Weapon_00'})
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / self.rcol_path.name
            self.invoke(['rcol', 'request-edit', self.rcol_path, '-o', output, '--id', template['id'],
                         '--set', '_HitEndDelay=7'])
            after = self.invoke(['rcol', 'query', output])['requests']
            for old, new in zip(before, after):
                if old['id'] == template['id']:
                    self.assertEqual(new['fields']['_HitEndDelay']['value'], 7)
                else:
                    self.assertEqual(old, new)
            self.invoke(['rcol', 'request-add', self.rcol_path, '-o', output,
                         '--template-id', template['id'], '--name', 'cli_added_request'])
            after = self.invoke(['rcol', 'query', output])['requests']
            self.assertEqual(after[:-1], before)
            self.assertEqual(after[-1]['name'], 'cli_added_request')

    def test_motion_duplicate_and_cross_group_batch(self):
        source_slot = next(slot for slot in self.model.slots if slot.payload is not None and
                           any(sequence.category.name == 'SOUND' and any(
                               prop.keys and isinstance(prop.keys[0].value, int)
                               for child in sequence.clip.root.children for prop in child.properties)
                               for sequence in slot.payload.value.sequences))
        motion = source_slot.payload.value
        sound = next(seq for seq in motion.sequences if seq.category.name == 'SOUND')
        node = next(node for node in sound.clip.root.children if
                    any(prop.keys and isinstance(prop.keys[0].value, int) for prop in node.properties))
        new_id = next_motion_id(self.model)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / self.motion_path.name
            plan = Path(folder) / 'plan.json'
            plan.write_text(json.dumps([
                ['motion', 'duplicate', '--motion', str(source_slot.motion_id), '--new-id', str(new_id)],
                ['clip', 'events-drop', '--motion', str(new_id), '--node-class', node.name, '--from-frame', '0'],
            ]), encoding='utf-8')
            args = ['batch', self.motion_path, '-o', output, '--plan', plan]
            self.assertTrue(self.invoke([*args, '--dry-run'])['dry_run'])
            self.assertFalse(output.exists())
            self.invoke(args)
            doc = RISE.parse(output.read_bytes())
            self.assertEqual([s.motion_id for s in doc.slots], [s.motion_id for s in self.model.slots] + [new_id])
            copied = doc.slots[-1].payload.value
            copied_sound = next(seq for seq in copied.sequences if seq.category.name == 'SOUND')
            copied_node = next(n for n in copied_sound.clip.root.children if n.name == node.name)
            self.assertTrue(all(key.value == 0 for prop in copied_node.properties for key in prop.keys))
            saved = output.read_bytes()
            plan.write_text(json.dumps([['motion', 'duplicate', '--motion', str(source_slot.motion_id)],
                                         ['clip', 'effect-set', '--motion', '-1', '--old', 'xx', '--new', 'yy']]), encoding='utf-8')
            self.invoke(args, 2)
            self.assertEqual(output.read_bytes(), saved)

    def test_clip_queries_and_track_addition(self):
        path = next(CORPUS.rglob('plw_ChargeAxe_100.motlist.528'))
        model = RISE.parse(path.read_bytes())
        choices = []
        for slot in model.slots:
            if slot.payload is None:
                continue
            for sequence in slot.payload.value.sequences:
                if sequence.clip is None:
                    continue
                for child in sequence.clip.root.children:
                    if 'ChainsawRunTrack' in child.name:
                        choices.append((slot, sequence, child))
        self.assertTrue(choices)
        template, sequence, track = choices[0]
        target = next(slot for slot in model.slots if slot.payload and any(
            seq.category == sequence.category and seq.clip is not None and
            all(child.name != track.name for child in seq.clip.root.children)
            for seq in slot.payload.value.sequences))
        self.invoke(['clip', 'dump', path, '--motion', template.motion_id])
        self.invoke(['clip', 'layout', path, '--motion', template.motion_id])
        prop = next(prop for prop in track.properties if prop.property_type.name == 'BOOL')
        with tempfile.TemporaryDirectory() as folder:
            targets = [target, next(slot for slot in model.slots if slot is not target and slot.payload and any(
                seq.category == sequence.category and seq.clip is not None and
                all(child.name != track.name for child in seq.clip.root.children)
                for seq in slot.payload.value.sequences))]
            for feature in ('extra_ranges', 'curves', 'speeds', 'last_keys'):
                for slot in model.slots:
                    if not slot.payload:
                        continue
                    selected = next((s for s in slot.payload.value.sequences if s.category == sequence.category), None)
                    if selected is None or selected.clip is None or any(n.name == track.name for n in selected.clip.root.children):
                        continue
                    props = [p for n in [selected.clip.root, *selected.clip.root.children] for p in n.properties]
                    present = {'extra_ranges': bool(selected.clip.extra_ranges),
                               'curves': any(k.curve for p in props for k in p.keys),
                               'speeds': any(p.speed_points for p in props),
                               'last_keys': any(p.last_key is not None for p in props)}[feature]
                    if present:
                        if slot not in targets:
                            targets.append(slot)
                        break
            for selected_target in targets:
                with self.subTest(motion=selected_target.motion_id):
                    output = Path(folder) / str(selected_target.motion_id) / path.name
                    self.invoke(['clip', 'track-add', path, '-o', output, '--motion', selected_target.motion_id,
                                 '--template-motion', template.motion_id, '--category', int(sequence.category),
                                 '--track', track.name, '--flag', prop.name, '--true-frame', 6, '--false-frame', 20])
                    doc = RISE.parse(output.read_bytes())
                    after = next(s for s in doc.slots if s.motion_id == selected_target.motion_id).payload.value
                    clip = next(seq.clip for seq in after.sequences if seq.category == sequence.category)
                    added = next(n for n in clip.root.children if n.name == track.name)
                    keys = next(p.keys for p in added.properties if p.name == prop.name)
                    self.assertEqual([(key.frame, key.value) for key in keys], [(0, False), (6, True), (19, True), (20, False)])

    def test_motion_duplicate_inserts_gap_id_in_native_order(self):
        import struct
        from file_handlers.motion.mhr_editing import validate_slot_order
        path = next(CORPUS.rglob('plw_ShortSword_100.motlist.528'))
        before = RISE.parse(path.read_bytes())
        ids = {s.motion_id for s in before.slots}
        new_id = next(i for i in range(1, max(ids)) if i not in ids)
        template = next(s for s in before.slots if s.payload)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / path.name
            self.invoke(['motion', 'duplicate', path, '-o', output, '--motion', template.motion_id,
                         '--new-id', new_id, '--name', 'gap_copy'])
            after = RISE.parse(output.read_bytes())
            validate_slot_order(after)
            by_id = {s.motion_id: s for s in after.slots}
            self.assertEqual(by_id[new_id].payload.value.name, 'gap_copy')
            old_rows = struct.unpack_from('<Q', before.source, 24)[0]
            new_rows = struct.unpack_from('<Q', after.source, 24)[0]
            for old_index, old in enumerate(before.slots):
                new_index = next(i for i, s in enumerate(after.slots) if s.motion_id == old.motion_id)
                self.assertEqual(before.source[old_rows+old_index*72+8:old_rows+(old_index+1)*72],
                                 after.source[new_rows+new_index*72+8:new_rows+(new_index+1)*72])
                if old.payload:
                    original, saved = old.payload.value, by_id[old.motion_id].payload.value
                    self.assertEqual(original.name, saved.name)
                    self.assertEqual(original.end_frame, saved.end_frame)
                    channels = lambda motion: [(n.joint.name, n.weight, n.translation, n.rotation, n.scale)
                                                for n in motion.animation_nodes]
                    self.assertEqual(channels(original), channels(saved))

    def test_hit_request_resolves_authoring_id_to_field0(self):
        from tools.fsm.editing import open_document
        path = next(CORPUS.rglob('ChargeAxe.rcol.20'))
        fsm_path = next(CORPUS.rglob('ChargeAxe.motfsm2.43'))
        requests = self.invoke(['rcol', 'query', path])['requests']
        entry = next(r for r in requests if r['id'] != r['field0'] and
                     sum(q['field0'] == r['field0'] for q in requests) == 1)
        self.assertEqual(self.invoke(['rcol', 'query', path, '--field0', entry['field0']])['requests'], [entry])
        self.assertEqual(self.invoke(['rcol', 'query', path, '--index', entry['index']])['requests'], [entry])
        doc = open_document(fsm_path)
        candidates = [(i, p, a) for i, n in enumerate(doc.bhvt.nodes) for p, ref in enumerate(n.actions)
                      if (a := doc.references.action(ref.id_hash, ref.ex_id)) and a.class_name.endswith('.PlayerHitAction2')]
        index, position, action = candidates[0]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / fsm_path.name
            self.invoke(['fsm', 'hit-request', fsm_path, '-o', output, '--node', f'index:{index}',
                         '--position', position, '--rcol', path, '--id', entry['id']])
            new = open_document(output)
            ref = new.bhvt.nodes[index].actions[position]
            fields = {f.name: f.value for f in new.references.action(ref.id_hash, ref.ex_id).fields}
            self.assertEqual(fields['_hitIndex'], entry['field0'])

    def test_clip_move_effect_range_and_sequence_copy(self):
        from dataclasses import asdict
        import struct
        def effect_nodes(motion):
            return [(seq, node) for seq in motion.sequences if seq.clip for node in seq.clip.root.children
                    if node.name.endswith('VFXRangeTrack') and any(p.name == 'EffectId' and p.keys for p in node.properties)]
        slot = next(s for s in self.model.slots if s.payload and effect_nodes(s.payload.value))
        seq, node = effect_nodes(slot.payload.value)[0]
        effect_prop = next(p for p in node.properties if p.name == 'EffectId')
        old_frames = [k.frame for k in effect_prop.keys]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'edit' / self.motion_path.name
            self.invoke(['clip', 'event-move', self.motion_path, '-o', output,
                         '--motion', slot.motion_id, '--track-index', 0, '--delta', 3])
            after = RISE.parse(output.read_bytes())
            moved = next(s.payload.value for s in after.slots if s.motion_id == slot.motion_id)
            prop = next(p for p in effect_nodes(moved)[0][1].properties if p.name == 'EffectId')
            self.assertEqual([k.frame for k in prop.keys], [frame + 3 for frame in old_frames])
            old_value = effect_prop.keys[0].value
            new_value = '9' * len(old_value)
            self.invoke(['clip', 'effect-set', self.motion_path, '-o', output,
                         '--motion', slot.motion_id, '--old', old_value, '--new', new_value])
            after = RISE.parse(output.read_bytes())
            moved = next(s.payload.value for s in after.slots if s.motion_id == slot.motion_id)
            self.assertTrue(any(p.keys[0].value == new_value for _, n in effect_nodes(moved)
                                for p in n.properties if p.name == 'EffectId'))
            desired_end = effect_prop.end_frame + 123.125
            self.invoke(['clip', 'property-range', self.motion_path, '-o', output,
                         '--motion', slot.motion_id, '--category', seq.category.name, '--node', node.name,
                         '--property', 'EffectId', '--end-frame', desired_end])
            after = RISE.parse(output.read_bytes())
            moved = next(s.payload.value for s in after.slots if s.motion_id == slot.motion_id)
            prop = next(p for p in effect_nodes(moved)[0][1].properties if p.name == 'EffectId')
            self.assertEqual(prop.end_frame, desired_end)
            candidate = Path(folder) / 'duplicated' / self.motion_path.name
            new_id = next_motion_id(self.model)
            self.invoke(['motion', 'duplicate', self.motion_path, '-o', candidate, '--motion', slot.motion_id, '--new-id', new_id])
            self.invoke(['clip', 'sequence-copy', candidate, '-o', output, '--from', slot.motion_id,
                         '--to', new_id, '--categories', seq.category.name, '--shift', 2])
            result = RISE.parse(output.read_bytes())
            target = result.slots[-1].payload.value
            self.assertEqual([asdict(s) for s in target.sequences[1:]], [asdict(s) for s in slot.payload.value.sequences])

    def test_rsz_scalar_edit_and_batch_input_protection(self):
        before = self.invoke(['pfb', 'elements', self.pfb_path])['elements'][0]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / self.pfb_path.name
            self.invoke(['rsz', 'field-edit', self.pfb_path, '-o', output,
                         '--instance', before['instance'], '--set', 'PlaySpeed=1.25'])
            after = self.invoke(['pfb', 'elements', output])['elements'][0]
            self.assertEqual(after['fields']['PlaySpeed']['value'], 1.25)
            plan = Path(folder) / 'plan.json'
            donor = Path(folder) / 'donor.motlist.992'
            donor.write_bytes(b'unchanged input')
            plan.write_text(json.dumps([['motion', 'import-wilds', '--donor', str(donor), '--motion', '1']]), encoding='utf-8')
            self.invoke(['batch', self.motion_path, '-o', donor, '--plan', plan], 2)
            self.assertEqual(donor.read_bytes(), b'unchanged input')

    def test_wilds_import_cli_uses_native_verifiers(self):
        from file_handlers.motion.wilds_codec import WILDS_MOTION_FORMAT_CODEC as WILDS
        from file_handlers.motion.preview.mhr_assets import find_rise_installation
        game = find_rise_installation()
        if not game:
            self.skipTest('Rise rig assets not installed')
        donor = next(CORPUS.rglob('wp03_00.motlist.992'))
        source = WILDS.parse(donor.read_bytes())
        slot = min((s for s in source.slots if s.payload and s.payload.value.end_frame > 0),
                   key=lambda s: s.payload.value.end_frame)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / self.motion_path.name
            report = self.invoke(['motion', 'import-wilds', self.motion_path, '-o', output,
                                  '--donor', donor, '--motion', slot.motion_id,
                                  '--game-dir', game, '--hold-template', self.motion_path])
            self.assertEqual(report['details']['imported'][0]['source_id'], slot.motion_id)
            doc = RISE.parse(output.read_bytes())
            self.assertEqual(len(doc.slots), len(self.model.slots) + 1)
            self.assertEqual(RISE.write(doc), output.read_bytes())


if __name__ == '__main__':
    unittest.main()
