import tempfile
import unittest
from pathlib import Path

from tools.wwise_graph_validation import verify_wwiser_xml


class ScalarValidationTests(unittest.TestCase):
    def compare(self, storage, value, xml_value):
        graph = {'nodes': [{'id': 766403778, 'payload_file_offset': 100, 'fields': [
            {'storage': storage, 'offset': 0, 'path': 'Transition source', 'value': value}]}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bank.xml'
            path.write_text(f'<root filename="music.nbnk"><field offset="100" '
                            f'name="srcID" value="{xml_value}"/></root>', encoding='utf8')
            return verify_wwiser_xml(graph, path, 'music.nbnk')

    def test_signed_xml_any_transition_matches_unsigned_word(self):
        self.assertEqual(self.compare('u32', 0xffffffff, '-1')['mismatches'], [])
        self.assertEqual(len(self.compare('u32', 0xffffffff, '-2')['mismatches']), 1)

    def test_object_id_difference_is_not_a_float_tolerance(self):
        self.assertEqual(len(self.compare('u32', 4294967294, '4294967295')['mismatches']), 1)

    def test_float_dump_rounding_remains_accepted(self):
        self.assertEqual(self.compare('f32', 0.10000000149011612, '0.1')['mismatches'], [])


if __name__ == '__main__':
    unittest.main()
