"""Structural EFX edits preserve references, raw values and transactional history."""
import struct
import unittest

from file_handlers.efx.efx_file import EfxFile, EfxField, EfxRecord
from file_handlers.efx.editing import EfxEditor, data, empty_file, references
from tests.test_efx import golden


class EfxEditingTests(unittest.TestCase):
    def setUp(self):
        self.editor = EfxEditor(EfxFile.from_bytes(golden(), version=2818730))

    def test_empty_file_and_explicit_attribute_creation(self):
        editor = EfxEditor(empty_file(2818730))
        editor.apply({'op': 'insert', 'path': 'entries', 'value': {'name': 'Fire'}})
        donor = self.editor.document.entries[0].attributes[1]
        editor.apply({'op': 'insert', 'path': 'entries/0/attributes', 'value': data(donor)})
        reopened = EfxFile.from_bytes(editor.document.build_validated(), version=2818730)
        self.assertEqual(reopened.entries[0].name, 'Fire')
        self.assertEqual(reopened.entries[0].attributes[0].body['LocalScale'], (1, 1, 1))
        with self.assertRaisesRegex(ValueError, 'Missing value'):
            editor.apply({'op': 'insert', 'path': 'entries/0/attributes', 'value': {'type': 'Spawn', 'fields': {}}})
        self.assertEqual(len(editor.document.entries[0].attributes), 1)

    def test_sunbreak_additional_attribute_layouts(self):
        from file_handlers.efx.efx_file import schema_for_version, Reader, read_record, write_record
        schema = schema_for_version(2818730)
        cases = [('EmitMask', struct.pack('<I', 4)),
                 ('RgbCommon', struct.pack('<I3fI3f15I', 0xff80c0ff, 8., 10., 5., 0xffffffff, 1., .5, .5, *range(15)))]
        for name, raw in cases:
            definition = schema['AttributeTypes'][name]
            reader = Reader(raw, 2818730)
            record = read_record(reader, definition['Classname'], definition=definition)
            self.assertEqual(reader.pos, len(raw))
            self.assertEqual(write_record(record), raw)
        self.assertEqual(schema_for_version(2621987)['AttributeTypes']['EmitMask']['TypeID'], 208)
        self.assertEqual(schema['AttributeTypes']['EmitMask']['TypeID'], 213)

    def test_2d_modifier_native_width_and_edit_offset(self):
        from file_handlers.efx.efx_file import Reader, read_record, write_record, schema_for_version
        definition = schema_for_version(2818730)['AttributeTypes']['Transform2DModifier']
        raw = struct.pack('<I30f', 7, *([0.] * 10), -2., 0., 1., *([0.] * 17))
        reader = Reader(raw, 2818730)
        record = read_record(reader, definition['Classname'], definition=definition)
        self.assertEqual(reader.pos, 124)
        self.assertEqual(record['unknown11'], -2.)
        self.assertEqual(write_record(record), raw)
        record['unknown11'] = -4.
        expected = bytearray(raw)
        struct.pack_into('<f', expected, 44, -4.)
        self.assertEqual(write_record(record), expected)

    def test_gpu_lightning_three_bone_slots_and_string_lengths(self):
        from file_handlers.efx.efx_file import schema_for_version
        definition = schema_for_version(2818730)['AttributeTypes']['TypeGpuLightning3D']
        values = {f['Name']: '' if f['FieldType'] == 'String' else 0.0 if f['FieldType'] == 'F32' else 0
                  for f in definition['Fields']}
        values.update(str1='Start', str2='Middle', str3='End')
        editor = self.editor
        editor.apply({'op': 'insert', 'path': 'entries/0/attributes', 'value': {'type': 'TypeGpuLightning3D', 'fields': values}})
        editor.apply({'op': 'set', 'path': 'entries/0/attributes/3/fields/str2', 'value': 'LongMiddleBone'})
        opened = EfxFile.from_bytes(editor.document.build_validated(), version=2818730)
        attribute = opened.entries[0].attributes[3]
        self.assertEqual(opened.header[10], 4)
        self.assertEqual([b.name for b in attribute.bone_bindings], ['Start', 'LongMiddleBone', 'End'])
        self.assertEqual(attribute.body['str2Len'], len('LongMiddleBone') + 1)
        end = next(i for i, b in enumerate(editor.document.bones) if b.name == 'End')
        with self.assertRaisesRegex(ValueError, 'referenced'):
            editor.apply({'op': 'delete', 'path': f'bones/{end}'})

    def test_duplicate_entry_preserves_bindings_and_allocates_ids(self):
        editor = self.editor
        editor.apply({'op': 'copy', 'source': 'entries/0', 'path': 'entries', 'name': 'Second'})
        first, second = editor.document.entries
        self.assertIs(first.attributes[-1].parent_bone, second.attributes[-1].parent_bone)
        self.assertIsNot(first.attributes[1].body, second.attributes[1].body)
        self.assertNotEqual(first.index, second.index)
        ids = [a.unique_id for a in editor.document.all_attributes()]
        self.assertEqual(len(ids), len(set(ids)))
        editor.apply({'op': 'set', 'path': 'entries/1/attributes/1/fields/LocalPosition/1', 'value': 5})
        self.assertEqual(first.attributes[1].body['LocalPosition'], (0, 1, 2))
        self.assertEqual(second.attributes[1].body['LocalPosition'], (0, 5, 2))

    def test_groups_follow_positions_through_insert_move_delete(self):
        editor = self.editor
        editor.apply({'op': 'insert', 'path': 'groups', 'value': {'name': 'Flames', 'indices': [0]}})
        editor.apply({'op': 'insert', 'path': 'entries', 'index': 0, 'value': {'name': 'New'}})
        self.assertEqual(editor.document.groups[0]['indices'], [1])
        editor.apply({'op': 'move', 'path': 'entries/1', 'index': 0})
        self.assertEqual(editor.document.groups[0]['indices'], [0])
        editor.apply({'op': 'set', 'path': 'groups/0/name', 'value': '火焰'})
        editor.apply({'op': 'delete', 'path': 'entries/0'})
        reopened = EfxFile.from_bytes(editor.document.build_validated(), version=2818730)
        self.assertEqual(reopened.groups[0]['indices'], [])
        self.assertEqual(reopened.groups[0].name, '火焰')

    def test_duplicate_preserves_group_membership(self):
        editor = self.editor
        editor.apply({'op': 'insert', 'path': 'groups', 'value': {'name': 'Flames', 'indices': [0]}})
        before = editor.document.build_validated()
        editor.apply({'op': 'copy', 'source': 'entries/0', 'path': 'entries'})
        self.assertEqual(editor.document.groups[0]['indices'], [0, 1])
        editor.apply({'op': 'delete', 'path': 'entries/1'})
        self.assertEqual(editor.document.build_validated(), before)

    def test_action_reference_reorder_and_referenced_delete(self):
        editor = self.editor
        for name in ('First', 'Second'):
            editor.apply({'op': 'insert', 'path': 'actions', 'value': {'name': name}})
        editor.apply({'op': 'insert', 'path': 'entries/0/attributes', 'value': {
            'type': 'PtLife', 'fields': {'Flags': 4, 'Status': 3, 'ActionIndex': 0}}})
        self.assertEqual([r['target'] for r in references(editor.document) if r['kind'] == 'action'], ['actions/0'])
        editor.apply({'op': 'move', 'path': 'actions/0', 'index': 1})
        self.assertEqual([r['target'] for r in references(editor.document) if r['kind'] == 'action'], ['actions/1'])
        before = editor.document.build_validated()
        with self.assertRaisesRegex(ValueError, 'referenced'):
            editor.apply({'op': 'delete', 'path': 'actions/1'})
        self.assertEqual(editor.document.build_validated(), before)
        editor.apply({'op': 'delete', 'path': 'entries/0/attributes/3'})
        editor.apply({'op': 'delete', 'path': 'actions/1'})
        self.assertEqual(len(editor.document.actions), 1)

    def test_history_and_error_rollback(self):
        editor = self.editor
        original = editor.document.build_validated()
        editor.apply({'op': 'set', 'path': 'entries/0/name', 'value': 'Renamed'})
        changed = editor.document.build_validated()
        self.assertTrue(editor.modified)
        with self.assertRaises(ValueError):
            editor.apply({'op': 'copy', 'source': 'entries/0/attributes/0', 'path': 'entries/0/attributes'})
        self.assertEqual(editor.document.build_validated(), changed)
        self.assertTrue(editor.undo())
        self.assertEqual(editor.document.build_validated(), original)
        self.assertFalse(editor.modified)
        self.assertTrue(editor.redo())
        self.assertEqual(editor.document.build_validated(), changed)
        editor.mark_clean()
        editor.undo()
        self.assertTrue(editor.modified)
        editor.redo()
        self.assertFalse(editor.modified)

    def test_bone_rename_updates_binding_and_rejects_referenced_delete(self):
        editor = self.editor
        editor.apply({'op': 'set', 'path': 'bones/0/name', 'value': 'Head_00'})
        reopened = EfxFile.from_bytes(editor.document.build_validated(), version=2818730)
        self.assertEqual(reopened.entries[0].attributes[-1].body['BoneName'], 'Head_00')
        with self.assertRaisesRegex(ValueError, 'referenced'):
            editor.apply({'op': 'delete', 'path': 'bones/0'})
        editor.apply({'op': 'set', 'path': 'entries/0/attributes/2/fields/BoneName', 'value': ''})
        editor.apply({'op': 'delete', 'path': 'bones/0'})
        self.assertEqual(editor.document.bones, [])

    def test_array_reorder_keeps_nan_payload_with_element(self):
        from file_handlers.efx.efx_file import Reader, read_record, write_record
        definition = {'Fields': [{'Name': 'count', 'FieldType': 'S32', 'CountOf': 'values'},
                                 {'Name': 'values', 'FieldType': 'F32', 'IsArray': True, 'ArrayLength': ['count']}]}
        raw = struct.pack('<iII', 2, 0x7f812345, 0x3f800000)
        record = read_record(Reader(raw, 2818730), 'Fixture', definition=definition)
        doc = empty_file(2818730)
        # Exercise the shared array operation without pretending this fixture is an EFX attribute.
        editor = EfxEditor(doc)
        doc.field_parameters.append(record)
        editor._apply({'op': 'move', 'path': 'field_parameters/0/fields/values/0', 'index': 1})
        self.assertEqual(write_record(record), struct.pack('<iII', 2, 0x3f800000, 0x7f812345))
        editor._apply({'op': 'delete', 'path': 'field_parameters/0/fields/values/0'})
        self.assertEqual(write_record(record), struct.pack('<iI', 1, 0x7f812345))
        editor._apply({'op': 'insert', 'path': 'field_parameters/0/fields/values', 'value': 2.0})
        self.assertEqual(write_record(record), struct.pack('<iII', 2, 0x7f812345, 0x40000000))

    def test_nested_vector_component_does_not_replace_array(self):
        record = EfxRecord('Fixture', {'values': EfxField('values', 'Array', [(1., 2., 3.), (4., 5., 6.)],
                           {'FieldType': 'Vec3', 'IsArray': True})}, {}, 2818730)
        doc = empty_file(2818730)
        editor = EfxEditor(doc)
        doc.field_parameters.append(record)
        editor._apply({'op': 'set', 'path': 'field_parameters/0/fields/values/1/0', 'value': 9.0})
        self.assertEqual(record['values'], [(1., 2., 3.), (9., 5., 6.)])

    def test_invalid_references_are_rolled_back(self):
        editor = self.editor
        editor.apply({'op': 'insert', 'path': 'groups', 'value': {'name': 'Group', 'indices': [0]}})
        before = editor.document.build_validated()
        with self.assertRaisesRegex(ValueError, 'Dangling entry'):
            editor.apply({'op': 'set', 'path': 'groups/0/fields/indices/0', 'value': 5})
        self.assertEqual(editor.document.build_validated(), before)
        with self.assertRaisesRegex(ValueError, 'Dangling action'):
            editor.apply({'op': 'insert', 'path': 'entries/0/attributes', 'value': {
                'type': 'PtLife', 'fields': {'Flags': 4, 'Status': 3, 'ActionIndex': 0}}})
        self.assertEqual(editor.document.build_validated(), before)

    def test_behavior_property_creation_resize_and_delete(self):
        editor = self.editor
        editor.apply({'op': 'insert', 'path': 'entries/0/attributes', 'value': {
            'type': 'PtBehavior', 'fields': {'unkn1': 0, 'behaviorString': 'test.Behavior', 'properties': [
                {'dataType': 17, 'variable': {'unkn': 0, 'str': 'test.pfb'}, 'behaviorProperty': 'Prefab'},
                {'dataType': 9, 'variable': {'unkn': 0, 'value': 1.0, 'restData': []}, 'behaviorProperty': 'Weight'},
            ]}}})
        path = 'entries/0/attributes/3/fields/properties'
        editor.apply({'op': 'set', 'path': path + '/0/variable/str', 'value': 'much/longer/火焰.pfb'})
        editor.apply({'op': 'copy', 'source': path + '/1', 'path': path})
        editor.apply({'op': 'delete', 'path': path + '/1'})
        reopened = EfxFile.from_bytes(editor.document.build_validated(), version=2818730)
        body = reopened.entries[0].attributes[3].body
        self.assertEqual(body['varCount'], 2)
        prop = body['properties'][0]
        self.assertEqual(prop['variable']['str'], 'much/longer/火焰.pfb')
        self.assertEqual(prop['variable']['size'], len('much/longer/火焰.pfb\0'.encode('utf-16le')))
        self.assertEqual(body['properties'][1]['variable']['size'], 4)

    def test_copied_embedded_file_keeps_root_bone_table(self):
        child = golden(embedded=True)
        strings = b'L_Nose_00\0' + 'L_Nose_00\0'.encode('utf-16le') + b'spawn\0'
        raw = struct.pack('<4s11i', b'efxr', 1, 0, len(strings), 1, 0, 0, 0, 0, 1, 0, 0)
        raw += strings + struct.pack('<IIiIiiiI', 0xAABBCCDD, 0, 0, 0x12345678, 1, 141, 201, len(child)) + child + bytes(8)
        editor = EfxEditor(EfxFile.from_bytes(raw, version=2818730))
        editor.apply({'op': 'copy', 'source': 'actions/0', 'path': 'actions'})
        editor.apply({'op': 'set', 'path': 'actions/1/attributes/0/fields/efxrData/entries/0/attributes/2/fields/BoneName', 'value': 'Head_00'})
        reopened = EfxFile.from_bytes(editor.document.build_validated(), version=2818730)
        self.assertEqual([b.name for b in reopened.bones], ['L_Nose_00', 'Head_00'])
        self.assertIs(reopened.actions[0].attributes[0].body['efxrData'].entries[0].attributes[2].parent_bone, reopened.bones[0])
        self.assertIs(reopened.actions[1].attributes[0].body['efxrData'].entries[0].attributes[2].parent_bone, reopened.bones[1])


class EfxEditingViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_toolbar_duplicate_move_delete_history_and_save_state(self):
        from PySide6.QtWidgets import QTreeWidgetItemIterator
        from file_handlers.efx.efx_handler import EfxHandler
        from file_handlers.efx.efx_viewer import PATH_ROLE
        handler = EfxHandler()
        handler.filepath = 'fixture.efx.2818730'
        handler.read(golden())
        view = handler.create_viewer()
        iterator = QTreeWidgetItemIterator(view.tree)
        while iterator.value():
            row = iterator.value()
            if row.data(0, PATH_ROLE) == ('entries', '0'):
                view.tree.setCurrentItem(row)
                break
            iterator += 1
        view.buttons['Duplicate'].click()
        self.assertEqual(len(handler.efx.entries), 2)
        self.assertEqual(view._selected_path(), ('entries', '1'))
        view.buttons['Up'].click()
        self.assertEqual(handler.efx.entries[0].name, 'flame_copy')
        self.assertEqual(view._selected_path(), ('entries', '0'))
        view.buttons['Delete'].click()
        self.assertEqual(handler.rebuild(), golden())
        self.assertFalse(handler.modified)
        view.buttons['Undo'].click()
        self.assertTrue(handler.modified)
        handler.modified = False
        view.buttons['Undo'].click()
        self.assertTrue(handler.modified)
        view.buttons['Redo'].click()
        self.assertFalse(handler.modified)
        view.close()


if __name__ == '__main__':
    unittest.main()
