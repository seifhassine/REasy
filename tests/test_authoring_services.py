"""Batch publication and public command contracts without local game assets."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.file_io import publish_files
from tools.cli.main import main


class AuthoringServiceTests(unittest.TestCase):
    def test_batch_write_failure_restores_existing_outputs_and_keeps_user_backups(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); a, b = root / 'a', root / 'b'
            backup = root / 'a.bak'; backup.write_bytes(b'user backup')
            a.write_bytes(b'original a'); b.write_bytes(b'original b')
            replace = os.replace
            def fail_second(source, destination):
                if Path(destination) == b:
                    raise OSError('simulated publication failure')
                return replace(source, destination)
            with patch('services.file_io.os.replace', side_effect=fail_second):
                with self.assertRaisesRegex(OSError, 'publication failure'):
                    publish_files({a: b'new a', b: b'new b'})
            self.assertEqual(a.read_bytes(), b'original a')
            self.assertEqual(b.read_bytes(), b'original b')
            self.assertEqual(backup.read_bytes(), b'user backup')
            self.assertEqual({path.name for path in root.iterdir()}, {'a', 'b', 'a.bak'})

    def test_inputs_are_protected_before_any_output_is_written(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source'; output = root / 'output'
            source.write_bytes(b'source')
            with self.assertRaisesRegex(ValueError, 'replace the source'):
                publish_files({output: b'candidate', source: b'bad'}, inputs=[source])
            self.assertFalse(output.exists())
            self.assertEqual(source.read_bytes(), b'source')

    def invoke(self, args, code=0):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([*map(str, args), '--json']), code)
        return json.loads(output.getvalue())

    def test_native_motion_build_dry_run_and_publication(self):
        from test_mhr_events import document
        from file_handlers.motion.mot_clip.model import ClipPropertyType
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'source.motlist.528'
            output = root / 'candidate.motlist.528'; report = root / 'report.json'
            raw = document(ClipPropertyType.U32, 'SoundTrack', '_TriggerId', [1, 2], 1).source
            source.write_bytes(raw)
            plan = root / 'plan.json'
            plan.write_text(json.dumps({'schema_version': 1, 'documents': {
                'main': {'template': str(source), 'output': str(output)}}, 'operations': []}), encoding='utf-8')
            result = self.invoke(['motion', 'build', plan, '--report', report, '--dry-run'])
            self.assertTrue(result['dry_run']); self.assertFalse(output.exists()); self.assertFalse(report.exists())
            self.invoke(['motion', 'build', plan, '--report', report])
            self.assertEqual(output.read_bytes(), raw)
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(json.loads(report.read_text())['resources'][0]['bytes'], len(raw))

    def test_audio_unbound_dry_run_reports_requirements_without_publishing(self):
        from test_wwise_playback_graph import bank
        from file_handlers.sound.bnk_parser import create_play_action_payload
        from file_handlers.sound.playback_graph import export_playback_graph
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); graph = root / 'graph.json'; template = root / 'template.bnk'
            graph.write_text(json.dumps(export_playback_graph(bank([(3, create_play_action_payload(20, 40))]))), encoding='utf-8')
            template.write_bytes(bank([]))
            plan = root / 'jobs.json'; output = root / 'candidate'
            plan.write_text(json.dumps({'schema': 'reasy.playback-batch/1', 'jobs': [{
                'name': 'fixture', 'graph': str(graph), 'template': str(template),
                'media': {}, 'object_ids': {'20': 120}}]}), encoding='utf-8')
            result = self.invoke(['audio', 'build', plan, '-o', output, '--dry-run'], 2)
            self.assertEqual(result['status'], 'error')
            self.assertTrue(result['details']['jobs'][0]['unbound'])
            self.assertFalse(output.exists())

    def test_documented_legacy_audio_imports_remain_available(self):
        from tools.wem_converter import convert_file_to_wwise_wem
        from services.audio.wem_converter import convert_file_to_wwise_wem as service
        from tools.riff_metadata import RiffMetadata
        from file_handlers.sound.riff_metadata import RiffMetadata as native
        self.assertIs(convert_file_to_wwise_wem, service)
        self.assertIs(RiffMetadata, native)


if __name__ == '__main__':
    unittest.main()
