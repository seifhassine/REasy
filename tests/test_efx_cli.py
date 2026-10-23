import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.cli.main import main
from file_handlers.efx.efx_file import EfxFile
from tests.test_efx import golden


class EfxCliTests(unittest.TestCase):
    def call(self, argv):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main([*map(str, argv), '--json'])
        return status, json.loads(output.getvalue())

    def test_apply_publishes_one_candidate_and_protects_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'input.efx.2818730'
            target = Path(folder) / 'output.efx.2818730'
            plan = Path(folder) / 'operations.json'
            source.write_bytes(golden())
            plan.write_text(json.dumps([
                {'op': 'copy', 'source': 'entries/0', 'path': 'entries', 'name': 'Second'},
                {'op': 'set', 'path': 'entries/1/attributes/1/fields/LocalPosition', 'value': [3, 4, 5]},
            ]), encoding='utf-8')
            status, report = self.call(['efx', 'apply', source, '-o', target, '--plan', plan, '--dry-run'])
            self.assertEqual(status, 0, report)
            self.assertFalse(target.exists())
            status, report = self.call(['efx', 'apply', source, '-o', target, '--plan', plan])
            self.assertEqual(status, 0, report)
            reopened = EfxFile.from_bytes(target.read_bytes(), version=2818730)
            self.assertEqual(reopened.entries[1].attributes[1].body['LocalPosition'], (3, 4, 5))
            self.assertEqual(source.read_bytes(), golden())
            status, report = self.call(['efx', 'apply', source, '-o', plan, '--plan', plan])
            self.assertEqual(status, 2)
            self.assertIn('replace', report['error'])

    def test_failed_plan_does_not_publish_partial_edits(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'input.efx.2818730'
            target = Path(folder) / 'output.efx.2818730'
            plan = Path(folder) / 'operations.json'
            source.write_bytes(golden())
            plan.write_text(json.dumps([
                {'op': 'set', 'path': 'entries/0/name', 'value': 'Changed'},
                {'op': 'delete', 'path': 'bones/0'},
            ]), encoding='utf-8')
            status, report = self.call(['efx', 'apply', source, '-o', target, '--plan', plan])
            self.assertEqual(status, 2)
            self.assertFalse(target.exists())
            self.assertIn('referenced', report['error'])

    def test_new_and_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'input.efx.2818730'
            target = Path(folder) / 'output.efx.2818730'
            plan = Path(folder) / 'batch.json'
            source.write_bytes(golden())
            status, report = self.call(['efx', 'new', source, '-o', target])
            self.assertEqual(status, 0, report)
            self.assertEqual(EfxFile.from_bytes(target.read_bytes(), version=2818730).entries, [])
            plan.write_text(json.dumps([
                ['copy', '--from', 'entries/0', '--path', 'entries'],
                ['set', '--path', 'entries/1/name', '--value', '"Batch"'],
            ]), encoding='utf-8')
            status, report = self.call(['efx', 'batch', source, '-o', target, '--plan', plan])
            self.assertEqual(status, 0, report)
            self.assertEqual(EfxFile.from_bytes(target.read_bytes(), version=2818730).entries[1].name, 'Batch')

    def test_import_and_preset_input_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'input.efx.2818730'
            donor = Path(folder) / 'donor.efx.2818730'
            target = Path(folder) / 'output.efx.2818730'
            preset = Path(folder) / 'preset.json'
            plan = Path(folder) / 'plan.json'
            source.write_bytes(golden())
            donor.write_bytes(golden())
            status, report = self.call(['efx', 'import', source, '--donor', donor, '--from', 'entries/0', '--path', 'entries', '-o', donor])
            self.assertEqual(status, 2)
            status, report = self.call(['efx', 'preset-export', donor, '--from', 'entries/0', '-o', preset])
            self.assertEqual(status, 0, report)
            plan.write_text(json.dumps([{'op': 'preset-import', 'donor': 'preset.json', 'path': 'entries'}]), encoding='utf-8')
            status, report = self.call(['efx', 'apply', source, '--plan', plan, '-o', target])
            self.assertEqual(status, 0, report)
            self.assertEqual(len(EfxFile.from_bytes(target.read_bytes(), version=2818730).entries), 2)


if __name__ == '__main__':
    unittest.main()
