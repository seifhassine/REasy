import struct
import unittest

from file_handlers.efx.efx_file import EfxFile, schema_for_version
from file_handlers.efx.editing import EfxEditor, empty_file, references
from file_handlers.efx.metadata import parameter_value
from utils.hash_util import murmur3_hash
from tests.test_efx import golden


def simple_values(type_name):
    result = {}
    for descriptor in schema_for_version(2818730)['AttributeTypes'][type_name]['Fields']:
        kind = descriptor['FieldType']
        if descriptor.get('Flag') in ('StructSize', 'StringLength') or 'CountOf' in descriptor or 'ByteCountOf' in descriptor:
            continue
        if descriptor.get('Flag') == 'BitSet':
            value = [0] * descriptor['FixedLength']
        elif kind in ('Vec2', 'Vec3', 'Vec4', 'Int2', 'Int3', 'Int4', 'Range', 'RangeI'):
            value = [0] * (int(kind[-1]) if kind[-1].isdigit() else 2)
        elif kind == 'String':
            value = ''
        elif kind == 'F32':
            value = 0.0
        elif kind in ('Struct', 'Object'):
            value = {'expressions': []}
        else:
            value = 0
        result[descriptor['Name']] = value
    return result


def expression_attribute(name):
    fields = simple_values('Transform3DExpression')
    fields['expressionBits'] = [1]
    hash_value = murmur3_hash(name.encode())
    fields['expressions'] = {'expressions': [{
        'parameters': [{'parameterNameHash': hash_value, 'constantValue': 0., 'source': 0}],
        'components': [{'type': 4, 'data': {'parameterHash': hash_value}}],
    }]}
    return {'type': 'Transform3DExpression', 'fields': fields}


def field_parameter(name):
    return {'name': name, 'fields': {'unknown0': 2, 'unknown2': 1, 'type': 185, 'unknown4': 50,
            'value1': 0, 'value2': 0, 'value3': 0, 'value4': 1., 'value5': 1., 'value6': 1.}}


class EfxGraphTests(unittest.TestCase):
    def setUp(self):
        self.editor = EfxEditor(EfxFile.from_bytes(golden(), version=2818730))

    def test_native_group_noop_replacement_and_header_identity_edits(self):
        from file_handlers.efx.editing import data
        editor = self.editor
        editor.apply({'op':'insert','path':'groups','value':{'name':'Group','indices':[0]}})
        native = EfxEditor(EfxFile.from_bytes(editor.document.build_validated(), version=2818730))
        before = native.document.build_validated()
        native.apply({'op':'replace','path':'groups/0','value':data(native.document.groups[0])})
        self.assertEqual(native.document.build_validated(), before)
        native.apply({'op':'set','path':'dimension','value':0})
        native.apply({'op':'set','path':'header_flags','value':0x80000001})
        native.apply({'op':'set','path':'entries/0/attributes/0/unique_id','value':200})
        reopened = EfxFile.from_bytes(native.document.build_validated(), version=2818730)
        self.assertEqual((reopened.dimension, reopened.header_flags), (0,0x80000001))
        self.assertEqual(reopened.entries[0].attributes[0].unique_id, 200)
        with self.assertRaises(ValueError):
            native.apply({'op':'set','path':'entries/0/attributes/1/unique_id','value':200})

    def add_expression(self, editor, name='Strength', value=2.):
        editor.apply({'op': 'insert', 'path': 'expression_parameters', 'value': {'name': name, 'type': 0, 'value': value}})
        editor.apply({'op': 'insert', 'path': 'entries/0/attributes', 'value': expression_attribute(name)})

    def test_expression_parameter_rename_rewrites_components_and_blocks_delete(self):
        editor = self.editor
        self.add_expression(editor)
        editor.apply({'op': 'set', 'path': 'expression_parameters/0/name', 'value': 'Power'})
        expr = next(a for a in editor.document.entries[0].attributes if a.name == 'Transform3DExpression').body['expressions']['expressions'][0]
        expected = murmur3_hash(b'Power')
        self.assertEqual(expr['parameters'][0]['parameterNameHash'], expected)
        self.assertEqual(expr['components'][0]['data']['parameterHash'], expected)
        with self.assertRaisesRegex(ValueError, 'referenced'):
            editor.apply({'op': 'delete', 'path': 'expression_parameters/0'})
        editor.apply({'op': 'copy', 'source': 'expression_parameters/0', 'path': 'expression_parameters'})
        editor.apply({'op': 'move', 'path': 'expression_parameters/0', 'index': 1})
        self.assertEqual([r['target'] for r in references(editor.document) if r['kind'] == 'expression'], ['expression_parameters/1'])

    def test_color_parameter_preserves_all_rgba_bits(self):
        editor = self.editor
        editor.apply({'op': 'insert', 'path': 'expression_parameters', 'value': {'name': 'Tint', 'type': 1, 'value': 0xff812345}})
        editor.apply({'op': 'replace', 'path': 'expression_parameters/0', 'value': {'name': 'Tint', 'type': 1, 'value': 0xff923456}})
        editor.apply({'op': 'parameter-value', 'path': 'expression_parameters/0', 'value': 0xff834567})
        reopened = EfxFile.from_bytes(editor.document.build_validated(), version=2818730)
        self.assertEqual(parameter_value(reopened.expression_parameters[0]), 0xff834567)

    def test_field_parameter_reorder_and_resource_variant(self):
        editor = self.editor
        for name in ('Wind', 'Wind2'):
            editor.apply({'op': 'insert', 'path': 'field_parameters', 'value': field_parameter(name)})
        fields = simple_values('VectorFieldParameter')
        fields['FieldIndex'] = 0
        editor.apply({'op': 'insert', 'path': 'entries/0/attributes', 'value': {'type': 'VectorFieldParameter', 'fields': fields}})
        editor.apply({'op': 'move', 'path': 'field_parameters/0', 'index': 1})
        self.assertEqual([r['target'] for r in references(editor.document) if r['kind'] == 'field'], ['field_parameters/1'])
        with self.assertRaisesRegex(ValueError, 'referenced'):
            editor.apply({'op': 'delete', 'path': 'field_parameters/1'})
        editor.apply({'op': 'replace', 'path': 'field_parameters/0', 'value': {'name': 'Resource', 'fields': {
            'unknown0': 0, 'unknown2': 1, 'type': 196, 'unknown4': 0, 'filePath': 'vfx/场.efx'}}})
        reopened = EfxFile.from_bytes(editor.document.build_validated(), version=2818730)
        record = reopened.field_parameters[0]
        self.assertEqual(record['value1'], len('vfx/场.efx') + 1)
        self.assertEqual(record['nameHash'], murmur3_hash(b'Resource'))

    def test_uvar_binding_replace_removes_old_layout(self):
        editor = self.editor
        editor.apply({'op': 'replace', 'path': 'uvar_groups/0', 'value': {'type': 2, 'path': 'vfx/test.uvar', 'group': 'Flame'}})
        editor.apply({'op': 'replace', 'path': 'uvar_groups/0', 'value': {'type': 1}})
        reopened = EfxFile.from_bytes(editor.document.build_validated(), version=2818730)
        self.assertEqual(list(reopened.uvar_groups[0].fields), ['type'])
        self.assertEqual(reopened.uvar_groups[0]['type'], 1)

    def test_cross_file_import_copies_bone_and_renames_parameter_conflict(self):
        donor = self.editor
        self.add_expression(donor, value=2.)
        target = EfxEditor(empty_file(2818730))
        target.apply({'op': 'insert', 'path': 'expression_parameters', 'value': {'name': 'Strength', 'type': 0, 'value': 9.}})
        target.apply({'op': 'import', 'source': 'entries/0', 'path': 'entries'}, donor=donor.document)
        opened = EfxFile.from_bytes(target.document.build_validated(), version=2818730)
        self.assertEqual([p.name for p in opened.expression_parameters], ['Strength', 'Strength_2'])
        self.assertEqual([b.name for b in opened.bones], ['L_Nose_00'])
        self.assertTrue(all(r['valid'] for r in references(opened)))
        expression = next(a for a in opened.entries[0].attributes if a.name == 'Transform3DExpression').body['expressions']['expressions'][0]
        self.assertEqual(expression['parameters'][0]['parameterNameHash'], murmur3_hash(b'Strength_2'))
        self.assertEqual(donor.document.expression_parameters[0].name, 'Strength')

    def test_import_action_cycle_is_finite_and_independent(self):
        donor = self.editor
        donor.apply({'op': 'insert', 'path': 'actions', 'value': {'name': 'OnDeath'}})
        lifetime = {'type': 'PtLife', 'fields': {'Flags': 4, 'Status': 3, 'ActionIndex': 0}}
        donor.apply({'op': 'insert', 'path': 'actions/0/attributes', 'value': lifetime})
        donor.apply({'op': 'insert', 'path': 'entries/0/attributes', 'value': lifetime})
        target = EfxEditor(empty_file(2818730))
        target.apply({'op': 'import', 'source': 'entries/0', 'path': 'entries'}, donor=donor.document)
        self.assertEqual(len(target.document.actions), 1)
        self.assertTrue(all(r['valid'] for r in references(target.document)))
        target.apply({'op': 'set', 'path': 'actions/0/name', 'value': 'Renamed'})
        self.assertEqual(donor.document.actions[0].name, 'OnDeath')

    def test_direct_action_import_and_group_dependencies(self):
        donor = self.editor
        donor.apply({'op': 'insert', 'path': 'actions', 'value': {'name': 'Loop'}})
        donor.apply({'op': 'insert', 'path': 'actions/0/attributes', 'value': {'type': 'PtLife', 'fields': {'Flags': 4, 'Status': 3, 'ActionIndex': 0}}})
        target = EfxEditor(empty_file(2818730))
        target.apply({'op': 'import', 'source': 'actions/0', 'path': 'actions'}, donor=donor.document)
        self.assertEqual(len(target.document.actions), 1)
        self.assertEqual(target.document.actions[0].attributes[0].body['ActionIndex'], 0)
        donor.apply({'op': 'insert', 'path': 'groups', 'value': {'name': 'Flames', 'indices': [0]}})
        target.apply({'op': 'import', 'source': 'groups/0', 'path': 'groups'}, donor=donor.document)
        self.assertEqual(target.document.groups[0]['indices'], [0])
        self.assertEqual(len(target.document.entries), 1)
        self.assertTrue(all(r['valid'] for r in references(target.document)))

    def test_preset_retains_native_data_and_dependency_import(self):
        from file_handlers.efx.templates import export_template, import_template
        self.add_expression(self.editor)
        preset = export_template(self.editor.document, 'entries/0')
        donor, source = import_template(preset)
        self.assertEqual(donor.build_validated(), self.editor.document.build_validated())
        target = EfxEditor(empty_file(2818730))
        target.apply({'op': 'import', 'source': source, 'path': 'entries'}, donor=donor)
        self.assertEqual(target.document.expression_parameters[0].name, 'Strength')
        self.assertTrue(all(r['valid'] for r in references(target.document)))

    def test_explicit_embedded_creation_inherits_initially_empty_bone_table(self):
        from file_handlers.efx.editing import data
        template = EfxFile.from_bytes(golden(), version=2818730)
        child = data(template)
        child['bones'] = []
        editor = EfxEditor(empty_file(2818730))
        editor.apply({'op': 'insert', 'path': 'actions', 'value': {'name': 'Spawn', 'attributes': [
            {'type': 'PlayEmitter', 'fields': {'efxrData': child}}
        ]}})
        reopened = EfxFile.from_bytes(editor.document.build_validated(), version=2818730)
        self.assertEqual([b.name for b in reopened.bones], ['L_Nose_00'])
        embedded = reopened.actions[0].attributes[0].body['efxrData']
        self.assertEqual(embedded.bones, [])
        self.assertIs(embedded.entries[0].attributes[-1].parent_bone, reopened.bones[0])
        native = EfxEditor(reopened)
        before = native.document.build_validated()
        payload = data(native.document.actions[0].attributes[0].body)
        native.apply({'op': 'replace', 'path': 'actions/0/attributes/0/fields', 'value': payload})
        self.assertEqual(native.document.build_validated(), before)
        payload['efxrData']['entries'][0]['attributes'][1]['fields']['LocalPosition'][0] = 5.
        native.apply({'op': 'replace', 'path': 'actions/0/attributes/0/fields', 'value': payload})
        child = native.document.actions[0].attributes[0].body['efxrData']
        self.assertEqual(child.entries[0].attributes[1].body['LocalPosition'][0], 5.)
        self.assertEqual([a.unique_id for a in child.all_attributes()], [a.unique_id for a in embedded.all_attributes()])


if __name__ == '__main__':
    unittest.main()
