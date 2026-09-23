"""Structural CLIP CLI selector and input-protection contracts."""
import unittest
from pathlib import Path

from tools.cli.main import build_parser
from tools.cli.runtime import check_output


class ClipCliTests(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def parse(self, command, *flags):
        return self.parser.parse_args(['clip', command, 'target.motlist.528',
                                      '-o', 'candidate.motlist.528', *flags])

    def test_delete_requires_explicit_selection(self):
        with self.assertRaises(ValueError):
            self.parse('sequence-delete', '--motion', '1')
        args = self.parse('sequence-delete', '--motion', '1', '--sequence', '0', '--sequence', '2')
        self.assertEqual(args.positions, [0, 2])

    def test_sequence_selectors_are_exclusive(self):
        with self.assertRaises(ValueError):
            self.parse('sequence-copy', '--from', '1', '--to', '2', '--sequence', '0', '--categories', 'VFX')

    def test_graph_copy_selects_source_and_target_independently(self):
        args = self.parse('key-copy', '--from', '1', '--to', '2', '--source-sequence', '0',
                          '--sequence', '1', '--property-index', '3', '--key-index', '4',
                          '--target-property-index', '5', '--source-scope', 'override')
        self.assertEqual((args.source_sequence, args.sequence, args.property_index,
                          args.key_index, args.target_property_index), (0, 1, 3, 4, 5))
        self.assertEqual(args.source_scope, 'override')

    def test_property_copy_accepts_a_container_owner(self):
        args = self.parse('property-copy', '--from', '1', '--to', '2', '--property-index', '3',
                          '--target-property-index', '7')
        self.assertEqual(args.target_property_index, 7)
        self.assertIsNone(args.target_node_index)

    def test_all_copy_commands_protect_donor(self):
        cases = {'sequence-copy': [], 'node-copy': ['--node-index', '1', '--target-node-index', '0'],
                 'property-copy': ['--property-index', '1', '--target-node-index', '0'],
                 'key-copy': ['--property-index', '1', '--key-index', '0', '--target-property-index', '2']}
        for command, selectors in cases.items():
            with self.subTest(command=command):
                args = self.parse(command, '--from', '1', '--to', '2', '--donor', 'donor.motlist.528', *selectors)
                self.assertEqual(args.extra_inputs(args), (Path('donor.motlist.528'),))
                with self.assertRaises(ValueError):
                    check_output(args.donor, args.extra_inputs(args))


if __name__ == '__main__':
    unittest.main()
