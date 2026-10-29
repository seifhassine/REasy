"""Map ownership preserves every transition record across native FSM layouts."""
import copy
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from file_handlers.motfsm.map_ownership import detach_state_maps
from file_handlers.motfsm.motfsm_file import MotfsmFile, BHVTNode
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.transitions import TransitionTables, TransitionCopies
from file_handlers.motfsm.serialization import serialize_nodes
from test_motfsm_native_writer import fixture


def mapped_fixture(folder, compact=False, identity=5):
    doc = fixture(folder, compact=compact)
    editor = doc.graph_editor()
    first = editor.add_node(BHVTNode(id_hash=10, name='target', ex_id=0), 0)
    second = editor.add_node(BHVTNode(id_hash=10, name='target', ex_id=1), 0)
    editor.connect_state(0, first)
    editor.connect_state(0, second)
    raw = bytearray(doc.build_validated())
    raw.extend(bytes((-len(raw)) % 16))
    maps = len(raw)
    raw.extend(struct.pack('<Ii', identity, 0) + bytes(8))
    data = len(raw)
    raw.extend(struct.pack('<II4f', 123, 0x20, 1.0, 2.0, 3.0, 4.0))
    if not compact:
        raw.extend(struct.pack('<fHHI', 5.0, 6, 7, 8))
    struct.pack_into('<QQ', raw, 24, maps, data)
    struct.pack_into('<II', raw, 48, 1, 1)
    loaded = MotfsmFile()
    loaded.set_rsz_type_info_path(doc.type_registry.json_path)
    loaded.read(bytes(raw))
    for state in loaded.bhvt.nodes[0].states:
        struct.pack_into('<I', raw, loaded.bindings.get(state, 'TransitionMaps').offset, identity)
    loaded.read(bytes(raw))
    return loaded


class MapOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def reopen(self, doc, raw):
        saved = MotfsmFile()
        saved.set_rsz_type_info_path(doc.type_registry.json_path)
        saved.read(raw)
        return saved

    def test_single_and_batch_detach_preserve_nodes_blocks_and_data(self):
        for compact in (False, True):
            for locations in ([(0, 1)], [(0, 0), (0, 1)]):
                with self.subTest(compact=compact, locations=locations):
                    doc = mapped_fixture(self.folder.name, compact)
                    source = doc.source
                    original_nodes = copy.deepcopy(doc.bhvt.nodes)
                    output = detach_state_maps(source, locations, doc.type_registry)
                    saved = self.reopen(doc, output)
                    before, after = TransitionTables(doc), TransitionTables(saved)
                    self.assertEqual(after.record(0), before.record(0))
                    self.assertEqual(after.maps[5], before.maps[5])
                    self.assertEqual(saved.transition_map_count, 1 + len(locations))
                    expected = copy.deepcopy(original_nodes)
                    for index, (node, state) in enumerate(locations, 6):
                        expected[node].states[state].TransitionMaps = index
                        self.assertEqual(after.resolve(index), 0)
                    self.assertEqual(saved.bhvt.nodes, expected)
                    self.assertEqual(doc.bhvt.nodes, original_nodes)
                    for name in BLOCK_NAMES:
                        a, b = doc.rsz_blocks.get_block(name), saved.rsz_blocks.get_block(name)
                        self.assertEqual(source[a.offset:a.end], output[b.offset:b.end])
                    self.assertEqual(saved.build_validated(), output)
                    self.assertEqual(doc.source, source)

    def test_invalid_locations_and_exhausted_ids_are_rejected(self):
        doc = mapped_fixture(self.folder.name)
        for locations in ([(0, 0), (0, 0)], [(-1, 0)], [(0, -1)], [(3, 0)], [(0, 2)]):
            with self.subTest(locations=locations), self.assertRaises(ValueError):
                detach_state_maps(doc.source, locations, doc.type_registry)
        full = mapped_fixture(self.folder.name, identity=0xFFFFFFFE)
        with self.assertRaisesRegex(ValueError, 'exhausted'):
            detach_state_maps(full.source, [(0, 0)], full.type_registry)
        self.assertEqual(detach_state_maps(doc.source, [], doc.type_registry), doc.source)

    def test_cli_selects_the_exact_target_extension(self):
        from tools.fsm.operations import state_own_map
        doc = mapped_fixture(self.folder.name)
        path = Path(self.folder.name) / 'fixture.motfsm2.43'
        path.write_bytes(doc.source)

        def document():
            result = MotfsmFile()
            result.set_rsz_type_info_path(doc.type_registry.json_path)
            return result

        args = SimpleNamespace(source=path, node='index:0', target='0xA:1', allow_uvar_drift=False)
        with patch.object(state_own_map, 'MotfsmFile', side_effect=document):
            output = state_own_map.run(args)
        saved = self.reopen(doc, output)
        self.assertEqual(saved.bhvt.nodes[0].states[0], doc.bhvt.nodes[0].states[0])
        self.assertNotEqual(saved.bhvt.nodes[0].states[1].TransitionMaps, 5)

    def test_independent_data_copy_preserves_original_records_in_both_layouts(self):
        for compact in (False, True):
            with self.subTest(compact=compact):
                doc = mapped_fixture(self.folder.name, compact)
                plan = TransitionCopies(doc)
                identity, index = plan.clone(5, independent_data=True)
                doc.bhvt.nodes[0].states[1].TransitionMaps = identity
                start = doc.bhvt.offsets['nodes']
                raw = plan.apply([(start, doc.bhvt.node_data_end, serialize_nodes(doc))])
                saved = self.reopen(doc, raw)
                tables = TransitionTables(saved)
                self.assertEqual(tables.resolve(identity), index)
                self.assertEqual(tables.record(0), plan.tables.record(0))
                self.assertEqual(tables.record(index)[4:], tables.record(0)[4:])
                self.assertNotEqual(tables.record(index)[:4], tables.record(0)[:4])
                self.assertEqual(saved.transition_data_count, 2)
                self.assertEqual(saved.build_validated(), raw)

    def test_node_recipe_uses_public_bytes_api_and_allocates_private_maps(self):
        from services.mhr.fsm.node_clone import NodeClonePlan, build
        doc = mapped_fixture(self.folder.name)
        doc.bhvt.nodes[0].node_attribute = 0x20
        doc.bhvt.nodes[0].selector_id = 0
        doc.bhvt.nodes[0].states[0].mStates.values = [0x40000000]
        source = doc.build_validated()
        plan = NodeClonePlan(name='cloned', copy_node='index:0', parent='index:0', motion=1,
                             set_effect=0, allow_no_motion=True)
        output = build(source, plan, registry=doc.type_registry)
        saved = self.reopen(doc, output)
        clone = saved.bhvt.nodes[-1]
        self.assertEqual([s.TransitionMaps for s in clone.states], [6, 7])
        self.assertEqual(saved.transition_map_count, 3)
        self.assertEqual(saved.transition_data_count, 1)
        self.assertEqual(clone.states[0].mStates.values, [0x40000000])
        self.assertIsNotNone(saved.references.object_instance('transition_events', 0x40000000))
        self.assertEqual(saved.build_validated(), output)


if __name__ == '__main__':
    unittest.main()
