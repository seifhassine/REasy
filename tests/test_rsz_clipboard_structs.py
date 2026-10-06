import unittest

from file_handlers.rsz.rsz_array_clipboard import RszArrayClipboard as Clipboard
from file_handlers.rsz.rsz_data_types import ArrayData, F32Data, ObjectData, StructData, U32Data


class StructClipboardTests(unittest.TestCase):
    def test_primitive_fields_are_reconstructed_as_typed_values(self):
        source = StructData([{'ParamID': U32Data(123, 'System.UInt32'),
                              'Value': F32Data(4.5, 'System.Single')}], 'NativeTuneParameter')
        payload = Clipboard._serialize_field_with_mapping(source, {}, set(), set())
        result = Clipboard._deserialize_field_with_relative_mapping(payload, {})
        self.assertIsInstance(result.values[0]['ParamID'], U32Data)
        self.assertIsInstance(result.values[0]['Value'], F32Data)
        self.assertEqual(result.values[0]['ParamID'].value, 123)
        self.assertEqual(result.values[0]['Value'].value, 4.5)
        self.assertEqual(result.orig_type, source.orig_type)
        self.assertIsInstance(source.values[0]['Value'], F32Data)
        plain_result = Clipboard._deserialize_element(Clipboard._serialize_element(source), None)
        self.assertIsInstance(plain_result.values[0]['Value'], F32Data)

    def test_owned_references_inside_structs_follow_graph_remapping(self):
        source = StructData([{'Owner': ObjectData(7, 'NativeOwner')}], 'NativeReferences')
        payload = Clipboard._serialize_field_with_mapping(source, {7: 1}, {7}, set())
        result = Clipboard._deserialize_field_with_relative_mapping(payload, {1: 23})
        self.assertEqual(result.values[0]['Owner'].value, 23)
        self.assertEqual(source.values[0]['Owner'].value, 7)

    def test_nested_struct_array_preserves_typed_values(self):
        child = StructData([{'Value': F32Data(2.25, 'System.Single')}], 'Child')
        source = StructData([{'Children': ArrayData([child], StructData, 'Child')}], 'Parent')
        payload = Clipboard._serialize_field_with_mapping(source, {}, set(), set())
        result = Clipboard._deserialize_field_with_relative_mapping(payload, {})
        self.assertEqual(result.values[0]['Children'].values[0].values[0]['Value'].value, 2.25)


if __name__ == '__main__':
    unittest.main()
