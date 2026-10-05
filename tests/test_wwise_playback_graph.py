"""Playback regression tests. Set REASY_AUDIO_CORPUS for the actual World/Rise run."""
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest

from file_handlers.sound.bnk_parser import (
    HircObject, create_play_action_payload, create_stop_action_payload,
    create_set_rtpc_action_payload, parse_soundbank, rewrite_soundbank, set_action_specific, extract_embedded_wem,
)
from file_handlers.sound.playback_graph import (
    UnboundPlaybackGraph, export_playback_graph, compile_v140_graph,
    build_v140_object, build_v140_from_template, native_template_fields,
    build_v140_chunk,
    register_v140_rtpc_defaults,
)
from file_handlers.sound.wwise_v132 import parse_structured_object
from tools.wwise_graph_validation import verify_wwiser_xml
from tools.wwise_graph_importer import import_batch


def bank(payloads, version=140):
    hirc = struct.pack('<I', len(payloads)) + b''.join(
        struct.pack('<BI', kind, len(payload)) + payload for kind, payload in payloads)
    bkhd = struct.pack('<6I', version, 17, 0, 0, 21, 0)
    return b'BKHD' + struct.pack('<I', len(bkhd)) + bkhd + b'HIRC' + struct.pack('<I', len(hirc)) + hirc


class PlaybackGraphTests(unittest.TestCase):
    def test_native_action_event_order_and_strict_fields(self):
        event = build_v140_object(4, 30, {'Actions/Action count': 2,
            'Actions/Action 1': 20, 'Actions/Action 2': 21})
        self.assertEqual(event, struct.pack('<IBII', 30, 2, 20, 21))
        for kind, payload in ((3, create_play_action_payload(20, 40)),
                              (3, create_stop_action_payload(21, 40))):
            obj = parse_soundbank(bank([(kind, payload)])).objects[0]
            self.assertEqual(build_v140_object(kind, obj.object_id, native_template_fields(obj)), payload)
        with self.assertRaisesRegex(ValueError, 'Missing native'):
            build_v140_object(4, 30, {'Actions/Action count': 1})
        with self.assertRaisesRegex(ValueError, 'Unused native'):
            build_v140_object(4, 30, {'Actions/Action count': 0, 'unknown': 1})

    def test_no_cross_game_identity_assumption(self):
        graph = export_playback_graph(bank([(3, create_play_action_payload(20, 40))]))
        with self.assertRaises(UnboundPlaybackGraph) as caught:
            compile_v140_graph(graph, object_ids={20: 120}, media={}, external_ids={})
        self.assertEqual(caught.exception.report['unbound'],
                         [{'object': 20, 'path': 'Target', 'kind': 'hirc', 'id': 40}])

    def test_rtpc_action_scope_and_stmg_defaults(self):
        for local, action_type in ((False, 0x1302), (True, 0x1303)):
            payload = create_set_rtpc_action_payload(1, 3174500655, 100, game_object=local)
            layout = parse_structured_object(3, payload, 140)
            self.assertTrue(layout.complete, layout.error)
            self.assertEqual(layout.field('Action type').value, action_type)
            self.assertEqual(layout.field('Value').value, 100)
            self.assertEqual(layout.field('Value meaning').value, 1)
        # Minimal independently authored settings structure with a real parameter default.
        from file_handlers.sound.wwise_v132 import parse_structured_chunk
        empty = struct.pack('<fHH4I', -96, 128, 0, 0, 0, 0, 0)
        native = parse_structured_chunk('STMG', empty, 140)
        self.assertTrue(native.structure.complete, native.structure.error)
        fields = {field.path: field.value for field in native.structure.fields}
        fields.update({'Game parameter count': 1, 'Game parameters/1/Game parameter': 3174500655,
            'Game parameters/1/Default value': 100, 'Game parameters/1/Ramping': 0,
            'Game parameters/1/Ramp up': 0, 'Game parameters/1/Ramp down': 0,
            'Game parameters/1/Built-in parameter': 0})
        built = parse_structured_chunk('STMG', build_v140_chunk('STMG', fields), 140)
        self.assertEqual(built.structure.field('Game parameters/1/Default value').value, 100)
        original = bank([]) + b'STMG' + struct.pack('<I', len(empty)) + empty
        registered = register_v140_rtpc_defaults(original, {3174500655: 100, 3400427870: 0, 3794646980: 0})
        stmg = next(chunk for chunk in parse_soundbank(registered).bank_chunks if chunk.chunk_id == 'STMG')
        self.assertEqual(stmg.structure.field('Game parameter count').value, 3)
        self.assertEqual(stmg.structure.field('Game parameters/1/Default value').value, 100)
        with self.assertRaisesRegex(ValueError, 'already registered'):
            register_v140_rtpc_defaults(registered, {3174500655: 100})

    def test_legacy_event_and_stop_edit_keep_layout(self):
        stop = struct.pack('<IHIBBBB I', 20, 0x0103, 40, 0, 0, 0, 4, 0)
        event = struct.pack('<III', 30, 1, 20)
        data = bank([(3, stop), (4, event)], 120)
        result = parse_soundbank(data)
        self.assertTrue(all(obj.structure.complete for obj in result.objects))
        obj = result.objects[0]
        self.assertEqual(set_action_specific(obj, obj.action_settings), stop)
        self.assertEqual(rewrite_soundbank(data, {}, event_actions={30: [20]}), data)


@unittest.skipUnless(os.environ.get('REASY_AUDIO_CORPUS'), 'actual bank corpus not configured')
class ActualAudioCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ['REASY_AUDIO_CORPUS'])
        cls.world = cls.root / 'Glavenus/raw/sound/wwise/Windows'
        cls.xml = cls.root / 'Glavenus/parsed/audio/bank-events.xml'
        cls.validation = cls.root / 'REasyAudioValidation'

    def test_actual_world_voice_se_against_xml(self):
        for name, count in (('em080_se', 761), ('em080_vo', 425)):
            with self.subTest(bank=name):
                data = (self.world / (name + '.nbnk')).read_bytes()
                parsed = parse_soundbank(data)
                self.assertEqual(len(parsed.objects), count)
                self.assertTrue(all(obj.structure.complete for obj in parsed.objects))
                self.assertFalse(parsed.warnings)
                graph = export_playback_graph(data)
                oracle = verify_wwiser_xml(graph, self.xml, name + '.nbnk')
                self.assertEqual(oracle['compared_scalars'], 19244 if name == 'em080_se' else 9627)
                self.assertEqual(oracle['mismatches'], [])
                self.assertEqual(rewrite_soundbank(data, {}), data)
                for obj in parsed.objects:
                    if obj.type_id == 3 and obj.action_settings is not None:
                        self.assertEqual(set_action_specific(obj, obj.action_settings), obj.payload)

    def test_actual_batch_preserves_playback_graph_and_media(self):
        jobs = json.loads((self.validation / 'offline/jobs.json').read_text(encoding='utf-8'))['jobs']
        with tempfile.TemporaryDirectory() as temp:
            report = import_batch(jobs, Path(temp))
            self.assertEqual(report['status'], 'built')
            self.assertEqual(sum(len(job['media']) for job in jobs), 378)
            for job, detail in zip(jobs, report['jobs']):
                graph = json.loads(Path(job['graph']).read_text(encoding='utf-8'))
                parsed = parse_soundbank((Path(temp) / Path(job['template']).name).read_bytes())
                target = {obj.object_id: obj for obj in parsed.objects}
                ids = {int(key): int(value) for key, value in job['object_ids'].items()}
                for node in graph['nodes']:
                    obj = target[ids[node['id']]]
                    source = {field['path']: field for field in node['fields']}
                    for field in obj.structure.fields:
                        # Verify authored ordering, weights, loop/fade/parameter values
                        # against the source, independent of the adapter implementation.
                        if field.path not in source or field.path == 'Object ID':
                            continue
                        original = source[field.path]
                        if field.path.endswith('/In-memory media bytes'):
                            prefix = field.path.rsplit('/', 1)[0]
                            media_id = int(obj.structure.field(prefix + '/Source ID').value)
                            record = next(record for record in job['media'].values() if record['id'] == media_id)
                            self.assertEqual(field.value, len(Path(record['file']).read_bytes()))
                            continue
                        if field.path == 'Node/Properties/Property count':
                            attenuation = source.get('Node/Positioning/Attenuation', {}).get('value', 0)
                            self.assertEqual(field.value, original['value'] + int(bool(attenuation)))
                            continue
                        if not field.id_kind and field.storage != 'bit' and not field.path.endswith('Target property'):
                            self.assertEqual(field.value, original['value'], (node['id'], field.path))
                        if original['id_kind'] == 'hirc' and original['value'] in ids:
                            self.assertEqual(field.value, ids[original['value']])
                    if node['type'] == 4:
                        actions = [ids[field['value']] for field in node['fields'] if field['reference_role'] == 'event action']
                        self.assertEqual(list(obj.event_action_ids), actions)
                media_bank = parse_soundbank((Path(temp) / Path(job['media_template']).name).read_bytes())
                self.assertEqual({row.source_id for row in media_bank.didx_entries},
                                 {record['id'] for record in job['media'].values()})
                tracks = {track.source_id: track for track in media_bank.tracks}
                for record in job['media'].values():
                    self.assertEqual(extract_embedded_wem((Path(temp) / Path(job['media_template']).name).read_bytes(),
                                                         tracks[record['id']]), Path(record['file']).read_bytes())

    def test_native_templates_roundtrip_and_new_children(self):
        jobs = json.loads((self.validation / 'production/jobs.json').read_text(encoding='utf-8'))['jobs']
        for job in jobs:
            for obj in parse_soundbank(Path(job['template']).read_bytes()).objects:
                if obj.type_id in (2, 3, 4, 5, 6, 7, 9, 14):
                    self.assertEqual(build_v140_object(obj.type_id, obj.object_id, native_template_fields(obj)), obj.payload)
                    self.assertEqual(build_v140_from_template(obj, obj.object_id), obj.payload)
        native = parse_soundbank(Path(jobs[0]['template']).read_bytes())
        template = next(obj for obj in native.objects if obj.type_id == 7)
        fields = native_template_fields(template)
        fields = {path: value for path, value in fields.items() if not path.startswith('Children/')}
        fields.update({'Children/Child count': 1, 'Children/Child 1': 123,
            'Layers/Layer count': 1, 'Layers/1/Layer ID': 999,
            'Layers/1/RTPC curves/Curve count': 0, 'Layers/1/Crossfade input': 1198277404,
            'Layers/1/Input type': 0, 'Layers/1/Child curve count': 1,
            'Layers/1/Child curve 1/Child': 123, 'Layers/1/Child curve 1/Point count': 2,
            'Layers/1/Child curve 1/Points/1/From': 0, 'Layers/1/Child curve 1/Points/1/To': 0,
            'Layers/1/Child curve 1/Points/1/Interpolation': 4,
            'Layers/1/Child curve 1/Points/2/From': 100, 'Layers/1/Child curve 1/Points/2/To': 1,
            'Layers/1/Child curve 1/Points/2/Interpolation': 4, 'Continuous validation': 1})
        layer = parse_structured_object(9, build_v140_object(9, 456, fields), 140)
        self.assertEqual(layer.field('Layers/1/Child curve 1/Points/2/To').value, 1)

    def test_music_state_branch_and_source_parameters(self):
        job = json.loads((self.validation / 'music-offline/jobs.json').read_text(encoding='utf-8'))['jobs'][0]
        graph = json.loads(Path(job['graph']).read_text(encoding='utf-8'))
        self.assertEqual(len(graph['nodes']), 14)
        with tempfile.TemporaryDirectory() as temp:
            report = import_batch([job], Path(temp))
            self.assertEqual(report['status'], 'built')
            data = (Path(temp) / job['bank_output']).read_bytes()
            parsed = parse_soundbank(data)
            self.assertEqual(len(parsed.objects), 17)
            self.assertEqual(len(parsed.didx_entries), 2)
            target = {obj.object_id: obj for obj in parsed.objects}
            ids = {int(key): int(value) for key, value in job['object_ids'].items()}
            for node in graph['nodes']:
                values = {field.path: field.value for field in target[ids[node['id']]].structure.fields}
                for field in node['fields']:
                    path = field['path']
                    if path.startswith(('State values/', 'Clips/', 'Clip automation/', 'Cues/', 'Music playlist/', 'Transition rules/')):
                        if path.endswith('/Name bytes'):
                            name = target[ids[node['id']]].structure.field(path.rsplit('/', 1)[0] + '/Name')
                            self.assertEqual(name.size, len(name.value.encode('utf-8')) + 1)
                            continue
                        if field['id_kind'] == 'hirc' and field['value'] in ids:
                            self.assertEqual(values[path], ids[field['value']])
                        elif field['id_kind'] == 'source':
                            self.assertEqual(values[path], job['media'][str(field['value'])]['id'])
                        else:
                            self.assertEqual(values[path], field['value'], (node['id'], path))
            root = target[job['music_root']]
            branch = next(obj for obj in parsed.objects if obj.type_id == 12)
            self.assertEqual(root.parent_id, branch.object_id)
            self.assertEqual(branch.child_ids, (root.object_id,))
            self.assertEqual(branch.structure.field('Decision tree/2/Audio object').value, root.object_id)
            # State declarations must accompany the preserved State value nodes.
            track = target[ids[454720115]]
            self.assertEqual(track.structure.field('Node/State properties/1/Property').value, 0)
            self.assertEqual(track.structure.field('Node/State properties/1/Accumulation').value, 2)
            self.assertEqual(track.structure.field('Node/State properties/1/Stored in decibels').value, 1)
        native = parse_soundbank(Path(job['template']).read_bytes())
        self.assertEqual(len(native.objects), 907)
        for obj in native.objects:
            self.assertEqual(build_v140_object(obj.type_id, obj.object_id, native_template_fields(obj)), obj.payload)


if __name__ == '__main__':
    unittest.main()
