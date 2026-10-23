import struct
import unittest

from file_handlers.efx.efx_file import EfxFile
from file_handlers.efx.editing import EfxEditor, record_from_data
from file_handlers.efx.channels import curve_snapshot
from file_handlers.efx.expressions import compile_expression, expression_text, evaluate_expression
from file_handlers.efx.material import texture_index
from tests.test_efx import golden
from tests.test_efx_graph import simple_values


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.editor = EfxEditor(EfxFile.from_bytes(golden(), version=2818730))

    def insert(self, name, fields):
        return self.editor.apply({'op': 'insert', 'path': 'entries/0/attributes', 'value': {'type': name, 'fields': fields}})

    def test_path_translate_array_count_tracks_insert_copy_move_delete(self):
        fields = simple_values('PtPathTranslate'); fields['substruct2'] = []
        path = self.insert('PtPathTranslate', fields)
        collection = path + '/fields/substruct2'
        self.editor.apply({'op': 'insert', 'path': collection, 'value': {f'unkn{i}': float(i) for i in range(7)}})
        self.editor.apply({'op': 'copy', 'source': collection + '/0', 'path': collection})
        self.editor.apply({'op': 'move', 'path': collection + '/0', 'index': 1})
        self.editor.apply({'op': 'delete', 'path': collection + '/0'})
        reopened = EfxFile.from_bytes(self.editor.document.build_validated(), version=2818730)
        body = next(a.body for a in reopened.entries[0].attributes if a.name == 'PtPathTranslate')
        self.assertEqual(body['dataSize'], 1)
        self.assertEqual(body['substruct2'][0]['unkn6'], 6.)

    def test_curve_keys_channels_tangents_and_integer_raw_values(self):
        path = self.insert('Transform3DClip', {'clipBits': [0], 'unkn1': 0, 'clipData': {'loopType': 2, 'clipDuration': 0., 'clips': [], 'frames': [], 'interpolationData': []}})
        op = {'op': 'curve-set', 'path': path, 'channel': 2, 'value': {'value_type': 5, 'keys': [
            {'time': 0, 'value': 3., 'interpolation': 5, 'tangents': {'out_x': .25, 'out_y': .5, 'in_x': -.25, 'in_y': -.5}},
            {'time': 20, 'value': 8., 'interpolation': 2}]}}
        self.editor.apply(op)
        self.editor.apply({'op': 'curve-set', 'path': path, 'channel': 0, 'value': {'value_type': 3, 'keys': [{'time': 5, 'value': -8388607, 'interpolation': 2}]}})
        body = next(a.body for a in self.editor.document.entries[0].attributes if a.name == 'Transform3DClip')
        self.assertEqual(body['clipBits'], [5])
        self.assertEqual([r['channel'] for r in curve_snapshot(body)], [0, 2])
        self.assertEqual(curve_snapshot(body)[0]['keys'][0]['value'], -8388607)
        self.assertEqual(body['clipData']['frameCount'], 3)
        raw = self.editor.document.build_validated()
        reopened = EfxFile.from_bytes(raw, version=2818730)
        rb = next(a.body for a in reopened.entries[0].attributes if a.name == 'Transform3DClip')
        self.assertEqual(curve_snapshot(rb), curve_snapshot(body))
        self.editor.apply({'op': 'curve-delete', 'path': path, 'channel': 2})
        body = next(a.body for a in self.editor.document.entries[0].attributes if a.name == 'Transform3DClip')
        self.assertEqual(len(body['clipData']['interpolationData']), 0)
        self.editor.undo(); self.assertEqual(self.editor.document.build_validated(), raw)
        with self.assertRaises(ValueError):
            self.editor.apply({'op': 'set', 'path': path + '/fields/clipBits/0', 'value': 0})
        with self.assertRaises(ValueError):
            self.editor.apply({'op': 'set', 'path': path + '/fields/clipData/clips/0/frameCount', 'value': 50})
        self.assertEqual(self.editor.document.build_validated(), raw)

    def test_material_curve_explicit_mapping_and_double_count(self):
        path = self.insert('TypeMeshClip', {'clipBits': [0], 'clipData': {'loopType': 2, 'clipDuration': 0., 'clips': [], 'frames': [], 'interpolationData': [], 'mdfProperties': [], 'indices': []}})
        payload = {'value_type': 5, 'keys': [{'time': 0, 'value': 1, 'interpolation': 2}], 'material': {'mdfPropertyHash': 123, 'unkn1': 0, 'unkn2': 0, 'unkn3': 5}}
        with self.assertRaisesRegex(ValueError, 'indices'):
            self.editor.apply({'op': 'curve-set', 'path': path, 'channel': 0, 'value': payload})
        self.editor.apply({'op': 'curve-set', 'path': path, 'channel': 0, 'value': payload, 'indices': [8]})
        body = next(a.body for a in self.editor.document.entries[0].attributes if a.name == 'TypeMeshClip')
        self.assertEqual(body['clipHeader'], 2)
        self.assertEqual(curve_snapshot(body)[0]['material']['mdfPropertyHash'], 123)
        self.editor.apply({'op': 'curve-delete', 'path': path, 'channel': 0, 'indices': []})
        self.assertEqual(body['clipHeader'], 0)

    def test_material_expression_headers_and_mesh_clip_low_flag(self):
        fields = simple_values('TypeBillboard3DMaterialExpression')
        fields['materialExpressions'] = {'expressions': [], 'indices': []}
        path = self.insert('TypeBillboard3DMaterialExpression', fields)
        op = {'op': 'expression-set', 'path': path, 'material': True, 'channel': 0, 'indices': [0], 'value': {
            'formula': '2.0', 'unkn1': 0, 'unkn2': 0, 'mdfPropertyHash': 123, 'propertyComponentIndex': 0}}
        before = self.editor.document.build_validated()
        with self.assertRaisesRegex(ValueError, 'header'):
            self.editor.apply(op)
        self.assertEqual(self.editor.document.build_validated(), before)
        op['headers'] = {'materialExpressionCount': 1}
        self.editor.apply(op)
        body = next(a.body for a in self.editor.document.entries[0].attributes if a.name == 'TypeBillboard3DMaterialExpression')
        self.assertEqual(body['materialExpressionCount'], 1)
        self.assertEqual(len(body['materialExpressions']['expressions']), 1)
        self.editor.apply({'op': 'expression-delete', 'path': path, 'material': True, 'channel': 0, 'indices': [], 'headers': {'materialExpressionCount': 0}})
        path = self.insert('TypeMeshClip', {'clipBits': [0], 'clipHeader': 1, 'clipData': {
            'loopType':2, 'clipDuration':0, 'clips':[], 'frames':[], 'interpolationData':[], 'mdfProperties':[], 'indices':[]}})
        payload = {'value_type':5, 'keys':[{'time':0,'value':1,'interpolation':2}], 'material':{'mdfPropertyHash':1,'unkn1':0,'unkn2':0,'unkn3':5}}
        self.editor.apply({'op':'curve-set','path':path,'channel':0,'value':payload,'indices':[8]})
        body = next(a.body for a in self.editor.document.entries[0].attributes if a.name == 'TypeMeshClip')
        self.assertEqual(body['clipHeader'], 3)

    def test_postfix_compilation_associativity_and_safe_syntax(self):
        formula = 'lerp(2, 10, .25) - 3 - 1 + sin(0)'
        payload = compile_expression(formula)
        expr = record_from_data(2818730, 'ReeLib.Efx.Structs.Common.EFXExpressionObject', payload)
        self.assertEqual(evaluate_expression(expr, {}), 0)
        self.assertEqual(compile_expression(expression_text(expr)), payload)
        for text in ('__import__("os")', '(1).__class__', '[x for x in []]', 'lambda: 1', 'sin(1,2)', 'unknown + 1'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                compile_expression(text)
        payload = compile_expression('const("offset", -2) | (10 / 2)')
        expr = record_from_data(2818730, 'ReeLib.Efx.Structs.Common.EFXExpressionObject', payload)
        self.assertEqual(evaluate_expression(expr, {}), (-2, 5))

    def test_expression_crud_and_reference_validation(self):
        self.editor.apply({'op': 'insert', 'path': 'expression_parameters', 'value': {'name': 'Strength', 'type': 0, 'value': 2}})
        fields = simple_values('Transform3DExpression')
        path = self.insert('Transform3DExpression', fields)
        self.editor.apply({'op': 'expression-set', 'path': path, 'channel': 5, 'value': {'formula': 'Strength * 3 + ext("TIMER")'}})
        self.editor.apply({'op': 'expression-set', 'path': path, 'channel': 1, 'value': {'formula': '2'}})
        body = next(a.body for a in self.editor.document.entries[0].attributes if a.name == 'Transform3DExpression')
        self.assertEqual(body['expressionBits'], [34])
        self.assertEqual(expression_text(body['expressions']['expressions'][0]), '2.0')
        before = self.editor.document.build_validated()
        with self.assertRaises(ValueError):
            self.editor.apply({'op': 'expression-set', 'path': path, 'channel': 5, 'value': {'parameters': [], 'components': [{'type': 1, 'data': {'value': 4}}]}})
        self.assertEqual(self.editor.document.build_validated(), before)
        self.editor.apply({'op': 'expression-delete', 'path': path, 'channel': 5})
        self.editor.apply({'op': 'delete', 'path': 'expression_parameters/0'})

    def test_texture_slot_remap_path_lengths_and_payload_values(self):
        fields = simple_values('TypeMesh')
        fields.update(properties=[{'PropertyNameUTF8Hash': 123, 'mdfPropertyIndex': 0, 'mdfParameterValueCount': 1, 'parameterType': 3, 'flags': 0,
                                   'rawValue': list(struct.pack('<4i', 1, 1, 77, 0))}], texPaths=['first.tex', 'second.tex'])
        path = self.insert('TypeMesh', fields)
        base = path + '/fields'
        self.editor.apply({'op': 'move', 'path': base + '/texPaths/1', 'index': 0})
        body = next(a.body for a in self.editor.document.entries[0].attributes if a.name == 'TypeMesh')
        self.assertEqual(texture_index(body['properties'][0]), 0)
        self.editor.apply({'op': 'set', 'path': base + '/texPaths/0', 'value': 'vfx/🔥.tex'})
        words = struct.unpack('<4i', bytes(body['properties'][0]['rawValue']))
        self.assertEqual(words, (11, 0, 77, 0))
        self.assertEqual(body['texPathBlockLength'], sum(len((p+'\0').encode('utf-16le')) for p in body['texPaths']))
        with self.assertRaises(ValueError):
            self.editor.apply({'op': 'delete', 'path': base + '/texPaths/0'})
        self.editor.apply({'op': 'material-value', 'path': base + '/properties/0', 'value': 1})
        self.editor.apply({'op': 'delete', 'path': base + '/texPaths/0'})
        body = next(a.body for a in self.editor.document.entries[0].attributes if a.name == 'TypeMesh')
        self.assertEqual(texture_index(body['properties'][0]), 0)


if __name__ == '__main__':
    unittest.main()
