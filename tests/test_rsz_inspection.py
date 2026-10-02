"""CLI field metadata preserves values while exposing the client's enum meanings."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from file_handlers.rsz.rsz_data_types import ArrayData, StructData, S32Data, U32Data, ObjectData
from tools.rsz_inspection import field_record, scalar
from utils.enum_manager import registry_enums


class FieldInspectionTests(unittest.TestCase):
    def setUp(self):
        self.registry = SimpleNamespace(json_path='rsztest.json')
        self.members = [{'name': 'None', 'value': -1}, {'name': 'Off', 'value': 0},
                        {'name': 'Disabled', 'value': 0}, {'name': 'A', 'value': 1},
                        {'name': 'B', 'value': 2}]
        self.catalog = {'test.Mode': self.members}
        patcher = patch('tools.rsz_inspection.registry_enums', return_value=self.catalog)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_exact_aliases_unknown_and_unsigned_sentinel_keep_raw_values(self):
        for value, names, enum_value in [(0, ['Off', 'Disabled'], 0),
                                         (3, [], 3), (0xFFFFFFFF, ['None'], -1)]:
            with self.subTest(value=value):
                data = U32Data(value, orig_type='test.Mode')
                enums = {}
                row = field_record(data, self.registry, enums)
                self.assertEqual(row['value'], value)
                self.assertEqual(data.value, value)
                self.assertEqual(row['enum_names'], names)
                self.assertEqual(row['enum_name'], names[0] if names else None)
                self.assertEqual(row['enum_matched'], bool(names))
                self.assertEqual(row['enum_value'], enum_value)
                self.assertEqual(enums, self.catalog)

    def test_reverse_signedness_and_exact_match_precedence(self):
        self.catalog['test.Mode'] = [{'name': 'Unsigned', 'value': 0xFFFFFFFF}]
        row = field_record(S32Data(-1, orig_type='test.Mode'), self.registry)
        self.assertEqual(row['enum_name'], 'Unsigned')
        self.assertEqual(row['value'], -1)
        self.catalog['test.Mode'].append({'name': 'Signed', 'value': -1})
        self.assertEqual(field_record(S32Data(-1, orig_type='test.Mode'), self.registry)['enum_name'], 'Signed')

    def test_nested_struct_arrays_keep_element_metadata_and_values(self):
        data = StructData([{'Modes': ArrayData([S32Data(1), S32Data(3)], S32Data, 'test.Mode')}], 'test.Struct')
        row = field_record(data, self.registry)
        self.assertEqual(row['value'], [{'Modes': [1, 3]}])
        modes = row['items'][0]['Modes']
        self.assertEqual(modes['count'], 2)
        self.assertEqual(modes['items'][0]['enum_name'], 'A')
        self.assertFalse(modes['items'][1]['enum_matched'])
        self.assertEqual(scalar(data), row['value'])

    def test_empty_enum_array_still_exposes_choices_but_references_are_not_enums(self):
        enums = {}
        row = field_record(ArrayData([], S32Data, 'test.Mode'), self.registry, enums)
        self.assertEqual(row['items'], [])
        self.assertEqual(enums, self.catalog)
        ref = field_record(ObjectData(1, orig_type='test.Mode'), self.registry)
        self.assertTrue(ref['reference'])
        self.assertNotIn('enum_name', ref)
        self.assertNotIn('enum_name', field_record(S32Data(1, orig_type='unknown'), self.registry))


class RegistryCatalogTests(unittest.TestCase):
    def test_wilds_uses_the_clients_mhws_catalog(self):
        import json
        path = Path(__file__).resolve().parents[1] / 'resources/data/enums/mhws_enums.json'
        self.assertEqual(registry_enums('rszmhwilds.json'), json.loads(path.read_text(encoding='utf-8')))


if __name__ == '__main__':
    unittest.main()
